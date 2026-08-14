import pytest

from bazzitehue.protocol import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    LightState,
    decode_brightness,
    decode_mired,
    decode_power,
    decode_xy,
    encode_brightness,
    encode_mired,
    encode_power,
    encode_xy,
    percent_to_raw,
    raw_to_percent,
)


class TestPower:
    def test_encoding(self):
        assert encode_power(True) == b"\x01"
        assert encode_power(False) == b"\x00"

    def test_decoding(self):
        assert decode_power(b"\x01") is True
        assert decode_power(b"\x00") is False

    def test_any_nonzero_counts_as_on(self):
        assert decode_power(b"\x02") is True

    def test_empty_payload_is_an_error(self):
        with pytest.raises(ValueError):
            decode_power(b"")


class TestBrightness:
    def test_percent_endpoints(self):
        assert percent_to_raw(0) == BRIGHTNESS_MIN
        assert percent_to_raw(100) == BRIGHTNESS_MAX

    def test_percent_is_clamped(self):
        assert percent_to_raw(-20) == BRIGHTNESS_MIN
        assert percent_to_raw(500) == BRIGHTNESS_MAX

    def test_round_trip(self):
        for percent in (0, 10, 25, 50, 75, 100):
            assert raw_to_percent(percent_to_raw(percent)) == pytest.approx(percent, abs=0.5)

    def test_never_encodes_zero(self):
        # The firmware treats 0 as invalid; off is the power characteristic's job.
        assert encode_brightness(0) == bytes([BRIGHTNESS_MIN])
        assert encode_brightness(-5) == bytes([BRIGHTNESS_MIN])

    def test_encoding_is_clamped_high(self):
        assert encode_brightness(999) == bytes([BRIGHTNESS_MAX])

    def test_decoding(self):
        assert decode_brightness(b"\x7f") == 127

    def test_decoding_empty(self):
        with pytest.raises(ValueError):
            decode_brightness(b"")


class TestColor:
    def test_encodes_two_little_endian_uint16s(self):
        payload = encode_xy((1.0, 0.0))
        assert payload == b"\xff\xff\x00\x00"
        assert len(payload) == 4

    def test_round_trip(self):
        for point in [(0.0, 0.0), (0.3227, 0.3290), (0.675, 0.322), (1.0, 1.0)]:
            decoded = decode_xy(encode_xy(point))
            assert decoded == pytest.approx(point, abs=1e-4)

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            encode_xy((1.5, 0.2))
        with pytest.raises(ValueError):
            encode_xy((0.2, -0.1))

    def test_rejects_short_payload(self):
        with pytest.raises(ValueError):
            decode_xy(b"\x01\x02")


class TestTemperature:
    def test_round_trip(self):
        for mired in (153, 250, 370, 500):
            assert decode_mired(encode_mired(mired)) == mired

    def test_little_endian(self):
        assert encode_mired(500) == b"\xf4\x01"

    def test_rejects_short_payload(self):
        with pytest.raises(ValueError):
            decode_mired(b"\x01")


class TestLightState:
    def test_brightness_percent_is_derived(self):
        state = LightState(address="AA:BB:CC:DD:EE:FF", brightness=BRIGHTNESS_MAX)
        assert state.brightness_percent == 100.0

    def test_brightness_percent_is_none_when_unknown(self):
        assert LightState(address="AA:BB:CC:DD:EE:FF").brightness_percent is None
