"""Tests for the light layer, driven by a stand-in for bleak's BleakClient."""

import asyncio

import pytest

from bazzitehue import light as light_module
from bazzitehue import protocol
from bazzitehue.color import GAMUT_C, ColorTarget, parse_color
from bazzitehue.errors import ConnectionFailedError, NotPairedError, UnsupportedFeatureError
from bazzitehue.light import HueLight

ADDRESS = "AA:BB:CC:DD:EE:FF"

COLOR_BULB_CHARS = {
    protocol.CHAR_POWER: b"\x01",
    protocol.CHAR_BRIGHTNESS: bytes([protocol.BRIGHTNESS_MAX]),
    protocol.CHAR_COLOR: protocol.encode_xy((0.4, 0.4)),
    protocol.CHAR_TEMPERATURE: protocol.encode_mired(370),
    protocol.CHAR_LIGHT_NAME: b"Desk lamp",
    protocol.CHAR_MODEL_NUMBER: b"LCA001",
    protocol.CHAR_MANUFACTURER: b"Signify Netherlands B.V.",
    protocol.CHAR_FIRMWARE_REVISION: b"1.104.2",
}

WHITE_BULB_CHARS = {
    protocol.CHAR_POWER: b"\x00",
    protocol.CHAR_BRIGHTNESS: bytes([100]),
}


class FakeDevice:
    """Stands in for bleak's BLEDevice, which is what a real scan returns.

    ``str()`` gives the address so the fake client can key its stores by it.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        self.name = "Hue color lamp"

    def __str__(self) -> str:
        return self.address


class FakeCharacteristic:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.properties = ["read", "write"]
        self.description = "fake"


class FakeServices:
    def __init__(self, values: dict[str, bytes]) -> None:
        self._uuids = set(values)

    def get_characteristic(self, uuid: str):
        return FakeCharacteristic(uuid) if uuid in self._uuids else None

    def __iter__(self):
        return iter(())


def reset_fake_client(values: dict[str, bytes] | None = None) -> type["FakeClient"]:
    """Put the fake backend back to a known state between tests."""
    FakeClient.instances = []
    FakeClient.stores = {}
    FakeClient.connect_failures = 0
    FakeClient.auth_error = False
    FakeClient.values = values or COLOR_BULB_CHARS
    return FakeClient


class FakeClient:
    """Minimal stand-in for BleakClient, recording every write."""

    instances: list["FakeClient"] = []

    #: Set to raise on connect, to exercise the retry path.
    connect_failures = 0
    #: Set to raise BlueZ's authentication error on read/write.
    auth_error = False
    values: dict[str, bytes] = COLOR_BULB_CHARS
    #: Characteristic values survive a disconnect, as they do on a real bulb.
    stores: dict[str, dict[str, bytes]] = {}

    def __init__(self, target, timeout: float = 30, **kwargs) -> None:
        self.target = target
        self.timeout = timeout
        self.is_connected = False
        self.writes: list[tuple[str, bytes, bool]] = []
        self.paired = False
        self.values = FakeClient.stores.setdefault(str(target), dict(type(self).values))
        self.services = FakeServices(self.values)
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        if FakeClient.connect_failures > 0:
            FakeClient.connect_failures -= 1
            raise light_module.BleakError("device not found")
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def pair(self) -> None:
        self.paired = True

    async def unpair(self) -> None:
        self.paired = False

    async def read_gatt_char(self, uuid: str) -> bytes:
        if FakeClient.auth_error:
            raise light_module.BleakError("Insufficient Authentication")
        return self.values[uuid]

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = True) -> None:
        if FakeClient.auth_error:
            raise light_module.BleakError("Insufficient Authentication")
        if uuid not in self.values:
            raise KeyError(uuid)
        self.values[uuid] = bytes(data)
        self.writes.append((uuid, bytes(data), response))


@pytest.fixture
def no_sleep(monkeypatch):
    """Collapse the retry and fade delays so timing tests stay instant."""
    real_sleep = asyncio.sleep

    async def instant(_delay, result=None):
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    reset_fake_client()

    monkeypatch.setattr(light_module, "BleakClient", FakeClient)

    async def fake_resolve(address, timeout=8.0):
        return FakeDevice(address)

    monkeypatch.setattr(light_module, "resolve_device", fake_resolve)
    yield FakeClient


async def connected_light(**kwargs) -> HueLight:
    light = HueLight(ADDRESS, **kwargs)
    await light.connect()
    return light


class TestConnection:
    async def test_connect_and_disconnect(self):
        light = await connected_light()
        assert light.is_connected
        await light.disconnect()
        assert not light.is_connected

    async def test_context_manager_disconnects(self):
        async with HueLight(ADDRESS) as light:
            assert light.is_connected
        assert not light.is_connected

    async def test_retries_then_succeeds(self, no_sleep):
        FakeClient.connect_failures = 2
        light = HueLight(ADDRESS, retries=2)
        await light.connect()
        assert light.is_connected
        assert len(FakeClient.instances) == 3

    async def test_gives_up_with_a_helpful_error(self, no_sleep):
        FakeClient.connect_failures = 99
        with pytest.raises(light_module.DeviceNotFoundError) as caught:
            await HueLight(ADDRESS, retries=1).connect()
        assert "advertising" in (caught.value.hint or "")

    async def test_operations_require_a_connection(self):
        with pytest.raises(ConnectionFailedError):
            await HueLight(ADDRESS).get_power()

    async def test_it_resolves_once_and_reuses_the_device(self, monkeypatch):
        """bleak rescans internally when handed a bare address, so resolve first."""
        calls = []
        real_resolve = light_module.resolve_device

        async def counting(address, timeout=8.0):
            calls.append(address)
            return await real_resolve(address, timeout=timeout)

        monkeypatch.setattr(light_module, "resolve_device", counting)

        light = HueLight(ADDRESS)
        await light.connect()
        assert len(calls) == 1

        # A later reconnect reuses the resolved device instead of scanning again.
        await light.disconnect()
        await light.connect()
        assert len(calls) == 1

    async def test_a_bulb_that_is_not_advertising_says_why(self, monkeypatch, no_sleep):
        async def nothing_found(address, timeout=8.0):
            return None

        monkeypatch.setattr(light_module, "resolve_device", nothing_found)

        with pytest.raises(light_module.DeviceNotFoundError) as caught:
            await HueLight(ADDRESS, retries=1).connect()

        hint = caught.value.hint or ""
        assert "one Bluetooth connection at a time" in hint
        assert "phone" in hint
        # A Bridge is Zigbee and must not be blamed for this.
        assert "Zigbee, not Bluetooth" in hint

    async def test_a_broken_adapter_is_not_mistaken_for_a_missing_bulb(self, monkeypatch):
        async def broken(address, timeout=8.0):
            raise OSError("No such file or directory")

        monkeypatch.setattr(light_module, "resolve_device", broken)

        with pytest.raises(ConnectionFailedError) as caught:
            await HueLight(ADDRESS, retries=0).connect()
        assert "bluetoothctl" in (caught.value.hint or "")

    async def test_the_gate_serialises_connection_attempts(self, monkeypatch):
        """BlueZ is unreliable when several connects run at once on one adapter."""
        active = 0
        peak = 0
        original = FakeClient.connect

        async def slow_connect(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                await original(self)
            finally:
                active -= 1

        monkeypatch.setattr(FakeClient, "connect", slow_connect)

        gate = asyncio.Semaphore(1)
        lights = [
            HueLight(ADDRESS, gate=gate),
            HueLight("11:22:33:44:55:66", gate=gate),
            HueLight("22:33:44:55:66:77", gate=gate),
        ]
        await asyncio.gather(*(light.connect() for light in lights))

        assert peak == 1
        assert all(light.is_connected for light in lights)

    async def test_without_a_gate_connections_overlap(self, monkeypatch):
        active = 0
        peak = 0
        original = FakeClient.connect

        async def slow_connect(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                await original(self)
            finally:
                active -= 1

        monkeypatch.setattr(FakeClient, "connect", slow_connect)

        lights = [HueLight(ADDRESS), HueLight("11:22:33:44:55:66")]
        await asyncio.gather(*(light.connect() for light in lights))
        assert peak == 2

    async def test_pairing(self):
        light = await connected_light()
        assert await light.pair() is True
        assert FakeClient.instances[-1].paired is True
        assert await light.unpair() is True


class TestReads:
    async def test_power(self):
        light = await connected_light()
        assert await light.get_power() is True

    async def test_brightness(self):
        light = await connected_light()
        assert await light.get_brightness() == protocol.BRIGHTNESS_MAX
        assert await light.get_brightness_percent() == 100.0

    async def test_color(self):
        light = await connected_light()
        point = await light.get_color_xy()
        assert point == pytest.approx((0.4, 0.4), abs=1e-4)

    async def test_name_is_trimmed(self):
        light = await connected_light()
        assert await light.get_name() == "Desk lamp"

    async def test_device_info(self):
        light = await connected_light()
        info = await light.get_device_info()
        assert info["model"] == "LCA001"
        assert info["manufacturer"].startswith("Signify")

    async def test_full_state(self):
        light = await connected_light()
        state = await light.get_state(include_device_info=True)
        assert state.power is True
        assert state.brightness_percent == 100.0
        assert state.mired == 370
        assert state.firmware == "1.104.2"

    async def test_missing_optional_characteristics_read_as_none(self):
        reset_fake_client(WHITE_BULB_CHARS)
        light = await connected_light()
        assert await light.get_color_xy() is None
        assert await light.get_mired() is None
        assert await light.get_name() is None


class TestWrites:
    async def test_power(self):
        light = await connected_light()
        await light.set_power(False)
        assert FakeClient.instances[-1].values[protocol.CHAR_POWER] == b"\x00"

    async def test_toggle_flips_the_current_value(self):
        light = await connected_light()
        assert await light.toggle() is False
        assert await light.toggle() is True

    async def test_brightness_percent_is_scaled(self):
        light = await connected_light()
        await light.set_brightness_percent(50)
        raw = FakeClient.instances[-1].values[protocol.CHAR_BRIGHTNESS][0]
        assert protocol.raw_to_percent(raw) == pytest.approx(50, abs=0.5)

    async def test_color_is_clamped_to_the_gamut(self):
        light = await connected_light(gamut=GAMUT_C)
        await light.set_color_xy((0.99, 0.005))
        stored = protocol.decode_xy(FakeClient.instances[-1].values[protocol.CHAR_COLOR])
        assert stored != pytest.approx((0.99, 0.005))
        assert 0 <= stored[0] <= 1 and 0 <= stored[1] <= 1

    async def test_kelvin_becomes_mireds(self):
        light = await connected_light()
        await light.set_kelvin(2700)
        stored = protocol.decode_mired(FakeClient.instances[-1].values[protocol.CHAR_TEMPERATURE])
        assert stored == 370

    async def test_temperature_is_clamped_to_the_firmware_range(self):
        light = await connected_light()
        await light.set_kelvin(100000)
        stored = protocol.decode_mired(FakeClient.instances[-1].values[protocol.CHAR_TEMPERATURE])
        assert stored == 153

    async def test_writing_a_missing_characteristic_is_explained(self):
        reset_fake_client(WHITE_BULB_CHARS)
        light = await connected_light()
        with pytest.raises(UnsupportedFeatureError) as caught:
            await light.set_color_xy((0.4, 0.4))
        assert "colour" in str(caught.value)


class TestApply:
    async def test_power_on_precedes_the_colour_change(self):
        reset_fake_client({**COLOR_BULB_CHARS, protocol.CHAR_POWER: b"\x00"})
        light = await connected_light()
        await light.apply(power=True, color=parse_color("#ff0000"), brightness_percent=40)

        uuids = [uuid for uuid, _, _ in FakeClient.instances[-1].writes]
        assert uuids == [
            protocol.CHAR_POWER,
            protocol.CHAR_COLOR,
            protocol.CHAR_BRIGHTNESS,
        ]

    async def test_power_off_comes_last(self):
        light = await connected_light()
        await light.apply(power=False, brightness_percent=10)
        uuids = [uuid for uuid, _, _ in FakeClient.instances[-1].writes]
        assert uuids[-1] == protocol.CHAR_POWER

    async def test_brightness_implied_by_the_colour_is_used(self):
        light = await connected_light()
        await light.apply(color=parse_color("hsv:0,100,25"))
        raw = FakeClient.instances[-1].values[protocol.CHAR_BRIGHTNESS][0]
        assert protocol.raw_to_percent(raw) == pytest.approx(25, abs=1)

    async def test_explicit_brightness_wins_over_the_implied_one(self):
        light = await connected_light()
        await light.apply(color=parse_color("hsv:0,100,25"), brightness_percent=80)
        raw = FakeClient.instances[-1].values[protocol.CHAR_BRIGHTNESS][0]
        assert protocol.raw_to_percent(raw) == pytest.approx(80, abs=1)

    async def test_a_temperature_target_writes_the_temperature_characteristic(self):
        light = await connected_light()
        await light.apply(color=ColorTarget(mired=250))
        assert protocol.decode_mired(
            FakeClient.instances[-1].values[protocol.CHAR_TEMPERATURE]
        ) == 250


class TestFade:
    async def test_it_steps_and_lands_on_target(self, no_sleep):
        light = await connected_light()
        target = parse_color("#0000ff")

        await light.fade(color=target, brightness_percent=20, duration=0.5, steps_per_second=10)

        client = FakeClient.instances[-1]
        colour_writes = [w for w in client.writes if w[0] == protocol.CHAR_COLOR]
        assert len(colour_writes) > 1
        # Intermediate writes go out unacknowledged; the final one is confirmed.
        assert colour_writes[0][2] is False
        assert colour_writes[-1][2] is True

        assert target.xy is not None
        final = protocol.decode_xy(client.values[protocol.CHAR_COLOR])
        expected = light.gamut and target.xy
        assert final == pytest.approx(expected, abs=1e-3)
        assert protocol.raw_to_percent(
            client.values[protocol.CHAR_BRIGHTNESS][0]
        ) == pytest.approx(20, abs=1)

    async def test_zero_duration_applies_immediately(self):
        light = await connected_light()
        await light.fade(brightness_percent=30, duration=0)
        client = FakeClient.instances[-1]
        assert len([w for w in client.writes if w[0] == protocol.CHAR_BRIGHTNESS]) == 1


class TestPairingErrors:
    async def test_reads_report_a_missing_bond(self):
        light = await connected_light()
        FakeClient.auth_error = True
        with pytest.raises(NotPairedError) as caught:
            await light.get_power()
        assert "bazzitehue pair" in (caught.value.hint or "")

    async def test_writes_report_a_missing_bond(self):
        light = await connected_light()
        FakeClient.auth_error = True
        with pytest.raises(NotPairedError):
            await light.set_power(True)


class TestForEach:
    async def test_failures_do_not_stop_the_other_lamps(self, no_sleep):
        good = HueLight(ADDRESS)
        bad = HueLight("11:22:33:44:55:66", retries=0)

        async def action(light: HueLight):
            if light.address == bad.address:
                raise RuntimeError("nope")
            return await light.get_power()

        results = await light_module.for_each([good, bad], action)
        outcomes = {light.address: result for light, result in results}
        assert outcomes[good.address] is True
        assert isinstance(outcomes[bad.address], RuntimeError)
