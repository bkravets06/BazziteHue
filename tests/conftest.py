import os

import pytest

# Qt must know it has no display before anything imports it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session; Qt allows no more than one."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


@pytest.fixture
def pump(qapp):
    """Run the Qt event loop briefly so queued signals get delivered."""
    import time

    def _pump(seconds: float = 0.4) -> None:
        end = time.time() + seconds
        while time.time() < end:
            qapp.processEvents()
            time.sleep(0.01)

    return _pump
