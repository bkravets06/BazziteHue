"""The bridge between Qt (synchronous, main thread) and bleak (asyncio).

A single background thread owns an asyncio loop and every open BLE connection.
The GUI never blocks on Bluetooth: it drops a desired value into a per-lamp
mailbox and gets told what happened through Qt signals.

Two things here matter for how the app feels:

* **Coalescing.** Dragging a slider produces updates far faster than a bulb can
  accept writes, so each lamp applies the most recent value at most every
  ``WRITE_INTERVAL`` seconds and throws away everything superseded in between.
* **Idle disconnect.** The link is kept open while you are actively adjusting a
  lamp (connecting costs seconds), but dropped once idle, because a BLE bulb
  accepts one connection at a time: while this app holds it, the Hue phone app
  cannot reach the bulb at all. ``Config.share_mode`` shortens that wait to a
  few seconds, trading responsiveness for getting out of the way quickly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Iterable

from PySide6.QtCore import QObject, Signal

from ..color import GAMUTS, ColorTarget
from ..config import Config, SavedLight
from ..light import HueLight, discover
from ..protocol import LightState

log = logging.getLogger(__name__)

WRITE_INTERVAL = 0.06
IDLE_DISCONNECT = 120.0
#: How long to hold a bulb when the user has asked to share it with other apps.
SHARED_IDLE_DISCONNECT = 3.0
#: A bulb held by a phone cannot be connected to at all, so a failed change is
#: kept and retried on a widening delay until the bulb comes free.
RETRY_BASE = 2.0
RETRY_CEILING = 20.0
MAX_RETRIES = 12


@dataclass
class _Lamp:
    """A lamp the worker is managing, plus whatever the GUI last asked for."""

    alias: str
    light: HueLight
    pending: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task | None = None
    wakeup: asyncio.Event | None = None
    connected: bool = False
    failures: int = 0


class BleWorker(QObject):
    """Owns the Bluetooth event loop and every lamp connection."""

    #: alias, LightState -- the lamp's current state as far as we know it
    lamp_state = Signal(str, object)
    #: alias, status text ("connecting", "connected", "disconnected")
    lamp_status = Signal(str, str)
    #: alias, message, hint -- something went wrong with one lamp
    lamp_error = Signal(str, str, str)
    #: list of (address, name) found by a scan
    scan_finished = Signal(object)
    #: message, hint
    scan_failed = Signal(str, str)
    #: address, succeeded, message
    pair_finished = Signal(str, bool, str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._lamps: dict[str, _Lamp] = {}
        self._gate: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closing = False

    # ------------------------------------------------------------------ #
    # Thread lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="bazzitehue-ble", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def stop(self, timeout: float = 5.0) -> None:
        """Disconnect everything and stop the loop, without hanging on exit."""
        self._closing = True
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        done = threading.Event()

        async def shutdown() -> None:
            try:
                for lamp in list(self._lamps.values()):
                    if lamp.task is not None:
                        lamp.task.cancel()
                    await lamp.light.disconnect()
            finally:
                done.set()

        asyncio.run_coroutine_threadsafe(shutdown(), loop)
        done.wait(timeout=timeout)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Fire a coroutine on the BLE loop from the GUI thread."""
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, loop)

    def _call(self, function: Callable[[], None]) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(function)

    @property
    def idle_timeout(self) -> float:
        """How long to keep a bulb connected with nothing to do."""
        return SHARED_IDLE_DISCONNECT if self.config.share_mode else IDLE_DISCONNECT

    # ------------------------------------------------------------------ #
    # Lamp registry
    # ------------------------------------------------------------------ #

    def _lamp_for(self, alias: str) -> _Lamp | None:
        """Look up a managed lamp, creating it from config on first use."""
        lamp = self._lamps.get(alias)
        if lamp is not None:
            return lamp

        saved: SavedLight | None = self.config.lights.get(alias)
        if saved is None:
            return None

        if self._gate is None:
            # One connection attempt at a time across every bulb: BlueZ is
            # unreliable when several are established at once on one adapter.
            self._gate = asyncio.Semaphore(1)

        light = HueLight(
            saved.address,
            gamut=GAMUTS.get(saved.gamut.upper(), GAMUTS["C"]),
            alias=alias,
            timeout=15.0,
            retries=1,
            gate=self._gate,
        )
        lamp = _Lamp(alias=alias, light=light)
        lamp.wakeup = asyncio.Event()
        lamp.task = asyncio.ensure_future(self._serve(lamp))
        self._lamps[alias] = lamp
        return lamp

    def forget(self, alias: str) -> None:
        """Drop a lamp the user removed, closing its connection."""

        async def close() -> None:
            lamp = self._lamps.pop(alias, None)
            if lamp is None:
                return
            if lamp.task is not None:
                lamp.task.cancel()
            await lamp.light.disconnect()

        self._submit(close())

    # ------------------------------------------------------------------ #
    # The per-lamp service task
    # ------------------------------------------------------------------ #

    async def _serve(self, lamp: _Lamp) -> None:
        """Apply whatever the GUI has asked for, one lamp, forever."""
        assert lamp.wakeup is not None
        while not self._closing:
            try:
                await asyncio.wait_for(lamp.wakeup.wait(), timeout=self.idle_timeout)
            except asyncio.TimeoutError:
                # Nothing wanted for a while: let go of the bulb so other apps
                # (and the Hue phone app) can reach it.
                if lamp.connected:
                    await lamp.light.disconnect()
                    lamp.connected = False
                    self.lamp_status.emit(lamp.alias, "disconnected")
                continue
            except asyncio.CancelledError:
                return

            lamp.wakeup.clear()
            payload, lamp.pending = lamp.pending, {}
            if not payload:
                continue

            try:
                await self._ensure_connected(lamp)
                await self._apply(lamp, payload)
                lamp.failures = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                await self._handle_failure(lamp, payload, exc)
                continue

            # Rate-limit: anything the user does during this pause is merged
            # into the next payload instead of queueing another write.
            await asyncio.sleep(WRITE_INTERVAL)

    async def _handle_failure(self, lamp: _Lamp, payload: dict[str, Any], exc: Exception) -> None:
        """Report a failed change and arrange to try it again.

        The commonest reason a change fails is that a phone is holding the bulb,
        which is temporary. Rather than throwing the user's change away, put it
        back in the mailbox (behind anything newer) and retry on a widening
        delay, so the bulb picks it up as soon as it comes free.
        """
        lamp.connected = False
        lamp.failures += 1
        with contextlib.suppress(Exception):
            await lamp.light.disconnect()

        hint = getattr(exc, "hint", "") or ""
        self.lamp_error.emit(lamp.alias, str(exc), hint)
        self.lamp_status.emit(lamp.alias, "disconnected")

        if lamp.failures > MAX_RETRIES:
            lamp.failures = 0
            return

        restored = dict(payload)
        restored.update(lamp.pending)  # anything newer wins
        lamp.pending = restored

        delay = min(RETRY_CEILING, RETRY_BASE * (2 ** (lamp.failures - 1)))
        self.lamp_status.emit(lamp.alias, f"waiting for the bulb (retry in {delay:.0f}s)")
        await asyncio.sleep(delay)
        if lamp.wakeup is not None:
            lamp.wakeup.set()

    async def _ensure_connected(self, lamp: _Lamp) -> None:
        if lamp.light.is_connected:
            lamp.connected = True
            return

        self.lamp_status.emit(lamp.alias, "connecting")
        await lamp.light.connect()
        lamp.connected = True
        self.lamp_status.emit(lamp.alias, "connected")
        await self._read_state(lamp)

    async def _read_state(self, lamp: _Lamp) -> None:
        state = await lamp.light.get_state()
        self.lamp_state.emit(lamp.alias, state)

    async def _apply(self, lamp: _Lamp, payload: dict[str, Any]) -> None:
        light = lamp.light

        if "power" in payload:
            await light.set_power(bool(payload["power"]))

        color: ColorTarget | None = payload.get("color")
        if color is not None:
            if color.xy is not None:
                await light.set_color_xy(color.xy, response=False)
            elif color.mired is not None:
                await light.set_mired(color.mired, response=False)

        if "brightness" in payload:
            await light.set_brightness_percent(float(payload["brightness"]), response=False)

        if payload.get("refresh"):
            await self._read_state(lamp)
            return

        # Report the state we just asked for rather than reading it back: a read
        # costs another round trip, and the bulb agrees with us if the write
        # succeeded.
        self.lamp_state.emit(lamp.alias, self._optimistic_state(lamp, payload))

    def _optimistic_state(self, lamp: _Lamp, payload: dict[str, Any]) -> LightState:
        color: ColorTarget | None = payload.get("color")
        brightness = payload.get("brightness")
        from ..protocol import percent_to_raw

        return LightState(
            address=lamp.light.address,
            name=lamp.alias,
            power=bool(payload["power"]) if "power" in payload else None,
            brightness=percent_to_raw(float(brightness)) if brightness is not None else None,
            xy=color.xy if color else None,
            mired=color.mired if color else None,
        )

    # ------------------------------------------------------------------ #
    # Commands from the GUI thread
    # ------------------------------------------------------------------ #

    def _queue(self, aliases: Iterable[str], **changes: Any) -> None:
        wanted = list(aliases)

        def apply() -> None:
            for alias in wanted:
                lamp = self._lamp_for(alias)
                if lamp is None or lamp.wakeup is None:
                    continue
                lamp.pending.update(changes)
                lamp.wakeup.set()

        self._call(apply)

    def set_power(self, aliases: Iterable[str], on: bool) -> None:
        self._queue(aliases, power=on)

    def set_brightness(self, aliases: Iterable[str], percent: float) -> None:
        self._queue(aliases, brightness=percent)

    def set_color(self, aliases: Iterable[str], color: ColorTarget) -> None:
        changes: dict[str, Any] = {"color": color}
        if color.brightness is not None:
            changes["brightness"] = color.brightness
        self._queue(aliases, **changes)

    def apply_scene(
        self,
        aliases: Iterable[str],
        color: ColorTarget | None,
        brightness: float | None,
        power: bool | None,
    ) -> None:
        changes: dict[str, Any] = {}
        if power is not None:
            changes["power"] = power
        if color is not None:
            changes["color"] = color
        if brightness is not None:
            changes["brightness"] = brightness
        if changes:
            self._queue(aliases, **changes)

    def refresh(self, aliases: Iterable[str]) -> None:
        """Read the real state back from the lamp."""
        self._queue(aliases, refresh=True)

    def release(self, aliases: Iterable[str] | None = None) -> None:
        """Drop the connection now, handing the bulb to whatever wants it next."""
        wanted = list(aliases) if aliases is not None else None

        async def run() -> None:
            for alias, lamp in list(self._lamps.items()):
                if wanted is not None and alias not in wanted:
                    continue
                lamp.pending.clear()
                lamp.failures = 0
                if lamp.light.is_connected:
                    await lamp.light.disconnect()
                lamp.connected = False
                self.lamp_status.emit(alias, "released")

        self._submit(run())

    # ------------------------------------------------------------------ #
    # One-off operations
    # ------------------------------------------------------------------ #

    def scan(self, timeout: float = 8.0, all_devices: bool = False) -> None:
        async def run() -> None:
            try:
                devices = await discover(timeout=timeout, all_devices=all_devices)
                self.scan_finished.emit([(d.address, d.name or "") for d in devices])
            except Exception as exc:
                self.scan_failed.emit(str(exc), _adapter_hint(exc))

        self._submit(run())

    def pair(self, address: str) -> None:
        async def run() -> None:
            light = HueLight(address, timeout=20.0, retries=1)
            try:
                async with light:
                    ok = await light.pair()
                self.pair_finished.emit(address, bool(ok), "")
            except Exception as exc:
                self.pair_finished.emit(address, False, str(exc))

        self._submit(run())


def _adapter_hint(exc: BaseException) -> str:
    """Turn a low-level Bluetooth failure into something a user can act on."""
    text = str(exc).lower()
    if "no such file" in text or "dbus" in text or "not available" in text:
        return (
            "Bluetooth does not appear to be running. Open System Settings and "
            "switch Bluetooth on, or run 'sudo systemctl enable --now bluetooth'."
        )
    return "Check that Bluetooth is switched on and the bulb has power."
