#!/bin/bash
# proxyfilteroff.sh - HOLFY27 Disable Proxy Filter (Development Only)
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Places a file in NFS share to signal router to disable proxy filtering
# Only works in dev cloud environments

#==============================================================================
# CONFIGURATION
#==============================================================================

HOLOROUTER_DIR="/tmp/holorouter"
DISABLE_PROXY_FILE="${HOLOROUTER_DIR}/disable-proxy-filter"

#==============================================================================
# FUNCTIONS
#==============================================================================

log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
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

# Create the disable-proxy-filter flag file
log_message "Sending proxy filter disable request to router..."
echo "Proxy filter disable requested at $(date)" > "${DISABLE_PROXY_FILE}"
echo "Requested by: $(whoami)@$(hostname)" >> "${DISABLE_PROXY_FILE}"

if [ -f "${DISABLE_PROXY_FILE}" ]; then
    log_message "SUCCESS: Proxy filter disable request sent."
    log_message "The router watcher will process this on its next cycle. (every 5 seconds)"
    log_message ""
    log_message "NOTE: The proxy filter will be re-enabled on router reboot."
    log_message "      To re-enable manually, run: proxyfilteron.sh"
    log_message ""
    log_message "IMPORTANT: You may need to wait a few seconds for the change to take effect."
    log_message "           The router watcher runs every 5 seconds"
else
    log_message "ERROR: Failed to create proxy filter disable request file."
    exit 1
fi

# Remind about local proxy settings
log_message ""
log_message "REMINDER: You may also need to configure your local environment:"
log_message "  - For CLI: source ~/.bashrc (proxy is pre-configured)"
log_message "  - For browsers: Ensure proxy is set to proxy.site-a.vcf.lab:3128"
log_message "  - To bypass proxy entirely on your console: . ~/noproxy.sh"
