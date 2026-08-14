"""Tests for the desktop app, run headless against Qt's offscreen platform."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from test_light import FakeClient, reset_fake_client  # noqa: E402

from bazzitehue import light as light_module  # noqa: E402
from bazzitehue import protocol  # noqa: E402
from bazzitehue.color import ColorTarget, kelvin_to_mired  # noqa: E402
from bazzitehue.config import Config, Scene  # noqa: E402
from bazzitehue.gui import worker as worker_module  # noqa: E402
from bazzitehue.gui.dialogs import ScanDialog, suggest_alias  # noqa: E402
from bazzitehue.gui.icons import bulb_pixmap  # noqa: E402
from bazzitehue.gui.tray import Tray  # noqa: E402
from bazzitehue.gui.widgets import (  # noqa: E402
    ColorPreview,
    ColorWheel,
    wheel_color_at,
    wheel_point,
)
from bazzitehue.gui.window import ALL_LAMPS, ControlWindow  # noqa: E402
from bazzitehue.gui.worker import BleWorker  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class FakeWorker(QObject):
    """Same signals and command surface as BleWorker; records what it is told."""

    lamp_state = Signal(str, object)
    lamp_status = Signal(str, str)
    lamp_error = Signal(str, str, str)
    scan_finished = Signal(object)
    scan_failed = Signal(str, str)
    pair_finished = Signal(str, bool, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def set_power(self, aliases, on):
        self.calls.append(("power", list(aliases), on))

    def set_brightness(self, aliases, percent):
        self.calls.append(("brightness", list(aliases), percent))

    def set_color(self, aliases, color):
        self.calls.append(("color", list(aliases), color))

    def apply_scene(self, aliases, color, brightness, power):
        self.calls.append(("scene", list(aliases), color, brightness, power))

    def refresh(self, aliases):
        self.calls.append(("refresh", list(aliases)))

    def scan(self, timeout=8.0, all_devices=False):
        self.calls.append(("scan", timeout, all_devices))

    def pair(self, address):
        self.calls.append(("pair", address))

    def of(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]

    def last(self, kind: str) -> tuple | None:
        found = self.of(kind)
        return found[-1] if found else None


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("BAZZITEHUE_CONFIG", str(tmp_path / "config.json"))
    cfg = Config()
    cfg.add_light("desk", ADDRESS)
    cfg.add_light("sofa", OTHER)
    cfg.add_scene("Cosy", Scene(color="2200k", brightness=25))
    cfg.save()
    return cfg


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def window(qapp, config, worker):
    win = ControlWindow(config, worker)
    yield win
    win.close()


# --------------------------------------------------------------------------- #
# Colour wheel geometry
# --------------------------------------------------------------------------- #


class TestWheelGeometry:
    RECT = QRectF(0, 0, 200, 200)

    @pytest.mark.parametrize(
        "hue,expected",
        [
            (0, (100, 0)),  # top
            (90, (0, 100)),  # left  (Qt conical gradients run counter-clockwise)
            (180, (100, 200)),  # bottom
            (270, (200, 100)),  # right
        ],
    )
    def test_hue_positions_match_the_painted_gradient(self, hue, expected):
        point = wheel_point(self.RECT, hue, 100)
        assert point.x() == pytest.approx(expected[0], abs=0.001)
        assert point.y() == pytest.approx(expected[1], abs=0.001)

    def test_point_and_colour_round_trip(self):
        for hue in (0, 45, 137, 200, 359):
            for saturation in (10, 55, 100):
                point = wheel_point(self.RECT, hue, saturation)
                back_hue, back_saturation = wheel_color_at(self.RECT, point)
                assert back_hue == pytest.approx(hue, abs=0.01)
                assert back_saturation == pytest.approx(saturation, abs=0.01)

    def test_centre_is_unsaturated(self):
        _, saturation = wheel_color_at(self.RECT, QPointF(100, 100))
        assert saturation == pytest.approx(0, abs=0.01)

    def test_outside_the_circle_clamps_to_full_saturation(self):
        _, saturation = wheel_color_at(self.RECT, QPointF(400, 100))
        assert saturation == 100

    def test_degenerate_rect_is_survivable(self):
        assert wheel_color_at(QRectF(0, 0, 0, 0), QPointF(0, 0)) == (0.0, 0.0)


class TestWidgets:
    def test_wheel_reports_what_was_set(self, qapp):
        wheel = ColorWheel()
        wheel.set_hsv(200, 50)
        assert wheel.hue() == 200
        assert wheel.saturation() == 50

    def test_wheel_wraps_and_clamps(self, qapp):
        wheel = ColorWheel()
        wheel.set_hsv(400, 150)
        assert wheel.hue() == 40
        assert wheel.saturation() == 100

    def test_wheel_renders(self, qapp):
        wheel = ColorWheel()
        wheel.resize(200, 200)
        assert not wheel.grab().isNull()

    def test_preview_renders_with_a_caption(self, qapp):
        preview = ColorPreview()
        preview.resize(120, 60)
        preview.set_color(QColor(255, 200, 120), "2700 K")
        assert not preview.grab().isNull()

    @pytest.mark.parametrize("size", [16, 22, 64])
    def test_bulb_icon_sizes(self, qapp, size):
        assert bulb_pixmap(QColor("#ff8800"), size=size).size().width() == size

    def test_bulb_icon_off_state_differs(self, qapp):
        on = bulb_pixmap(QColor("#ff8800"), size=32, on=True).toImage()
        off = bulb_pixmap(QColor("#ff8800"), size=32, on=False).toImage()
        assert on != off


# --------------------------------------------------------------------------- #
# The control window
# --------------------------------------------------------------------------- #


class TestControlWindow:
    def test_lamp_picker_lists_saved_lamps(self, window):
        items = [window.lamp_box.itemText(i) for i in range(window.lamp_box.count())]
        assert items == [ALL_LAMPS, "desk", "sofa"]

    def test_all_lamps_targets_everything(self, window):
        window.lamp_box.setCurrentText(ALL_LAMPS)
        assert window.targets() == ["desk", "sofa"]

    def test_selecting_one_lamp_narrows_the_target(self, window):
        window.lamp_box.setCurrentText("sofa")
        assert window.targets() == ["sofa"]

    def test_power_button_sends_power(self, window, worker):
        window.power_button.setChecked(True)
        assert worker.last("power")[2] is True
        window.power_button.setChecked(False)
        assert worker.last("power")[2] is False

    def test_power_button_label_follows_state(self, window):
        window.power_button.setChecked(True)
        assert window.power_button.text() == "Turn off"
        window.power_button.setChecked(False)
        assert window.power_button.text() == "Turn on"

    def test_brightness_slider_sends_percent(self, window, worker):
        window.brightness.setValue(42)
        assert worker.last("brightness")[2] == 42.0
        assert window.brightness_label.text() == "42%"

    def test_hex_entry_sends_a_colour(self, window, worker):
        window.apply_hex("#00ff00")
        kind, targets, color = worker.last("color")
        assert color.xy is not None
        assert targets == ["desk", "sofa"]

    def test_bad_hex_is_reported_not_sent(self, window, worker):
        window.apply_hex("nonsense")
        assert worker.of("color") == []
        assert "hex" in window.status.text().lower()

    def test_wheel_picking_sends_a_colour(self, window, worker):
        window.wheel.set_hsv(120, 100)
        window._wheel_picked(120, 100)
        target = worker.last("color")[2]
        assert isinstance(target, ColorTarget)
        assert target.xy is not None

    def test_temperature_slider_sends_mireds(self, window, worker):
        window.temperature.setValue(4000)
        target = worker.last("color")[2]
        assert target.mired == kelvin_to_mired(4000)
        assert target.xy is None

    def test_white_preset_switches_mode_and_sends(self, window, worker):
        window.apply_kelvin(2700)
        assert window.white_mode.isChecked()
        assert window.white_page.isVisibleTo(window)
        assert worker.last("color")[2].mired == kelvin_to_mired(2700)

    def test_state_from_the_lamp_updates_controls_without_echoing_back(self, window, worker):
        worker.calls.clear()
        state = protocol.LightState(
            address=ADDRESS,
            power=True,
            brightness=protocol.percent_to_raw(35),
            xy=(0.2, 0.1),
        )
        worker.lamp_state.emit("desk", state)

        assert window.power_button.isChecked()
        assert window.brightness.value() == pytest.approx(35, abs=1)
        # Crucially, reflecting the lamp's state must not send it straight back.
        assert worker.calls == []

    def test_state_for_another_lamp_is_ignored(self, window, worker):
        window.lamp_box.setCurrentText("desk")
        state = protocol.LightState(address=OTHER, power=True, brightness=254)
        window.brightness.setValue(10)
        worker.lamp_state.emit("sofa", state)
        assert window.brightness.value() == 10

    def test_status_is_shown(self, window, worker):
        worker.lamp_status.emit("desk", "connecting")
        assert window.connection_label.text() == "connecting"

    def test_errors_are_shown(self, window, worker):
        worker.lamp_error.emit("desk", "boom", "")
        assert "boom" in window.status.text()

    def test_appearance_is_forwarded_for_the_tray(self, window, worker):
        seen = []
        window.appearance_changed.connect(lambda *args: seen.append(args))
        worker.lamp_state.emit(
            "desk", protocol.LightState(address=ADDRESS, power=True, xy=(0.5, 0.4))
        )
        assert seen and seen[-1][0] == "desk"
        assert isinstance(seen[-1][1], QColor)
        assert seen[-1][2] is True


class TestScenes:
    def test_apply_sends_the_saved_look(self, window, worker):
        window.scene_box.setCurrentText("Cosy")
        window.apply_scene()
        kind, targets, color, brightness, power = worker.last("scene")
        assert color.mired == kelvin_to_mired(2200)
        assert brightness == 25

    def test_apply_without_a_selection_says_so(self, window, worker):
        window.config.scenes.clear()
        window.reload_lamps()
        window.apply_scene()
        assert worker.of("scene") == []
        assert "scene" in window.status.text().lower()

    def test_saving_captures_the_current_colour(self, window, monkeypatch, config):
        monkeypatch.setattr(
            "bazzitehue.gui.window.QInputDialog.getText", lambda *a, **k: ("Evening", True)
        )
        window._set_mode(white=True, quiet=True)
        window.temperature.setValue(2400)
        window.brightness.setValue(30)
        window.save_scene()

        saved = Config.load().scenes["Evening"]
        assert saved.color == "2400k"
        assert saved.brightness == 30

    def test_saving_a_colour_scene_stores_hex(self, window, monkeypatch):
        monkeypatch.setattr(
            "bazzitehue.gui.window.QInputDialog.getText", lambda *a, **k: ("Party", True)
        )
        window._set_mode(white=False, quiet=True)
        window.wheel.set_hsv(0, 100)
        window.save_scene()
        assert Config.load().scenes["Party"].color.startswith("#")

    def test_cancelling_the_name_saves_nothing(self, window, monkeypatch):
        monkeypatch.setattr(
            "bazzitehue.gui.window.QInputDialog.getText", lambda *a, **k: ("", False)
        )
        window.save_scene()
        assert "Evening" not in window.config.scenes

    def test_delete(self, window):
        window.scene_box.setCurrentText("Cosy")
        window.delete_scene()
        assert "Cosy" not in Config.load().scenes


class TestEmptyConfig:
    def test_controls_are_disabled_with_no_lamps(self, qapp, tmp_path, monkeypatch, worker):
        monkeypatch.setenv("BAZZITEHUE_CONFIG", str(tmp_path / "empty.json"))
        window = ControlWindow(Config(), worker)
        assert not window.power_button.isEnabled()
        assert "Find bulbs" in window.status.text()
        window.close()


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #


class TestTray:
    def test_menu_lists_lamps_and_actions(self, qapp, config, worker):
        tray = Tray(config, worker)
        labels = [action.text() for action in tray.menu.actions()]
        assert "Controls…" in labels
        assert "desk" in labels and "sofa" in labels
        assert "All on" in labels and "All off" in labels
        assert "Quit BazziteHue" in labels

    def test_toggling_a_lamp_action_sends_power(self, qapp, config, worker):
        tray = Tray(config, worker)
        action = next(a for a in tray.menu.actions() if a.text() == "desk")
        action.setChecked(True)
        assert worker.last("power") == ("power", ["desk"], True)

    def test_empty_config_shows_a_placeholder(self, qapp, worker, tmp_path, monkeypatch):
        monkeypatch.setenv("BAZZITEHUE_CONFIG", str(tmp_path / "empty.json"))
        tray = Tray(Config(), worker)
        labels = [action.text() for action in tray.menu.actions()]
        assert "No bulbs saved" in labels

    def test_appearance_updates_the_tooltip(self, qapp, config, worker):
        tray = Tray(config, worker)
        tray.update_appearance("desk", QColor("#ff8800"), True)
        assert "desk on" in tray.icon.toolTip()
        tray.update_appearance("desk", QColor("#ff8800"), False)
        assert "all off" in tray.icon.toolTip()

    def test_lamp_check_state_follows_the_lamp(self, qapp, config, worker):
        tray = Tray(config, worker)
        worker.calls.clear()
        tray.update_appearance("desk", QColor("#ff8800"), True)
        action = next(a for a in tray.menu.actions() if a.text() == "desk")
        assert action.isChecked()
        # Reflecting state must not command the lamp back on.
        assert worker.of("power") == []


# --------------------------------------------------------------------------- #
# Scan dialog
# --------------------------------------------------------------------------- #


class TestSuggestAlias:
    def test_slugifies_the_bulb_name(self):
        assert suggest_alias("Hue color lamp", ADDRESS, set()) == "color-lamp"

    def test_falls_back_to_the_address(self):
        assert suggest_alias("", "AA:BB:CC:DD:EE:FF", set()) == "lamp-eeff"

    def test_avoids_collisions(self):
        assert suggest_alias("Hue go", ADDRESS, {"go"}) == "go-2"
        assert suggest_alias("Hue go", ADDRESS, {"go", "go-2"}) == "go-3"


class TestSharing:
    """Bulbs take one Bluetooth connection at a time, so letting go matters."""

    def test_default_is_to_hold_the_connection(self, config):
        assert config.share_mode is False

    def test_share_mode_round_trips(self, config):
        config.share_mode = True
        config.save()
        assert Config.load().share_mode is True

    def test_worker_idle_timeout_follows_the_setting(self, qapp, config):
        worker = BleWorker(config)
        assert worker.idle_timeout == worker_module.IDLE_DISCONNECT
        config.share_mode = True
        assert worker.idle_timeout == worker_module.SHARED_IDLE_DISCONNECT

    def test_tray_shows_the_current_setting(self, qapp, config, worker):
        config.share_mode = True
        tray = Tray(config, worker)
        action = next(a for a in tray.menu.actions() if "Share" in a.text())
        assert action.isChecked()

    def test_tray_toggle_is_reported(self, qapp, config, worker):
        tray = Tray(config, worker)
        seen = []
        tray.share_toggled.connect(seen.append)
        action = next(a for a in tray.menu.actions() if "Share" in a.text())
        action.setChecked(True)
        assert seen == [True]

    def test_sharing_releases_the_bulb_quickly(self, qapp, config, monkeypatch, pump):
        reset_fake_client()
        monkeypatch.setattr(light_module, "BleakClient", FakeClient)
        monkeypatch.setattr(worker_module, "SHARED_IDLE_DISCONNECT", 0.2)

        async def fake_resolve(address, timeout=8.0):
            return address

        monkeypatch.setattr(light_module, "resolve_device", fake_resolve)

        config.share_mode = True
        worker = BleWorker(config)
        worker.start()
        try:
            statuses = []
            worker.lamp_status.connect(lambda alias, status: statuses.append(status))
            worker.set_power(["desk"], True)
            pump(1.5)
            assert "connected" in statuses
            assert statuses[-1] == "disconnected"
        finally:
            worker.stop()


class TestScanDialog:
    def test_lists_devices_and_marks_saved_ones(self, qapp, config, worker):
        dialog = ScanDialog(config, worker)
        worker.scan_finished.emit([(ADDRESS, "Hue color lamp"), ("99:88:77:66:55:44", "Hue go")])

        assert dialog.results.count() == 2
        saved_row = dialog.results.item(0)
        assert "already saved" in saved_row.text()
        assert not (saved_row.flags() & Qt.ItemIsEnabled)
        dialog.close()

    def test_adding_saves_and_pairs(self, qapp, config, worker):
        dialog = ScanDialog(config, worker)
        worker.scan_finished.emit([("99:88:77:66:55:44", "Hue go")])
        dialog.results.setCurrentRow(0)
        dialog._add_selected()

        assert "go" in Config.load().lights
        assert worker.last("pair") == ("pair", "99:88:77:66:55:44")
        dialog.close()

    def test_pairing_failure_still_keeps_the_lamp(self, qapp, config, worker):
        dialog = ScanDialog(config, worker)
        worker.scan_finished.emit([("99:88:77:66:55:44", "Hue go")])
        dialog.results.setCurrentRow(0)
        dialog._add_selected()
        worker.pair_finished.emit("99:88:77:66:55:44", False, "not authorized")

        assert "go" in Config.load().lights
        assert "pairing failed" in dialog.status.text().lower()
        dialog.close()

    def test_showing_every_device_is_passed_through(self, qapp, config, worker):
        dialog = ScanDialog(config, worker)
        assert worker.last("scan")[2] is False
        dialog.show_everything.setChecked(True)
        assert worker.last("scan")[2] is True
        dialog.close()

    def test_adding_by_address_saves_and_pairs(self, qapp, config, worker, monkeypatch):
        dialog = ScanDialog(config, worker)
        monkeypatch.setattr(
            "bazzitehue.gui.dialogs.QInputDialog.getText",
            lambda *a, **k: ("99:88:77:66:55:44", True),
        )
        dialog.add_by_address()

        assert "99:88:77:66:55:44" in {
            light.address for light in Config.load().lights.values()
        }
        assert worker.last("pair") == ("pair", "99:88:77:66:55:44")
        dialog.close()

    def test_adding_a_bad_address_is_refused(self, qapp, config, worker, monkeypatch):
        dialog = ScanDialog(config, worker)
        monkeypatch.setattr(
            "bazzitehue.gui.dialogs.QInputDialog.getText", lambda *a, **k: ("nope", True)
        )
        warned = []
        monkeypatch.setattr(
            "bazzitehue.gui.dialogs.QMessageBox.warning", lambda *a, **k: warned.append(a)
        )
        dialog.add_by_address()

        assert warned
        assert worker.of("pair") == []
        dialog.close()

    def test_adding_an_address_already_saved_is_refused(self, qapp, config, worker, monkeypatch):
        dialog = ScanDialog(config, worker)
        monkeypatch.setattr(
            "bazzitehue.gui.dialogs.QInputDialog.getText", lambda *a, **k: (ADDRESS, True)
        )
        told = []
        monkeypatch.setattr(
            "bazzitehue.gui.dialogs.QMessageBox.information", lambda *a, **k: told.append(a)
        )
        dialog.add_by_address()

        assert told
        assert worker.of("pair") == []
        dialog.close()

    def test_an_empty_scan_explains_itself(self, qapp, config, worker):
        dialog = ScanDialog(config, worker)
        worker.scan_finished.emit([])
        assert "No bulbs found" in dialog.status.text()
        dialog.close()

    def test_a_broken_adapter_is_explained(self, qapp, config, worker):
        dialog = ScanDialog(config, worker)
        worker.scan_failed.emit("no adapter", "Switch Bluetooth on")
        assert "Switch Bluetooth on" in dialog.status.text()
        dialog.close()


# --------------------------------------------------------------------------- #
# The real worker, driven against the fake BLE backend
# --------------------------------------------------------------------------- #


class TestBleWorker:
    @pytest.fixture
    def live(self, qapp, config, monkeypatch):
        reset_fake_client()
        monkeypatch.setattr(light_module, "BleakClient", FakeClient)

        async def fake_resolve(address, timeout=8.0):
            return address

        monkeypatch.setattr(light_module, "resolve_device", fake_resolve)

        worker = BleWorker(config)
        worker.start()
        yield worker
        worker.stop()

    def test_power_reaches_the_bulb(self, live, pump):
        live.set_power(["desk"], False)
        pump(1.0)
        assert FakeClient.stores[ADDRESS][protocol.CHAR_POWER] == b"\x00"

    def test_colour_and_brightness_reach_the_bulb(self, live, pump):
        live.set_color(["desk"], ColorTarget(xy=(0.2, 0.1)))
        live.set_brightness(["desk"], 40)
        pump(1.0)

        store = FakeClient.stores[ADDRESS]
        assert protocol.decode_xy(store[protocol.CHAR_COLOR]) == pytest.approx((0.2, 0.1), abs=1e-3)
        assert protocol.raw_to_percent(store[protocol.CHAR_BRIGHTNESS][0]) == pytest.approx(
            40, abs=1
        )

    def test_rapid_updates_are_coalesced(self, live, pump):
        # A slider drag: many values, but the bulb should not see all of them.
        for value in range(0, 100, 2):
            live.set_brightness(["desk"], float(value))
        pump(1.2)

        writes = [
            write
            for client in FakeClient.instances
            for write in client.writes
            if write[0] == protocol.CHAR_BRIGHTNESS
        ]
        assert len(writes) < 25
        assert protocol.raw_to_percent(
            FakeClient.stores[ADDRESS][protocol.CHAR_BRIGHTNESS][0]
        ) == pytest.approx(98, abs=2)

    def test_state_is_reported_back_to_the_gui(self, live, pump):
        seen = []
        live.lamp_state.connect(lambda alias, state: seen.append((alias, state)))
        live.refresh(["desk"])
        pump(1.0)

        assert seen
        alias, state = seen[-1]
        assert alias == "desk"
        assert state.power is True

    def test_connection_status_is_reported(self, live, pump):
        seen = []
        live.lamp_status.connect(lambda alias, status: seen.append(status))
        live.refresh(["desk"])
        pump(1.0)
        assert "connecting" in seen and "connected" in seen

    def test_a_refusing_bulb_reports_an_error(self, live, pump):
        errors = []
        live.lamp_error.connect(lambda alias, message, hint: errors.append(message))
        FakeClient.auth_error = True
        live.set_power(["desk"], True)
        pump(1.5)
        assert errors and "refused" in errors[0]

    def test_several_lamps_at_once(self, live, pump):
        live.set_power(["desk", "sofa"], True)
        pump(1.2)
        assert FakeClient.stores[ADDRESS][protocol.CHAR_POWER] == b"\x01"
        assert FakeClient.stores[OTHER][protocol.CHAR_POWER] == b"\x01"

    def test_unknown_alias_is_ignored(self, live, pump):
        live.set_power(["ghost"], True)
        pump(0.5)  # must not raise

    def test_scan_reports_devices(self, live, pump, monkeypatch):
        class FakeDevice:
            def __init__(self, address, name):
                self.address = address
                self.name = name

        async def fake_discover(timeout=8.0, all_devices=False):
            return [FakeDevice("99:88:77:66:55:44", "Hue go")]

        monkeypatch.setattr(worker_module, "discover", fake_discover)

        found = []
        live.scan_finished.connect(found.append)
        live.scan(0.1)
        pump(1.0)
        assert found == [[("99:88:77:66:55:44", "Hue go")]]

    def test_scan_failure_carries_a_hint(self, live, pump, monkeypatch):
        async def broken(timeout=8.0, all_devices=False):
            raise FileNotFoundError("No such file or directory")

        monkeypatch.setattr(worker_module, "discover", broken)

        failures = []
        live.scan_failed.connect(lambda message, hint: failures.append(hint))
        live.scan(0.1)
        pump(1.0)
        assert failures and "Bluetooth" in failures[0]

    def test_pairing_reports_success(self, live, pump):
        results = []
        live.pair_finished.connect(lambda address, ok, message: results.append(ok))
        live.pair(ADDRESS)
        pump(1.0)
        assert results == [True]

    def test_stop_is_idempotent(self, live):
        live.stop()
        live.stop()
