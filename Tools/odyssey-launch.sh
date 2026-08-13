#!/bin/bash
# version 1.5 2026-08-13

odyssey_client=/home/holuser/desktop-hol/squashfs-root/AppRun
chmod 774 $odyssey_client

echo "#!/usr/bin/bash
nohup ${odyssey_client} > /dev/null 2>&1 &
exit" > /tmp/runit.sh
chmod 775 /tmp/runit.sh
pkill -f pwsh > /dev/null 2>&1 || true
nohup /tmp/runit.sh
exit
