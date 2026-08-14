#!/usr/bin/env bash
# Install BazziteHue: the desktop app, its panel applet, and the CLI.
#
# Bazzite's root filesystem is immutable, so everything lands in ~/.local and
# nothing is layered onto the base image with rpm-ostree. Python and BlueZ are
# already part of the OS.
#
#   ./install.sh              app + panel applet + CLI (default)
#   ./install.sh --cli-only   just the terminal tool, no Qt download
#   ./install.sh --uninstall  remove everything this script created

set -euo pipefail

PREFIX="${BAZZITEHUE_PREFIX:-$HOME/.local/share/bazzitehue}"
BIN_DIR="${BAZZITEHUE_BIN:-$HOME/.local/bin}"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="full"
[ "${1:-}" = "--cli-only" ] && MODE="cli"
[ "${1:-}" = "--uninstall" ] && MODE="uninstall"

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$MODE" = "uninstall" ]; then
    info "Removing BazziteHue"
    rm -rf "$PREFIX"
    rm -f "$BIN_DIR/bazzitehue" "$BIN_DIR/bazzitehue-gui"
    rm -f "$APPS_DIR/bazzitehue.desktop" "$AUTOSTART_DIR/bazzitehue.desktop"
    rm -f "$ICON_DIR/bazzitehue.svg"
    info "Done. Your saved bulbs in ~/.config/bazzitehue were left alone."
    exit 0
fi

command -v python3 >/dev/null || die "python3 not found"

PYTHON_OK=$(python3 - <<'EOF'
import sys
print("yes" if sys.version_info >= (3, 10) else "no")
EOF
)
[ "$PYTHON_OK" = "yes" ] || die "Python 3.10 or newer is required (found $(python3 -V))"

command -v bluetoothctl >/dev/null || warn "bluetoothctl not found — is BlueZ installed?"

info "Creating virtualenv at $PREFIX"
mkdir -p "$PREFIX"
python3 -m venv --upgrade-deps "$PREFIX/venv" >/dev/null

if [ "$MODE" = "cli" ]; then
    info "Installing bazzitehue (command line only)"
    "$PREFIX/venv/bin/pip" install --quiet --upgrade "$SOURCE_DIR"
else
    info "Installing bazzitehue with the desktop app (this downloads Qt, ~80 MB)"
    "$PREFIX/venv/bin/pip" install --quiet --upgrade "$SOURCE_DIR[gui]"
fi

mkdir -p "$BIN_DIR"
ln -sf "$PREFIX/venv/bin/bazzitehue" "$BIN_DIR/bazzitehue"
info "Linked $BIN_DIR/bazzitehue"

if [ "$MODE" != "cli" ]; then
    ln -sf "$PREFIX/venv/bin/bazzitehue-gui" "$BIN_DIR/bazzitehue-gui"

    mkdir -p "$ICON_DIR" "$APPS_DIR"
    install -m 644 "$SOURCE_DIR/packaging/bazzitehue.svg" "$ICON_DIR/bazzitehue.svg"
    sed "s|@EXEC@|$BIN_DIR/bazzitehue-gui|g" \
        "$SOURCE_DIR/packaging/bazzitehue.desktop" > "$APPS_DIR/bazzitehue.desktop"
    chmod 644 "$APPS_DIR/bazzitehue.desktop"

    # Make the new launcher and icon visible without a logout.
    command -v update-desktop-database >/dev/null && \
        update-desktop-database "$APPS_DIR" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && \
        gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

    info "Installed the BazziteHue app launcher and icon"

    mkdir -p "$AUTOSTART_DIR"
    sed "s|@EXEC@|$BIN_DIR/bazzitehue-gui --tray|g" \
        "$SOURCE_DIR/packaging/bazzitehue.desktop" > "$AUTOSTART_DIR/bazzitehue.desktop"
    chmod 644 "$AUTOSTART_DIR/bazzitehue.desktop"
    info "Enabled the panel applet at login (toggle it off in the tray menu)"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH; add it to ~/.bashrc to use the 'bazzitehue' command" ;;
esac

if ! systemctl is-active --quiet bluetooth; then
    warn "the bluetooth service is not running: sudo systemctl enable --now bluetooth"
fi

if [ "$MODE" != "cli" ]; then
    # GNOME hides StatusNotifierItem tray icons unless this extension is on.
    if [ "${XDG_CURRENT_DESKTOP:-}" = "GNOME" ] && command -v gnome-extensions >/dev/null; then
        if ! gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
            warn "GNOME needs the AppIndicator extension to show tray icons:"
            warn "  gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com"
        fi
    fi
    echo
    info "Done. Launch “BazziteHue” from your app menu, or run: bazzitehue-gui"
else
    echo
    info "Done. Next: bazzitehue scan"
fi
