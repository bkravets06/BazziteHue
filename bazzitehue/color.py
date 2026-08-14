"""Colour maths for Philips Hue bulbs.

Hue bulbs speak CIE 1931 xy chromaticity plus a separate brightness channel, and
(for white-ambiance capable bulbs) a colour temperature expressed in mireds.
Everything a user might type -- hex, RGB, HSV, a colour name, a Kelvin value --
is converted here into one of those two representations.

The sRGB <-> XYZ matrices below are the "Wide RGB D65" ones Philips documents for
Hue, not the textbook sRGB ones; they are what the lamps are calibrated against.
"""

from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass
from typing import Iterable

# Mired = micro reciprocal degrees = 1_000_000 / Kelvin.
# The Hue firmware accepts 153..500 mireds (~6535K..2000K).
MIN_MIRED = 153
MAX_MIRED = 500
MIN_KELVIN = 2000
MAX_KELVIN = 6535

Point = tuple[float, float]


class ColorError(ValueError):
    """Raised when a colour string cannot be understood."""


@dataclass(frozen=True)
class Gamut:
    """The triangle of colours a particular lamp can actually reproduce."""

    name: str
    red: Point
    green: Point
    blue: Point

    def corners(self) -> tuple[Point, Point, Point]:
        return (self.red, self.green, self.blue)


# Philips' published gamuts. A = first-gen LivingColors, B = older Hue A19 colour
# bulbs, C = everything modern (Hue Color, Lightstrip Plus, Play, Go, ...).
GAMUT_A = Gamut("A", (0.704, 0.296), (0.2151, 0.7106), (0.138, 0.08))
GAMUT_B = Gamut("B", (0.675, 0.322), (0.4090, 0.5180), (0.167, 0.04))
GAMUT_C = Gamut("C", (0.692, 0.308), (0.1700, 0.7000), (0.153, 0.048))
GAMUTS = {"A": GAMUT_A, "B": GAMUT_B, "C": GAMUT_C}
DEFAULT_GAMUT = GAMUT_C

# A small, deliberately opinionated set of names. Anything not here can still be
# given as hex, so this only needs to cover the words people actually type at a
# light bulb.
NAMED_COLORS: dict[str, str] = {
    "red": "#ff0000",
    "crimson": "#dc143c",
    "orange": "#ff7f00",
    "amber": "#ffbf00",
    "gold": "#ffd700",
    "yellow": "#ffff00",
    "lime": "#bfff00",
    "green": "#00ff00",
    "forest": "#228b22",
    "mint": "#7fffd4",
    "teal": "#008080",
    "cyan": "#00ffff",
    "sky": "#87ceeb",
    "azure": "#007fff",
    "blue": "#0000ff",
    "indigo": "#4b0082",
    "violet": "#8a2be2",
    "purple": "#800080",
    "magenta": "#ff00ff",
    "pink": "#ff69b4",
    "rose": "#ff007f",
    "salmon": "#fa8072",
    "peach": "#ffdab9",
    "lavender": "#e6e6fa",
    "white": "#ffffff",
}

# Named colour temperatures, in Kelvin.
NAMED_WHITES: dict[str, int] = {
    "candle": 2000,
    "warm": 2700,
    "soft": 2900,
    "relax": 2200,
    "reading": 3200,
    "neutral": 4000,
    "concentrate": 4600,
    "cool": 5000,
    "energize": 6000,
    "daylight": 6500,
}


# --------------------------------------------------------------------------- #
# sRGB <-> CIE xy
# --------------------------------------------------------------------------- #


def _gamma_expand(channel: float) -> float:
    return ((channel + 0.055) / 1.055) ** 2.4 if channel > 0.04045 else channel / 12.92


def _gamma_compress(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * channel
    return 1.055 * (channel ** (1 / 2.4)) - 0.055


def rgb_to_xy(rgb: tuple[int, int, int], gamut: Gamut = DEFAULT_GAMUT) -> tuple[Point, float]:
    """Convert 8-bit sRGB to (xy, relative brightness 0..1).

    The brightness returned is the Y (luminance) component, which the caller may
    use as a suggested brightness or ignore in favour of an explicit one.
    """
    r, g, b = (_gamma_expand(max(0, min(255, c)) / 255) for c in rgb)

    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039

    total = x + y + z
    if total == 0:
        # Pure black has no chromaticity; anchor it to the gamut's white-ish centre.
        return clamp_to_gamut((0.3227, 0.3290), gamut), 0.0

    point = clamp_to_gamut((x / total, y / total), gamut)
    return point, min(1.0, y)


def xy_to_rgb(point: Point, brightness: float = 1.0) -> tuple[int, int, int]:
    """Convert CIE xy plus relative brightness (0..1) back to 8-bit sRGB.

    Used for previews and for the TUI's colour swatch, never for talking to the
    lamp, so the result is normalised rather than gamut-clipped.
    """
    x, y = point
    if y <= 0:
        return (0, 0, 0)

    brightness = max(0.0, min(1.0, brightness))
    z = 1.0 - x - y
    big_y = brightness
    big_x = (big_y / y) * x
    big_z = (big_y / y) * z

    r = big_x * 1.656492 - big_y * 0.354851 - big_z * 0.255038
    g = -big_x * 0.707196 + big_y * 1.655397 + big_z * 0.036152
    b = big_x * 0.051713 - big_y * 0.121364 + big_z * 1.011530

    # Desaturate towards white rather than clipping a single channel, which would
    # shift the hue.
    floor = min(r, g, b)
    if floor < 0:
        r, g, b = (c - floor for c in (r, g, b))

    peak = max(r, g, b)
    if peak > 1:
        r, g, b = (c / peak for c in (r, g, b))

    return tuple(round(max(0.0, min(1.0, _gamma_compress(c))) * 255) for c in (r, g, b))  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Gamut clamping
# --------------------------------------------------------------------------- #


def _closest_point_on_segment(start: Point, end: Point, point: Point) -> Point:
    ax, ay = start
    bx, by = end
    px, py = point
    abx, aby = bx - ax, by - ay
    denominator = abx * abx + aby * aby
    if denominator == 0:
        return start
    t = ((px - ax) * abx + (py - ay) * aby) / denominator
    t = max(0.0, min(1.0, t))
    return (ax + abx * t, ay + aby * t)


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def in_gamut(point: Point, gamut: Gamut = DEFAULT_GAMUT, epsilon: float = 1e-9) -> bool:
    """Barycentric containment test against the lamp's colour triangle.

    ``epsilon`` keeps a colour that lands exactly on a corner or an edge -- which
    happens routinely, since clamping puts it there -- from reading as outside on
    the next pass because of floating point error.
    """
    (rx, ry), (gx, gy), (bx, by) = gamut.corners()
    px, py = point

    v1 = (gx - rx, gy - ry)
    v2 = (bx - rx, by - ry)
    q = (px - rx, py - ry)

    denominator = v1[0] * v2[1] - v2[0] * v1[1]
    if denominator == 0:
        return False

    s = (q[0] * v2[1] - v2[0] * q[1]) / denominator
    t = (v1[0] * q[1] - q[0] * v1[1]) / denominator
    return s >= -epsilon and t >= -epsilon and (s + t) <= 1 + epsilon


def clamp_to_gamut(point: Point, gamut: Gamut = DEFAULT_GAMUT) -> Point:
    """Move an out-of-range colour to the nearest point the lamp can produce."""
    if in_gamut(point, gamut):
        return point

    red, green, blue = gamut.corners()
    candidates = [
        _closest_point_on_segment(red, green, point),
        _closest_point_on_segment(green, blue, point),
        _closest_point_on_segment(blue, red, point),
    ]
    return min(candidates, key=lambda candidate: _distance(candidate, point))


# --------------------------------------------------------------------------- #
# Colour temperature
# --------------------------------------------------------------------------- #


def kelvin_to_mired(kelvin: float) -> int:
    if kelvin <= 0:
        raise ColorError("colour temperature must be positive")
    return int(round(1_000_000 / kelvin))


def mired_to_kelvin(mired: float) -> int:
    if mired <= 0:
        raise ColorError("mired value must be positive")
    return int(round(1_000_000 / mired))


def clamp_mired(mired: int) -> int:
    return max(MIN_MIRED, min(MAX_MIRED, int(round(mired))))


def kelvin_to_xy(kelvin: float) -> Point:
    """Planckian locus approximation (Kim et al.), good for 1667K..25000K.

    Only used to render a temperature as a colour on screen; the lamp itself is
    driven with the mired value.
    """
    kelvin = max(1667.0, min(25000.0, float(kelvin)))
    t = kelvin
    if t <= 4000:
        x = (
            -0.2661239e9 / t**3
            - 0.2343589e6 / t**2
            + 0.8776956e3 / t
            + 0.179910
        )
    else:
        x = (
            -3.0258469e9 / t**3
            + 2.1070379e6 / t**2
            + 0.2226347e3 / t
            + 0.240390
        )

    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return (x, y)


# --------------------------------------------------------------------------- #
# Parsing user input
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColorTarget:
    """A parsed colour request.

    Exactly one of ``xy`` or ``mired`` is set: the lamp is either in colour mode
    or in white/ambiance mode. ``brightness`` is a 0..100 percentage and is only
    populated when the user's input implied one (e.g. ``hsv:200,100,50``).
    """

    xy: Point | None = None
    mired: int | None = None
    brightness: float | None = None

    def describe(self) -> str:
        if self.mired is not None:
            return f"{mired_to_kelvin(self.mired)}K ({self.mired} mired)"
        assert self.xy is not None
        r, g, b = xy_to_rgb(self.xy, 1.0)
        return f"xy({self.xy[0]:.4f}, {self.xy[1]:.4f}) ≈ #{r:02x}{g:02x}{b:02x}"


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_KELVIN_RE = re.compile(r"^(\d{3,7})\s*k$", re.IGNORECASE)
_MIRED_RE = re.compile(r"^(\d{2,4})\s*(?:mired|mireds|mirek)$", re.IGNORECASE)
_TRIPLE_RE = re.compile(r"^\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$")
_PAIR_RE = re.compile(r"^\s*([\d.]+)\s*,\s*([\d.]+)\s*$")


def parse_hex(value: str) -> tuple[int, int, int]:
    match = _HEX_RE.match(value.strip())
    if not match:
        raise ColorError(f"not a hex colour: {value!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def parse_color(spec: str, gamut: Gamut = DEFAULT_GAMUT) -> ColorTarget:
    """Turn a user-supplied colour string into a :class:`ColorTarget`.

    Accepted forms::

        #ff8800  ff8800  f80        hex
        255,136,0                   RGB triple
        rgb:255,136,0               explicit RGB
        hsv:30,100,100              hue/sat/value, degrees + percentages
        hsl:30,100,50               hue/sat/lightness
        xy:0.5,0.4                  raw CIE xy
        3000k  3000K                colour temperature in Kelvin
        250mired                    colour temperature in mireds
        red / warm / daylight       named colour or named white
    """
    raw = spec.strip()
    if not raw:
        raise ColorError("empty colour")

    lowered = raw.lower()

    if lowered in NAMED_WHITES:
        return ColorTarget(mired=clamp_mired(kelvin_to_mired(NAMED_WHITES[lowered])))

    if lowered in NAMED_COLORS:
        return _from_rgb(parse_hex(NAMED_COLORS[lowered]), gamut)

    match = _KELVIN_RE.match(lowered)
    if match:
        return ColorTarget(mired=clamp_mired(kelvin_to_mired(int(match.group(1)))))

    match = _MIRED_RE.match(lowered)
    if match:
        return ColorTarget(mired=clamp_mired(int(match.group(1))))

    prefix, _, rest = lowered.partition(":")
    if rest:
        if prefix == "xy":
            pair = _PAIR_RE.match(rest)
            if not pair:
                raise ColorError(f"expected xy:X,Y -- got {spec!r}")
            point = (float(pair.group(1)), float(pair.group(2)))
            if not (0 <= point[0] <= 1 and 0 <= point[1] <= 1):
                raise ColorError("xy coordinates must be between 0 and 1")
            return ColorTarget(xy=clamp_to_gamut(point, gamut))

        if prefix in {"hsv", "hsb", "hsl"}:
            triple = _TRIPLE_RE.match(rest)
            if not triple:
                raise ColorError(f"expected {prefix}:H,S,V -- got {spec!r}")
            hue = float(triple.group(1)) % 360 / 360
            second = max(0.0, min(100.0, float(triple.group(2)))) / 100
            third = max(0.0, min(100.0, float(triple.group(3)))) / 100
            if prefix == "hsl":
                rgb_float = colorsys.hls_to_rgb(hue, third, second)
                # In HSL, L=50% is the fully saturated colour, so treat that as
                # full brightness and scale the rest around it.
                brightness = min(100.0, third * 200)
            else:
                rgb_float = colorsys.hsv_to_rgb(hue, second, 1.0)
                brightness = third * 100
            rgb = tuple(round(c * 255) for c in rgb_float)
            target = _from_rgb(rgb, gamut)  # type: ignore[arg-type]
            return ColorTarget(xy=target.xy, brightness=brightness)

        if prefix == "rgb":
            triple = _TRIPLE_RE.match(rest)
            if not triple:
                raise ColorError(f"expected rgb:R,G,B -- got {spec!r}")
            rgb = tuple(int(round(float(triple.group(i)))) for i in (1, 2, 3))
            return _from_rgb(rgb, gamut)  # type: ignore[arg-type]

        if prefix == "hex":
            return _from_rgb(parse_hex(rest), gamut)

        raise ColorError(f"unknown colour prefix {prefix!r} in {spec!r}")

    triple = _TRIPLE_RE.match(lowered)
    if triple:
        rgb = tuple(int(round(float(triple.group(i)))) for i in (1, 2, 3))
        return _from_rgb(rgb, gamut)  # type: ignore[arg-type]

    if _HEX_RE.match(lowered):
        return _from_rgb(parse_hex(lowered), gamut)

    raise ColorError(
        f"cannot parse colour {spec!r}. Try a hex value (#ff8800), an RGB triple "
        f"(255,136,0), hsv:30,100,100, xy:0.5,0.4, 3000k, or a name such as 'red'."
    )


def _from_rgb(rgb: tuple[int, int, int], gamut: Gamut) -> ColorTarget:
    for channel in rgb:
        if not 0 <= channel <= 255:
            raise ColorError(f"RGB channels must be 0..255, got {rgb}")
    point, _ = rgb_to_xy(rgb, gamut)
    return ColorTarget(xy=point)


def color_names() -> Iterable[str]:
    yield from sorted(NAMED_COLORS)
    yield from sorted(NAMED_WHITES)


def lerp_xy(start: Point, end: Point, fraction: float) -> Point:
    fraction = max(0.0, min(1.0, fraction))
    return (
        start[0] + (end[0] - start[0]) * fraction,
        start[1] + (end[1] - start[1]) * fraction,
    )
