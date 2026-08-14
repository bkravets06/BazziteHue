"""The Philips Hue Bluetooth LE GATT layout.

Philips does not publish a specification for the BLE interface on their bulbs, so
these UUIDs and payload formats come from community reverse engineering and match
what other open-source Hue BLE clients use. They are stable across the Hue bulbs
people have tested (Color/Ambiance A19/A60, Play, Go, Lightstrip, Filament), but
if a lamp misbehaves, ``bazzitehue gatt-dump`` prints the real attribute table so
the layout can be checked against the hardware in front of you.
"""

from __future__ import annotations

from dataclasses import dataclass

# Advertised by every Hue lamp; used to spot them during a scan.
HUE_ADVERTISED_SERVICE = "0000fe0f-0000-1000-8000-00805f9b34fb"

# The service that carries the actual light controls.
LIGHT_SERVICE = "932c32bd-0000-47a2-835a-a8d455b859dd"
CHAR_POWER = "932c32bd-0002-47a2-835a-a8d455b859dd"
CHAR_BRIGHTNESS = "932c32bd-0003-47a2-835a-a8d455b859dd"
CHAR_TEMPERATURE = "932c32bd-0004-47a2-835a-a8d455b859dd"
CHAR_COLOR = "932c32bd-0005-47a2-835a-a8d455b859dd"

# Standard Bluetooth SIG device information service.
DEVICE_INFO_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"
CHAR_MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"
CHAR_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"

# The name shown in the Hue app, writable, on a Hue-proprietary service.
CHAR_LIGHT_NAME = "97fe6561-0003-4f62-86e9-b71ee2da3d22"

# The firmware rejects 0 as a brightness; it wants 1..254 with off handled by the
# power characteristic instead.
BRIGHTNESS_MIN = 1
BRIGHTNESS_MAX = 254


@dataclass(frozen=True)
class LightState:
    """A snapshot of everything we can read back from a lamp."""

    address: str
    name: str | None = None
    power: bool | None = None
    brightness: int | None = None  # raw, 1..254
    xy: tuple[float, float] | None = None
    mired: int | None = None
    model: str | None = None
    manufacturer: str | None = None
    firmware: str | None = None

    @property
    def brightness_percent(self) -> float | None:
        if self.brightness is None:
            return None
        return raw_to_percent(self.brightness)


def percent_to_raw(percent: float) -> int:
    """Map a 0..100 percentage onto the firmware's 1..254 brightness range."""
    percent = max(0.0, min(100.0, float(percent)))
    span = BRIGHTNESS_MAX - BRIGHTNESS_MIN
    return int(round(BRIGHTNESS_MIN + span * percent / 100))


def raw_to_percent(raw: int) -> float:
    raw = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, int(raw)))
    span = BRIGHTNESS_MAX - BRIGHTNESS_MIN
    return round((raw - BRIGHTNESS_MIN) * 100 / span, 1)


def encode_power(on: bool) -> bytes:
    return bytes([1 if on else 0])


def decode_power(payload: bytes) -> bool:
    if not payload:
        raise ValueError("empty power payload")
    return bool(payload[0])


def encode_brightness(raw: int) -> bytes:
    return bytes([max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, int(raw)))])


def decode_brightness(payload: bytes) -> int:
    if not payload:
        raise ValueError("empty brightness payload")
    return payload[0]


def encode_xy(point: tuple[float, float]) -> bytes:
    """Two little-endian uint16s, each a 0..1 chromaticity scaled by 0xFFFF."""
    x, y = point
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError(f"xy out of range: {point}")
    scaled_x = int(round(x * 0xFFFF))
    scaled_y = int(round(y * 0xFFFF))
    return scaled_x.to_bytes(2, "little") + scaled_y.to_bytes(2, "little")


def decode_xy(payload: bytes) -> tuple[float, float]:
    if len(payload) < 4:
        raise ValueError(f"expected 4 bytes of colour data, got {len(payload)}")
    x = int.from_bytes(payload[0:2], "little") / 0xFFFF
    y = int.from_bytes(payload[2:4], "little") / 0xFFFF
    return (x, y)


def encode_mired(mired: int) -> bytes:
    return int(mired).to_bytes(2, "little")


def decode_mired(payload: bytes) -> int:
    if len(payload) < 2:
        raise ValueError(f"expected 2 bytes of temperature data, got {len(payload)}")
    return int.from_bytes(payload[0:2], "little")
