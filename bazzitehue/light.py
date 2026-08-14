"""Async client for a single Philips Hue bulb over Bluetooth LE.

Everything here goes through BlueZ via bleak, so no bridge, no Wi-Fi and no
Philips cloud account is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable, Iterable

from bleak import BleakClient, BleakScanner
from bleak import exc as bleak_exc
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import protocol
from .color import (
    DEFAULT_GAMUT,
    ColorTarget,
    Gamut,
    clamp_mired,
    clamp_to_gamut,
    kelvin_to_mired,
    lerp_xy,
)
from .errors import (
    ConnectionFailedError,
    DeviceNotFoundError,
    NotPairedError,
    UnsupportedFeatureError,
)
from .protocol import LightState

log = logging.getLogger(__name__)

# BlueZ reports a bonding failure in several different wordings depending on
# version and on whether the lamp or the kernel rejected the operation.
_AUTH_MARKERS = (
    "insufficient authentication",
    "insufficient encryption",
    "not authorized",
    "notpermitted",
    "not permitted",
    "authentication",
)


def _is_auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _looks_like_missing_device(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    # bleak 3.x has a dedicated exception; older releases only say so in the text.
    device_not_found = getattr(bleak_exc, "BleakDeviceNotFoundError", None)
    if device_not_found is not None and isinstance(exc, device_not_found):
        return True
    return isinstance(exc, BleakError) and "not found" in str(exc).lower()


async def discover(timeout: float = 8.0, all_devices: bool = False) -> list[BLEDevice]:
    """Scan for Hue lamps in range.

    Hue bulbs advertise service ``0xFE0F``. Lamps that are currently connected to
    another host (a Hue Bridge acting over BLE, a phone) may not advertise at all,
    which is why ``all_devices`` exists as an escape hatch.
    """
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    devices: list[BLEDevice] = []
    for device, advertisement in found.values():
        services = {uuid.lower() for uuid in (advertisement.service_uuids or ())}
        looks_like_hue = (
            protocol.HUE_ADVERTISED_SERVICE in services
            or protocol.LIGHT_SERVICE in services
            or (device.name or "").lower().startswith("hue")
        )
        if all_devices or looks_like_hue:
            devices.append(device)

    devices.sort(key=lambda d: (d.name or "￿", d.address))
    return devices


async def resolve_device(address: str, timeout: float = 8.0) -> BLEDevice | None:
    """Return a :class:`BLEDevice` for ``address``, or None if it is not there.

    A BLE central can only open a connection in response to a connectable
    advertisement, so a bulb that does not turn up in a scan cannot be connected
    to by any means -- there is no point falling back to a bare address.
    """
    return await BleakScanner.find_device_by_address(address, timeout=timeout)


class HueLight:
    """A connected (or connectable) Hue bulb.

    Use it as an async context manager::

        async with HueLight("AA:BB:CC:DD:EE:FF") as light:
            await light.set_power(True)
            await light.set_brightness_percent(60)
    """

    def __init__(
        self,
        address: str | BLEDevice,
        *,
        gamut: Gamut = DEFAULT_GAMUT,
        timeout: float = 20.0,
        retries: int = 2,
        alias: str | None = None,
        gate: "asyncio.Semaphore | None" = None,
    ) -> None:
        self.address = address if isinstance(address, str) else address.address
        self.alias = alias
        self.gamut = gamut
        self.timeout = timeout
        self.retries = retries
        # BlueZ copes badly with several connection attempts at once on one
        # adapter, so callers driving multiple bulbs share a gate that lets one
        # connection be established at a time. Established links run in parallel.
        self.gate = gate
        self._target = address
        self._client: BleakClient | None = None
        self._notify_callbacks: list[Callable[[LightState], Awaitable[None] | None]] = []
        self._cached: dict[str, object] = {}

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        if self.is_connected:
            return

        gate = self.gate if self.gate is not None else contextlib.nullcontext()
        async with gate:  # type: ignore[union-attr]
            await self._connect_once_serialised()

    async def _connect_once_serialised(self) -> None:
        """Find the bulb, then connect, retrying with a fresh scan each round.

        bleak's BlueZ backend runs its own ``find_device_by_address`` whenever it
        is handed a bare address, so resolving here first and passing the
        resulting device avoids paying for two scans on every attempt.
        """
        if self.is_connected:
            return

        scan_timeout = min(self.timeout, 10.0)
        device: BLEDevice | None = self._target if not isinstance(self._target, str) else None
        last_error: Exception | None = None
        found_it = device is not None

        for attempt in range(self.retries + 1):
            if device is None:
                try:
                    resolved = await resolve_device(self.address, timeout=scan_timeout)
                except Exception as exc:
                    # The scan itself failing means the adapter is unwell, which
                    # a connection attempt is not going to survive either.
                    raise ConnectionFailedError(
                        f"could not scan for {self.address}: {exc}",
                        hint=(
                            "Check that Bluetooth is on: 'bluetoothctl show' should "
                            "list an adapter with Powered: yes."
                        ),
                    ) from exc

                if resolved is None:
                    log.debug("%s is not advertising (attempt %d)", self.address, attempt + 1)
                    if attempt < self.retries:
                        await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                device = resolved
                found_it = True

            client = BleakClient(device, timeout=self.timeout)
            try:
                await client.connect()
                self._client = client
                # Remember the resolved device: reconnecting can then skip the scan.
                self._target = device
                log.debug("connected to %s on attempt %d", self.address, attempt + 1)
                return
            except Exception as exc:
                last_error = exc
                with contextlib.suppress(Exception):
                    await client.disconnect()
                # The D-Bus path may be stale now, so scan again next round.
                device = None
                if attempt < self.retries:
                    await asyncio.sleep(1.0 * (attempt + 1))

        if not found_it or _looks_like_missing_device(last_error):
            raise DeviceNotFoundError(
                f"{self.address} is not advertising, so nothing can connect to it",
                hint=(
                    "A Hue bulb takes one Bluetooth connection at a time, and stops "
                    "advertising while it has one. Either something else is holding "
                    "it (the Hue app on a phone is the usual culprit), or the bulb is "
                    "out of range or unpowered. A Hue Bridge does not cause this — it "
                    "uses Zigbee, not Bluetooth."
                ),
            ) from last_error

        raise ConnectionFailedError(
            f"could not connect to {self.address}: {last_error}",
            hint=(
                "The bulb answered the scan but refused the connection. Moving closer "
                "usually helps; so does power-cycling the bulb."
            ),
        ) from last_error

    async def disconnect(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None

    async def __aenter__(self) -> "HueLight":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    async def pair(self) -> bool:
        """Bond with the lamp so it will accept commands.

        Hue bulbs answer reads and writes only for a bonded host. On BlueZ this
        goes through the adapter's pairing agent; Hue lamps use Just Works, so no
        PIN is involved.
        """
        await self.connect()
        assert self._client is not None
        try:
            result = await self._client.pair()
        except NotImplementedError as exc:  # pragma: no cover - backend dependent
            raise UnsupportedFeatureError(
                "this bleak backend cannot pair; use 'bluetoothctl pair "
                f"{self.address}' instead"
            ) from exc
        # bleak 3.x returns None and signals failure by raising; older versions
        # return a bool.
        return True if result is None else bool(result)

    async def unpair(self) -> bool:
        await self.connect()
        assert self._client is not None
        result = await self._client.unpair()
        return True if result is None else bool(result)

    # ------------------------------------------------------------------ #
    # Low-level GATT access
    # ------------------------------------------------------------------ #

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise ConnectionFailedError(
                f"not connected to {self.address}; call connect() first"
            )
        return self._client

    def _has_characteristic(self, uuid: str) -> bool:
        client = self._require_client()
        return client.services.get_characteristic(uuid) is not None

    async def _read(self, uuid: str, *, required: bool = True) -> bytes | None:
        client = self._require_client()
        if not self._has_characteristic(uuid):
            if required:
                raise UnsupportedFeatureError(
                    f"{self.address} does not expose characteristic {uuid}"
                )
            return None
        try:
            return bytes(await client.read_gatt_char(uuid))
        except Exception as exc:
            if _is_auth_error(exc):
                raise NotPairedError(f"{self.address} refused a read: {exc}") from exc
            if required:
                raise
            log.debug("optional read of %s failed: %s", uuid, exc)
            return None

    async def _write(self, uuid: str, payload: bytes, *, response: bool = True) -> None:
        client = self._require_client()
        if not self._has_characteristic(uuid):
            raise UnsupportedFeatureError(
                f"{self.address} does not expose characteristic {uuid}. "
                "This lamp may not support that feature (white-only bulbs have no "
                "colour characteristic)."
            )
        try:
            await client.write_gatt_char(uuid, payload, response=response)
        except Exception as exc:
            if _is_auth_error(exc):
                raise NotPairedError(f"{self.address} refused a write: {exc}") from exc
            raise

    # ------------------------------------------------------------------ #
    # Capabilities
    # ------------------------------------------------------------------ #

    @property
    def supports_color(self) -> bool:
        return self._has_characteristic(protocol.CHAR_COLOR)

    @property
    def supports_temperature(self) -> bool:
        return self._has_characteristic(protocol.CHAR_TEMPERATURE)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    async def get_power(self) -> bool:
        payload = await self._read(protocol.CHAR_POWER)
        assert payload is not None
        return protocol.decode_power(payload)

    async def get_brightness(self) -> int:
        payload = await self._read(protocol.CHAR_BRIGHTNESS)
        assert payload is not None
        return protocol.decode_brightness(payload)

    async def get_brightness_percent(self) -> float:
        return protocol.raw_to_percent(await self.get_brightness())

    async def get_color_xy(self) -> tuple[float, float] | None:
        payload = await self._read(protocol.CHAR_COLOR, required=False)
        return protocol.decode_xy(payload) if payload else None

    async def get_mired(self) -> int | None:
        payload = await self._read(protocol.CHAR_TEMPERATURE, required=False)
        return protocol.decode_mired(payload) if payload else None

    async def get_name(self) -> str | None:
        payload = await self._read(protocol.CHAR_LIGHT_NAME, required=False)
        if not payload:
            return None
        return payload.decode("utf-8", errors="replace").rstrip("\x00").strip() or None

    async def get_device_info(self) -> dict[str, str | None]:
        async def text(uuid: str) -> str | None:
            payload = await self._read(uuid, required=False)
            if not payload:
                return None
            return payload.decode("utf-8", errors="replace").rstrip("\x00").strip()

        return {
            "model": await text(protocol.CHAR_MODEL_NUMBER),
            "manufacturer": await text(protocol.CHAR_MANUFACTURER),
            "firmware": await text(protocol.CHAR_FIRMWARE_REVISION),
        }

    async def get_state(self, *, include_device_info: bool = False) -> LightState:
        power = await self.get_power()
        brightness = await self.get_brightness()
        xy = await self.get_color_xy()
        mired = await self.get_mired()
        name = await self.get_name()

        info: dict[str, str | None] = {}
        if include_device_info:
            info = await self.get_device_info()

        return LightState(
            address=self.address,
            name=name or self.alias,
            power=power,
            brightness=brightness,
            xy=xy,
            mired=mired,
            model=info.get("model"),
            manufacturer=info.get("manufacturer"),
            firmware=info.get("firmware"),
        )

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    async def set_power(self, on: bool) -> None:
        await self._write(protocol.CHAR_POWER, protocol.encode_power(on))

    async def toggle(self) -> bool:
        new_state = not await self.get_power()
        await self.set_power(new_state)
        return new_state

    async def set_brightness(self, raw: int, *, response: bool = True) -> None:
        await self._write(
            protocol.CHAR_BRIGHTNESS, protocol.encode_brightness(raw), response=response
        )

    async def set_brightness_percent(self, percent: float, *, response: bool = True) -> None:
        await self.set_brightness(protocol.percent_to_raw(percent), response=response)

    async def set_color_xy(self, point: tuple[float, float], *, response: bool = True) -> None:
        clamped = clamp_to_gamut(point, self.gamut)
        await self._write(protocol.CHAR_COLOR, protocol.encode_xy(clamped), response=response)

    async def set_mired(self, mired: int, *, response: bool = True) -> None:
        await self._write(
            protocol.CHAR_TEMPERATURE,
            protocol.encode_mired(clamp_mired(mired)),
            response=response,
        )

    async def set_kelvin(self, kelvin: float, *, response: bool = True) -> None:
        await self.set_mired(kelvin_to_mired(kelvin), response=response)

    async def set_name(self, name: str) -> None:
        await self._write(protocol.CHAR_LIGHT_NAME, name.encode("utf-8"))

    async def apply(
        self,
        *,
        power: bool | None = None,
        color: ColorTarget | None = None,
        brightness_percent: float | None = None,
    ) -> None:
        """Apply several changes in the order that looks best on the lamp.

        Turning on first means a colour change lands on an already-lit bulb rather
        than flashing the old colour; turning off happens last so the user sees the
        other changes take effect if they asked for both.
        """
        if power is True:
            await self.set_power(True)

        if color is not None:
            if color.xy is not None:
                await self.set_color_xy(color.xy)
            elif color.mired is not None:
                await self.set_mired(color.mired)
            if color.brightness is not None and brightness_percent is None:
                brightness_percent = color.brightness

        if brightness_percent is not None:
            await self.set_brightness_percent(brightness_percent)

        if power is False:
            await self.set_power(False)

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #

    async def fade(
        self,
        *,
        color: ColorTarget | None = None,
        brightness_percent: float | None = None,
        duration: float = 2.0,
        steps_per_second: float = 20.0,
    ) -> None:
        """Interpolate to a target over ``duration`` seconds.

        The lamps have no built-in transition timer over BLE, so the ramp is done
        here with unacknowledged writes to keep the step rate achievable.
        """
        if duration <= 0:
            await self.apply(color=color, brightness_percent=brightness_percent)
            return

        start_xy = await self.get_color_xy() if color and color.xy else None
        start_brightness = (
            await self.get_brightness_percent() if brightness_percent is not None else None
        )

        # A colour temperature is a single scalar, so ramp it directly.
        start_mired = await self.get_mired() if color and color.mired is not None else None

        total_steps = max(1, int(duration * steps_per_second))
        interval = duration / total_steps

        for step in range(1, total_steps + 1):
            fraction = step / total_steps
            if color is not None and color.xy is not None:
                point = lerp_xy(start_xy, color.xy, fraction) if start_xy else color.xy
                await self.set_color_xy(point, response=False)
            elif color is not None and color.mired is not None and start_mired:
                value = start_mired + (color.mired - start_mired) * fraction
                await self.set_mired(int(round(value)), response=False)

            if brightness_percent is not None:
                if start_brightness is None:
                    await self.set_brightness_percent(brightness_percent, response=False)
                else:
                    value = start_brightness + (brightness_percent - start_brightness) * fraction
                    await self.set_brightness_percent(value, response=False)

            await asyncio.sleep(interval)

        # Land exactly on the target with an acknowledged write.
        await self.apply(color=color, brightness_percent=brightness_percent)


async def for_each(
    lights: Iterable[HueLight],
    action: Callable[[HueLight], Awaitable[object]],
) -> list[tuple[HueLight, object | Exception]]:
    """Run ``action`` against several lamps concurrently, collecting failures.

    One unreachable bulb should not stop the rest of the room from responding.
    """
    lights = list(lights)

    async def run(light: HueLight) -> object | Exception:
        try:
            await light.connect()
            return await action(light)
        except Exception as exc:  # reported per-light by the caller
            return exc
        finally:
            await light.disconnect()

    results = await asyncio.gather(*(run(light) for light in lights))
    return list(zip(lights, results))
