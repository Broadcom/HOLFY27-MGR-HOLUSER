#!/bin/bash
# version 1.2 29-May 2025

odyssey_client=/home/holuser/desktop-hol/squashfs-root/AppRun
chmod 774 $odyssey_client

echo "#!/usr/bin/bash
nohup ${odyssey_client} > /dev/null 2>&1 &
exit" > /tmp/runit.sh
chmod 775 /tmp/runit.sh
nohup /tmp/runit.sh
exit
    
