#!/bin/bash
# Author: Burke Azbill
# Version: 1.0
# Date: 2026-02-05
# Script to check and fix critical vCenter services for Workload Control Plane (WCP)
# This script:
# 1. Verifies vCenter is reachable
# 2. Checks and starts vAPI endpoint service
# 3. Checks and starts trustmanagement service (critical for SCP encryption key delivery)
# 4. Checks and starts WCP service
#
# Usage: ./check_wcp_vcenter.sh [vcenter_host]
# Example: ./check_wcp_vcenter.sh vc-wld01-a.site-a.vcf.lab
# If no parameter is provided, it will use vc-wld01-a.site-a.vcf.lab
#
# Exit Codes:
#   0 - Success (all services running)
#   1 - General error (vCenter not reachable)
#   5 - vCenter service issues (could not start required services)

# Don't exit on error - we handle errors explicitly
set +e

# Configuration
VCENTER_HOST="${1:-vc-wld01-a.site-a.vcf.lab}"
VCENTER_USER="root"
CREDS_FILE="/home/holuser/creds.txt"
LOG_FILE="/lmchol/hol/labstartup.log"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

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
    local port=${2:-22}
    
    if ping -c 1 -W 2 "${host}" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Function to check vCenter service using vmon-cli
check_vcenter_service() {
    local vc_host=$1
    local service_name=$2
    
    local status
    status=$(ssh_with_fallback "${VCENTER_USER}" "${vc_host}" "vmon-cli -s ${service_name} 2>/dev/null | grep 'RunState:' | awk '{print \$2}'" 2>/dev/null)
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

log_msg "=========================================="
log_msg "WCP vCenter Services Check Script v1.0"
log_msg "=========================================="
log_msg "vCenter Host: ${VCENTER_HOST}"

# Pre-flight check: Verify vCenter is reachable
if ! check_host_reachable "${VCENTER_HOST}"; then
    log_error "Cannot reach vCenter at ${VCENTER_HOST}"
    exit 1
fi

log_msg "Checking critical Workload Control Plane vCenter services..."

###### vCenter vAPI Endpoint check/fix ######

log_msg "Checking vCenter vAPI endpoint service..."
VAPI_STATUS=$(check_vcenter_service "${VCENTER_HOST}" "vapi-endpoint")
if [ "${VAPI_STATUS}" != "STARTED" ]; then
    log_msg "vCenter vAPI endpoint is not running - attempting to start..."
    VAPI_SVC_STATUS=$(start_vcenter_service "${VCENTER_HOST}" "vapi-endpoint")
    if [ "${VAPI_SVC_STATUS}" == "STARTED" ]; then
        log_msg "vCenter vAPI endpoint started successfully"
    else
        log_warn "WARNING: vCenter vAPI endpoint may still have issues - status: ${VAPI_SVC_STATUS}"
    fi
else
    log_msg "vCenter vAPI endpoint is running (status: ${VAPI_STATUS})"
fi

###### trustmanagement service check/fix ######
# This service is critical for encryption key delivery to Supervisor Control Plane VMs

TRUST_STATUS=$(check_vcenter_service "${VCENTER_HOST}" "trustmanagement")
log_msg "trustmanagement service status: ${TRUST_STATUS}"
if [[ "${TRUST_STATUS}" != "STARTED" ]]; then
    log_warn "trustmanagement service is not running - this is critical for Supervisor key delivery"
    TRUST_STATUS=$(start_vcenter_service "${VCENTER_HOST}" "trustmanagement")
    if [[ "${TRUST_STATUS}" == "STARTED" ]]; then
        log_msg "trustmanagement service started successfully"
        log_msg "NOTE: If Supervisor was stuck, you may need to power cycle the SupervisorControlPlaneVM"
    else
        log_error "Failed to start trustmanagement service"
        log_error "Status: ${TRUST_STATUS}"
        exit 5
    fi
fi

###### WCP service check/fix ######

WCP_STATUS=$(check_vcenter_service "${VCENTER_HOST}" "wcp")
log_msg "wcp service status: ${WCP_STATUS}"
if [[ "${WCP_STATUS}" != "STARTED" ]]; then
    log_warn "WCP service is not running - attempting to start..."
    WCP_STATUS=$(start_vcenter_service "${VCENTER_HOST}" "wcp")
    if [[ "${WCP_STATUS}" == "STARTED" ]]; then
        log_msg "WCP service started successfully"
    else
        log_error "Failed to start WCP service"
        log_error "Status: ${WCP_STATUS}"
        exit 5
    fi
fi

log_msg ""
log_msg "=========================================="
log_msg "✓ All critical vCenter WCP services are running"
log_msg "=========================================="

exit 0
