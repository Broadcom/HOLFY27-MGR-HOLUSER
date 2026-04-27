#!/usr/bin/bash
# doupdate.sh - HOLFY27 Update Script
# Version 1.1 - 2026-04-17
# Author - Burke Azbill and HOL Core Team
# Trigger: Runs on holorouter from watcher.sh (and related startup paths).
# Action:
#   - certsrv_proxy.py: copy from NFS mount, restart certsrv-proxy DaemonSet, remove drop-in
#   - renew_nginx_tls.request + renew-nginx-tls-from-vault.sh: run Vault PKI nginx renewal
#     (manager queues via /tmp/holorouter → /mnt/manager; no SSH required)
###############################################################################
# Version 1.0 code:
###############################################################################
WATCH_DIR="/mnt/manager"
export KUBECONFIG=/etc/kubernetes/admin.conf
# WATCH_INTERVAL=5  # seconds between checks
logfile="/tmp/doupdate.log"
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "${logfile}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# if the file /home/holuser/hol/Tools/holorouter/certsrv_proxy.py exists, then copy it to /tmp/holorouter/certsrv_proxy.py
if [ -f "${WATCH_DIR}/certsrv_proxy.py" ]; then
    log_message "Found certsrv_proxy.py, processing" "${logfile}"
    cp -f "${WATCH_DIR}/certsrv_proxy.py" "/root/certsrv-proxy/certsrv_proxy.py"
    log_message "Restarting certsrv-proxy pod..."
    kubectl delete pod -n default -l app=certsrv-proxy >> "${logfile}" 2>&1
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

# Nginx TLS (auth/dns/vault/gitlab/ca) — manager drops script + renew_nginx_tls.request on NFS share
RENEW_REQ="${WATCH_DIR}/renew_nginx_tls.request"
RENEW_SH="${WATCH_DIR}/renew-nginx-tls-from-vault.sh"
if [ -f "${RENEW_REQ}" ] && [ -f "${RENEW_SH}" ]; then
    log_message "Found renew_nginx_tls request + script; running Vault PKI nginx renewal..."
    if bash "${RENEW_SH}" >>"${logfile}" 2>&1; then
        log_message "renew-nginx-tls-from-vault.sh completed successfully"
        rm -f "${RENEW_REQ}" "${RENEW_SH}"
    else
        log_message "renew-nginx-tls-from-vault.sh failed (see ${logfile}); leaving ${RENEW_REQ} for retry"
    fi
elif [ -f "${RENEW_REQ}" ] && [ ! -f "${RENEW_SH}" ]; then
    log_message "renew_nginx_tls.request present but renew-nginx-tls-from-vault.sh missing on share — skipping"
fi
###############################################################################
# End doupdate.sh
###############################################################################