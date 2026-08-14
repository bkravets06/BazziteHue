"""Exception types that carry user-facing advice."""

from __future__ import annotations


class BazziteHueError(Exception):
    """Base class for every error this tool raises deliberately."""

    #: Optional extra line printed under the message to tell the user what to do.
    hint: str | None = None

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class DeviceNotFoundError(BazziteHueError):
    """The requested lamp did not turn up during a scan."""


class ConnectionFailedError(BazziteHueError):
    """We found the lamp but could not establish a GATT connection."""


class NotPairedError(BazziteHueError):
    """The lamp refused a read/write because the host is not bonded to it."""

    hint = (
        "Hue bulbs only accept commands from a paired (bonded) host.\n"
        "Power-cycle the lamp so it accepts pairing, then run:\n"
        "  bazzitehue pair <address-or-alias>"
    )


class UnsupportedFeatureError(BazziteHueError):
    """The lamp does not expose the characteristic needed for this command."""


class ConfigError(BazziteHueError):
    """Something is wrong with the saved configuration or the arguments given."""
