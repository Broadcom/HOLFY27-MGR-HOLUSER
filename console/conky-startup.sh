#!/bin/sh
# conky-startup.sh - Start conky with the VMware.config
# Deployed to: /home/holuser/.conky/conky-startup.sh on the Main Console
# Called by: conkywatch.sh (cron) and autostart desktop entry

conky -X :0 --config=/home/holuser/desktop-hol/VMware.config > /tmp/conky.log 2>&1 &

exit 0
