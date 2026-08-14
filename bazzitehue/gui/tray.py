"""The panel/menu-bar applet.

Qt publishes this over the StatusNotifierItem D-Bus interface, which is what
Plasma's system tray and GNOME's AppIndicator support both consume, so the same
icon works on Bazzite's KDE and GNOME images.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..color import parse_color
from ..config import Config
from .icons import app_icon, bulb_icon
from .worker import BleWorker

QUICK_BRIGHTNESS = (100, 75, 50, 25)


class Tray(QObject):
    """Tray icon plus its menu. Owns no state beyond what it displays."""

    show_window = Signal()
    find_bulbs = Signal()
    quit_requested = Signal()
    autostart_toggled = Signal(bool)
    share_toggled = Signal(bool)

    def __init__(self, config: Config, worker: BleWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.worker = worker
        self._colors: dict[str, QColor] = {}
        self._powered: dict[str, bool] = {}

        self.icon = QSystemTrayIcon(app_icon(), self)
        self.icon.setToolTip("BazziteHue")
        self.icon.activated.connect(self._activated)

        self.menu = QMenu()
        self.icon.setContextMenu(self.menu)
        self.rebuild()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    # ------------------------------------------------------------------ #
    # Menu
    # ------------------------------------------------------------------ #

    def rebuild(self, autostart_enabled: bool = False) -> None:
        """Rebuild from config; called whenever lamps or scenes change."""
        self.menu.clear()

        controls = QAction("Controls…", self.menu)
        controls.triggered.connect(self.show_window.emit)
        self.menu.addAction(controls)
        self.menu.addSeparator()

        aliases = list(self.config.lights)
        if not aliases:
            empty = QAction("No bulbs saved", self.menu)
            empty.setEnabled(False)
            self.menu.addAction(empty)
        else:
            for alias in aliases:
                action = QAction(alias, self.menu)
                action.setCheckable(True)
                action.setChecked(self._powered.get(alias, False))
                action.toggled.connect(
                    lambda checked, name=alias: self.worker.set_power([name], checked)
                )
                self.menu.addAction(action)

            if len(aliases) > 1:
                self.menu.addSeparator()
                all_on = QAction("All on", self.menu)
                all_on.triggered.connect(lambda: self.worker.set_power(aliases, True))
                self.menu.addAction(all_on)
                all_off = QAction("All off", self.menu)
                all_off.triggered.connect(lambda: self.worker.set_power(aliases, False))
                self.menu.addAction(all_off)

            brightness = self.menu.addMenu("Brightness")
            for percent in QUICK_BRIGHTNESS:
                action = QAction(f"{percent}%", brightness)
                action.triggered.connect(
                    lambda _=False, value=percent: self.worker.set_brightness(aliases, float(value))
                )
                brightness.addAction(action)

            if self.config.scenes:
                scenes = self.menu.addMenu("Scenes")
                for name, scene in self.config.scenes.items():
                    action = QAction(name, scenes)
                    action.triggered.connect(
                        lambda _=False, chosen=scene: self._apply_scene(aliases, chosen)
                    )
                    scenes.addAction(action)

        self.menu.addSeparator()
        find = QAction("Find bulbs…", self.menu)
        find.triggered.connect(self.find_bulbs.emit)
        self.menu.addAction(find)

        share = QAction("Share bulbs with other apps", self.menu)
        share.setCheckable(True)
        share.setChecked(self.config.share_mode)
        share.setToolTip(
            "Release each bulb straight after a change so the Hue phone app can "
            "reach it. Slightly slower to respond."
        )
        share.toggled.connect(self.share_toggled.emit)
        self.menu.addAction(share)

        autostart = QAction("Start at login", self.menu)
        autostart.setCheckable(True)
        autostart.setChecked(autostart_enabled)
        autostart.toggled.connect(self.autostart_toggled.emit)
        self.menu.addAction(autostart)

        self.menu.addSeparator()
        quit_action = QAction("Quit BazziteHue", self.menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        self.menu.addAction(quit_action)

    def _apply_scene(self, aliases: list[str], scene) -> None:
        color = parse_color(scene.color) if scene.color else None
        self.worker.apply_scene(aliases, color, scene.brightness, scene.power)

    # ------------------------------------------------------------------ #
    # Appearance
    # ------------------------------------------------------------------ #

    def update_appearance(self, alias: str, color: QColor | None, powered: bool) -> None:
        """Track each lamp so the icon shows what the room actually looks like."""
        if color is not None:
            self._colors[alias] = color
        self._powered[alias] = powered

        any_on = any(self._powered.values())
        tint = None
        for name, is_on in self._powered.items():
            if is_on and name in self._colors:
                tint = self._colors[name]
                break

        self.icon.setIcon(bulb_icon(tint, size=64, on=any_on))
        lit = [name for name, is_on in self._powered.items() if is_on]
        self.icon.setToolTip("BazziteHue — " + (", ".join(lit) + " on" if lit else "all off"))

        for action in self.menu.actions():
            if action.text() == alias and action.isCheckable():
                was_blocked = action.blockSignals(True)
                action.setChecked(powered)
                action.blockSignals(was_blocked)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.show_window.emit()
        elif reason == QSystemTrayIcon.MiddleClick:
            aliases = list(self.config.lights)
            self.worker.set_power(aliases, not any(self._powered.values()))
