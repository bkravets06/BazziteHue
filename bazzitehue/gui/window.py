"""The main control window."""

from __future__ import annotations

import colorsys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..color import (
    ColorError,
    ColorTarget,
    kelvin_to_mired,
    mired_to_kelvin,
    parse_color,
    parse_hex,
    xy_to_rgb,
)
from ..config import Config, Scene
from ..protocol import LightState
from .dialogs import ScanDialog
from .icons import app_icon, swatch_pixmap
from .widgets import ColorPreview, ColorWheel, TemperatureBar
from .worker import BleWorker

ALL_LAMPS = "— all lamps —"

PRESETS = [
    ("Red", "#ff0000"),
    ("Orange", "#ff7f00"),
    ("Yellow", "#ffd700"),
    ("Green", "#00ff00"),
    ("Cyan", "#00ffff"),
    ("Blue", "#0000ff"),
    ("Purple", "#8a2be2"),
    ("Pink", "#ff69b4"),
]

WHITE_PRESETS = [("Candle", 2000), ("Warm", 2700), ("Neutral", 4000), ("Daylight", 6500)]

MIN_KELVIN = 2000
MAX_KELVIN = 6500


class ControlWindow(QWidget):
    """Everything you can do to a bulb, in one panel."""

    #: emitted when the lamp list changes so the tray menu can rebuild
    lamps_changed = Signal()
    #: alias, colour, powered -- lets the tray icon follow the lamp
    appearance_changed = Signal(str, object, bool)

    def __init__(self, config: Config, worker: BleWorker) -> None:
        super().__init__()
        self.config = config
        self.worker = worker
        self._updating = False
        self._pair_prompted: set[str] = set()

        self.setWindowTitle("BazziteHue")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(420)

        self._build()
        self._connect_worker()
        self.reload_lamps()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)

        layout.addLayout(self._build_header())
        layout.addWidget(self._build_power_row())
        layout.addWidget(self._divider())
        layout.addLayout(self._build_brightness())
        layout.addWidget(self._build_tabs(), 1)
        layout.addWidget(self._divider())
        layout.addLayout(self._build_scenes())

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.lamp_box = QComboBox()
        self.lamp_box.currentIndexChanged.connect(self._lamp_changed)
        row.addWidget(self.lamp_box, 1)

        self.find_button = QPushButton("Find bulbs…")
        self.find_button.clicked.connect(self.open_scan_dialog)
        row.addWidget(self.find_button)
        return row

    def _build_power_row(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)

        self.power_button = QPushButton("Turn on")
        self.power_button.setCheckable(True)
        self.power_button.setMinimumHeight(44)
        self.power_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.power_button.toggled.connect(self._power_toggled)
        row.addWidget(self.power_button, 1)

        self.connection_label = QLabel("")
        self.connection_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.connection_label)
        return holder

    def _build_brightness(self) -> QVBoxLayout:
        column = QVBoxLayout()
        header = QHBoxLayout()
        header.addWidget(QLabel("Brightness"))
        header.addStretch(1)
        self.brightness_label = QLabel("—")
        header.addWidget(self.brightness_label)
        column.addLayout(header)

        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(60)
        self.brightness.setPageStep(10)
        self.brightness.valueChanged.connect(self._brightness_changed)
        column.addWidget(self.brightness)
        return column

    def _build_tabs(self) -> QWidget:
        # A QTabWidget would work, but two toggle buttons read better here and
        # keep the whole window usable at a glance.
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        switcher = QHBoxLayout()
        self.colour_mode = QPushButton("Colour")
        self.white_mode = QPushButton("White")
        for button in (self.colour_mode, self.white_mode):
            button.setCheckable(True)
            button.setMinimumHeight(30)
            switcher.addWidget(button)
        self.colour_mode.setChecked(True)
        self.colour_mode.clicked.connect(lambda: self._set_mode(white=False))
        self.white_mode.clicked.connect(lambda: self._set_mode(white=True))
        outer.addLayout(switcher)

        self.colour_page = self._build_colour_page()
        self.white_page = self._build_white_page()
        self.white_page.hide()
        outer.addWidget(self.colour_page, 1)
        outer.addWidget(self.white_page, 1)
        return holder

    def _build_colour_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)

        self.wheel = ColorWheel()
        self.wheel.color_picked.connect(self._wheel_picked)
        column.addWidget(self.wheel, 1)

        swatches = QGridLayout()
        swatches.setSpacing(6)
        for index, (name, hex_value) in enumerate(PRESETS):
            button = QToolButton()
            button.setIcon(swatch_pixmap(QColor(hex_value), 28))
            button.setToolTip(name)
            button.setAutoRaise(True)
            button.clicked.connect(lambda _=False, value=hex_value: self.apply_hex(value))
            swatches.addWidget(button, 0, index)
        column.addLayout(swatches)

        entry = QHBoxLayout()
        entry.addWidget(QLabel("Hex"))
        self.hex_entry = QLineEdit()
        self.hex_entry.setPlaceholderText("#ff8800")
        self.hex_entry.setMaxLength(7)
        self.hex_entry.returnPressed.connect(lambda: self.apply_hex(self.hex_entry.text()))
        entry.addWidget(self.hex_entry, 1)
        apply_hex = QPushButton("Apply")
        apply_hex.clicked.connect(lambda: self.apply_hex(self.hex_entry.text()))
        entry.addWidget(apply_hex)
        column.addLayout(entry)
        return page

    def _build_white_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)

        column.addWidget(TemperatureBar(MIN_KELVIN, MAX_KELVIN))
        self.temperature = QSlider(Qt.Horizontal)
        self.temperature.setRange(MIN_KELVIN, MAX_KELVIN)
        self.temperature.setSingleStep(50)
        self.temperature.setPageStep(250)
        self.temperature.setValue(2700)
        self.temperature.valueChanged.connect(self._temperature_changed)
        column.addWidget(self.temperature)

        self.white_preview = ColorPreview()
        column.addWidget(self.white_preview, 1)

        presets = QHBoxLayout()
        for name, kelvin in WHITE_PRESETS:
            button = QPushButton(name)
            button.clicked.connect(lambda _=False, value=kelvin: self.apply_kelvin(value))
            presets.addWidget(button)
        column.addLayout(presets)
        self._update_white_preview(self.temperature.value())
        return page

    def _build_scenes(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Scene"))
        self.scene_box = QComboBox()
        row.addWidget(self.scene_box, 1)

        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(self.apply_scene)
        row.addWidget(apply_button)

        save_button = QPushButton("Save…")
        save_button.clicked.connect(self.save_scene)
        row.addWidget(save_button)

        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self.delete_scene)
        row.addWidget(delete_button)
        return row

    # ------------------------------------------------------------------ #
    # Worker wiring
    # ------------------------------------------------------------------ #

    def _connect_worker(self) -> None:
        self.worker.lamp_state.connect(self._state_arrived)
        self.worker.lamp_status.connect(self._status_arrived)
        self.worker.lamp_error.connect(self._error_arrived)

    def _state_arrived(self, alias: str, state: LightState) -> None:
        if alias not in self.targets():
            return

        self._updating = True
        try:
            if state.power is not None:
                self.power_button.setChecked(state.power)
                self.power_button.setText("Turn off" if state.power else "Turn on")
            if state.brightness_percent is not None:
                self.brightness.setValue(int(round(state.brightness_percent)))
                self.brightness_label.setText(f"{state.brightness_percent:.0f}%")
            if state.mired:
                kelvin = mired_to_kelvin(state.mired)
                self.temperature.setValue(max(MIN_KELVIN, min(MAX_KELVIN, kelvin)))
                self._update_white_preview(kelvin)
            if state.xy is not None:
                red, green, blue = xy_to_rgb(state.xy, 1.0)
                hue, saturation, _ = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
                self.wheel.set_hsv(hue * 360, saturation * 100)
                self.hex_entry.setPlaceholderText(f"#{red:02x}{green:02x}{blue:02x}")
        finally:
            self._updating = False

        self._announce_appearance(alias, state)

    def _announce_appearance(self, alias: str, state: LightState) -> None:
        color = None
        if state.xy is not None:
            color = QColor(*xy_to_rgb(state.xy, 1.0))
        elif state.mired:
            from ..color import kelvin_to_xy

            color = QColor(*xy_to_rgb(kelvin_to_xy(mired_to_kelvin(state.mired)), 1.0))
        self.appearance_changed.emit(alias, color, bool(state.power))

    def _status_arrived(self, alias: str, status: str) -> None:
        if alias in self.targets():
            self.connection_label.setText(status)
            if status == "connected":
                self.status.setText("")

    def _error_arrived(self, alias: str, message: str, hint: str) -> None:
        self.status.setText(f"{alias}: {message}")
        self.connection_label.setText("not connected")

        # A lamp that has never been bonded is the single most common failure,
        # and it is fixable from here, so offer the fix instead of the error.
        if "pair" in hint.lower() and alias not in self._pair_prompted:
            self._pair_prompted.add(alias)
            self._offer_pairing(alias, message)

    def _offer_pairing(self, alias: str, message: str) -> None:
        saved = self.config.lights.get(alias)
        if saved is None:
            return

        answer = QMessageBox.question(
            self,
            "Pair with this bulb?",
            f"{alias} refused the command because this computer is not paired with it.\n\n"
            "Make sure the bulb has power, then pair now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.status.setText(f"Pairing with {alias}…")
            self.worker.pair(saved.address)

    # ------------------------------------------------------------------ #
    # Lamp selection
    # ------------------------------------------------------------------ #

    def reload_lamps(self) -> None:
        """Rebuild the lamp and scene pickers from the saved configuration."""
        self._updating = True
        try:
            current = self.lamp_box.currentText()
            self.lamp_box.clear()
            aliases = list(self.config.lights)
            if len(aliases) > 1:
                self.lamp_box.addItem(ALL_LAMPS)
            self.lamp_box.addItems(aliases)
            if current:
                index = self.lamp_box.findText(current)
                if index >= 0:
                    self.lamp_box.setCurrentIndex(index)

            self.scene_box.clear()
            self.scene_box.addItems(list(self.config.scenes))
        finally:
            self._updating = False

        has_lamps = bool(self.config.lights)
        for widget in (self.power_button, self.brightness, self.wheel, self.temperature):
            widget.setEnabled(has_lamps)
        if not has_lamps:
            self.status.setText("No bulbs saved yet — press “Find bulbs…”.")
        self.lamps_changed.emit()
        if has_lamps:
            self.refresh()

    def targets(self) -> list[str]:
        """Which lamps the controls currently address."""
        choice = self.lamp_box.currentText()
        if not choice or choice == ALL_LAMPS:
            return list(self.config.lights)
        return [choice] if choice in self.config.lights else []

    def _lamp_changed(self) -> None:
        if not self._updating:
            self.refresh()

    def refresh(self) -> None:
        targets = self.targets()
        if targets:
            self.connection_label.setText("connecting…")
            self.worker.refresh(targets)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _power_toggled(self, on: bool) -> None:
        self.power_button.setText("Turn off" if on else "Turn on")
        if self._updating:
            return
        self.worker.set_power(self.targets(), on)

    def set_power(self, on: bool) -> None:
        self.power_button.setChecked(on)

    def _brightness_changed(self, value: int) -> None:
        self.brightness_label.setText(f"{value}%")
        if self._updating:
            return
        self.worker.set_brightness(self.targets(), float(value))

    def _wheel_picked(self, hue: float, saturation: float) -> None:
        if self._updating:
            return
        self._set_mode(white=False, quiet=True)
        target = parse_color(f"hsv:{hue:.1f},{saturation:.1f},100")
        self.worker.set_color(self.targets(), ColorTarget(xy=target.xy))

    def _temperature_changed(self, kelvin: int) -> None:
        self._update_white_preview(kelvin)
        if self._updating:
            return
        self.worker.set_color(self.targets(), ColorTarget(mired=kelvin_to_mired(kelvin)))

    def _update_white_preview(self, kelvin: int) -> None:
        from ..color import kelvin_to_xy

        self.white_preview.set_color(
            QColor(*xy_to_rgb(kelvin_to_xy(kelvin), 1.0)), f"{kelvin} K"
        )

    def apply_hex(self, text: str) -> None:
        try:
            red, green, blue = parse_hex(text)
        except ColorError as exc:
            self.status.setText(str(exc))
            return

        hue, saturation, _ = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        self.wheel.set_hsv(hue * 360, saturation * 100)
        self._set_mode(white=False, quiet=True)
        self.status.setText("")
        self.worker.set_color(self.targets(), parse_color(f"#{red:02x}{green:02x}{blue:02x}"))

    def apply_kelvin(self, kelvin: int) -> None:
        self._set_mode(white=True, quiet=True)
        # Move the slider without letting it send, then send once by hand: a
        # preset must reach the bulb even when the slider is already sitting on
        # that value (which it is, whenever the lamp is in colour mode).
        was_blocked = self.temperature.blockSignals(True)
        self.temperature.setValue(kelvin)
        self.temperature.blockSignals(was_blocked)
        self._temperature_changed(kelvin)

    def _set_mode(self, white: bool, quiet: bool = False) -> None:
        self.white_mode.setChecked(white)
        self.colour_mode.setChecked(not white)
        self.white_page.setVisible(white)
        self.colour_page.setVisible(not white)
        if quiet:
            return
        # Switching tabs by hand re-sends the value that tab represents, so the
        # bulb matches what is on screen.
        if white:
            self._temperature_changed(self.temperature.value())
        else:
            self._wheel_picked(self.wheel.hue(), self.wheel.saturation())

    # ------------------------------------------------------------------ #
    # Scenes
    # ------------------------------------------------------------------ #

    def apply_scene(self, name: str | None = None) -> None:
        name = name or self.scene_box.currentText()
        scene = self.config.scenes.get(name)
        if scene is None:
            self.status.setText("Pick a scene to apply.")
            return

        color = parse_color(scene.color) if scene.color else None
        self.worker.apply_scene(self.targets(), color, scene.brightness, scene.power)
        self.status.setText(f"Applied {name}.")

    def save_scene(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save scene", "Name for this look:")
        if not accepted or not name.strip():
            return

        name = name.strip()
        if self.white_mode.isChecked():
            color = f"{self.temperature.value()}k"
        else:
            target = parse_color(f"hsv:{self.wheel.hue():.1f},{self.wheel.saturation():.1f},100")
            assert target.xy is not None
            red, green, blue = xy_to_rgb(target.xy, 1.0)
            color = f"#{red:02x}{green:02x}{blue:02x}"

        self.config.add_scene(
            name,
            Scene(color=color, brightness=float(self.brightness.value()), power=True),
        )
        self.config.save()
        self.reload_lamps()
        index = self.scene_box.findText(name)
        if index >= 0:
            self.scene_box.setCurrentIndex(index)
        self.status.setText(f"Saved scene {name}.")

    def delete_scene(self) -> None:
        name = self.scene_box.currentText()
        if not name:
            return
        self.config.remove_scene(name)
        self.config.save()
        self.reload_lamps()
        self.status.setText(f"Deleted scene {name}.")

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def open_scan_dialog(self) -> None:
        dialog = ScanDialog(self.config, self.worker, self)
        dialog.exec()
        if dialog.added:
            self.config.save()
            self.reload_lamps()
            self.status.setText("Added: " + ", ".join(dialog.added))
