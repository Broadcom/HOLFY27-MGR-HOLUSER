#!/bin/bash
# Author: Burke Azbill
# Version: 2.0
# Date: 2026-02-06
# Script to check and fix critical vCenter services for Workload Control Plane (WCP)
# This script:
# 1. Verifies vCenter is reachable
# 2. Checks and starts vAPI endpoint service
# 3. Checks and starts trustmanagement service (critical for SCP encryption key delivery)
# 4. Checks and starts WCP service
# 5. Verifies Supervisor config_status is RUNNING via vCenter REST API (with polling)
#
# Usage: ./check_wcp_vcenter.sh [vcenter_host] [sso_domain]
# Example: ./check_wcp_vcenter.sh vc-wld01-a.site-a.vcf.lab wld.sso
# If no parameter is provided, it will use vc-wld01-a.site-a.vcf.lab / wld.sso
#
# Exit Codes:
#   0 - Success (all services running, Supervisor RUNNING)
#   1 - General error (vCenter not reachable)
#   5 - vCenter service issues (could not start required services)
#   6 - Supervisor did not reach RUNNING state within timeout

# Don't exit on error - we handle errors explicitly
set +e

# Configuration
VCENTER_HOST="${1:-vc-wld01-a.site-a.vcf.lab}"
SSO_DOMAIN="${2:-wld.sso}"
VCENTER_USER="root"
CREDS_FILE="/home/holuser/creds.txt"
LOG_FILE="/lmchol/hol/labstartup.log"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

# Polling configuration
POLL_INTERVAL=30      # seconds between polls
MAX_POLL_TIME=1800    # 30 minutes maximum wait

# Ensure log directory exists
mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null

# Helper function for logging
log_msg() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${timestamp}] $1" | tee -a "${LOG_FILE}"
}

log_error() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${timestamp}] ERROR: $1" | tee -a "${LOG_FILE}" >&2
}

log_warn() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${timestamp}] WARNING: $1" | tee -a "${LOG_FILE}"
}

# Helper function to execute SSH with fallback to sshpass
ssh_with_fallback() {
    local user=$1
    local host=$2
    shift 2
    local cmd="$*"

    # Try key-based authentication first
    if ssh ${SSH_OPTS} -o BatchMode=yes "${user}@${host}" "${cmd}" 2>/dev/null; then
        return 0
    fi

    # Fall back to sshpass if key auth fails
    if [[ -f "${CREDS_FILE}" ]]; then
        local password
        password=$(cat "${CREDS_FILE}")
        /usr/bin/sshpass -p "${password}" ssh ${SSH_OPTS} "${user}@${host}" "${cmd}"
    else
        log_error "Key-based authentication failed and credentials file not found at ${CREDS_FILE}"
        return 1
    fi
}

# Function to check if a host is reachable
check_host_reachable() {
    local host=$1
    if ping -c 1 -W 2 "${host}" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Function to check vCenter service using vmon-cli
# Returns just the RunState value (STARTED, STOPPED, etc.)
check_vcenter_service() {
    local vc_host=$1
    local service_name=$2

    local status
    # Use grep + head + sed to extract exactly the RunState value from the first matching line
    status=$(ssh_with_fallback "${VCENTER_USER}" "${vc_host}" "vmon-cli -s ${service_name} 2>/dev/null | grep 'RunState:' | head -1 | sed 's/.*RunState: //'" 2>/dev/null)
    echo "${status}"
}

# Function to start vCenter service using vmon-cli
start_vcenter_service() {
    local vc_host=$1
    local service_name=$2

    log_msg "Starting ${service_name} service on vCenter..."
    ssh_with_fallback "${VCENTER_USER}" "${vc_host}" "vmon-cli -i ${service_name}" 2>/dev/null
    sleep 15

    local status
    status=$(check_vcenter_service "${vc_host}" "${service_name}")
    echo "${status}"
}

# Function to check Supervisor status via vCenter REST API
# Returns: RUNNING, CONFIGURING, ERROR, or empty string on failure
check_supervisor_status() {
    local vc_host=$1
    local sso_domain=$2

    # Read password
    local password
    if [[ -f "${CREDS_FILE}" ]]; then
        password=$(cat "${CREDS_FILE}")
    else
        echo ""
        return 1
    fi

    # Get API session token
    local session
    session=$(curl -sk -X POST "https://${vc_host}/api/session" \
        -H "Content-Type: application/json" \
        -u "administrator@${sso_domain}:${password}" 2>/dev/null | tr -d '"')

    if [[ -z "${session}" ]]; then
        echo ""
        return 1
    fi

    # Get supervisor clusters
    local response
    response=$(curl -sk "https://${vc_host}/api/vcenter/namespace-management/clusters" \
        -H "vmware-api-session-id: ${session}" 2>/dev/null)

    # Extract config_status and kubernetes_status from first cluster
    local config_status
    local k8s_status
    config_status=$(echo "${response}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and len(data) > 0:
        print(data[0].get('config_status', ''))
    else:
        print('')
except:
    print('')
" 2>/dev/null)

    k8s_status=$(echo "${response}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and len(data) > 0:
        print(data[0].get('kubernetes_status', ''))
    else:
        print('')
except:
    print('')
" 2>/dev/null)

    # Delete session
    curl -sk -X DELETE "https://${vc_host}/api/session" \
        -H "vmware-api-session-id: ${session}" 2>/dev/null >/dev/null

    # Return combined status
    echo "${config_status}:${k8s_status}"
}

log_msg "=========================================="
log_msg "WCP vCenter Services Check Script v2.0"
log_msg "=========================================="
log_msg "vCenter Host: ${VCENTER_HOST}"
log_msg "SSO Domain: ${SSO_DOMAIN}"
log_msg "Max wait time: $((MAX_POLL_TIME / 60)) minutes"

# Pre-flight check: Verify vCenter is reachable
if ! check_host_reachable "${VCENTER_HOST}"; then
    log_error "Cannot reach vCenter at ${VCENTER_HOST}"
    exit 1
fi

log_msg "Checking critical Workload Control Plane vCenter services..."

###### Service check/fix loop ######
# Check each critical service and attempt to start if not running

ALL_SERVICES_OK=true

for service in vapi-endpoint trustmanagement wcp; do
    log_msg "Checking ${service} service..."
    SVC_STATUS=$(check_vcenter_service "${VCENTER_HOST}" "${service}")

    if [[ "${SVC_STATUS}" == "STARTED" ]]; then
        log_msg "  ${service}: RUNNING"
    else
        log_warn "  ${service}: NOT RUNNING (status: ${SVC_STATUS})"
        log_msg "  Attempting to start ${service}..."
        NEW_STATUS=$(start_vcenter_service "${VCENTER_HOST}" "${service}")
        if [[ "${NEW_STATUS}" == "STARTED" ]]; then
            log_msg "  ${service}: Started successfully"
        else
            log_error "  ${service}: Failed to start (status: ${NEW_STATUS})"
            if [[ "${service}" == "trustmanagement" ]]; then
                log_warn "  NOTE: trustmanagement is critical for Supervisor encryption key delivery"
            fi
            ALL_SERVICES_OK=false
        fi
    fi
done

if [[ "${ALL_SERVICES_OK}" != "true" ]]; then
    log_error "One or more critical vCenter services could not be started"
    exit 5
fi

log_msg "All vCenter WCP services are running"

###### Supervisor Status Verification ######
# Poll the vCenter REST API to verify the Supervisor config_status reaches RUNNING
# The Supervisor may take time to initialize after services are started

log_msg "=========================================="
log_msg "Verifying Supervisor cluster status via vCenter API..."
log_msg "=========================================="

ELAPSED=0
SUPERVISOR_OK=false

while [[ ${ELAPSED} -lt ${MAX_POLL_TIME} ]]; do
    RESULT=$(check_supervisor_status "${VCENTER_HOST}" "${SSO_DOMAIN}")
    CONFIG_STATUS=$(echo "${RESULT}" | cut -d: -f1)
    K8S_STATUS=$(echo "${RESULT}" | cut -d: -f2)

    if [[ -z "${CONFIG_STATUS}" ]]; then
        log_warn "  Could not query Supervisor API (may still be initializing) - elapsed ${ELAPSED}s"
    elif [[ "${CONFIG_STATUS}" == "RUNNING" && "${K8S_STATUS}" == "READY" ]]; then
        log_msg "  Supervisor config_status: ${CONFIG_STATUS}, kubernetes_status: ${K8S_STATUS}"
        SUPERVISOR_OK=true
        break
    elif [[ "${CONFIG_STATUS}" == "RUNNING" ]]; then
        log_msg "  Supervisor config_status: RUNNING, kubernetes_status: ${K8S_STATUS} (waiting for READY)"
    elif [[ "${CONFIG_STATUS}" == "ERROR" ]]; then
        log_error "  Supervisor config_status: ERROR - check vCenter Supervisor Management UI"
        break
    else
        log_msg "  Supervisor config_status: ${CONFIG_STATUS}, kubernetes_status: ${K8S_STATUS} - waiting... (${ELAPSED}s / ${MAX_POLL_TIME}s)"
    fi

    sleep ${POLL_INTERVAL}
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

if [[ "${SUPERVISOR_OK}" == "true" ]]; then
    log_msg ""
    log_msg "=========================================="
    log_msg "All critical vCenter WCP services are running"
    log_msg "Supervisor cluster is RUNNING and Kubernetes is READY"
    log_msg "=========================================="
    exit 0
else
    if [[ ${ELAPSED} -ge ${MAX_POLL_TIME} ]]; then
        log_error "Supervisor did not reach RUNNING/READY state within $((MAX_POLL_TIME / 60)) minutes"
    fi
    log_error "Supervisor status: config=${CONFIG_STATUS:-unknown}, k8s=${K8S_STATUS:-unknown}"
    exit 6
fi
