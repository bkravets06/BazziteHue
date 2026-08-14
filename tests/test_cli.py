"""Tests for argument parsing and command output.

The BLE layer is replaced by the same fake client the light tests use, so these
exercise the real code paths without hardware.
"""

import json

import pytest
from test_light import FakeClient, reset_fake_client

from bazzitehue import cli, protocol
from bazzitehue import light as light_module
from bazzitehue.config import Config

ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("BAZZITEHUE_CONFIG", str(path))
    return path


@pytest.fixture
def fake_ble(monkeypatch):
    reset_fake_client()

    monkeypatch.setattr(light_module, "BleakClient", FakeClient)

    async def fake_resolve(address, timeout=8.0):
        return address

    monkeypatch.setattr(light_module, "resolve_device", fake_resolve)
    return FakeClient


@pytest.fixture
def saved_light(isolated_config):
    config = Config()
    config.add_light("desk", ADDRESS)
    config.save(isolated_config)
    return config


class TestParser:
    def test_flags_before_the_subcommand(self):
        args = cli.build_parser().parse_args(["--light", "desk", "on"])
        assert args.light == "desk"
        assert args.value is True

    def test_flags_after_the_subcommand(self):
        args = cli.build_parser().parse_args(["on", "--light", "desk"])
        assert args.light == "desk"

    def test_a_subcommand_does_not_clobber_an_earlier_flag(self):
        args = cli.build_parser().parse_args(["--timeout", "45", "status"])
        assert args.timeout == 45

    def test_scan_gets_a_shorter_default_timeout(self):
        parser = cli.build_parser()
        args = parser.parse_args(["scan"])
        assert args.timeout is None  # resolved per-command in main()

    def test_toggle_has_no_target_state(self):
        assert cli.build_parser().parse_args(["toggle"]).value is None

    def test_scene_subcommands(self):
        args = cli.build_parser().parse_args(["scene", "save", "cosy", "-c", "red", "-b", "20"])
        assert (args.scene_command, args.name, args.color, args.brightness) == (
            "save",
            "cosy",
            "red",
            20,
        )

    def test_a_missing_subcommand_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])


class TestRelativeValues:
    @pytest.mark.parametrize(
        "text,expected",
        [("50", (50, False)), ("+10", (10, True)), ("-10", (-10, True)), ("75%", (75, False))],
    )
    def test_parsing(self, text, expected):
        assert cli._relative(text) == expected


class TestConfigCommands:
    def test_add_then_list(self, capsys):
        assert cli.main(["add", "desk", ADDRESS]) == 0
        capsys.readouterr()

        assert cli.main(["lights"]) == 0
        assert "desk" in capsys.readouterr().out

    def test_add_rejects_a_bad_address(self, capsys):
        assert cli.main(["add", "desk", "not-an-address"]) == 2
        assert "Bluetooth address" in capsys.readouterr().err

    def test_use_sets_the_default(self, capsys, isolated_config):
        cli.main(["add", "desk", ADDRESS])
        cli.main(["add", "lamp", "11:22:33:44:55:66"])
        assert cli.main(["use", "lamp"]) == 0
        assert Config.load(isolated_config).default == "lamp"

    def test_rm(self, isolated_config):
        cli.main(["add", "desk", ADDRESS])
        assert cli.main(["rm", "desk"]) == 0
        assert Config.load(isolated_config).lights == {}

    def test_lights_json(self, capsys):
        cli.main(["add", "desk", ADDRESS])
        capsys.readouterr()
        assert cli.main(["lights", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["desk"]["address"] == ADDRESS

    def test_no_lamp_configured_explains_itself(self, capsys):
        assert cli.main(["status"]) == 2
        assert "scan" in capsys.readouterr().err

    def test_colors_lists_names(self, capsys):
        assert cli.main(["colors"]) == 0
        out = capsys.readouterr().out
        assert "red" in out and "daylight" in out


class TestSceneCommands:
    def test_save_validates_the_colour(self, capsys):
        assert cli.main(["scene", "save", "bad", "-c", "not-a-colour"]) == 2
        assert "cannot parse colour" in capsys.readouterr().err

    def test_save_then_list(self, capsys):
        assert cli.main(["scene", "save", "cosy", "-c", "2200k", "-b", "20"]) == 0
        capsys.readouterr()
        assert cli.main(["scene", "list"]) == 0
        assert "cosy" in capsys.readouterr().out

    def test_apply_an_unknown_scene(self, capsys):
        assert cli.main(["scene", "apply", "ghost"]) == 2
        assert "no scene" in capsys.readouterr().err

    def test_rm(self, capsys, isolated_config):
        cli.main(["scene", "save", "cosy", "-c", "red"])
        assert cli.main(["scene", "rm", "cosy"]) == 0
        assert Config.load(isolated_config).scenes == {}


class TestLightCommands:
    def test_status(self, capsys, fake_ble, saved_light):
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "desk" in out
        assert "on" in out

    def test_status_json(self, capsys, fake_ble, saved_light):
        assert cli.main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["address"] == ADDRESS
        assert payload[0]["power"] is True
        assert payload[0]["brightness_percent"] == 100.0

    def test_on_and_off(self, fake_ble, saved_light):
        assert cli.main(["off"]) == 0
        assert FakeClient.instances[-1].values[protocol.CHAR_POWER] == b"\x00"
        assert cli.main(["on"]) == 0
        assert FakeClient.instances[-1].values[protocol.CHAR_POWER] == b"\x01"

    def test_color_by_name(self, capsys, fake_ble, saved_light):
        assert cli.main(["color", "red"]) == 0
        stored = protocol.decode_xy(FakeClient.instances[-1].values[protocol.CHAR_COLOR])
        assert stored[0] > stored[1]  # red sits at high x, low y

    def test_color_with_brightness(self, fake_ble, saved_light):
        assert cli.main(["color", "#00ff00", "-b", "40"]) == 0
        raw = FakeClient.instances[-1].values[protocol.CHAR_BRIGHTNESS][0]
        assert protocol.raw_to_percent(raw) == pytest.approx(40, abs=1)

    def test_temp_accepts_a_bare_number(self, fake_ble, saved_light):
        assert cli.main(["temp", "3000"]) == 0
        stored = protocol.decode_mired(FakeClient.instances[-1].values[protocol.CHAR_TEMPERATURE])
        assert stored == 333

    def test_temp_accepts_a_name(self, fake_ble, saved_light):
        assert cli.main(["temp", "warm"]) == 0
        stored = protocol.decode_mired(FakeClient.instances[-1].values[protocol.CHAR_TEMPERATURE])
        assert stored == 370

    def test_relative_brightness(self, fake_ble, saved_light):
        cli.main(["brightness", "50"])
        assert cli.main(["brightness", "-20"]) == 0
        raw = FakeClient.instances[-1].values[protocol.CHAR_BRIGHTNESS][0]
        assert protocol.raw_to_percent(raw) == pytest.approx(30, abs=1)

    def test_set_combines_changes(self, fake_ble, saved_light):
        assert cli.main(["set", "--on", "-c", "#0000ff", "-b", "25"]) == 0
        client = FakeClient.instances[-1]
        assert client.values[protocol.CHAR_POWER] == b"\x01"
        assert protocol.raw_to_percent(
            client.values[protocol.CHAR_BRIGHTNESS][0]
        ) == pytest.approx(25, abs=1)

    def test_set_needs_something_to_do(self, capsys, fake_ble, saved_light):
        assert cli.main(["set"]) == 2
        assert "nothing to do" in capsys.readouterr().err

    def test_addressing_a_lamp_directly(self, fake_ble, isolated_config):
        assert cli.main(["-l", "11:22:33:44:55:66", "on"]) == 0
        assert FakeClient.instances[-1].target == "11:22:33:44:55:66"

    def test_all_addresses_every_saved_lamp(self, fake_ble, isolated_config):
        cli.main(["add", "desk", ADDRESS])
        cli.main(["add", "lamp", "11:22:33:44:55:66"])
        assert cli.main(["-l", "all", "on"]) == 0
        targets = {client.target for client in FakeClient.instances}
        assert targets == {ADDRESS, "11:22:33:44:55:66"}

    def test_a_failing_lamp_reports_and_exits_nonzero(self, capsys, fake_ble, saved_light):
        FakeClient.auth_error = True
        assert cli.main(["on"]) == 1
        assert "refused" in capsys.readouterr().err

    def test_info_shows_capabilities(self, capsys, fake_ble, saved_light):
        assert cli.main(["info"]) == 0
        out = capsys.readouterr().out
        assert "LCA001" in out
        assert "colour        yes" in out


class TestFormatting:
    def test_state_line_without_colour_support(self):
        state = protocol.LightState(address=ADDRESS, power=False, brightness=1)
        line = cli.describe_state(state, "desk")
        assert "desk" in line and "off" in line

    def test_state_dict_includes_hex_and_kelvin(self):
        state = protocol.LightState(
            address=ADDRESS, power=True, brightness=254, xy=(0.4, 0.4), mired=370
        )
        payload = cli.state_to_dict(state, "desk")
        assert payload["hex"].startswith("#")
        assert payload["kelvin"] == 2703
