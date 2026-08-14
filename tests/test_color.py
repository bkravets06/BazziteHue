import pytest

from bazzitehue.color import (
    GAMUT_A,
    GAMUT_C,
    ColorError,
    clamp_mired,
    clamp_to_gamut,
    in_gamut,
    kelvin_to_mired,
    kelvin_to_xy,
    lerp_xy,
    mired_to_kelvin,
    parse_color,
    parse_hex,
    rgb_to_xy,
    xy_to_rgb,
)


class TestParseHex:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("#ff8800", (255, 136, 0)),
            ("ff8800", (255, 136, 0)),
            ("#f80", (255, 136, 0)),
            ("FFF", (255, 255, 255)),
            ("#000000", (0, 0, 0)),
        ],
    )
    def test_accepts_common_forms(self, text, expected):
        assert parse_hex(text) == expected

    @pytest.mark.parametrize("text", ["#ff88", "gg0000", "", "#1234567"])
    def test_rejects_junk(self, text):
        with pytest.raises(ColorError):
            parse_hex(text)


class TestRgbXyRoundTrip:
    @pytest.mark.parametrize(
        "rgb",
        [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 136, 0), (128, 128, 128)],
    )
    def test_hue_survives_the_round_trip(self, rgb):
        point, _ = rgb_to_xy(rgb)
        back = xy_to_rgb(point, 1.0)

        # The lamp's gamut cannot hit every sRGB colour, so compare the ordering of
        # the channels rather than exact values: red must stay the dominant one.
        assert back.index(max(back)) == rgb.index(max(rgb))

    def test_black_is_handled(self):
        point, brightness = rgb_to_xy((0, 0, 0))
        assert brightness == 0.0
        assert in_gamut(point)

    def test_white_lands_near_d65(self):
        point, brightness = rgb_to_xy((255, 255, 255))
        assert brightness == pytest.approx(1.0, abs=0.01)
        assert point[0] == pytest.approx(0.3227, abs=0.02)
        assert point[1] == pytest.approx(0.3290, abs=0.02)

    def test_output_is_always_inside_the_gamut(self):
        for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255)]:
            point, _ = rgb_to_xy(rgb, GAMUT_A)
            assert in_gamut(point, GAMUT_A)


class TestGamut:
    def test_corners_are_inside(self):
        for corner in GAMUT_C.corners():
            assert in_gamut(corner, GAMUT_C)

    def test_centre_is_inside(self):
        assert in_gamut((0.33, 0.33), GAMUT_C)

    def test_outside_point_is_pulled_in(self):
        clamped = clamp_to_gamut((0.9, 0.05), GAMUT_C)
        assert in_gamut(clamped, GAMUT_C) or _almost_in(clamped, GAMUT_C)

    def test_inside_point_is_untouched(self):
        point = (0.4, 0.4)
        assert clamp_to_gamut(point, GAMUT_C) == point

    def test_narrow_gamut_clamps_more(self):
        # Gamut C's green corner sits outside the older, narrower gamut A.
        deep_green = (0.17, 0.70)
        assert clamp_to_gamut(deep_green, GAMUT_C) == deep_green
        pulled_in = clamp_to_gamut(deep_green, GAMUT_A)
        assert pulled_in != deep_green
        assert _almost_in(pulled_in, GAMUT_A)


def _almost_in(point, gamut, tolerance=1e-9):
    """Clamping lands exactly on an edge, where float error can read as outside."""
    x, y = point
    nudged = clamp_to_gamut((x, y), gamut)
    return abs(nudged[0] - x) < 1e-6 and abs(nudged[1] - y) < 1e-6


class TestColorTemperature:
    def test_mired_kelvin_round_trip(self):
        # Mireds are integers, so the round trip is lossy by up to a few Kelvin at
        # the cool end -- far below anything the eye picks up.
        for kelvin in (2000, 2700, 4000, 6500):
            assert mired_to_kelvin(kelvin_to_mired(kelvin)) == pytest.approx(kelvin, rel=0.005)

    def test_clamping_matches_firmware_range(self):
        assert clamp_mired(10) == 153
        assert clamp_mired(9000) == 500
        assert clamp_mired(300) == 300

    def test_warm_is_redder_than_cool(self):
        warm = xy_to_rgb(kelvin_to_xy(2000), 1.0)
        cool = xy_to_rgb(kelvin_to_xy(6500), 1.0)
        assert warm[0] >= cool[0]
        assert warm[2] < cool[2]

    def test_rejects_nonsense(self):
        with pytest.raises(ColorError):
            kelvin_to_mired(0)
        with pytest.raises(ColorError):
            mired_to_kelvin(-5)


class TestParseColor:
    def test_hex(self):
        target = parse_color("#ff0000")
        assert target.xy is not None
        assert target.mired is None
        assert xy_to_rgb(target.xy, 1.0)[0] > 200

    def test_rgb_triple(self):
        assert parse_color("255,0,0").xy == parse_color("#ff0000").xy

    def test_explicit_rgb_prefix(self):
        assert parse_color("rgb:255,0,0").xy == parse_color("#ff0000").xy

    def test_named_colour(self):
        assert parse_color("red").xy == parse_color("#ff0000").xy

    def test_named_white_becomes_a_temperature(self):
        target = parse_color("warm")
        assert target.mired == kelvin_to_mired(2700)
        assert target.xy is None

    def test_kelvin(self):
        assert parse_color("3000k").mired == kelvin_to_mired(3000)
        assert parse_color("3000K").mired == kelvin_to_mired(3000)

    def test_kelvin_is_clamped_to_the_supported_range(self):
        assert parse_color("100000k").mired == 153
        assert parse_color("1000k").mired == 500

    def test_mireds(self):
        assert parse_color("250mired").mired == 250

    def test_hsv_carries_brightness(self):
        target = parse_color("hsv:0,100,50")
        assert target.xy is not None
        assert target.brightness == pytest.approx(50)

    def test_hsv_hue_wraps(self):
        assert parse_color("hsv:360,100,100").xy == parse_color("hsv:0,100,100").xy

    def test_hsl(self):
        target = parse_color("hsl:120,100,50")
        assert target.xy is not None
        rgb = xy_to_rgb(target.xy, 1.0)
        assert rgb.index(max(rgb)) == 1

    def test_hsl_lightness_maps_around_the_pure_colour(self):
        # L=50% is the fully saturated colour, so it should mean full brightness.
        assert parse_color("hsl:120,100,50").brightness == 100
        assert parse_color("hsl:120,100,25").brightness == pytest.approx(50)
        assert parse_color("hsl:120,100,100").brightness == 100

    def test_xy_passthrough(self):
        target = parse_color("xy:0.4,0.4")
        assert target.xy == (0.4, 0.4)

    def test_xy_is_clamped_to_the_gamut(self):
        target = parse_color("xy:0.99,0.005")
        assert target.xy is not None
        assert _almost_in(target.xy, GAMUT_C)

    @pytest.mark.parametrize(
        "spec", ["", "   ", "not-a-colour", "xy:2,3", "hsv:1,2", "rgb:300,0,0", "zzz:1,2,3"]
    )
    def test_rejects_bad_input(self, spec):
        with pytest.raises(ColorError):
            parse_color(spec)

    def test_describe_is_readable(self):
        assert "K" in parse_color("2700k").describe()
        assert "#" in parse_color("#ff0000").describe()


class TestLerp:
    def test_endpoints(self):
        assert lerp_xy((0.1, 0.1), (0.5, 0.5), 0.0) == (0.1, 0.1)
        assert lerp_xy((0.1, 0.1), (0.5, 0.5), 1.0) == (0.5, 0.5)

    def test_midpoint(self):
        assert lerp_xy((0.0, 0.0), (0.4, 0.8), 0.5) == pytest.approx((0.2, 0.4))

    def test_fraction_is_clamped(self):
        assert lerp_xy((0.1, 0.1), (0.5, 0.5), 5.0) == (0.5, 0.5)
        assert lerp_xy((0.1, 0.1), (0.5, 0.5), -1.0) == (0.1, 0.1)
