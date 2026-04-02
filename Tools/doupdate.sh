#!/usr/bin/bash
# doupdate.sh - HOLFY27 Update Script
# Version 1.0 - April 2026
# Author - Burke Azbill and HOL Core Team
# Trigger: Runs on holorouterfrom watcher.sh - after getrules.sh completes
# Action: Copies certsrv_proxy.py to /mnt/manager/certsrv_proxy.py if it exists
#         Restarts the certsrv-proxy pod
#         Deletes the certsrv_proxy.py file after the pod is restarted
#         Deletes the certsrv_proxy.py file after the pod is restarted
###############################################################################
# Version 1.0 code:
###############################################################################
WATCH_DIR="/mnt/manager"
# WATCH_INTERVAL=5  # seconds between checks
logfile="/tmp/doupdate.log"
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> ${logfile}
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    
    # Rotate log periodically (every 100 messages approximately)
    if [ $((RANDOM % 100)) -eq 0 ]; then
        rotate_log
    fi
}

# if the file /home/holuser/hol/Tools/holorouter/certsrv_proxy.py exists, then copy it to /tmp/holorouter/certsrv_proxy.py
if [ -f "${WATCH_DIR}/certsrv_proxy.py" ]; then
    log_message "Found certsrv_proxy.py, processing" "${logfile}"
    cp -f "${WATCH_DIR}/certsrv_proxy.py" "/root/certsrv-proxy/certsrv_proxy.py"
    log_message "Restarting certsrv-proxy pod..."
    kubectl -s https://192.168.0.2:6443 delete pod -n default -l app=certsrv-proxy > "${logfile}" 2>&1
    sleep 10
    # Make sure that the pod is running and age is less than 30 seconds
    if kubectl get pod -n default -l app=certsrv-proxy -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' | grep -q 0; then
        log_message "Certsrv-proxy pod restarted successfully" "${logfile}"
        rm -f "${WATCH_DIR}/certsrv_proxy.py"
    else
        log_message "Certsrv-proxy pod failed to restart" "${logfile}"
        rm -f "${WATCH_DIR}/certsrv_proxy.py"
    fi
fi
###############################################################################
# End Version 1.0 code:
###############################################################################