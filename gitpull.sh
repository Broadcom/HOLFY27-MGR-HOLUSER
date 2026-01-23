#!/bin/bash
# gitpull.sh - HOLFY27 Manager holuser Git Pull Script
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Executed by holuser cron at boot to pull Core Team repository updates

# Source environment
. /home/holuser/.bashrc

LOGFILE="/tmp/gitpull-holuser.log"
HOLROOT="/home/holuser/hol"

log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> ${LOGFILE}
}

log_message "Starting holuser gitpull.sh"

# Wait for network/proxy to be available
wait_for_proxy() {
    local max_attempts=60
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s --max-time 5 -x http://proxy.site-a.vcf.lab:3128 https://github.com > /dev/null 2>&1; then
            log_message "Proxy is available"
            return 0
        fi
        log_message "Waiting for proxy (attempt ${attempt}/${max_attempts})..."
        sleep 5
        attempt=$((attempt + 1))
    done
    
    log_message "WARNING: Proxy not available after ${max_attempts} attempts"
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
    git pull origin ${branch} >> ${LOGFILE} 2>&1
    
    if [ $? -eq 0 ]; then
        log_message "Git pull successful"
    else
        log_message "Git pull failed - continuing with existing code"
    fi
}

# Wait for proxy before git operations
wait_for_proxy

# Perform git pull
if [ -d "${HOLROOT}/.git" ]; then
    do_git_pull
else
    log_message "No git repository found at ${HOLROOT}"
fi

log_message "holuser gitpull.sh completed"
