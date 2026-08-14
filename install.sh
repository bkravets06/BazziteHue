#!/usr/bin/env bash
# Install BazziteHue into a private virtualenv under ~/.local.
#
# Bazzite's root filesystem is immutable, so nothing here touches the base image
# and no rpm-ostree layering is needed: Python and BlueZ already ship with the OS.

set -euo pipefail

PREFIX="${BAZZITEHUE_PREFIX:-$HOME/.local/share/bazzitehue}"
BIN_DIR="${BAZZITEHUE_BIN:-$HOME/.local/bin}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 not found"

PYTHON_OK=$(python3 - <<'EOF'
import sys
print("yes" if sys.version_info >= (3, 10) else "no")
EOF
)
[ "$PYTHON_OK" = "yes" ] || die "Python 3.10 or newer is required (found $(python3 -V))"

if ! command -v bluetoothctl >/dev/null; then
    warn "bluetoothctl not found -- BlueZ is normally part of Bazzite; check your image"
fi

info "Creating virtualenv at $PREFIX"
mkdir -p "$PREFIX"
python3 -m venv --upgrade-deps "$PREFIX/venv" >/dev/null

info "Installing bazzitehue and its dependencies"
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SOURCE_DIR"

mkdir -p "$BIN_DIR"
ln -sf "$PREFIX/venv/bin/bazzitehue" "$BIN_DIR/bazzitehue"
info "Linked $BIN_DIR/bazzitehue"

# A desktop entry so the TUI can be launched from the app grid or Steam's
# desktop mode.
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/bazzitehue.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BazziteHue
Comment=Control Philips Hue bulbs over Bluetooth
Exec=$BIN_DIR/bazzitehue tui
Icon=preferences-desktop-display
Terminal=true
Categories=Utility;Settings;
Keywords=hue;light;bluetooth;
EOF
info "Installed desktop entry"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH; add it to ~/.bashrc to run 'bazzitehue' directly" ;;
esac

if ! systemctl is-active --quiet bluetooth; then
    warn "the bluetooth service is not running; start it with: sudo systemctl enable --now bluetooth"
fi

info "Done. Next: bazzitehue scan"
