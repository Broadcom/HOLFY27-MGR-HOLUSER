#!/bin/bash
# Author: Burke Azbill
# Version: 4.0
# Date: 2026-02-06
# Script to fix Kubernetes certificates and webhooks on Supervisor Control Plane
# This script:
# 1. SSH to vCenter and run decryptK8Pwd.py to get SCP credentials
# 2. Wait for Supervisor Control Plane to be accessible (with polling up to 30m)
# 3. Wait for hypercrypt and kubelet services to become active (with polling)
# 4. Wait for Kubernetes API to be available (with polling)
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
#   2 - Supervisor Control Plane services did not start within timeout
#   3 - Cannot connect to Supervisor / K8s API not available within timeout
#   4 - kubectl commands failed

# Don't exit on error - we handle errors explicitly
set +e

# Parse options: --stdout-only means don't write directly to log file
# (caller handles logging, e.g. VCFfinal.py captures stdout/stderr)
STDOUT_ONLY=false
POSITIONAL_ARGS=()
for arg in "$@"; do
    case $arg in
        --stdout-only)
            STDOUT_ONLY=true
            ;;
        *)
            POSITIONAL_ARGS+=("$arg")
            ;;
    esac
done

# Configuration
VCENTER_HOST="${POSITIONAL_ARGS[0]:-vc-wld01-a.site-a.vcf.lab}"
VCENTER_USER="root"
DECRYPT_CMD="/usr/lib/vmware-wcp/decryptK8Pwd.py"
CREDS_FILE="/home/holuser/creds.txt"
LOG_FILE="/lmchol/hol/labstartup.log"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"

# Source shared logging library
source "${SCRIPT_DIR}/log_functions.sh"

# Polling configuration
POLL_INTERVAL=30      # seconds between polls
MAX_POLL_TIME=1800    # 30 minutes maximum total wait

# Ensure log directory exists
if [[ "${STDOUT_ONLY}" != "true" ]]; then
    mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null
fi

# Track total elapsed time across all wait phases
TOTAL_START_TIME=$(date +%s)

get_remaining_time() {
    local now
    now=$(date +%s)
    local elapsed=$((now - TOTAL_START_TIME))
    local remaining=$((MAX_POLL_TIME - elapsed))
    if [[ ${remaining} -lt 0 ]]; then
        remaining=0
    fi
    echo ${remaining}
}

get_elapsed_time() {
    local now
    now=$(date +%s)
    echo $((now - TOTAL_START_TIME))
}

# The shared log_functions.sh provides log_msg, log_error, log_warn.
# They automatically use $LOG_FILE when no file argument is passed.

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
log_msg "WCP Webhook Fix Script v4.0"
log_msg "=========================================="
log_msg "vCenter Host: ${VCENTER_HOST}"
log_msg "Max total wait time: $((MAX_POLL_TIME / 60)) minutes"
log_msg "Poll interval: ${POLL_INTERVAL} seconds"

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
# Step 2: Wait for Supervisor Control Plane to be accessible
# Poll until the VIP or a fallback SCP VM IP is reachable and SSH works
#==========================================================================

log_msg "Waiting for Supervisor Control Plane to become accessible..."

ACTUAL_IP=""
FOUND_SCP=false

# Build list of IPs to try: VIP first, then VIP+1, VIP+2, VIP+3
BASE_IP=$(echo "${nodeIP}" | cut -d. -f1-3)
VIP_LAST=$(echo "${nodeIP}" | cut -d. -f4)
CANDIDATE_IPS="${nodeIP}"
for offset in 1 2 3; do
    CANDIDATE_IPS="${CANDIDATE_IPS} ${BASE_IP}.$((VIP_LAST + offset))"
done

while [[ $(get_remaining_time) -gt 0 ]]; do
    for try_ip in ${CANDIDATE_IPS}; do
        if check_host_reachable "${try_ip}"; then
            # Verify we can actually SSH and get a service status
            HYPERCRYPT_CHECK=$(/usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${try_ip}" "systemctl is-active hypercrypt 2>/dev/null" 2>/dev/null)
            if [[ -n "${HYPERCRYPT_CHECK}" ]]; then
                ACTUAL_IP="${try_ip}"
                FOUND_SCP=true
                if [[ "${try_ip}" != "${nodeIP}" ]]; then
                    log_msg "VIP ${nodeIP} not reachable, using SCP VM at ${ACTUAL_IP}"
                else
                    log_msg "Supervisor VIP ${nodeIP} is reachable"
                fi
                break 2  # break both loops
            fi
        fi
    done
    log_msg "  Supervisor not yet reachable - waiting... ($(get_elapsed_time)s / ${MAX_POLL_TIME}s)"
    sleep ${POLL_INTERVAL}
done

if [[ "${FOUND_SCP}" != "true" ]]; then
    log_error "Cannot reach any Supervisor Control Plane IP within timeout"
    exit 3
fi

#==========================================================================
# Step 3: Wait for hypercrypt and kubelet to become active
# These services take time after boot/hibernation wake
#==========================================================================

log_msg "Waiting for SCP services (hypercrypt, kubelet) to become active..."

SERVICES_OK=false

while [[ $(get_remaining_time) -gt 0 ]]; do
    HYPERCRYPT_STATUS=$(check_hypercrypt_status "${ACTUAL_IP}" "${nodePwd}")
    KUBELET_STATUS=$(check_kubelet_status "${ACTUAL_IP}" "${nodePwd}")

    log_msg "  hypercrypt: ${HYPERCRYPT_STATUS:-unknown}, kubelet: ${KUBELET_STATUS:-unknown} ($(get_elapsed_time)s / ${MAX_POLL_TIME}s)"

    if [[ "${HYPERCRYPT_STATUS}" == "active" && "${KUBELET_STATUS}" == "active" ]]; then
        log_msg "Both hypercrypt and kubelet are active"
        SERVICES_OK=true
        break
    fi

    if [[ "${HYPERCRYPT_STATUS}" == "activating" ]]; then
        log_msg "  hypercrypt is still initializing (encryption keys being delivered)..."
    elif [[ "${HYPERCRYPT_STATUS}" == "failed" ]]; then
        log_warn "  hypercrypt has failed - attempting restart..."
        /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${ACTUAL_IP}" "systemctl restart hypercrypt" 2>/dev/null
    fi

    if [[ "${KUBELET_STATUS}" != "active" && "${HYPERCRYPT_STATUS}" == "active" ]]; then
        log_msg "  hypercrypt is active but kubelet is not - attempting to start kubelet..."
        /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${ACTUAL_IP}" "systemctl start kubelet" 2>/dev/null
    fi

    sleep ${POLL_INTERVAL}
done

if [[ "${SERVICES_OK}" != "true" ]]; then
    log_error "SCP services did not become active within timeout"
    log_error "  hypercrypt: ${HYPERCRYPT_STATUS:-unknown}, kubelet: ${KUBELET_STATUS:-unknown}"
    log_error "  This may indicate encryption key delivery issues from ESXi/vTPM"
    exit 2
fi

#==========================================================================
# Step 4: Wait for Kubernetes API
#==========================================================================

log_msg "Waiting for Kubernetes API to become available..."

K8S_API_OK=false

while [[ $(get_remaining_time) -gt 0 ]]; do
    if check_k8s_api "${ACTUAL_IP}" "${nodePwd}"; then
        log_msg "Kubernetes API is available (healthz: ok)"
        K8S_API_OK=true
        break
    fi
    log_msg "  K8s API not yet available - waiting... ($(get_elapsed_time)s / ${MAX_POLL_TIME}s)"
    sleep ${POLL_INTERVAL}
done

if [[ "${K8S_API_OK}" != "true" ]]; then
    log_error "Kubernetes API did not become available within timeout"
    exit 3
fi

# Use ACTUAL_IP from now on (might be VIP or direct VM IP)
nodeIP="${ACTUAL_IP}"

#==========================================================================
# Step 5: Check certificate expiration and renew only if needed
# Only delete/regenerate certificates that are expired or expiring within 1 week
#==========================================================================

#EXPIRY_THRESHOLD=172800  # 48 hours in seconds
EXPIRY_THRESHOLD=604800  # 1 week in seconds

# Function to check if a K8s secret's certificate is expired or expiring soon
# Returns: 0 if expired/expiring (needs renewal), 1 if valid, 2 if secret not found
check_cert_expiry() {
    local namespace=$1
    local secret_name=$2

    # Get the certificate data from the secret
    local cert_data
    cert_data=$(/usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" \
        "kubectl -n ${namespace} get secret ${secret_name} -o jsonpath='{.data.tls\\.crt}' 2>/dev/null" 2>/dev/null)

    if [[ -z "${cert_data}" ]]; then
        # Secret doesn't exist or has no tls.crt
        return 2
    fi

    # Extract the expiration date from the certificate
    local end_date
    end_date=$(/usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" \
        "echo '${cert_data}' | base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null" 2>/dev/null)

    if [[ -z "${end_date}" ]]; then
        log_warn "Could not parse certificate in ${namespace}/${secret_name}"
        return 0  # Cannot determine - treat as needing renewal
    fi

    # Parse the date: "notAfter=May  7 14:54:44 2026 GMT"
    local expiry_str
    expiry_str=$(echo "${end_date}" | sed 's/notAfter=//')

    # Convert to epoch using date command
    local expiry_epoch
    expiry_epoch=$(date -d "${expiry_str}" +%s 2>/dev/null)

    if [[ -z "${expiry_epoch}" ]]; then
        log_warn "Could not parse expiry date: ${expiry_str}"
        return 0  # Cannot determine - treat as needing renewal
    fi

    local now_epoch
    now_epoch=$(date +%s)
    local remaining=$((expiry_epoch - now_epoch))
    local remaining_days=$((remaining / 86400))
    local remaining_hours=$(( (remaining % 86400) / 3600 ))

    if [[ ${remaining} -le 0 ]]; then
        log_msg "  ${namespace}/${secret_name}: EXPIRED (expired ${remaining_days#-} days ago)"
        return 0
    elif [[ ${remaining} -le ${EXPIRY_THRESHOLD} ]]; then
        log_msg "  ${namespace}/${secret_name}: EXPIRING SOON (${remaining_hours}h remaining)"
        return 0
    else
        log_msg "  ${namespace}/${secret_name}: Valid (${remaining_days}d ${remaining_hours}h remaining)"
        return 1
    fi
}

log_msg "=========================================="
log_msg "Checking WCP certificate expiration (1 week threshold)..."
log_msg "=========================================="

# Define the certificates to check: namespace:secret_name
CERTS_TO_CHECK=(
    "vmware-system-cert-manager:storage-quota-root-ca-secret"
    "kube-system:storage-quota-webhook-server-internal-cert"
    "kube-system:cns-storage-quota-extension-cert"
)

CERTS_NEED_RENEWAL=false

for cert_entry in "${CERTS_TO_CHECK[@]}"; do
    CERT_NS=$(echo "${cert_entry}" | cut -d: -f1)
    CERT_NAME=$(echo "${cert_entry}" | cut -d: -f2)

    check_cert_expiry "${CERT_NS}" "${CERT_NAME}"
    CERT_STATUS=$?

    if [[ ${CERT_STATUS} -eq 0 ]]; then
        # Expired or expiring - delete to trigger regeneration
        CERTS_NEED_RENEWAL=true
        log_msg "  Deleting ${CERT_NS}/${CERT_NAME} to trigger regeneration..."
        /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" \
            "kubectl -n ${CERT_NS} delete secret ${CERT_NAME} --ignore-not-found=true" >/dev/null 2>&1
        log_msg "  Deleted ${CERT_NAME}"
    elif [[ ${CERT_STATUS} -eq 2 ]]; then
        log_msg "  ${CERT_NS}/${CERT_NAME}: Not found (will be created automatically)"
    fi
    # CERT_STATUS 1 = valid, no action needed
done

if [[ "${CERTS_NEED_RENEWAL}" == "true" ]]; then
    log_msg "=========================================="
    log_msg "Restarting deployments to regenerate certificates..."
    log_msg "=========================================="

    if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" \
        "kubectl -n kube-system rollout restart deploy cns-storage-quota-extension 2>/dev/null || echo 'Deployment not found'" >/dev/null 2>&1; then
        log_msg "Restarted cns-storage-quota-extension deployment"
    fi

    if /usr/bin/sshpass -p "${nodePwd}" ssh ${SSH_OPTS} "root@${nodeIP}" \
        "kubectl -n kube-system rollout restart deploy storage-quota-webhook 2>/dev/null || echo 'Deployment not found'" >/dev/null 2>&1; then
        log_msg "Restarted storage-quota-webhook deployment"
    fi

    log_msg "Waiting 20 seconds for deployments to restart..."
    sleep 20
else
    log_msg "All certificates are valid - no renewal needed"
    log_msg "Skipping deployment restarts"
fi

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

TOTAL_ELAPSED=$(get_elapsed_time)
log_msg ""
log_msg "=========================================="
log_msg "Successfully completed certificate resets and webhook restarts"
log_msg "Total elapsed time: $((TOTAL_ELAPSED / 60))m $((TOTAL_ELAPSED % 60))s"
log_msg "=========================================="

exit 0
