#!/usr/bin/bash
# doupdate.sh - HOLFY27 Update Script
# Version 1.3 - 2026-04-27
# Author - Burke Azbill and HOL Core Team
# Trigger: Runs on holorouter from watcher.sh (and related startup paths).
# Action:
#   - certsrv_proxy.py: copy from NFS mount, restart certsrv-proxy DaemonSet, remove drop-in
#   - renew_nginx_tls.request + renew-nginx-tls-from-vault.sh: run Vault PKI nginx renewal
#     (prelim/labstartup via /tmp/holorouter → /mnt/manager; script-only drop also runs renewal)
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

log_message "doupdate.sh v1.3 invoked (WATCH_DIR=${WATCH_DIR}, log=${logfile})"

# if the file /home/holuser/hol/Tools/holorouter/certsrv_proxy.py exists, then copy it to /tmp/holorouter/certsrv_proxy.py
if [ -f "${WATCH_DIR}/certsrv_proxy.py" ]; then
    log_message "Found certsrv_proxy.py, processing"
    cp -f "${WATCH_DIR}/certsrv_proxy.py" "/root/certsrv-proxy/certsrv_proxy.py"
    log_message "Restarting certsrv-proxy pod..."
    kubectl delete pod -n default -l app=certsrv-proxy >> "${logfile}" 2>&1
    sleep 10
    # Make sure that the pod is running and age is less than 30 seconds
    if kubectl get pod -n default -l app=certsrv-proxy -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' | grep -q 0; then
        log_message "Certsrv-proxy pod restarted successfully"
        rm -f "${WATCH_DIR}/certsrv_proxy.py"
    else
        log_message "Certsrv-proxy pod failed to restart"
        rm -f "${WATCH_DIR}/certsrv_proxy.py"
    fi
fi

# Nginx TLS (auth/dns/vault/gitlab/ca) — manager drops script + renew_nginx_tls.request on NFS share
RENEW_REQ="${WATCH_DIR}/renew_nginx_tls.request"
RENEW_SH="${WATCH_DIR}/renew-nginx-tls-from-vault.sh"
log_message "nginx TLS renewal: evaluating (${RENEW_REQ##*/} + ${RENEW_SH##*/})"
if [ -f "${RENEW_REQ}" ] && [ -f "${RENEW_SH}" ]; then
    log_message "nginx TLS renewal: running renew-nginx-tls-from-vault.sh (both request flag and script present)..."
    if bash "${RENEW_SH}" >>"${logfile}" 2>&1; then
        log_message "nginx TLS renewal: renew-nginx-tls-from-vault.sh completed successfully; removing drop-ins"
        rm -f "${RENEW_REQ}" "${RENEW_SH}"
    else
        log_message "nginx TLS renewal: renew-nginx-tls-from-vault.sh failed (script output above in ${logfile}); leaving ${RENEW_REQ} for retry"
    fi
elif [ -f "${RENEW_REQ}" ] && [ ! -f "${RENEW_SH}" ]; then
    log_message "nginx TLS renewal: skipped — ${RENEW_REQ##*/} present but ${RENEW_SH##*/} missing on share (prelim/labstartup must copy the script)"
elif [ ! -f "${RENEW_REQ}" ] && [ -f "${RENEW_SH}" ]; then
    # labstartup.sh deploys the script to NFS but does not create the request flag; prelim
    # adds the flag when near expiry — if only the script is present, run renewal anyway
    # (idempotent) and remove the script so we do not retry every watcher interval forever.
    log_message "nginx TLS renewal: ${RENEW_SH##*/} on share without ${RENEW_REQ##*/} (labstartup script-only drop, or flag already consumed) — running renew-nginx-tls-from-vault.sh..."
    if bash "${RENEW_SH}" >>"${logfile}" 2>&1; then
        log_message "nginx TLS renewal: script-only path completed; removing ${RENEW_SH##*/} from share"
        rm -f "${RENEW_SH}"
    else
        log_message "nginx TLS renewal: script-only run failed (output above); leaving ${RENEW_SH##*/} for retry"
    fi
else
    log_message "nginx TLS renewal: nothing to do — no ${RENEW_REQ##*/} and no ${RENEW_SH##*/} on ${WATCH_DIR}"
fi
###############################################################################
# End doupdate.sh
###############################################################################
