"""Interactive terminal controller.

Curses from the standard library, so there is nothing extra to install on an
immutable system. Key presses update a local model of the light; a separate task
pushes changes to the lamps at a fixed rate, because BLE writes are far slower
than someone holding down an arrow key.
"""

from __future__ import annotations

import asyncio
import colorsys
import curses
import sys
import time
from dataclasses import dataclass

from .color import (
    kelvin_to_mired,
    kelvin_to_xy,
    mired_to_kelvin,
    parse_color,
    parse_hex,
    rgb_to_xy,
    xy_to_rgb,
)
from .light import HueLight

WRITE_INTERVAL = 0.06  # ~16 updates per second is about all a Hue bulb keeps up with
KEY_POLL_MS = 20  # how long getch() waits for a key before the loop comes round again
# The range the t/T keys sweep; Hue's white spectrum is 2000K..6500K.
MIN_TUI_KELVIN = 2000
MAX_TUI_KELVIN = 6500
PRESET_KEYS = "123456789"
PRESETS = [
    ("red", "#ff0000"),
    ("orange", "#ff7f00"),
    ("yellow", "#ffff00"),
    ("green", "#00ff00"),
    ("cyan", "#00ffff"),
    ("blue", "#0000ff"),
    ("purple", "#8a2be2"),
    ("pink", "#ff69b4"),
    ("white", "#ffffff"),
]

HELP_LINES = [
    "  ←/→   hue          -/+ 5°      (shift: 20°)",
    "  ↑/↓   brightness   -/+ 5%      (shift: 20%)",
    "  s/S   saturation   -/+ 5%",
    "  t/T   warmer / cooler white",
    "  w     white mode        c  colour mode",
    "  1-9   colour presets",
    "  space toggle power       r  re-read from lamp",
    "  q     quit",
]


@dataclass
class Model:
    """What the user has dialled in, independent of what the lamp last confirmed."""

    power: bool = True
    brightness: float = 60.0
    hue: float = 30.0
    saturation: float = 100.0
    kelvin: int = 2700
    white_mode: bool = False

    def rgb(self) -> tuple[int, int, int]:
        if self.white_mode:
            return xy_to_rgb(kelvin_to_xy(self.kelvin), 1.0)
        target = parse_color(f"hsv:{self.hue:.1f},{self.saturation:.1f},100")
        assert target.xy is not None
        return xy_to_rgb(target.xy, 1.0)

    def xy(self) -> tuple[float, float]:
        return rgb_to_xy(self.rgb())[0]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


async def _push(lights: list[HueLight], model: Model, previous: Model | None) -> str:
    """Send whatever changed since the last push.

    Returns a status message: empty if every lamp took the update, otherwise a
    short description of the failure. A dropped write mid-drag is not worth
    tearing the UI down for, but it must not look like success either.
    """
    failures: list[str] = []

    async def send(light: HueLight) -> None:
        try:
            if previous is None or model.power != previous.power:
                await light.set_power(model.power)
            if not model.power:
                return
            if previous is None or (model.brightness != previous.brightness):
                await light.set_brightness_percent(model.brightness, response=False)
            if model.white_mode:
                if previous is None or model.kelvin != previous.kelvin or not previous.white_mode:
                    await light.set_mired(kelvin_to_mired(model.kelvin), response=False)
            else:
                changed = (
                    previous is None
                    or model.hue != previous.hue
                    or model.saturation != previous.saturation
                    or previous.white_mode
                )
                if changed:
                    await light.set_color_xy(model.xy(), response=False)
        except Exception as exc:
            failures.append(f"{light.alias or light.address}: {exc}")

    await asyncio.gather(*(send(light) for light in lights))

    if not failures:
        return ""
    if len(failures) == 1:
        return failures[0]
    return f"{len(failures)} lamps rejected the update: {failures[0]}"


async def _read_back(light: HueLight, model: Model) -> str:
    """Seed the model from the lamp so the UI starts where the bulb actually is."""
    state = await light.get_state()
    if state.power is not None:
        model.power = state.power
    if state.brightness_percent is not None:
        model.brightness = state.brightness_percent
    if state.mired:
        model.kelvin = mired_to_kelvin(state.mired)
    if state.xy is not None:
        r, g, b = xy_to_rgb(state.xy, 1.0)
        h, s, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        model.hue = h * 360
        model.saturation = s * 100
    return state.name or light.alias or light.address


def _draw(screen: "curses._CursesWindow", model: Model, label: str, status: str) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    rgb = model.rgb()

    screen.addstr(0, 2, f"BazziteHue — {label}"[: width - 4], curses.A_BOLD)

    # Colour preview. Curses has no portable 24-bit colour, so the swatch is
    # quantised to the terminal's 256-colour cube.
    swatch_width = max(10, min(width - 8, 48))
    attribute = curses.A_REVERSE
    if curses.has_colors() and curses.COLORS >= 256:
        try:
            curses.init_pair(1, curses.COLOR_BLACK, _ansi256(rgb))
            attribute = curses.color_pair(1)
        except curses.error:
            pass
    try:
        screen.addstr(2, 4, " " * swatch_width, attribute)
    except curses.error:
        pass

    power = "ON " if model.power else "OFF"
    mode = f"white {model.kelvin}K" if model.white_mode else f"hue {model.hue:5.1f}°"
    lines = [
        f"power       {power}",
        f"brightness  {_bar(model.brightness)} {model.brightness:5.1f}%",
        f"mode        {mode}",
        (
            f"saturation  {_bar(model.saturation)} {model.saturation:5.1f}%"
            if not model.white_mode
            else (
                f"temperature "
                f"{_bar((model.kelvin - MIN_TUI_KELVIN) * 100 / (MAX_TUI_KELVIN - MIN_TUI_KELVIN))}"
                f" {model.kelvin}K"
            )
        ),
        f"colour      #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}",
    ]

    row = 4
    for line in lines:
        if row < height - 1:
            screen.addstr(row, 4, line[: width - 6])
            row += 1

    row += 1
    for line in HELP_LINES:
        if row < height - 2:
            screen.addstr(row, 2, line[: width - 4], curses.A_DIM)
            row += 1

    if status and height > 2:
        screen.addstr(height - 1, 2, status[: width - 4], curses.A_DIM)

    screen.noutrefresh()
    curses.doupdate()


def _ansi256(rgb: tuple[int, int, int]) -> int:
    """Map 8-bit RGB onto the xterm 6×6×6 colour cube (indices 16..231)."""
    r, g, b = (max(0, min(255, channel)) for channel in rgb)
    levels = [round(channel * 5 / 255) for channel in (r, g, b)]
    return 16 + 36 * levels[0] + 6 * levels[1] + levels[2]


def _bar(percent: float, width: int = 20) -> str:
    filled = int(round(_clamp(percent, 0, 100) / 100 * width))
    return "█" * filled + "·" * (width - filled)


def _apply_key(key: int, model: Model) -> tuple[bool, str]:
    """Fold a key press into the model. Returns (changed, status message)."""
    if key in (curses.KEY_LEFT, ord("h")):
        model.hue = (model.hue - 5) % 360
        model.white_mode = False
    elif key in (curses.KEY_RIGHT, ord("l")):
        model.hue = (model.hue + 5) % 360
        model.white_mode = False
    elif key == curses.KEY_SLEFT or key == ord("H"):
        model.hue = (model.hue - 20) % 360
        model.white_mode = False
    elif key == curses.KEY_SRIGHT or key == ord("L"):
        model.hue = (model.hue + 20) % 360
        model.white_mode = False
    elif key in (curses.KEY_UP, ord("k")):
        model.brightness = _clamp(model.brightness + 5, 0, 100)
    elif key in (curses.KEY_DOWN, ord("j")):
        model.brightness = _clamp(model.brightness - 5, 0, 100)
    elif key in (curses.KEY_SR, ord("K")):
        model.brightness = _clamp(model.brightness + 20, 0, 100)
    elif key in (curses.KEY_SF, ord("J")):
        model.brightness = _clamp(model.brightness - 20, 0, 100)
    elif key == ord("s"):
        model.saturation = _clamp(model.saturation - 5, 0, 100)
        model.white_mode = False
    elif key == ord("S"):
        model.saturation = _clamp(model.saturation + 5, 0, 100)
        model.white_mode = False
    elif key == ord("t"):
        model.white_mode = True
        model.kelvin = int(_clamp(model.kelvin - 100, MIN_TUI_KELVIN, MAX_TUI_KELVIN))
    elif key == ord("T"):
        model.white_mode = True
        model.kelvin = int(_clamp(model.kelvin + 100, MIN_TUI_KELVIN, MAX_TUI_KELVIN))
    elif key == ord("w"):
        model.white_mode = True
    elif key == ord("c"):
        model.white_mode = False
    elif key == ord(" "):
        model.power = not model.power
        return True, "power toggled"
    elif 0 <= key < 256 and chr(key) in PRESET_KEYS:
        index = PRESET_KEYS.index(chr(key))
        if index < len(PRESETS):
            name, hex_value = PRESETS[index]
            r, g, b = parse_hex(hex_value)
            h, s, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            model.hue = h * 360
            model.saturation = s * 100
            model.white_mode = False
            return True, f"preset: {name}"
    else:
        return False, ""

    return True, ""


async def run_tui(lights: list[HueLight]) -> int:
    if not sys.stdout.isatty():
        print("bazzitehue tui needs an interactive terminal", file=sys.stderr)
        return 2

    for light in lights:
        await light.connect()

    model = Model()
    label = ", ".join(light.alias or light.address for light in lights)
    try:
        label = await _read_back(lights[0], model)
        if len(lights) > 1:
            label += f" (+{len(lights) - 1} more)"
    except Exception as exc:  # a lamp that will not answer reads can still be driven
        label += f" [read failed: {exc}]"

    screen = curses.initscr()
    curses.noecho()
    curses.cbreak()
    curses.curs_set(0)
    screen.keypad(True)
    # A short blocking timeout rather than nodelay: ncurses needs a moment to
    # assemble a multi-byte escape sequence, and with nodelay an arrow key can
    # come back as a bare ESC instead of KEY_LEFT/KEY_RIGHT.
    screen.timeout(KEY_POLL_MS)
    if curses.has_colors():
        curses.start_color()
        with_default = getattr(curses, "use_default_colors", None)
        if with_default:
            with_default()

    status = "connected"
    last_pushed: Model | None = None
    dirty = True
    needs_draw = True
    last_push_at = 0.0

    running = True
    try:
        while running:
            key = screen.getch()
            while key != -1:
                if key == ord("q"):
                    running = False
                    break
                if key == ord("r"):
                    try:
                        label = await _read_back(lights[0], model)
                        status = "re-read from lamp"
                        dirty = True
                    except Exception as exc:
                        status = f"read failed: {exc}"
                    needs_draw = True
                elif key == curses.KEY_RESIZE:
                    needs_draw = True
                elif key == 27:
                    # A lone ESC (or an escape sequence this terminal did not
                    # finish sending); quitting on it would be far too easy.
                    pass
                else:
                    changed, message = _apply_key(key, model)
                    if changed:
                        dirty = True
                        needs_draw = True
                    if message:
                        status = message
                        needs_draw = True
                key = screen.getch()

            # On the way out, flush whatever the last keystrokes changed instead
            # of leaving the lamp a step behind what the user saw.
            now = time.monotonic()
            if dirty and (not running or now - last_push_at >= WRITE_INTERVAL):
                snapshot = Model(**vars(model))
                problem = await _push(lights, snapshot, last_pushed)
                last_push_at = now
                dirty = False
                # Only remember a clean push, so the next one re-sends everything
                # the failed one was carrying.
                if problem:
                    status = problem
                    last_pushed = None
                    needs_draw = True
                else:
                    last_pushed = snapshot

            if needs_draw and running:
                _draw(screen, model, label, status)
                needs_draw = False

            # getch() already paced this loop; yield so the event loop can run.
            await asyncio.sleep(0)

        return 0
    finally:
        screen.keypad(False)
        curses.nocbreak()
        curses.echo()
        curses.curs_set(1)
        curses.endwin()
        for light in lights:
            await light.disconnect()
