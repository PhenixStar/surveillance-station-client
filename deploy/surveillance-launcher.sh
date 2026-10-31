#!/usr/bin/env bash
# Runtime wrapper for the from-source Surveillance Station client.
#
# SURVEILLANCE_MPV_OPTS  extra mpv options (comma separated). Default raises the NVDEC surface pool:
#   deep-reorder camera streams (13-15 frames) otherwise exhaust it -> "No decoder surfaces left" -> ghosting.
#   Values above ~16 break HEVC NVDEC creation on some GPUs; 12 measured clean on 16 mixed streams.
# SURVEILLANCE_MEMORY_MAX  MemoryMax of a transient systemd user scope (default 16G) so a leak kills only this
#   app, never the host. Set empty to run without a scope (no systemd).
set -u
PREFIX="${SS_CLIENT_PREFIX:-$HOME/.local/opt/surveillance-station-client}"
BIN="$PREFIX/src/.venv/bin/surveillance"
export SURVEILLANCE_MPV_OPTS="${SURVEILLANCE_MPV_OPTS:-hwdec-extra-frames=12}"
MEM="${SURVEILLANCE_MEMORY_MAX-16G}"
if [ -n "$MEM" ] && command -v systemd-run >/dev/null 2>&1 && systemctl --user is-system-running >/dev/null 2>&1; then
  exec systemd-run --user --scope --quiet --unit="surveillance-client-$$" \
    -p MemoryMax="$MEM" -p MemorySwapMax=0 "$BIN" "$@"
fi
exec "$BIN" "$@"
