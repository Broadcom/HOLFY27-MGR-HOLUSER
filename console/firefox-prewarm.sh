#!/usr/bin/env bash
# Start Firefox at GNOME login and minimize the main window so it is warm when needed.
# Installs xdotool via "sudo apt install -y xdotool" if missing; on Wayland, xdotool usually does not work.

set -eu

FF="${FF:-/snap/bin/firefox}"
START_URL="${START_URL:-about:blank}"

if pgrep -f '/snap/firefox/.*/firefox' >/dev/null 2>&1; then
  exit 0
fi

# GNOME autostart normally sets DISPLAY; keep a sane default for Xorg seat.
export DISPLAY="${DISPLAY:-:0}"

if ! command -v xdotool >/dev/null 2>&1; then
  logger -t firefox-prewarm "xdotool not installed; attempting: sudo apt install -y xdotool"
  sudo apt install -y xdotool || true
fi

if ! command -v xdotool >/dev/null 2>&1; then
  logger -t firefox-prewarm "xdotool still unavailable after install attempt; starting Firefox without minimize"
  exec "$FF" "$START_URL" >/dev/null 2>&1 &
  exit 0
fi

# Brief pause so the compositor and panel are ready (avoids races on fast SSDs too).
sleep "${PREWARM_SLEEP:-2}"

"$FF" "$START_URL" >/dev/null 2>&1 &
disown

# WM_CLASS second field is "Firefox" for Mozilla builds (see: xprop WM_CLASS on the window).
for _ in $(seq 1 120); do
  wid="$(xdotool search --class Firefox 2>/dev/null | head -1 || true)"
  if [[ -n "${wid:-}" ]]; then
    xdotool windowminimize "$wid" 2>/dev/null || true
    exit 0
  fi
  sleep 0.5
done

logger -t firefox-prewarm "timed out waiting for Firefox window to minimize"
exit 0
