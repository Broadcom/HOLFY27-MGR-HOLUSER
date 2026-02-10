#!/bin/bash
# conkywatch.sh - Monitor and restart conky if it is not running or not rendering
# Called by cron every minute: */1 * * * * /home/holuser/desktop-hol/conkywatch.sh
#
# Checks:
#   1. Is the conky process running? If not, start it.
#   2. Is the conky window rendering? If the window is 1x1 pixels (X Error
#      after config reload), kill it and restart. This happens when
#      labstartup modifies VMware.config and conky reloads, sometimes
#      hitting an X Error that leaves the process alive but the window dead.

export DISPLAY=:0
export XAUTHORITY=$(cat /tmp/XAUTHORITY 2>/dev/null)
conkystart="/home/holuser/.conky/conky-startup.sh"

# Get the conky PID(s) - use pgrep for reliable process matching
conkypid=$(pgrep -x conky)

if [ -z "${conkypid}" ]; then
    # No conky process at all - start it
    date
    echo "No conky process found - starting conky"
    ${conkystart}
else
    # Conky process exists - check if the window is actually rendering
    # A healthy conky window has a real size (e.g. 351x200)
    # A broken conky window after an X Error collapses to 1x1
    window_info=$(xwininfo -tree -root 2>/dev/null | grep -i conky)
    if [ -n "${window_info}" ]; then
        # Extract window geometry (e.g. "351x200+200+600" or "1x1+0+0")
        window_size=$(echo "${window_info}" | grep -oP '\d+x\d+' | head -1)
        width=$(echo "${window_size}" | cut -dx -f1)
        height=$(echo "${window_size}" | cut -dx -f2)

        if [ "${width:-0}" -le 1 ] || [ "${height:-0}" -le 1 ]; then
            date
            echo "Conky window is broken (${window_size}) - killing PID(s): ${conkypid}"
            echo "${conkypid}" | xargs kill -9 2>/dev/null
            sleep 2
            echo "Restarting conky"
            ${conkystart}
        fi
    else
        # Process exists but no window found at all - kill and restart
        date
        echo "Conky process running but no window found - killing PID(s): ${conkypid}"
        echo "${conkypid}" | xargs kill -9 2>/dev/null
        sleep 2
        echo "Restarting conky"
        ${conkystart}
    fi
fi
