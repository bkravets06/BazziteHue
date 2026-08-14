"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Awaitable, Callable, Sequence

from bleak.exc import BleakError

from . import __version__
from .color import (
    GAMUTS,
    ColorError,
    ColorTarget,
    kelvin_to_mired,
    kelvin_to_xy,
    mired_to_kelvin,
    parse_color,
    xy_to_rgb,
)
from .config import Config, Scene, is_address
from .errors import BazziteHueError, ConfigError
from .light import HueLight, discover
from .protocol import LightState

log = logging.getLogger("bazzitehue")

Action = Callable[[HueLight], Awaitable[object]]


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def swatch(rgb: tuple[int, int, int], width: int = 3) -> str:
    if not use_color():
        return ""
    r, g, b = rgb
    return f"\x1b[48;2;{r};{g};{b}m{' ' * width}\x1b[0m "


def dim(text: str) -> str:
    return f"\x1b[2m{text}\x1b[0m" if use_color() else text


def bold(text: str) -> str:
    return f"\x1b[1m{text}\x1b[0m" if use_color() else text


_progress_width = 0


def progress(message: str) -> None:
    """Show what the tool is waiting on, in place, on stderr.

    Connecting to a bulb can take the better part of a minute when it is out of
    range, and silence for that long is indistinguishable from a hang. Written
    to stderr and only when it is a terminal, so pipes and --json stay clean.
    """
    global _progress_width
    if not sys.stderr.isatty():
        return
    padding = " " * max(0, _progress_width - len(message))
    print(f"\r{message}{padding}", end="", file=sys.stderr, flush=True)
    _progress_width = len(message)


def clear_progress() -> None:
    """Wipe the progress line before printing real output."""
    global _progress_width
    if _progress_width and sys.stderr.isatty():
        print(f"\r{' ' * _progress_width}\r", end="", file=sys.stderr, flush=True)
    _progress_width = 0


def describe_state(state: LightState, alias: str | None) -> str:
    label = alias or state.name or state.address
    power = "on " if state.power else "off"
    parts = [f"{bold(label):<24} {power}"]

    if state.brightness is not None:
        parts.append(f"{state.brightness_percent:5.1f}%")

    if state.xy is not None:
        rgb = xy_to_rgb(state.xy, 1.0)
        parts.append(
            f"{swatch(rgb)}#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x} "
            f"xy({state.xy[0]:.3f},{state.xy[1]:.3f})"
        )

    if state.mired is not None:
        parts.append(f"{mired_to_kelvin(state.mired)}K")

    line = "  ".join(parts)
    if alias and state.name and state.name != alias:
        line += dim(f"  ({state.name})")
    return line


def state_to_dict(state: LightState, alias: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "alias": alias,
        "address": state.address,
        "name": state.name,
        "power": state.power,
        "brightness_percent": state.brightness_percent,
        "brightness_raw": state.brightness,
    }
    if state.xy is not None:
        rgb = xy_to_rgb(state.xy, 1.0)
        payload["xy"] = list(state.xy)
        payload["hex"] = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    if state.mired is not None:
        payload["mired"] = state.mired
        payload["kelvin"] = mired_to_kelvin(state.mired)
    for key in ("model", "manufacturer", "firmware"):
        value = getattr(state, key)
        if value:
            payload[key] = value
    return payload


# --------------------------------------------------------------------------- #
# Running actions against one or many lamps
# --------------------------------------------------------------------------- #


def build_lights(config: Config, target: str | None, timeout: float) -> list[HueLight]:
    # One connection attempt at a time: BlueZ is flaky when several are
    # established at once, and these lamps are usually driven together.
    gate = asyncio.Semaphore(1)
    lights = []
    for alias, saved in config.resolve(target):
        gamut = GAMUTS.get(saved.gamut.upper(), GAMUTS["C"])
        lights.append(
            HueLight(
                saved.address,
                gamut=gamut,
                timeout=timeout,
                alias=alias or saved.name,
                gate=gate,
            )
        )
    return lights


async def run_on_lights(
    config: Config,
    target: str | None,
    timeout: float,
    action: Action,
    *,
    report: Callable[[HueLight, object], None] | None = None,
) -> int:
    """Connect to every selected lamp concurrently and run ``action``.

    Returns a process exit code: non-zero if any lamp failed.
    """
    lights = build_lights(config, target, timeout)

    async def run(light: HueLight) -> object:
        label = light.alias or light.address
        progress(f"connecting to {label}...")
        async with light:
            progress(f"connected to {label}")
            return await action(light)

    results = await asyncio.gather(*(run(light) for light in lights), return_exceptions=True)
    clear_progress()

    failures = 0
    for light, result in zip(lights, results):
        label = light.alias or light.address
        if isinstance(result, BaseException):
            failures += 1
            print(f"{label}: {result}", file=sys.stderr)
            hint = getattr(result, "hint", None)
            if hint:
                print(dim(hint), file=sys.stderr)
        elif report is not None:
            report(light, result)

    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


async def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    print(f"Scanning for {args.timeout:.0f}s...", file=sys.stderr)
    devices = await discover(timeout=args.timeout, all_devices=args.all)

    if not devices:
        print("No Hue lamps found.", file=sys.stderr)
        print(
            dim(
                "Bulbs already connected to a phone or Hue Bridge stay silent. "
                "Power-cycle the lamp, or re-run with --all to list every BLE device."
            ),
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(
            json.dumps(
                [{"address": d.address, "name": d.name} for d in devices],
                indent=2,
            )
        )
        return 0

    known = {light.address.upper(): alias for alias, light in config.lights.items()}
    for device in devices:
        alias = known.get(device.address.upper())
        suffix = dim(f"  (saved as {alias})") if alias else ""
        print(f"{device.address}  {device.name or '<unnamed>'}{suffix}")

    if not known:
        print()
        print(dim("Save one with: bazzitehue add <alias> <address>"))
    return 0


async def cmd_add(args: argparse.Namespace, config: Config) -> int:
    address = args.address
    if not is_address(address):
        raise ConfigError(f"{address!r} does not look like a Bluetooth address")

    config.add_light(args.alias, address, gamut=args.gamut)
    path = config.save()
    print(f"Saved {args.alias} -> {address.upper()} (gamut {args.gamut.upper()})")
    print(dim(f"config: {path}"))

    if args.pair:
        light = HueLight(address, timeout=args.timeout, alias=args.alias)
        async with light:
            paired = await light.pair()
            if paired:
                print("Paired.")
            else:
                print("Pairing reported failure; try again after a power cycle.")
    return 0


async def cmd_lights(args: argparse.Namespace, config: Config) -> int:
    if not config.lights:
        print("No saved lamps. Run 'bazzitehue scan' then 'bazzitehue add'.")
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    alias: {
                        "address": light.address,
                        "gamut": light.gamut,
                        "default": alias == config.default,
                    }
                    for alias, light in config.lights.items()
                },
                indent=2,
            )
        )
        return 0

    for alias, light in config.lights.items():
        marker = "*" if alias == config.default else " "
        print(f"{marker} {alias:<16} {light.address}  gamut {light.gamut}")
    return 0


async def cmd_rm(args: argparse.Namespace, config: Config) -> int:
    config.remove_light(args.alias)
    config.save()
    print(f"Removed {args.alias}")
    return 0


async def cmd_use(args: argparse.Namespace, config: Config) -> int:
    if args.alias not in config.lights:
        raise ConfigError(f"no saved lamp called {args.alias!r}")
    config.default = args.alias
    config.save()
    print(f"Default lamp is now {args.alias}")
    return 0


async def cmd_pair(args: argparse.Namespace, config: Config) -> int:
    async def action(light: HueLight) -> object:
        return await light.pair()

    def report(light: HueLight, result: object) -> None:
        label = light.alias or light.address
        print(f"{label}: {'paired' if result else 'pairing failed'}")

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


async def cmd_unpair(args: argparse.Namespace, config: Config) -> int:
    async def action(light: HueLight) -> object:
        return await light.unpair()

    def report(light: HueLight, result: object) -> None:
        label = light.alias or light.address
        print(f"{label}: {'unpaired' if result else 'unpair failed'}")

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


async def cmd_status(args: argparse.Namespace, config: Config) -> int:
    collected: list[dict[str, object]] = []

    async def action(light: HueLight) -> object:
        return await light.get_state(include_device_info=args.verbose_info)

    def report(light: HueLight, result: object) -> None:
        state = result  # type: ignore[assignment]
        assert isinstance(state, LightState)
        if args.json:
            collected.append(state_to_dict(state, light.alias))
        else:
            print(describe_state(state, light.alias))

    code = await run_on_lights(config, args.light, args.timeout, action, report=report)
    if args.json:
        print(json.dumps(collected, indent=2))
    return code


async def cmd_info(args: argparse.Namespace, config: Config) -> int:
    async def action(light: HueLight) -> object:
        state = await light.get_state(include_device_info=True)
        return (
            state,
            light.supports_color,
            light.supports_temperature,
        )

    def report(light: HueLight, result: object) -> None:
        state, has_color, has_temp = result  # type: ignore[misc]
        print(bold(light.alias or state.address))
        print(f"  address       {state.address}")
        print(f"  name          {state.name or '-'}")
        print(f"  manufacturer  {state.manufacturer or '-'}")
        print(f"  model         {state.model or '-'}")
        print(f"  firmware      {state.firmware or '-'}")
        print(f"  colour        {'yes' if has_color else 'no'}")
        print(f"  temperature   {'yes' if has_temp else 'no'}")
        print(f"  state         {'on' if state.power else 'off'} @ {state.brightness_percent}%")
        print()

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


async def cmd_power(args: argparse.Namespace, config: Config) -> int:
    want = args.value

    async def action(light: HueLight) -> object:
        if want is None:
            return await light.toggle()
        await light.set_power(want)
        return want

    def report(light: HueLight, result: object) -> None:
        label = light.alias or light.address
        print(f"{label}: {'on' if result else 'off'}")

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


def _relative(value: str) -> tuple[float, bool]:
    """Parse ``50``, ``+10`` or ``-10`` into (amount, is_relative)."""
    text = value.strip().rstrip("%")
    if text.startswith(("+", "-")):
        return float(text), True
    return float(text), False


async def cmd_brightness(args: argparse.Namespace, config: Config) -> int:
    amount, relative = _relative(args.value)

    async def action(light: HueLight) -> object:
        target = amount
        if relative:
            target = max(0.0, min(100.0, await light.get_brightness_percent() + amount))
        if args.on and target > 0:
            await light.set_power(True)
        if args.fade:
            await light.fade(brightness_percent=target, duration=args.fade)
        else:
            await light.set_brightness_percent(target)
        return target

    def report(light: HueLight, result: object) -> None:
        label = light.alias or light.address
        print(f"{label}: brightness {float(result):.0f}%")  # type: ignore[arg-type]

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


async def _apply_target(
    args: argparse.Namespace,
    config: Config,
    color: ColorTarget | None,
    brightness: float | None,
    power: bool | None,
) -> int:
    async def action(light: HueLight) -> object:
        if args.fade:
            if power is True:
                await light.set_power(True)
            await light.fade(
                color=color,
                brightness_percent=brightness,
                duration=args.fade,
            )
            if power is False:
                await light.set_power(False)
        else:
            await light.apply(power=power, color=color, brightness_percent=brightness)
        return True

    def report(light: HueLight, result: object) -> None:
        label = light.alias or light.address
        bits = []
        if power is not None:
            bits.append("on" if power else "off")
        if color is not None:
            bits.append(color.describe())
        if brightness is not None:
            bits.append(f"{brightness:.0f}%")
        print(f"{label}: {', '.join(bits) if bits else 'no change'}")

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


async def cmd_color(args: argparse.Namespace, config: Config) -> int:
    color = parse_color(args.spec)
    brightness = args.brightness if args.brightness is not None else color.brightness
    power = True if args.on else None
    return await _apply_target(args, config, color, brightness, power)


async def cmd_temp(args: argparse.Namespace, config: Config) -> int:
    color = parse_color(args.value if _looks_like_temp(args.value) else f"{args.value}k")
    if color.mired is None:
        raise ConfigError(f"{args.value!r} is not a colour temperature")
    power = True if args.on else None
    return await _apply_target(args, config, color, args.brightness, power)


def _looks_like_temp(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.endswith(("k", "mired", "mireds", "mirek")) or not lowered.isdigit()


async def cmd_set(args: argparse.Namespace, config: Config) -> int:
    color = parse_color(args.color) if args.color else None
    brightness = args.brightness
    if brightness is None and color is not None:
        brightness = color.brightness

    power: bool | None = None
    if args.on:
        power = True
    elif args.off:
        power = False

    if color is None and brightness is None and power is None:
        raise ConfigError("nothing to do: pass --on/--off, --color and/or --brightness")

    return await _apply_target(args, config, color, brightness, power)


async def cmd_scene(args: argparse.Namespace, config: Config) -> int:
    if args.scene_command == "list":
        if not config.scenes:
            print("No saved scenes.")
            return 0
        for name, scene in config.scenes.items():
            bits = []
            if scene.color:
                bits.append(scene.color)
            if scene.brightness is not None:
                bits.append(f"{scene.brightness:.0f}%")
            if scene.power is not None:
                bits.append("on" if scene.power else "off")
            print(f"{name:<16} {', '.join(bits)}")
        return 0

    if args.scene_command == "rm":
        config.remove_scene(args.name)
        config.save()
        print(f"Removed scene {args.name}")
        return 0

    if args.scene_command == "save":
        if args.from_light:
            lights = build_lights(config, args.light, args.timeout)
            async with lights[0] as light:
                state = await light.get_state()
            if state.xy is not None:
                rgb = xy_to_rgb(state.xy, 1.0)
                color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
            elif state.mired is not None:
                color = f"{mired_to_kelvin(state.mired)}k"
            else:
                color = None
            scene = Scene(
                color=color,
                brightness=state.brightness_percent,
                power=state.power,
            )
        else:
            if args.color:
                parse_color(args.color)  # validate now, not at apply time
            scene = Scene(
                color=args.color,
                brightness=args.brightness,
                power=None if args.no_power else True,
            )

        config.add_scene(args.name, scene)
        config.save()
        print(f"Saved scene {args.name}")
        return 0

    # apply
    scene = config.scenes.get(args.name)
    if scene is None:
        raise ConfigError(
            f"no scene called {args.name!r}",
            hint="List them with 'bazzitehue scene list'.",
        )
    color = parse_color(scene.color) if scene.color else None
    return await _apply_target(args, config, color, scene.brightness, scene.power)


async def cmd_loop(args: argparse.Namespace, config: Config) -> int:
    """Cycle through hues until interrupted or the duration runs out."""

    async def action(light: HueLight) -> object:
        import time

        started = time.monotonic()
        hue = 0.0
        step = 360.0 / max(1.0, args.speed * 20.0)  # 20 writes/second
        while True:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            target = parse_color(f"hsv:{hue:.1f},{args.saturation:.0f},100", light.gamut)
            assert target.xy is not None
            await light.set_color_xy(target.xy, response=False)
            hue = (hue + step) % 360
            await asyncio.sleep(0.05)
        return True

    print("Cycling colours. Ctrl-C to stop.", file=sys.stderr)
    try:
        return await run_on_lights(config, args.light, args.timeout, action)
    except asyncio.CancelledError:
        return 0


async def cmd_gatt_dump(args: argparse.Namespace, config: Config) -> int:
    """Print the lamp's real attribute table, for when a bulb behaves oddly."""

    async def action(light: HueLight) -> object:
        client = light._require_client()  # diagnostics command, internals are fine here
        lines: list[str] = []
        for service in client.services:
            lines.append(f"service {service.uuid}  {service.description}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                lines.append(f"  char {char.uuid}  [{props}]  {char.description}")
        return "\n".join(lines)

    def report(light: HueLight, result: object) -> None:
        print(bold(light.alias or light.address))
        print(result)
        print()

    return await run_on_lights(config, args.light, args.timeout, action, report=report)


async def cmd_colors(args: argparse.Namespace, config: Config) -> int:
    """Show the built-in colour names with a preview swatch."""
    from .color import NAMED_COLORS, NAMED_WHITES, parse_hex

    print(bold("colours"))
    for name, hex_value in NAMED_COLORS.items():
        print(f"  {swatch(parse_hex(hex_value))}{name:<12} {hex_value}")

    print()
    print(bold("whites"))
    for name, kelvin in NAMED_WHITES.items():
        rgb = xy_to_rgb(kelvin_to_xy(kelvin), 1.0)
        print(f"  {swatch(rgb)}{name:<12} {kelvin}K ({kelvin_to_mired(kelvin)} mired)")
    return 0


async def cmd_tui(args: argparse.Namespace, config: Config) -> int:
    from .tui import run_tui

    lights = build_lights(config, args.light, args.timeout)
    return await run_tui(lights)


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def add_common_options(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Repeat the global flags on a subcommand.

    argparse only accepts a top-level option before the subcommand, but
    ``bazzitehue on -l desk`` is the order people actually type. SUPPRESS keeps an
    unused copy from overwriting the value given before the subcommand.
    """
    parser.add_argument("-l", "--light", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bazzitehue",
        description=(
            "Control Philips Hue bulbs over Bluetooth LE -- no bridge, no Wi-Fi, "
            "no cloud account."
        ),
        epilog=(
            "Colour formats: #ff8800, 255,136,0, hsv:30,100,100, hsl:30,100,50, "
            "xy:0.5,0.4, 3000k, 250mired, or a name ('bazzitehue colors')."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"bazzitehue {__version__}")
    parser.add_argument(
        "-l",
        "--light",
        help="alias, address, comma-separated list, or 'all' (default: configured lamp)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="connection timeout in seconds (default 20, 8 while scanning)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true", help="log BLE chatter")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="look for Hue lamps in range")
    scan.add_argument("--all", action="store_true", help="list every BLE device, not just Hue")
    scan.set_defaults(func=cmd_scan)

    add = sub.add_parser("add", help="save a lamp under a short alias")
    add.add_argument("alias")
    add.add_argument("address")
    add.add_argument(
        "--gamut",
        default="C",
        choices=sorted(GAMUTS),
        help="colour gamut of the bulb (C covers all modern colour bulbs)",
    )
    add.add_argument("--pair", action="store_true", help="bond with the lamp right away")
    add.set_defaults(func=cmd_add)

    lights = sub.add_parser("lights", help="list saved lamps")
    lights.set_defaults(func=cmd_lights)

    remove = sub.add_parser("rm", help="forget a saved lamp")
    remove.add_argument("alias")
    remove.set_defaults(func=cmd_rm)

    use = sub.add_parser("use", help="set the default lamp")
    use.add_argument("alias")
    use.set_defaults(func=cmd_use)

    pair = sub.add_parser("pair", help="bond with a lamp so it accepts commands")
    pair.set_defaults(func=cmd_pair)

    unpair = sub.add_parser("unpair", help="remove the bond")
    unpair.set_defaults(func=cmd_unpair)

    status = sub.add_parser("status", help="show current power, brightness and colour")
    status.add_argument(
        "--full", dest="verbose_info", action="store_true", help="also read device info"
    )
    status.set_defaults(func=cmd_status, verbose_info=False)

    info = sub.add_parser("info", help="show model, firmware and capabilities")
    info.set_defaults(func=cmd_info)

    on = sub.add_parser("on", help="turn the lamp on")
    on.set_defaults(func=cmd_power, value=True)

    off = sub.add_parser("off", help="turn the lamp off")
    off.set_defaults(func=cmd_power, value=False)

    toggle = sub.add_parser("toggle", help="flip the lamp's power")
    toggle.set_defaults(func=cmd_power, value=None)

    brightness = sub.add_parser("brightness", help="set brightness (0-100, or +10/-10)")
    brightness.add_argument("value")
    brightness.add_argument("--fade", type=float, default=0.0, metavar="SECONDS")
    brightness.add_argument("--on", action="store_true", help="switch on first")
    brightness.set_defaults(func=cmd_brightness)

    color = sub.add_parser("color", help="set the colour")
    color.add_argument("spec", help="#ff8800, 255,136,0, hsv:30,100,100, xy:.5,.4, red, ...")
    color.add_argument("-b", "--brightness", type=float, metavar="PERCENT")
    color.add_argument("--fade", type=float, default=0.0, metavar="SECONDS")
    color.add_argument("--on", action="store_true", help="switch on first")
    color.set_defaults(func=cmd_color)

    temp = sub.add_parser("temp", help="set white colour temperature")
    temp.add_argument("value", help="3000, 3000k, 250mired, or a name such as 'warm'")
    temp.add_argument("-b", "--brightness", type=float, metavar="PERCENT")
    temp.add_argument("--fade", type=float, default=0.0, metavar="SECONDS")
    temp.add_argument("--on", action="store_true", help="switch on first")
    temp.set_defaults(func=cmd_temp)

    combined = sub.add_parser("set", help="change several things at once")
    combined.add_argument("-c", "--color")
    combined.add_argument("-b", "--brightness", type=float, metavar="PERCENT")
    combined.add_argument("--on", action="store_true")
    combined.add_argument("--off", action="store_true")
    combined.add_argument("--fade", type=float, default=0.0, metavar="SECONDS")
    combined.set_defaults(func=cmd_set)

    scene = sub.add_parser("scene", help="save and recall looks")
    scene_sub = scene.add_subparsers(dest="scene_command", required=True)

    scene_save = scene_sub.add_parser("save", help="store a scene")
    scene_save.add_argument("name")
    scene_save.add_argument("-c", "--color")
    scene_save.add_argument("-b", "--brightness", type=float, metavar="PERCENT")
    scene_save.add_argument(
        "--from-light", action="store_true", help="capture the lamp's current look"
    )
    scene_save.add_argument(
        "--no-power", action="store_true", help="do not switch the lamp on when applying"
    )
    scene_save.add_argument("--fade", type=float, default=0.0, metavar="SECONDS")

    scene_apply = scene_sub.add_parser("apply", help="apply a stored scene")
    scene_apply.add_argument("name")
    scene_apply.add_argument("--fade", type=float, default=0.0, metavar="SECONDS")

    scene_list = scene_sub.add_parser("list", help="list stored scenes")
    scene_list.set_defaults(fade=0.0)

    scene_rm = scene_sub.add_parser("rm", help="delete a stored scene")
    scene_rm.add_argument("name")
    scene_rm.set_defaults(fade=0.0)

    scene.set_defaults(func=cmd_scene, fade=0.0)
    for sub_parser in (scene_save, scene_apply, scene_list, scene_rm):
        sub_parser.set_defaults(func=cmd_scene)

    loop = sub.add_parser("loop", help="cycle through the colour wheel")
    loop.add_argument(
        "--speed", type=float, default=10.0, help="seconds per full rotation (default 10)"
    )
    loop.add_argument("--saturation", type=float, default=100.0, metavar="PERCENT")
    loop.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    loop.set_defaults(func=cmd_loop)

    tui = sub.add_parser("tui", help="interactive terminal controller")
    tui.set_defaults(func=cmd_tui)

    colors = sub.add_parser("colors", help="list the built-in colour names")
    colors.set_defaults(func=cmd_colors)

    dump = sub.add_parser("gatt-dump", help="print the lamp's GATT table (diagnostics)")
    dump.set_defaults(func=cmd_gatt_dump)

    gui = sub.add_parser("gui", help="open the desktop app")
    gui.add_argument("--tray", action="store_true", help="start in the panel, no window")
    gui.set_defaults(func=None)

    for subparser in list(sub.choices.values()) + [
        scene_save,
        scene_apply,
        scene_list,
        scene_rm,
    ]:
        add_common_options(subparser)

    return parser


def launch_gui(tray: bool = False) -> int:
    """Hand over to the desktop app."""
    try:
        from .gui.app import main as gui_main
    except ImportError as exc:
        print(f"error: the desktop app is not installed ({exc})", file=sys.stderr)
        print(
            dim(
                "Install it with:  pip install 'bazzitehue[gui]'\n"
                "or re-run ./install.sh, which sets up the app and its launcher."
            ),
            file=sys.stderr,
        )
        return 2
    return gui_main(["--tray"] if tray else [])


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "timeout", None) is None:
        args.timeout = 8.0 if args.command == "scan" else 20.0

    if args.command == "gui":
        # Qt runs its own event loop, so this must not go through asyncio.run().
        return launch_gui(tray=args.tray)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.load()
    except BazziteHueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(args.func(args, config))
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        # Piping into head/less closes stdout early; swallow the interpreter's
        # "Exception ignored" noise on the way out.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except (BazziteHueError, ColorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        hint = getattr(exc, "hint", None)
        if hint:
            print(dim(hint), file=sys.stderr)
        return 2
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            dim("Install the runtime dependency with: pip install --user bleak"),
            file=sys.stderr,
        )
        return 2
    except (BleakError, OSError) as exc:
        # Typically bluetoothd is stopped or the session has no D-Bus, which
        # otherwise surfaces as a bare FileNotFoundError from deep inside bleak.
        print(f"error: the Bluetooth stack is not reachable: {exc}", file=sys.stderr)
        print(
            dim(
                "Check that BlueZ is running and an adapter is present:\n"
                "  systemctl status bluetooth\n"
                "  sudo systemctl enable --now bluetooth\n"
                "  bluetoothctl show"
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
