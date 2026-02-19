#!/bin/bash
# fwoff.sh - HOLFY27 Disable Firewall (Development Only)
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Places a file in NFS share to signal router to disable firewall
# Only works in dev cloud environments

#==============================================================================
# CONFIGURATION
#==============================================================================

HOLOROUTER_DIR="/tmp/holorouter"
DISABLE_FW_FILE="${HOLOROUTER_DIR}/disable-firewall"

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
    # Check if we're in a dev cloud environment
    cloud=$(/usr/bin/vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>&1)
    
    if [[ "$cloud" == "No value found" ]]; then
        # No OVF properties = development environment
        return 0
    fi
    
    if [[ "$cloud" =~ "hol-dev" ]] || [[ "$cloud" =~ "HOL-Dev" ]]; then
        # In HOL-Dev cloud
        return 0
    fi
    
    # Production cloud - not allowed
    return 1
}

#==============================================================================
# MAIN
#==============================================================================

log_message "Checking cloud environment..."

if ! check_dev_cloud; then
    log_message "ERROR: Not in dev cloud. Firewall control is disabled in production."
    log_message "This script only works in HOL-Dev or development environments."
    exit 1
fi

log_message "Dev environment confirmed."

# Ensure holorouter directory exists
if [ ! -d "${HOLOROUTER_DIR}" ]; then
    log_message "Creating holorouter directory..."
    mkdir -p "${HOLOROUTER_DIR}"
    chmod 775 "${HOLOROUTER_DIR}"
fi

# Create the disable-firewall flag file
log_message "Sending firewall disable request to router..."
echo "Firewall disable requested at [$(_log_ts)]" > "${DISABLE_FW_FILE}"
echo "Requested by: $(whoami)@$(hostname)" >> "${DISABLE_FW_FILE}"

if [ -f "${DISABLE_FW_FILE}" ]; then
    log_message "SUCCESS: Firewall disable request sent."
    log_message "The router watcher will process this on its next cycle."
    log_message ""
    log_message "NOTE: The firewall will be re-enabled on router reboot."
    log_message "      To re-enable manually, run: fwon.sh"
else
    log_message "ERROR: Failed to create firewall disable request file."
    exit 1
fi

# Also remind user about proxy
log_message ""
log_message "REMINDER: You may also need to disable proxy settings:"
log_message "  - Run: proxyfilteroff.sh"
log_message "  - Or source: . ~/noproxy.sh"
