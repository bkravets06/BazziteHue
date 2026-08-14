"""Application entry point: wires the window, the tray and the BLE worker."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import __version__
from ..config import Config
from .icons import app_icon
from .tray import Tray
from .window import ControlWindow
from .worker import BleWorker

log = logging.getLogger(__name__)

APP_ID = "bazzitehue"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "bazzitehue.desktop"


def autostart_enabled() -> bool:
    return AUTOSTART_FILE.exists()


def set_autostart(enabled: bool) -> None:
    """Add or remove the XDG autostart entry that starts the tray at login."""
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return

    launcher = sys.argv[0]
    if not os.path.isabs(launcher):
        from shutil import which

        launcher = which("bazzitehue-gui") or which(launcher) or launcher

    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BazziteHue\n"
        "Comment=Control Philips Hue bulbs over Bluetooth\n"
        f"Exec={launcher} --tray\n"
        "Icon=bazzitehue\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


class Application:
    """Holds the pieces together and owns shutdown."""

    def __init__(self, argv: list[str], start_hidden: bool) -> None:
        self.qt = QApplication(argv)
        self.qt.setApplicationName("BazziteHue")
        self.qt.setApplicationDisplayName("BazziteHue")
        self.qt.setDesktopFileName(APP_ID)
        self.qt.setWindowIcon(app_icon())
        # Closing the control window leaves the tray running.
        self.qt.setQuitOnLastWindowClosed(False)

        self.config = Config.load()
        self.worker = BleWorker(self.config)
        self.worker.start()

        self.window = ControlWindow(self.config, self.worker)
        self.tray = Tray(self.config, self.worker)

        self.tray.show_window.connect(self.show_window)
        self.tray.find_bulbs.connect(self._find_bulbs)
        self.tray.quit_requested.connect(self.quit)
        self.tray.autostart_toggled.connect(self._autostart_toggled)
        self.window.lamps_changed.connect(self._lamps_changed)
        self.window.appearance_changed.connect(self.tray.update_appearance)

        self.tray.rebuild(autostart_enabled())

        if self.tray.available:
            self.tray.show()
        elif start_hidden:
            # Without a tray there would be no way to bring the app back, so
            # show the window instead of starting invisibly.
            start_hidden = False
            log.warning("no system tray available; showing the window instead")

        if not start_hidden:
            self.show_window()

    # ------------------------------------------------------------------ #

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.refresh()

    def _find_bulbs(self) -> None:
        self.show_window()
        self.window.open_scan_dialog()

    def _lamps_changed(self) -> None:
        self.tray.rebuild(autostart_enabled())

    def _autostart_toggled(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except OSError as exc:
            QMessageBox.warning(
                self.window, "Could not change autostart", f"{AUTOSTART_FILE}: {exc}"
            )

    def quit(self) -> None:
        self.tray.hide()
        self.worker.stop()
        self.qt.quit()

    def run(self) -> int:
        # Ctrl-C in a terminal should still close the app; Qt's loop otherwise
        # swallows SIGINT entirely.
        signal.signal(signal.SIGINT, lambda *_: self.quit())
        heartbeat = QTimer()
        heartbeat.start(400)
        heartbeat.timeout.connect(lambda: None)

        try:
            return self.qt.exec()
        finally:
            self.worker.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bazzitehue-gui",
        description="Control Philips Hue bulbs over Bluetooth from the desktop.",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="start in the panel without opening the control window",
    )
    parser.add_argument("--version", action="version", version=f"bazzitehue {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log Bluetooth chatter")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    application = Application(sys.argv[:1], start_hidden=args.tray)
    return application.run()


if __name__ == "__main__":
    raise SystemExit(main())
