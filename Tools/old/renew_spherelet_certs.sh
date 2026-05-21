#!/bin/bash
# Author: HOL Team
# Version: 1.1
# Date: 2026-05-21
#
# Renew expired ESXi Spherelet certificates for vSphere Supervisor worker nodes.
#
# Background:
#   ESXi hosts acting as Supervisor worker nodes use two certificates in
#   /etc/vmware/spherelet/:
#     - client.crt   : kubelet client cert (O=system:nodes) for API server auth
#     - spherelet.crt: kubelet serving cert (O=VMware, Inc) for server TLS
#   Both are signed by the Supervisor Kubernetes CA and issued with 1-year
#   validity. When they expire the spherelet stops posting node status, the
#   nodes become NotReady/unreachable, and workloads (including the LCI
#   controller manager) cannot be scheduled, causing the Local Consumption
#   Interface to return 502 Bad Gateway.
#
# This script:
#   1. Obtains Supervisor node credentials via decryptK8Pwd.py on vCenter
#   2. Dynamically discovers all ESXi agent nodes from the Supervisor cluster
#   3. Pre-checks each node's client.crt — skips all renewal if every cert is
#      valid for more than THRESHOLD_DAYS (2 years); proceeds otherwise
#   4. Copies the Supervisor Kubernetes CA cert and key
#   5. For each ESXi node, re-signs client.crt and spherelet.crt (reusing
#      the existing private keys) with a 5-year validity
#   6. Pushes the new certs back to each ESXi node and restarts spherelet
#
# Usage:
#   ./renew_spherelet_certs.sh [vcenter_host]
# Defaults to vc-wld01-a.site-a.vcf.lab if no argument provided.

set -euo pipefail

VCENTER_HOST="${1:-vc-wld01-a.site-a.vcf.lab}"
VCENTER_USER="root"
DECRYPT_CMD="/usr/lib/vmware-wcp/decryptK8Pwd.py"
CREDS_FILE="/home/holuser/creds.txt"
LOG_FILE="/lmchol/hol/labstartup.log"
CERT_DAYS=1825        # 5-year renewal validity
THRESHOLD_DAYS=730    # Renew if any cert expires within this many days (2 years)

log() { echo "$(date '+%m/%d/%Y %H:%M:%S') $*" | tee -a "${LOG_FILE}"; }

# SSH with key-based auth, falling back to sshpass
ssh_exec() {
    local user="$1" host="$2"; shift 2
    ssh-keygen -R "${host}" >/dev/null 2>&1 || true
    if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
           -o ConnectTimeout=10 -o BatchMode=yes \
           "${user}@${host}" "$@" 2>/dev/null; then
        return 0
    fi
    local pw
    pw=$(cat "${CREDS_FILE}")
    /usr/bin/sshpass -p "${pw}" \
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=10 \
            "${user}@${host}" "$@"
}

scp_get() {
    local user="$1" host="$2" remote="$3" local_dest="$4"
    ssh-keygen -R "${host}" >/dev/null 2>&1 || true
    if scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=10 -o BatchMode=yes \
            "${user}@${host}:${remote}" "${local_dest}" 2>/dev/null; then
        return 0
    fi
    local pw
    pw=$(cat "${CREDS_FILE}")
    /usr/bin/sshpass -p "${pw}" \
        scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=10 \
            "${user}@${host}:${remote}" "${local_dest}"
}

scp_put() {
    local user="$1" host="$2" local_src="$3" remote_dest="$4"
    ssh-keygen -R "${host}" >/dev/null 2>&1 || true
    if scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=10 -o BatchMode=yes \
            "${local_src}" "${user}@${host}:${remote_dest}" 2>/dev/null; then
        return 0
    fi
    local pw
    pw=$(cat "${CREDS_FILE}")
    /usr/bin/sshpass -p "${pw}" \
        scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=10 \
            "${local_src}" "${user}@${host}:${remote_dest}"
}

# ── Step 1: Retrieve Supervisor credentials ────────────────────────────────────
log "=========================================="
log "renew_spherelet_certs.sh: Starting"
log "vCenter: ${VCENTER_HOST}"
log "=========================================="

DECRYPT_OUTPUT=$(ssh_exec "${VCENTER_USER}" "${VCENTER_HOST}" "${DECRYPT_CMD}")
nodeIP=$(echo "${DECRYPT_OUTPUT}" | grep "^IP:" | awk '{print $2}')
nodePwd=$(echo "${DECRYPT_OUTPUT}" | grep "^PWD:" | awk '{print $2}')

if [[ -z "${nodeIP}" || -z "${nodePwd}" ]]; then
    log "ERROR: Failed to retrieve Supervisor credentials from ${VCENTER_HOST}"
    exit 1
fi

log "Supervisor node IP: ${nodeIP}"
log "Supervisor credentials retrieved."

# ── Step 2: Dynamically discover ESXi agent nodes from Supervisor ─────────────
# Agent nodes are ESXi hosts registered with the Supervisor; they carry the
# node-role.kubernetes.io/agent label. Control-plane VMs are excluded.
log "Discovering Supervisor agent nodes..."

mapfile -t ESX_NODES < <(
    /usr/bin/sshpass -p "${nodePwd}" \
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=30 "root@${nodeIP}" \
            "kubectl get nodes -l 'node-role.kubernetes.io/agent' \
                --no-headers -o custom-columns='NAME:.metadata.name'" 2>/dev/null \
    | awk 'NF {print $1}'
)

if [[ ${#ESX_NODES[@]} -eq 0 ]]; then
    log "ERROR: No agent nodes found on Supervisor ${nodeIP}"
    exit 1
fi

log "Found ${#ESX_NODES[@]} agent node(s): ${ESX_NODES[*]}"

# ── Step 3: Pre-check — scan every node's client.crt for upcoming expiry ─────
THRESHOLD_SEC=$((THRESHOLD_DAYS * 86400))
NEEDS_RENEWAL=false

log "Pre-checking certificate validity (threshold: ${THRESHOLD_DAYS} days / 2 years)..."
for ESX_HOST in "${ESX_NODES[@]}"; do
    EXPIRY=$(ssh_exec root "${ESX_HOST}" \
        "openssl x509 -in /etc/vmware/spherelet/client.crt -noout -enddate 2>/dev/null | cut -d= -f2" \
        2>/dev/null || echo "unreadable")

    # openssl -checkend returns 1 (cert will expire) or 0 (cert is fine)
    if ! ssh_exec root "${ESX_HOST}" \
            "openssl x509 -in /etc/vmware/spherelet/client.crt \
                -checkend ${THRESHOLD_SEC}" >/dev/null 2>&1; then
        log "  RENEW  ${ESX_HOST}  client.crt expires: ${EXPIRY}"
        NEEDS_RENEWAL=true
    else
        log "  OK     ${ESX_HOST}  client.crt expires: ${EXPIRY}"
    fi
done

if [[ "${NEEDS_RENEWAL}" == "false" ]]; then
    log "Supervisor Kubelet Host certificates are still valid for 2+ yrs, not renewing"
    log "=========================================="
    log "renew_spherelet_certs.sh: Done (no action needed)"
    log "=========================================="
    exit 0
fi

log "Supervisor Kubelet Host certificates are expired or expiring soon, renewing..."

# ── Step 4: Pull Supervisor CA cert and key to a local temp dir ───────────────
WORK_DIR=$(mktemp -d /tmp/spherelet-renew.XXXXXX)
trap 'rm -rf "${WORK_DIR}"' EXIT

log "Copying Supervisor CA from ${nodeIP}..."
/usr/bin/sshpass -p "${nodePwd}" \
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=30 \
        "root@${nodeIP}:/etc/kubernetes/pki/ca.crt" "${WORK_DIR}/ca.crt"

# ca.key is a symlink into /dev/shm on the Supervisor — read via SSH
/usr/bin/sshpass -p "${nodePwd}" \
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=30 \
        "root@${nodeIP}" "cat /etc/kubernetes/pki/ca.key" > "${WORK_DIR}/ca.key"

if [[ ! -s "${WORK_DIR}/ca.crt" || ! -s "${WORK_DIR}/ca.key" ]]; then
    log "ERROR: Could not copy Supervisor CA cert/key"
    exit 1
fi

CA_EXPIRY=$(openssl x509 -in "${WORK_DIR}/ca.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
log "Supervisor CA valid until: ${CA_EXPIRY}"

# ── Step 5: For each ESXi node, renew client.crt and spherelet.crt ───────────
for ESX_HOST in "${ESX_NODES[@]}"; do
    SHORT="${ESX_HOST%%.*}"    # e.g. esx-05a
    FQDN="${ESX_HOST}"
    NODE_NAME="system:node:${FQDN}"

    log "------------------------------------------"
    log "Processing node: ${ESX_HOST}"
    log "------------------------------------------"

    # ── 5a: Copy existing private keys from ESXi ──────────────────────────────
    log "  Copying existing private keys from ${ESX_HOST}..."
    scp_get root "${ESX_HOST}" "/etc/vmware/spherelet/client.key" "${WORK_DIR}/${SHORT}-client.key"
    scp_get root "${ESX_HOST}" "/etc/vmware/spherelet/server.key"  "${WORK_DIR}/${SHORT}-server.key"

    # ── 5b: Re-sign client.crt (kubelet client auth) ──────────────────────────
    log "  Generating new client.crt for ${FQDN}..."
    cat > "${WORK_DIR}/${SHORT}-client.ext" <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectAltName = DNS:${NODE_NAME}
EOF

    openssl req -new -key "${WORK_DIR}/${SHORT}-client.key" \
        -subj "/C=US/ST=CA/L=Palo Alto/O=system:nodes/CN=${NODE_NAME}" \
        -out "${WORK_DIR}/${SHORT}-client.csr" 2>/dev/null

    openssl x509 -req -in "${WORK_DIR}/${SHORT}-client.csr" \
        -CA "${WORK_DIR}/ca.crt" -CAkey "${WORK_DIR}/ca.key" -CAcreateserial \
        -extfile "${WORK_DIR}/${SHORT}-client.ext" \
        -days "${CERT_DAYS}" -sha256 \
        -out "${WORK_DIR}/${SHORT}-client.crt" 2>/dev/null

    NEW_CLIENT_EXPIRY=$(openssl x509 -in "${WORK_DIR}/${SHORT}-client.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
    log "  New client.crt valid until: ${NEW_CLIENT_EXPIRY}"

    # ── 5c: Re-sign spherelet.crt (kubelet serving cert) ─────────────────────
    log "  Generating new spherelet.crt for ${FQDN}..."

    # Resolve node IP from Supervisor API (avoids hard-coding)
    NODE_IP=$(/usr/bin/sshpass -p "${nodePwd}" \
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=10 "root@${nodeIP}" \
            "kubectl get node ${FQDN} \
                -o jsonpath='{.status.addresses[?(@.type==\"InternalIP\")].address}'" \
        2>/dev/null || echo "")

    # Fall back to querying the ESXi host directly if kubectl didn't return an IP
    if [[ -z "${NODE_IP}" ]]; then
        NODE_IP=$(ssh_exec root "${ESX_HOST}" \
            "esxcli network ip interface ipv4 get 2>/dev/null | awk '/vmk0/{print \$3}'" \
            2>/dev/null || echo "")
    fi

    SAN_LINE="DNS:${FQDN}"
    [[ -n "${NODE_IP}" ]] && SAN_LINE="${SAN_LINE}, IP:${NODE_IP}"

    cat > "${WORK_DIR}/${SHORT}-server.ext" <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${SAN_LINE}
EOF

    openssl req -new -key "${WORK_DIR}/${SHORT}-server.key" \
        -subj "/C=US/ST=CA/L=Palo Alto/O=VMware, Inc/CN=${FQDN}" \
        -out "${WORK_DIR}/${SHORT}-server.csr" 2>/dev/null

    openssl x509 -req -in "${WORK_DIR}/${SHORT}-server.csr" \
        -CA "${WORK_DIR}/ca.crt" -CAkey "${WORK_DIR}/ca.key" -CAcreateserial \
        -extfile "${WORK_DIR}/${SHORT}-server.ext" \
        -days "${CERT_DAYS}" -sha256 \
        -out "${WORK_DIR}/${SHORT}-spherelet.crt" 2>/dev/null

    NEW_SERVER_EXPIRY=$(openssl x509 -in "${WORK_DIR}/${SHORT}-spherelet.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
    log "  New spherelet.crt valid until: ${NEW_SERVER_EXPIRY}"

    # ── 5d: Push new certificates to ESXi node ────────────────────────────────
    log "  Deploying new certificates to ${ESX_HOST}..."
    scp_put root "${ESX_HOST}" "${WORK_DIR}/${SHORT}-client.crt"    "/etc/vmware/spherelet/client.crt"
    scp_put root "${ESX_HOST}" "${WORK_DIR}/${SHORT}-spherelet.crt" "/etc/vmware/spherelet/spherelet.crt"

    # ── 5e: Restart spherelet ─────────────────────────────────────────────────
    log "  Restarting spherelet on ${ESX_HOST}..."
    ssh_exec root "${ESX_HOST}" "/etc/init.d/spherelet restart" 2>&1 | \
        while IFS= read -r line; do log "    ${line}"; done || true

    log "  ${ESX_HOST}: Certificate renewal complete."
done

log "=========================================="
log "All spherelet certificates renewed."
log "Waiting 60s for nodes to re-register with Supervisor..."
log "=========================================="
sleep 60

# ── Step 6: Verify node status ────────────────────────────────────────────────
log "=== Supervisor node status after renewal ==="
/usr/bin/sshpass -p "${nodePwd}" \
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=30 "root@${nodeIP}" \
        "kubectl get nodes -o wide" 2>&1 | \
    while IFS= read -r line; do log "  ${line}"; done

log "=========================================="
log "renew_spherelet_certs.sh: Done"
log "=========================================="
