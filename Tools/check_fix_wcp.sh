#!/bin/bash
# Author: Burke Azbill
# Version: 3.0
# Date: 2026-02-05
# Script to fix Kubernetes certificates and webhooks on Supervisor Control Plane
# This script:
# 1. Calls check_wcp_vcenter.sh to verify vCenter services are running
# 2. SSH to vCenter and run decryptK8Pwd.py to get SCP credentials
# 3. Verify Supervisor Control Plane is accessible (with fallback to VM IP)
# 4. Check hypercrypt and kubelet services on SCP VM
# 5. Delete old certificates and restart webhooks
# 6. Scale up CCI, ArgoCD, and Harbor services
#
# Usage: ./check_fix_wcp.sh [vcenter_host]
# Example: ./check_fix_wcp.sh vc-wld01-a.site-a.vcf.lab
# If no parameter is provided, it will use vc-wld01-a.site-a.vcf.lab
#
# Exit Codes:
#   0 - Success
#   1 - General error
#   2 - Supervisor Control Plane not running (hypercrypt/encryption issue)
#   3 - Cannot connect to Supervisor
#   4 - kubectl commands failed
#   5 - vCenter service issues (from check_wcp_vcenter.sh)

# Don't exit on error - we handle errors explicitly
set +e

# Configuration
VCENTER_HOST="${1:-vc-wld01-a.site-a.vcf.lab}"
VCENTER_USER="root"
DECRYPT_CMD="/usr/lib/vmware-wcp/decryptK8Pwd.py"
CREDS_FILE="/home/holuser/creds.txt"
LOG_FILE="/lmchol/hol/labstartup.log"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"

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

# Function to check if Kubernetes API is accessible
check_k8s_api() {
    local ip=$1
    local pwd=$2
    
    local result
    result=$(/usr/bin/sshpass -p "${pwd}" ssh ${SSH_OPTS} "root@${ip}" "kubectl get --raw /healthz 2>&1" 2>/dev/null)
    if [[ "${result}" == "ok" ]]; then
        return 0
    fi
    return 1
}

# Function to check hypercrypt status on SCP VM
check_hypercrypt_status() {
    local ip=$1
    local pwd=$2
    
    local status
    status=$(/usr/bin/sshpass -p "${pwd}" ssh ${SSH_OPTS} "root@${ip}" "systemctl is-active hypercrypt 2>/dev/null" 2>/dev/null)
    echo "${status}"
}

# Function to check if kubelet is running on SCP
check_kubelet_status() {
    local ip=$1
    local pwd=$2
    
    local status
    status=$(/usr/bin/sshpass -p "${pwd}" ssh ${SSH_OPTS} "root@${ip}" "systemctl is-active kubelet 2>/dev/null" 2>/dev/null)
    echo "${status}"
}

log_msg "=========================================="
log_msg "WCP Webhook Fix Script v3.0"
log_msg "=========================================="
log_msg "vCenter Host: ${VCENTER_HOST}"

# NOTE: vCenter services check (check_wcp_vcenter.sh) should be run separately
# before this script. VCFfinal.py calls check_wcp_vcenter.sh before starting
# Supervisor Control Plane VMs, then calls this script after VMs are started.

#==========================================================================
# Step 1: Get credentials from vCenter
#==========================================================================

# Pre-flight check: Verify vCenter is reachable
if ! check_host_reachable "${VCENTER_HOST}"; then
    log_error "Cannot reach vCenter at ${VCENTER_HOST}"
    exit 1
fi

log_msg "Connecting to vCenter to retrieve SCP credentials..."

# SSH to vCenter and capture the output
DECRYPT_OUTPUT=$(ssh_with_fallback "${VCENTER_USER}" "${VCENTER_HOST}" "${DECRYPT_CMD}" 2>&1)
DECRYPT_EXIT=$?

if [[ ${DECRYPT_EXIT} -ne 0 ]]; then
    log_error "Failed to run decryptK8Pwd.py on vCenter"
    log_error "Output: ${DECRYPT_OUTPUT}"
    exit 1
fi

# Parse the output to extract IP and Password
nodeIP=$(echo "${DECRYPT_OUTPUT}" | grep "^IP:" | awk '{print $2}')
nodePwd=$(echo "${DECRYPT_OUTPUT}" | grep "^PWD:" | awk '{print $2}')

# Validate that we got the values
if [[ -z "${nodeIP}" ]]; then
    log_error "Failed to extract IP from vCenter output"
    log_error "Raw output: ${DECRYPT_OUTPUT}"
    exit 1
fi

if [[ -z "${nodePwd}" ]]; then
    log_error "Failed to extract PWD from vCenter output"
    exit 1
fi

log_msg "Successfully extracted credentials from vCenter"
log_msg "Node IP (VIP): ${nodeIP}"
log_msg "Password retrieved: $(echo "${nodePwd}" | sed 's/./*/g')"

#==========================================================================
# Step 2: Verify Supervisor Control Plane is accessible
#==========================================================================

ACTUAL_IP="${nodeIP}"
if ! check_host_reachable "${nodeIP}"; then
    log_warn "Supervisor VIP ${nodeIP} is not reachable"
    log_msg "Attempting to find actual SCP VM IP..."
    
    # Try common alternative IPs (VIP +1)
    # In vSphere with Tanzu, VMs typically get .86, .87, .88 for a VIP of .85
    BASE_IP=$(echo "${nodeIP}" | cut -d. -f1-3)
    VIP_LAST=$(echo "${nodeIP}" | cut -d. -f4)
    
    for offset in 1 2 3; do
        ALT_IP="${BASE_IP}.$((VIP_LAST + offset))"
        log_msg "Trying alternative IP: ${ALT_IP}"
        if check_host_reachable "${ALT_IP}"; then
            # Verify this is actually the SCP VM by checking for expected services
            HYPERCRYPT_STATUS=$(check_hypercrypt_status "${ALT_IP}" "${nodePwd}")
            if [[ -n "${HYPERCRYPT_STATUS}" ]]; then
                log_msg "Found SCP VM at ${ALT_IP}"
                ACTUAL_IP="${ALT_IP}"
                break
            fi
        fi
    done
fi

# Check if we can reach the SCP VM now
if ! check_host_reachable "${ACTUAL_IP}"; then
    log_error "Cannot reach Supervisor Control Plane at ${ACTUAL_IP}"
    exit 3
fi

#==========================================================================
# Step 3: Check hypercrypt and kubelet services
#==========================================================================

HYPERCRYPT_STATUS=$(check_hypercrypt_status "${ACTUAL_IP}" "${nodePwd}")
log_msg "Hypercrypt service status: ${HYPERCRYPT_STATUS}"

if [[ "${HYPERCRYPT_STATUS}" == "activating" ]]; then
    log_error "Supervisor Control Plane is stuck in hypercrypt initialization"
    log_error "This usually indicates that the encryption keys were not delivered to the SCP VM"
    log_error "The secrets in /dev/shm/secret are missing - this is a known issue after hibernation/reboot"
    log_error ""
    log_error "MANUAL FIX REQUIRED:"
    log_error "1. Power cycle the Supervisor Control Plane VM through vCenter UI"
    log_error "2. Wait for hypercrypt to receive encryption keys from ESXi"
    log_error "3. If issue persists, contact VMware Support or check KB articles for WCP encryption key delivery"
    log_error ""
    log_error "Alternative: If this is a lab environment, you may need to re-enable the Supervisor cluster"
    exit 2
fi

# Check kubelet status
KUBELET_STATUS=$(check_kubelet_status "${ACTUAL_IP}" "${nodePwd}")
log_msg "Kubelet service status: ${KUBELET_STATUS}"

if [[ "${KUBELET_STATUS}" != "active" ]]; then
    log_warn "Kubelet is not running on ${ACTUAL_IP}. Attempting to start..."
    /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${ACTUAL_IP}" "systemctl start kubelet" 2>/dev/null
    sleep 30
    
    KUBELET_STATUS=$(check_kubelet_status "${ACTUAL_IP}" "${nodePwd}")
    if [[ "${KUBELET_STATUS}" != "active" ]]; then
        log_error "Failed to start kubelet on Supervisor Control Plane"
        exit 2
    fi
    log_msg "Kubelet started successfully"
fi

#==========================================================================
# Step 4: Wait for Kubernetes API
#==========================================================================

log_msg "Waiting for Kubernetes API to become available..."
MAX_RETRIES=30
RETRY_COUNT=0
while ! check_k8s_api "${ACTUAL_IP}" "${nodePwd}"; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [[ ${RETRY_COUNT} -ge ${MAX_RETRIES} ]]; then
        log_error "Kubernetes API did not become available after ${MAX_RETRIES} attempts"
        exit 3
    fi
    log_msg "Waiting for API server... (attempt ${RETRY_COUNT}/${MAX_RETRIES})"
    sleep 10
done
log_msg "Kubernetes API is available"

# Use ACTUAL_IP from now on (might be VIP or direct VM IP)
nodeIP="${ACTUAL_IP}"

#==========================================================================
# Step 5: Delete old certificates and restart webhooks
#==========================================================================

# The following must be restarted for Workload Supervisor cluster when the certificates have expired
# The restart triggers the regeneration of the certificates, allowing for VM creation in the lab

log_msg "=========================================="
log_msg "Deleting storage-quota-root-ca-secret..."
log_msg "=========================================="
if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n vmware-system-cert-manager delete secret storage-quota-root-ca-secret --ignore-not-found=true" >> "${LOG_FILE}" 2>&1; then
    log_msg "Deleted storage-quota-root-ca-secret"
else
    log_warn "Could not delete storage-quota-root-ca-secret (may not exist)"
fi

log_msg "=========================================="
log_msg "Deleting storage-quota-webhook-server-internal-cert..."
log_msg "=========================================="
if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n kube-system delete secret storage-quota-webhook-server-internal-cert --ignore-not-found=true" >> "${LOG_FILE}" 2>&1; then
    log_msg "Deleted storage-quota-webhook-server-internal-cert"
else
    log_warn "Could not delete storage-quota-webhook-server-internal-cert (may not exist)"
fi

log_msg "=========================================="
log_msg "Deleting cns-storage-quota-extension-cert..."
log_msg "=========================================="
if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n kube-system delete secret cns-storage-quota-extension-cert --ignore-not-found=true" >> "${LOG_FILE}" 2>&1; then
    log_msg "Deleted cns-storage-quota-extension-cert"
else
    log_warn "Could not delete cns-storage-quota-extension-cert (may not exist)"
fi

log_msg "=========================================="
log_msg "Restarting cns-storage-quota-extension..."
log_msg "=========================================="
if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n kube-system rollout restart deploy cns-storage-quota-extension 2>/dev/null || echo 'Deployment not found'" >> "${LOG_FILE}" 2>&1; then
    log_msg "Restarted cns-storage-quota-extension deployment"
fi

log_msg "=========================================="
log_msg "Restarting storage-quota-webhook..."
log_msg "=========================================="
if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n kube-system rollout restart deploy storage-quota-webhook 2>/dev/null || echo 'Deployment not found'" >> "${LOG_FILE}" 2>&1; then
    log_msg "Restarted storage-quota-webhook deployment"
fi

log_msg "Waiting 20 seconds for deployments to restart..."
sleep 20

#==========================================================================
# Step 6: Scale up services
#==========================================================================

log_msg "=========================================="
log_msg "Scaling cci replicas back up to 1..."
log_msg "=========================================="
CCI_NS=$(/usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl get ns --no-headers | grep 'svc-cci-ns' | awk '{print \$1}'" 2>/dev/null)
if [[ -n "${CCI_NS}" ]]; then
    /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n ${CCI_NS} scale deployment --all --replicas=1" >> "${LOG_FILE}" 2>&1
    log_msg "Scaled CCI deployments in ${CCI_NS}"
else
    log_msg "CCI namespace not found - skipping"
fi

log_msg "=========================================="
log_msg "Scaling argocd replicas back up to 1..."
log_msg "=========================================="
if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl get ns argocd >/dev/null 2>&1"; then
    /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n argocd scale deployment --all --replicas=1" >> "${LOG_FILE}" 2>&1
    log_msg "Scaled ArgoCD deployments"
else
    log_msg "ArgoCD namespace not found - skipping"
fi

log_msg "=========================================="
log_msg "Scaling Harbor replicas back up to 1..."
log_msg "=========================================="
HARBOR_NS=$(/usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl get ns --no-headers | grep 'svc-harbor' | awk '{print \$1}'" 2>/dev/null)
if [[ -n "${HARBOR_NS}" ]]; then
    /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n ${HARBOR_NS} scale sts --all --replicas=1" >> "${LOG_FILE}" 2>&1
    /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" "kubectl -n ${HARBOR_NS} scale deployment --all --replicas=1" >> "${LOG_FILE}" 2>&1
    log_msg "Scaled Harbor deployments in ${HARBOR_NS}"
else
    log_msg "Harbor namespace not found - skipping"
fi

log_msg ""
log_msg "=========================================="
log_msg "✓ Successfully completed certificate resets and webhook restarts"
log_msg "=========================================="

exit 0
