#!/usr/bin/env bash
set -euo pipefail

# Both containers use the host network, which shares Xorg's abstract socket.
# The base image defaults to Wayland; this desktop intentionally uses X11.
unset WAYLAND_DISPLAY
mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
chmod 0700 "$HOME" "$XDG_RUNTIME_DIR"
exec /app/daedalus
