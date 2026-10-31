#!/usr/bin/env bash
# From-source install of the Surveillance Station client (als branch) for one user on Linux.
# Idempotent: re-running updates the checkout and reinstalls into the same venv.
#
#   deploy/install.sh [--branch als] [--prefix ~/.local/opt/surveillance-station-client]
#
# Needs the system GTK4 stack (PyGObject, pycairo, gir1.2-gtk-4.0), libmpv and portaudio:
#   Debian/Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 libmpv2 libportaudio2 python3-venv
# The venv is created with --system-site-packages so the distro's PyGObject is used
# (pip cannot build PyGObject >= 3.50 on Ubuntu 24.04; 3.48 works).
set -euo pipefail

REPO_URL="https://github.com/PhenixStar/surveillance-station-client"
BRANCH="${SS_CLIENT_BRANCH:-als}"
PREFIX="${SS_CLIENT_PREFIX:-$HOME/.local/opt/surveillance-station-client}"
while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
SRC="$PREFIX/src"
HERE="$(cd "$(dirname "$0")" && pwd)"

for mod in gi cairo; do
  python3 -c "import $mod" 2>/dev/null || { echo "python3 cannot import $mod — install the distro PyGObject/pycairo packages first" >&2; exit 1; }
done
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null \
  || { echo "GTK 4 introspection data missing (gir1.2-gtk-4.0)" >&2; exit 1; }
ldconfig -p 2>/dev/null | grep -q 'libmpv\.so' || echo "warning: libmpv not found in the linker cache (libmpv2 package)" >&2

mkdir -p "$PREFIX" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
if git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$SRC" fetch -q origin
  git -C "$SRC" switch -q "$BRANCH" 2>/dev/null || git -C "$SRC" switch -q -c "$BRANCH" "origin/$BRANCH"
  git -C "$SRC" pull -q --ff-only origin "$BRANCH" || echo "note: $BRANCH is not fast-forwardable (detached tag or local commits); left as is" >&2
else
  git clone -q -b "$BRANCH" "$REPO_URL" "$SRC"
fi

if [ ! -x "$SRC/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$SRC/.venv"
fi
"$SRC/.venv/bin/pip" install -q --upgrade pip
# Runtime deps from pyproject.toml minus PyGObject/pycairo (system). Kept explicit so --no-deps below
# cannot silently skip one; compare with pyproject.toml on each upgrade (upgrade.sh prints the diff).
"$SRC/.venv/bin/pip" install -q "httpx[http2]>=0.27" "python-mpv>=1.0" "PyOpenGL>=3.1" "keyring>=25.0" \
  "tomli-w>=1.0" "websockets>=13.0" "sounddevice>=0.5" "cryptography>=42.0"
"$SRC/.venv/bin/pip" install -q --no-deps -e "$SRC"

install -m 0755 "$HERE/surveillance-launcher.sh" "$PREFIX/surveillance-launcher.sh"
ln -sfn "$PREFIX/surveillance-launcher.sh" "$HOME/.local/bin/surveillance"
sed "s|^Exec=.*|Exec=$HOME/.local/bin/surveillance|" "$SRC/data/org.surveillance.app.desktop" \
  > "$HOME/.local/share/applications/surveillance-station-client.desktop"
install -m 0644 "$SRC/data/icons/hicolor/scalable/apps/org.surveillance.app.svg" \
  "$HOME/.local/share/icons/hicolor/scalable/apps/org.surveillance.app.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

ver=$(git -C "$SRC" describe --tags --always)
echo "installed $ver ($BRANCH) at $SRC"
echo "config: ~/.config/surveillance-station/config.toml (see deploy/config.example.toml)"
echo "run: surveillance   (debug: surveillance --debug --log-file=/tmp/ss.log)"
