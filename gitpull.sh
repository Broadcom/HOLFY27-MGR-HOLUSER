#!/bin/bash
# gitpull.sh - HOLFY27 Manager holuser Git Pull Script
# Version 3.2 - February 2026
# Author - Burke Azbill and HOL Core Team
# Executed by holuser cron at boot to pull Core Team repository updates
#
# Proxy Check Strategy:
#   The proxy (squid on holorouter) must be listening on port 3128 before
#   we can do git pull through it. We check TCP port 3128 (not curl through
#   the proxy to an external site) because:
#     - getrules.sh on the router restarts squid during its boot sequence
#     - The router's getrules.sh waits for our "gitdone" signal before
#       applying final iptables/squid config, creating a dependency cycle
#       if we test external connectivity through the proxy
#     - TCP port check confirms squid is listening and ready for connections
#
#   If the proxy port is not available, we attempt remediation by restarting
#   squid on the router via SSH. If the proxy remains unavailable after all
#   attempts, we FAIL the lab startup (writing to startup_status.txt and
#   the HTML status dashboard).

# Source environment
. /home/holuser/.bashrc

LOGFILE="/tmp/gitpull-holuser.log"
HOLROOT="/home/holuser/hol"
HOLOROUTER_DIR="/tmp/holorouter"
PROXY_HOST="proxy.site-a.vcf.lab"
PROXY_PORT=3128
ROUTER_HOST="10.1.10.129"
STARTUP_STATUS="/lmchol/hol/startup_status.txt"
PASSWORD_FILE="/home/holuser/creds.txt"

# Create holorouter NFS export directory EARLY in boot process
# This directory is exported via NFS and must exist before nfs-server.service
# runs exportfs. Creating it here (before any network waits) ensures it exists.
mkdir -p "${HOLOROUTER_DIR}"
chmod 755 "${HOLOROUTER_DIR}"

log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> ${LOGFILE}
}

# Write failure status to startup_status.txt and HTML dashboard
write_failure_status() {
    local reason="$1"
    log_message "FATAL: ${reason}"

    # Write to startup_status.txt (may not be available if NFS not mounted yet)
    if [ -d "/lmchol/hol" ]; then
        echo "FAIL - ${reason}" > "${STARTUP_STATUS}"
        sync
        # Verify the write - retry if NFS is slow
        for i in 1 2 3; do
            if grep -q "FAIL" "${STARTUP_STATUS}" 2>/dev/null; then
                break
            fi
            log_message "Retrying status file write (attempt $i)..."
            sleep 1
            echo "FAIL - ${reason}" > "${STARTUP_STATUS}"
            sync
        done
    else
        log_message "WARNING: /lmchol/hol not mounted - cannot write startup_status.txt"
    fi

    # Update HTML status dashboard if the Python tool is available
    if [ -f "${HOLROOT}/Tools/status_dashboard.py" ]; then
        /usr/bin/python3 -c "
import sys
sys.path.insert(0, '${HOLROOT}/Tools')
try:
    from status_dashboard import StatusDashboard
    dashboard = StatusDashboard('STARTUP')
    dashboard.set_failed('${reason}')
    dashboard.generate_html()
except Exception as e:
    print(f'Dashboard update failed: {e}')
" >> ${LOGFILE} 2>&1
    fi
}

# Check if proxy (squid) port is listening
# Uses nc (netcat) to test TCP connectivity to port 3128
check_proxy_port() {
    nc -z -w3 "${PROXY_HOST}" "${PROXY_PORT}" > /dev/null 2>&1
    return $?
}

# Attempt to remediate proxy by restarting squid on the router via SSH
# This handles the case where squid failed to start or crashed during boot
remediate_proxy() {
    log_message "Attempting proxy remediation: restarting squid on router..."
    local password=""
    if [ -f "${PASSWORD_FILE}" ]; then
        password=$(cat "${PASSWORD_FILE}")
    else
        log_message "WARNING: Password file not found, cannot SSH to router"
        return 1
    fi

    # Try restarting squid via SSH to the router
    local result
    result=$(sshpass -p "${password}" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 "root@${ROUTER_HOST}" \
        'systemctl restart squid 2>&1 && echo "SQUID_RESTART_OK" || echo "SQUID_RESTART_FAILED"' 2>/dev/null)

    if echo "${result}" | grep -q "SQUID_RESTART_OK"; then
        log_message "Squid restart command succeeded on router"
        # Give squid a moment to fully start
        sleep 3
        return 0
    else
        log_message "Squid restart failed on router: ${result}"
        return 1
    fi
}

# Wait for proxy (squid) to be available on TCP port 3128
# Strategy:
#   Phase 1 (attempts 1-30): Wait for squid to come up naturally during boot
#   Phase 2 (attempt 31): Attempt remediation by restarting squid via SSH
#   Phase 3 (attempts 31-60): Continue waiting after remediation
#   If still unavailable after 60 attempts: FAIL the startup
wait_for_proxy() {
    local max_attempts=60
    local remediation_attempt=31
    local attempt=1
    local remediated=false

    while [ $attempt -le $max_attempts ]; do
        if check_proxy_port; then
            log_message "Proxy is available (squid listening on ${PROXY_HOST}:${PROXY_PORT})"
            return 0
        fi

        # At the remediation point, try to restart squid on the router
        if [ $attempt -eq $remediation_attempt ] && [ "$remediated" = "false" ]; then
            log_message "Proxy not available after $((attempt - 1)) attempts - attempting remediation"
            if remediate_proxy; then
                remediated=true
                # Check immediately after remediation
                if check_proxy_port; then
                    log_message "Proxy is available after remediation"
                    return 0
                fi
            fi
        fi

        log_message "Waiting for proxy (attempt ${attempt}/${max_attempts})..."
        sleep 5
        attempt=$((attempt + 1))
    done

    log_message "ERROR: Proxy not available after ${max_attempts} attempts (5 minutes)"
    return 1
}

# Perform git pull
do_git_pull() {
    cd "${HOLROOT}" || exit 1

    # Determine branch based on cloud environment
    cloud=$(/usr/bin/vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>&1)
    holdev=$(echo "${cloud}" | grep -i hol-dev)

    if [ "${cloud}" = "No value found" ] || [ -n "${holdev}" ]; then
        branch="dev"
    else
        branch="main"
    fi

    log_message "Using branch: ${branch}"

    # Stash local changes in production
    if [ "${branch}" = "main" ]; then
        log_message "Stashing local changes for production"
        git stash >> ${LOGFILE} 2>&1
    fi

    # Perform pull
    git checkout ${branch} >> ${LOGFILE} 2>&1
    GIT_TERMINAL_PROMPT=0 git pull origin ${branch} >> ${LOGFILE} 2>&1

    if [ $? -eq 0 ]; then
        log_message "Git pull successful"
    else
        log_message "Git pull failed - continuing with existing code"
    fi
}

log_message "Starting holuser gitpull.sh"

# Check for offline mode (set by offline-ready.py for partner lab exports)
# In offline mode, skip all network operations and signal router immediately
if [ -f "${HOLROOT}/.offline-mode" ] || [ -f "/lmchol/hol/offline-mode" ]; then
    log_message "OFFLINE MODE: Skipping proxy wait and git pull"
    mkdir -p "${HOLOROUTER_DIR}"
    touch "${HOLOROUTER_DIR}/gitdone"
    log_message "holuser gitpull.sh completed (offline mode)"
    exit 0
fi

# Check for testing mode - skip git pull to preserve local changes
# Note: /lmchol may not be mounted yet (mount.sh runs after gitpull.sh),
# so we check the local path first, then the NFS path as a fallback.
TESTING_FLAG="${HOLROOT}/testing"
if [ -f "${TESTING_FLAG}" ] || [ -f "/lmchol/hol/testing" ]; then
    log_message "TESTING MODE: Skipping git pull to preserve local changes"
    log_message "*** Delete ${TESTING_FLAG} before capturing to catalog! ***"
    mkdir -p "${HOLOROUTER_DIR}"
    touch "${HOLOROUTER_DIR}/gitdone"
    log_message "holuser gitpull.sh completed (testing mode)"
    exit 0
fi

# Wait for proxy before git operations
if ! wait_for_proxy; then
    # Proxy is not available after all attempts including remediation.
    # FAIL the lab startup - write status to startup_status.txt and dashboard.
    write_failure_status "Proxy Unavailable"
    # Still signal gitdone so the router doesn't hang forever waiting,
    # and still attempt git pull (it may work without proxy in some environments)
    log_message "Continuing with git pull attempt despite proxy failure..."
fi

# Perform git pull
if [ -d "${HOLROOT}/.git" ]; then
    do_git_pull
else
    log_message "No git repository found at ${HOLROOT}"
fi
# The holorouter waits for the gitdone file to be created in /tmp/holorouter (mounted on holorouter as /mnt/manager/gitdone)
# Ensure the directory exists (should already exist from early in this script, but be safe)
mkdir -p "${HOLOROUTER_DIR}"
touch "${HOLOROUTER_DIR}/gitdone"
log_message "holuser gitpull.sh completed"
