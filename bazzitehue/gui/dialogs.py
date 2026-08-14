"""Dialogs: finding bulbs and pairing with them, so setup needs no terminal."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..config import Config, is_address
from .worker import BleWorker

SCAN_SECONDS = 8.0


def suggest_alias(name: str, address: str, taken: set[str]) -> str:
    """A short, typeable alias derived from whatever the bulb calls itself."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    for noise in ("hue-", "philips-"):
        if base.startswith(noise):
            base = base[len(noise) :]
    base = base or "lamp-" + address.replace(":", "")[-4:].lower()

    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


class ScanDialog(QDialog):
    """Find bulbs in range, then pair and save the one you pick."""

    def __init__(self, config: Config, worker: BleWorker, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.worker = worker
        self.added: list[str] = []
        self._pairing: str | None = None

        self.setWindowTitle("Find bulbs")
        self.setMinimumSize(440, 340)
        self._build()

        self.worker.scan_finished.connect(self._scan_finished)
        self.worker.scan_failed.connect(self._scan_failed)
        self.worker.pair_finished.connect(self._pair_finished)

        self.start_scan()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        intro = QLabel(
            "A bulb that is currently connected to something else — the Hue app on a "
            "phone, say — stops advertising and cannot be found. Close that app or "
            "switch the bulb off and on at the wall, then scan again. A bulb paired to "
            "a Hue Bridge talks Zigbee, not Bluetooth, so it should still show up."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.show_everything = QCheckBox("Show every Bluetooth device, not just Hue bulbs")
        self.show_everything.setToolTip(
            "Useful if a bulb has been renamed or does not advertise the Hue service."
        )
        self.show_everything.toggled.connect(lambda _: self.start_scan())
        layout.addWidget(self.show_everything)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.results = QListWidget()
        self.results.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.results, 1)

        self.status = QLabel("Scanning…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.rescan_button = QPushButton("Scan again")
        self.rescan_button.clicked.connect(self.start_scan)
        buttons.addWidget(self.rescan_button)

        self.manual_button = QPushButton("Add by address…")
        self.manual_button.setToolTip(
            "Type a bulb's Bluetooth address if you know it but it will not appear here."
        )
        self.manual_button.clicked.connect(self.add_by_address)
        buttons.addWidget(self.manual_button)
        buttons.addStretch(1)

        self.add_button = QPushButton("Pair and add")
        self.add_button.setDefault(True)
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self._add_selected)
        buttons.addWidget(self.add_button)

        close_button = QPushButton("Done")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #

    def start_scan(self) -> None:
        self.results.clear()
        self.progress.show()
        self.rescan_button.setEnabled(False)
        self.status.setText("Scanning…")
        self.worker.scan(SCAN_SECONDS, self.show_everything.isChecked())

    def add_by_address(self) -> None:
        """Save a bulb by typing its address, for one that will not show up."""
        text, accepted = QInputDialog.getText(
            self, "Add by address", "Bluetooth address (AA:BB:CC:DD:EE:FF):"
        )
        if not accepted:
            return

        address = text.strip().upper()
        if not is_address(address):
            QMessageBox.warning(
                self, "Not an address", f"“{text}” is not a Bluetooth address."
            )
            return

        known = {light.address.upper() for light in self.config.lights.values()}
        if address in known:
            QMessageBox.information(self, "Already saved", "That bulb is already saved.")
            return

        self._save_and_pair(address, "")

    def _scan_finished(self, devices: list[tuple[str, str]]) -> None:
        self.progress.hide()
        self.rescan_button.setEnabled(True)

        known = {light.address.upper(): alias for alias, light in self.config.lights.items()}
        for address, name in devices:
            alias = known.get(address.upper())
            label = f"{name or 'Hue bulb'} — {address}"
            if alias:
                label += f"  (already saved as “{alias}”)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (address, name))
            if alias:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.results.addItem(item)

        if not devices:
            self.status.setText("No bulbs found. Power-cycle the bulb and scan again.")
        else:
            self.status.setText(f"Found {len(devices)}. Pick one and press “Pair and add”.")

    def _scan_failed(self, message: str, hint: str) -> None:
        self.progress.hide()
        self.rescan_button.setEnabled(True)
        self.status.setText(f"{message}\n{hint}" if hint else message)

    def _selection_changed(self) -> None:
        self.add_button.setEnabled(bool(self.results.selectedItems()))

    # ------------------------------------------------------------------ #
    # Pairing
    # ------------------------------------------------------------------ #

    def _add_selected(self) -> None:
        items = self.results.selectedItems()
        if not items:
            return

        address, name = items[0].data(Qt.UserRole)
        self._save_and_pair(address, name)

    def _save_and_pair(self, address: str, name: str) -> None:
        alias = suggest_alias(name, address, set(self.config.lights))
        self.config.add_light(alias, address, name=name or None)
        self.config.save()
        self.added.append(alias)

        self._pairing = address
        self.add_button.setEnabled(False)
        self.status.setText(f"Saved as “{alias}”. Pairing… this can take a few seconds.")
        self.progress.show()
        self.worker.pair(address)

    def _pair_finished(self, address: str, succeeded: bool, message: str) -> None:
        if address != self._pairing:
            return

        self._pairing = None
        self.progress.hide()
        self.add_button.setEnabled(True)
        if succeeded:
            self.status.setText("Paired. The bulb is ready to use.")
        else:
            # The lamp is still saved: pairing often succeeds on a second try
            # after a power cycle, and the controls will offer it again.
            self.status.setText(
                f"Saved, but pairing failed: {message}\n"
                "Switch the bulb off and on at the wall, then try the control panel again."
            )
