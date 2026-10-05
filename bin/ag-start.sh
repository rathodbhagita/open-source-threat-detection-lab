#!/bin/bash
# Startup entry point: wait for the desktop session, then sign in to Antigravity.
# Wired into KDE autostart by install.sh. Safe to run repeatedly.
set -u

ROOT="/config/automata"
PY=/opt/venv/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)

export DISPLAY="${DISPLAY:-:1}"
mkdir -p "$ROOT/logs"
BOOTLOG="$ROOT/logs/startup.log"

log() { echo "$(date '+%F %T')  $*" >> "$BOOTLOG"; }

log "--- ag-start.sh ---"

# Wait for X/Xwayland to accept clients before touching the GUI.
for i in $(seq 1 60); do
    if xdotool getdisplaygeometry >/dev/null 2>&1; then
        log "display ready after ${i}s"
        break
    fi
    sleep 1
done

# Give Plasma a moment to finish starting so the window manager is up.
sleep "${AG_STARTUP_DELAY:-8}"

log "running autologin"
"$PY" "$ROOT/bin/ag_autologin.py" "$@" >> "$BOOTLOG" 2>&1
log "autologin exited with $?"
