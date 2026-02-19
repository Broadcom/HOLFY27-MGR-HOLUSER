#!/bin/bash
# proxyfilteron.sh - HOLFY27 Re-enable Proxy Filter (Development Only)
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Places a file in NFS share to signal router to re-enable proxy filtering
# Only works in dev cloud environments

#==============================================================================
# CONFIGURATION
#==============================================================================

HOLOROUTER_DIR="/tmp/holorouter"
ENABLE_PROXY_FILE="${HOLOROUTER_DIR}/enable-proxy-filter"
DISABLE_PROXY_FILE="${HOLOROUTER_DIR}/disable-proxy-filter"

# Source shared logging library
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/log_functions.sh"

#==============================================================================
# FUNCTIONS
#==============================================================================

log_message() {
    log_msg "$1"
}

check_dev_cloud() {
    cloud=$(/usr/bin/vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>&1)
    
    if [[ "$cloud" == "No value found" ]]; then
        return 0
    fi
    
    if [[ "$cloud" =~ "hol-dev" ]] || [[ "$cloud" =~ "HOL-Dev" ]]; then
        return 0
    fi
    
    return 1
}

#==============================================================================
# MAIN
#==============================================================================

log_message "Checking cloud environment..."

if ! check_dev_cloud; then
    log_message "ERROR: Not in dev cloud. Proxy control is disabled in production."
    exit 1
fi

log_message "Dev environment confirmed."

# Ensure holorouter directory exists
if [ ! -d "${HOLOROUTER_DIR}" ]; then
    mkdir -p "${HOLOROUTER_DIR}"
    chmod 775 "${HOLOROUTER_DIR}"
fi

# Remove any disable request first
if [ -f "${DISABLE_PROXY_FILE}" ]; then
    log_message "Removing pending disable request..."
    rm -f "${DISABLE_PROXY_FILE}"
fi

# Create the enable-proxy-filter flag file
log_message "Sending proxy filter enable request to router..."
echo "Proxy filter enable requested at [$(_log_ts)]" > "${ENABLE_PROXY_FILE}"
echo "Requested by: $(whoami)@$(hostname)" >> "${ENABLE_PROXY_FILE}"

if [ -f "${ENABLE_PROXY_FILE}" ]; then
    log_message "SUCCESS: Proxy filter enable request sent."
    log_message "The router watcher will process this on its next cycle."
else
    log_message "ERROR: Failed to create proxy filter enable request file."
    exit 1
fi
