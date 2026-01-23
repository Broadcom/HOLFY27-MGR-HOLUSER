#!/bin/bash
# Author: Burke Azbill
# Version: 1.1
# Date: 2025-10-24
# Script to delete old certificates and restart Kubernetes webhooks after extracting credentials from vCenter
# This script:
# 1. SSH to vCenter and run decryptK8Pwd.py
# 2. Parse output to extract IP and Password
# 3. Use those credentials to delete certificates and restart the webhooks
#
# Usage: ./restart_k8s_webhooks.sh [vcenter_host]
# Example: ./restart_k8s_webhooks.sh vc-wld01-a.site-a.vcf.lab
# If no parameter is provided, it will attempt to run the command on vc-wld01-a.site-a.vcf.lab

set -e  # Exit on error

# Configuration
VCENTER_HOST="${1:-vc-wld01-a.site-a.vcf.lab}"
VCENTER_USER="root"
DECRYPT_CMD="/usr/lib/vmware-wcp/decryptK8Pwd.py"
CREDS_FILE="/home/holuser/creds.txt"
LOG_FILE="/lmchol/hol/labstartup.log"

# Helper function to execute SSH with fallback to sshpass
ssh_with_fallback() {
    local user=$1
    local host=$2
    shift 2
    local cmd=("$@")
    
    # Try key-based authentication first
    if ssh -o ConnectTimeout=5 -o BatchMode=yes "${user}@${host}" "${cmd[@]}" 2>/dev/null; then
        return 0
    fi
    
    # Fall back to sshpass if key auth fails
    if [[ -f "${CREDS_FILE}" ]]; then
        local password
        password=$(cat "${CREDS_FILE}")
        /usr/bin/sshpass -p "${password}" ssh "${user}@${host}" "${cmd[@]}"
    else
        echo "ERROR: Key-based authentication failed and credentials file not found at ${CREDS_FILE}" >> "${LOG_FILE}"
        return 1
    fi
}

echo "==========================================" >> "${LOG_FILE}"
echo "Connecting to vCenter to retrieve credentials..." >> "${LOG_FILE}"
echo "vCenter Host: ${VCENTER_HOST}" >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"

# SSH to vCenter and capture the output
DECRYPT_OUTPUT=$(ssh_with_fallback "${VCENTER_USER}" "${VCENTER_HOST}" "${DECRYPT_CMD}")

# Parse the output to extract IP and Password
nodeIP=$(echo "${DECRYPT_OUTPUT}" | grep "^IP:" | awk '{print $2}')
nodePwd=$(echo "${DECRYPT_OUTPUT}" | grep "^PWD:" | awk '{print $2}')

# Validate that we got the values
if [[ -z "${nodeIP}" ]]; then
    echo "ERROR: Failed to extract IP from vCenter output" >> "${LOG_FILE}"
    echo "Raw output:" >> "${LOG_FILE}"
    echo "${DECRYPT_OUTPUT}" >> "${LOG_FILE}"
    exit 1
fi

if [[ -z "${nodePwd}" ]]; then
    echo "ERROR: Failed to extract PWD from vCenter output" >> "${LOG_FILE}"
    exit 1
fi

echo "Successfully extracted credentials from vCenter" >> "${LOG_FILE}"
echo "Node IP: ${nodeIP}" >> "${LOG_FILE}"
echo "Password retrieved: $(echo "${nodePwd}" | sed 's/./*/g')" >> "${LOG_FILE}"
echo ""

# Execute the kubectl commands to restart webhooks
# The following two must be restarted for HOL-2636 Workload Supervisor cluster when the certificates have expired
# the restart triggers the regenerationo fthe certificates, allowing for vm creation in the lab
# Execute the kubectl commands to restart webhooks
echo "==========================================" >> "${LOG_FILE}"
echo "Deleting storage-quota-root-ca-sert..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n vmware-system-cert-manager delete secret storage-quota-root-ca-secret" >> "${LOG_FILE}"

echo "==========================================" >> "${LOG_FILE}"
echo "Deleting storage-quota-webhook-server-internal-cert..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n kube-system delete secret storage-quota-webhook-server-internal-cert" >> "${LOG_FILE}"

echo "==========================================" >> "${LOG_FILE}"
echo "Deleting cns-storage-quota-extension-cert..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n kube-system delete secret cns-storage-quota-extension-cert" >> "${LOG_FILE}"

echo ""
echo "==========================================" >> "${LOG_FILE}"
echo "Restarting cns-storage-quota-extension..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n kube-system rollout restart deploy cns-storage-quota-extension" >> "${LOG_FILE}"

echo "==========================================" >> "${LOG_FILE}"
echo "Restarting storage-quota-webhook..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n kube-system rollout restart deploy storage-quota-webhook" >> "${LOG_FILE}"
sleep 20
echo "==========================================" >> "${LOG_FILE}"
echo "Scaling cci replicas back up to 1..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n svc-cci-ns-domain-c10 scale deployment --all --replicas=1" >> "${LOG_FILE}"

echo "==========================================" >> "${LOG_FILE}"
echo "Scaling argocd replicas back up to 1..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n argocd scale deployment --all --replicas=1" >> "${LOG_FILE}"

echo "==========================================" >> "${LOG_FILE}"
echo "Scaling Harbor replicas back up to 1..." >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n svc-harbor-domain-c10 scale sts --all --replicas=1" >> "${LOG_FILE}"
/usr/bin/sshpass -p "${nodePwd}" ssh "root@${nodeIP}" "kubectl -n svc-harbor-domain-c10 scale deployment --all --replicas=1" >> "${LOG_FILE}"

echo ""
echo "==========================================" >> "${LOG_FILE}"
echo "✓ Successfully completed certificate resets and webhook restarts" >> "${LOG_FILE}"
echo "==========================================" >> "${LOG_FILE}"


