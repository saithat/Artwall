#!/usr/bin/env bash
# Start headless Sway and Xvfb inside the container, then run the desktop tests.
set -euo pipefail
mkdir -p -m 700 "$XDG_RUNTIME_DIR"

Xvfb :99 -screen 0 1600x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export ARTWALL_TEST_X11_DISPLAY=:99

printf 'output HEADLESS-1 mode 2560x1440\n' > /tmp/sway.conf
WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 \
    sway --unsupported-gpu -c /tmp/sway.conf >/tmp/sway.log 2>&1 &
for _ in $(seq 50); do
    sock=$(ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock 2>/dev/null | head -1 || true)
    [ -n "$sock" ] && [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.2
done
if [ -z "${sock:-}" ]; then cat /tmp/sway.log; exit 1; fi
export ARTWALL_TEST_SWAYSOCK=$sock
wayland=$(cd "$XDG_RUNTIME_DIR" && ls wayland-* | grep -v lock | head -1)
export ARTWALL_TEST_WAYLAND_DISPLAY=$wayland

python3 -m unittest tests.live_desktops -v
