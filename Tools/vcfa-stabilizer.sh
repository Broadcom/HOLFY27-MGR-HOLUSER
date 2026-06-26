#!/bin/bash

# VCFA Complete Stabilization Script v2.8
# Version 2.8 - 2026-06-26
# Author - HOL Core Team
#
# Default-run philosophy (v2.6+): one run of the script with no flags should leave the VCFA in a
# known-good state regardless of what state it was in before, AND should be safe to run again any
# time (cron, post-reboot, between automation runs) without disrupting a healthy cluster.
#
# Default flow: status -> control-plane preflight (NEW Phase 1.5) -> auth services -> core
# components -> SDS NACK fix -> wait -> verify -> monitoring setup.
#
# What's persistent (applied once, survives reboot, idempotent on re-runs):
#   - kube-vip plndr-cp-lock lease tuning (60s/45s/10s, preserve_on_leadership_loss=true)
#   - kube-apiserver / kube-controller-manager / kube-scheduler probe timeouts
#     (period=10 timeout=30 failureThreshold=8)
#   - etcd defrag (only when slack >= 30%)
# What's a runtime backstop (re-applied every run, harmless if already correct):
#   - eth0 VIP pinning for .69, .70, .72 (kube-vip will reclaim, this is just a fallback)
#   - ccs-k3s service-tls cert freshness check (NEW v2.8)
# What's conditional (only applied when we detect actual trouble):
#   - kyverno --forceFailurePolicyIgnore=true + webhook Fail->Ignore. Triggers when load1>30, OR
#     kyverno pods not Ready, OR kube-controller-manager restarts>5. Reason: vmsp-operator owns
#     the kyverno HelmRelease and reverts our patch on every helm reconcile, so applying
#     unconditionally just churns rollouts. Set FORCE_KYVERNO_FIX=1 to bypass the heuristic.
#
# See VCFA_Stabilizer_Incident_Apr2026.md for the gateway/EnvoyProxy guidance and HTTP 503 recovery.
#
# v2.8 changelog (2026-06-26):
#  * NEW check_and_fix_ccs_k3s_cert(): detects and remediates stale service-tls certs across ALL
#    24 prelude deployments that share the service-tls Secret. cert-manager auto-renews the Secret
#    (~90-day validity) but every pod must restart to mount the new cert data. Without a restart
#    pods continue serving expired certs and Envoy rejects upstream TLS with CERTIFICATE_VERIFY_FAILED
#    → HTTP 503 on any affected route (/cci/*, /automation, and others).
#    Detection: compare cert notBefore (from Secret) vs each pod's startTime (from kubectl) — pure
#    kubectl, no network probe needed, ~2-3s on a healthy cluster. Stale deployments get parallel
#    rollout restarts, then we wait up to 120s for all to become ready.
#    Called from main() BEFORE the idempotency early-exit so it runs on every lab startup.
#    Impact: ~2-3s when all certs fresh; ~60-120s when stale (eliminates the 30-min URL retry loop).
#
# v2.7 changelog (2026-05-13):
#  * Added robust idempotency check to main() based on persistent configuration changes
#    (vcfa-eg-mem-keeper.sh and kube-vip lease duration) to allow safe, silent exits
#    when the script has already been applied.
#
# v2.6 changelog (Apr 30, 2026 - same evening as v2.5.1):
#  * NEW Phase 1.5 in `main()` ("Control-plane preflight") that wraps every persistent control-plane
#    fix from v2.5's `--fix-overload` so they're applied proactively on every default run. Every
#    operation is idempotent and compares current vs desired before writing:
#      - kube-vip manifest: write + touch + delete-lease only if any env var value differs.
#      - kube-apiserver / kcm / scheduler manifests: write + touch only if any probe field differs.
#      - etcd defrag: parse dbSize / dbSizeInUse from `etcdctl endpoint status -w json`; only
#        defrag if slack >= ETCD_DEFRAG_SLACK_PCT (default 30%). 0=always, 100=never.
#      - VIP pinning: `ip addr replace ... preferred_lft forever`, skipped if already non-deprecated.
#      - Kyverno fix: GATED on trouble heuristic (load1>30 / kyverno pods not Ready / kcm restarts>5)
#        because vmsp-operator's kyverno HelmRelease reconciler reverts our deployment patch on every
#        helm upgrade. Applying unconditionally just causes a continuous rollout cycle. The
#        underlying patch is unchanged from v2.5.1 (suspend HR + --forceFailurePolicyIgnore=true via
#        JSON patch + flip resource webhook to Ignore).
#    Net effect: on a healthy cluster, Phase 1.5 produces no kubelet restarts, no API-server churn,
#    no etcd writes, no kyverno rollouts -- just a status log. On a fresh boot or a degraded
#    cluster it applies the same fixes that the standalone `--fix-overload` did.
#  * Switched the heredoc terminator in `fix_overload_recovery` from unquoted `<<REMOTE` to quoted
#    `<<'REMOTE'` to avoid the bash command-substitution paren-balancing trap that came up while
#    adding the new idempotency checks. Variables are now injected as bash assignments at the top
#    of the body via a separate prefix.
#  * Switched the kyverno deployment patch from `kubectl replace` to `kubectl patch --type=json`
#    on a single args-array index; this avoids resource-version conflicts during rollouts and
#    survives concurrent helm-controller writes.
#  * Backups (`*.bak.<epoch>`) are only created when an actual manifest change is being written
#    (was: created on every call regardless).
#  * NEW `--preflight` flag: runs only Phase 1 + Phase 1.5. ~2-3 min, idempotent on a healthy
#    cluster. Use after a fresh boot, before a busy automation run, or in a periodic cron.
#  * NEW env vars:
#      - STABILIZER_SKIP_OVERLOAD_PROPHYLAXIS=1 to skip Phase 1.5 (legacy v2.5- behavior).
#      - ETCD_DEFRAG_SLACK_PCT to override the defrag threshold (default 30; 0=always, 100=never).
#      - VMSP_GW_VIP and VCFA_GW_VIP to override the gateway VIPs that get pinned.
#      - FORCE_KYVERNO_FIX=1 to bypass the trouble heuristic and apply the kyverno fix anyway.
#
# v2.5 changelog (Apr 30, 2026):
#  * NEW --fix-overload: control-plane "death spiral" recovery for the auto-platform-a-558jg lab where
#    we found:
#      - kube-vip plndr-cp-lock Lease object had leaseDurationSeconds=1 (vs default 15) -> kube-vip
#        couldn't renew under load -> client-go RunOrDie panicked -> kube-vip dropped 10.1.1.72 VIP
#        -> 100s of CrashLoop restarts -> all in-cluster controllers lost kubernetes.default.svc
#        access ("dial tcp 10.1.1.72:6443: connect: no route to host") -> CAPI/CKM/scheduler/metrics-
#        server lost their leases every few seconds -> kube-controller-manager couldn't reconcile
#        because kyverno admission webhooks (failurePolicy: Fail) rejected every write the moment
#        a kyverno replica blipped -> load avg 522-863 even though memory was fine
#      - etcd db at 329MB / 81% slack (no defrag had ever run) -> compounding I/O latency
#    The fix lands all of these in one idempotent script: defrag etcd, manually pin 10.1.1.72/32
#    + .69 + .70 as a backstop (non-deprecated), harden kube-vip lease (60s) + renew (45s) + retry
#    (10s) + preserve_on_leadership_loss=true via the static pod manifest, bump kube-apiserver/kcm/
#    scheduler probe timeouts to 30s, and use the durable kyverno fix:
#      (a) suspend the kyverno HelmRelease in vmsp-platform (NOT vmsp-policies — this is where flux
#          actually owns the Deployment); (b) patch kyverno-admission-controller with
#          --forceFailurePolicyIgnore=true so Kyverno's own reconciler keeps the resource webhook
#          at Ignore; (c) directly set the resource webhook to Ignore as belt-and-suspenders.
#      The 5 policy-config webhooks (cel-exception, cleanup, exception, global-context, policy)
#      stay at Fail intentionally — they only validate Kyverno's own CRDs.
#    Result on the affected lab: load avg 863 -> 10/63/145 (1m/5m/15m), ready pods 73% -> 99% of
#    non-completed pods, 0 active CrashLoops, all 3 GatewayConfigurations Ready.
#  * v2.5.1 (Apr 30 evening): Corrected step 5 after observing that:
#      - The kyverno HelmRelease lives in vmsp-platform, not vmsp-policies (where the deployment is).
#      - Flipping the 5 policy-config webhooks to Ignore is reverted by Kyverno's own reconciler in
#        ~10 seconds; only the resource-validating webhook needs to be Ignore for normal traffic.
#      - The kube-vip Deployment rollout caused a brief secondary cascade (image-pull burst loaded
#        zot-1, kube-vip lost lease again) that resolved itself in ~2 minutes. This is expected
#        recovery jitter, not a fix failure — give it 5 minutes before re-running --fix-overload.
#      - Manual VIPs need 'ip addr replace … preferred_lft forever' so the kernel doesn't age them
#        out as deprecated before kube-vip fully owns them again.
#
# v2.4.1 changelog (Apr 29, 2026):
#  * execute_remote / vcfa_ssh_nosudo: drop the outer `bash --norc --noprofile -c` wrapper. ssh joins
#    remote argv into one cmd; with `-c` the remote shell only ran the first word of our pipeline,
#    silently truncating output (e.g. `kubectl … | grep -E …` returned empty). Multi-statement pipes
#    now go straight to root's login shell.
#  * fix_envoy_gateway_sds_san_nack: install /etc/systemd/system/vcfa-eg-mem-keeper.{service,timer}
#    (60s drift watcher) so envoy-gateway memory limit is held at 4Gi even when vmsp-operator clobbers
#    spec.values on the HelmRelease. Survives reboots and operator reconciles.
#
# v2.4 changelog (Apr 29, 2026):
#  * Default VCFA_USER is now "root" (the appliance allows direct root SSH). When VCFA_USER=root we
#    skip the "echo $pwq | sudo -S" wrapper inside execute_remote / vcfa_ssh_nosudo callers; for any
#    other user we keep the legacy sudo path.
#  * NEW --fix-sds-sni and Phase 3.5 in main(): auto-replace BackendTLSPolicy.wellKnownCACertificates:System
#    with caCertificateRefs:platform-trust and bump envoy-gateway operator memory to 4Gi via the
#    helmrelease. This eliminates the Envoy 1.34 NACK "SAN-based verification of peer certificates
#    without trusted CA is insecure and not allowed" that produces upstream 503 with
#    "TLS_error:_Secret_is_not_supplied_by_SDS" on every BackendTLSPolicy-fronted prelude/vmsp service.
#  * --recover-gateway-503 now runs the SDS NACK fix BEFORE the legacy rolling restarts.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Configuration - Modify these as needed for your environment
# Sudo on the VCFA appliance must match the SSH password: we default it from
# CREDS_FILE line 1. Override with: export VCFA_PASSWORD='...'
# VCFA_HOST = VM / admin SSH target (not the kube-vip LoadBalancer for the gateway).
VCFA_HOST="${VCFA_HOST:-10.1.1.73}"
# VCFA_HTTP_VIP = kube-vip LB IP for Service vcfa-gateway-configuration (Envoy dataplane); use for curl --resolve.
VCFA_HTTP_VIP="${VCFA_HTTP_VIP:-10.1.1.70}"
VCFA_USER="${VCFA_USER:-vmware-system-user}"
CREDS_FILE="${CREDS_FILE:-/home/holuser/creds.txt}"
if [[ -z "${VCFA_PASSWORD:-}" ]] && [[ -f "$CREDS_FILE" ]]; then
  VCFA_PASSWORD=$(head -1 "$CREDS_FILE" | tr -d '\r\n')
fi
VCFA_PASSWORD="${VCFA_PASSWORD:-}"
VMSP_NAMESPACE="${VMSP_NAMESPACE:-vmsp-platform}"
PRELUDE_NAMESPACE="${PRELUDE_NAMESPACE:-prelude}"
API_SERVER="${API_SERVER:-https://10.1.1.73:6443}"
# Set to 1 to show SSH/kubectl stderr when a step uses suppress (default hides errors)
STABILIZER_DEBUG="${STABILIZER_DEBUG:-0}"

# --- SSH transport (v2.2 / multiplex 2.4.1) ---
# When running from your laptop, set STABILIZER_JUMP_HOST=jumphost (ssh(5) Host alias) so the
# inner hop uses sshpass from the jump with CREDS_ON_JUMP. When running ON the jump host, leave unset.
STABILIZER_JUMP_HOST="${STABILIZER_JUMP_HOST:-}"
CREDS_ON_JUMP="${CREDS_ON_JUMP:-/home/holuser/creds.txt}"
# Optional: passwordless second hop, e.g. ssh-copy-id to vmware-system-user
STABILIZER_VCFA_IDENTITY_FILE="${STABILIZER_VCFA_IDENTITY_FILE:-}"
# Force password auth to VCFA so the jump host's ssh agent keys are not offered first (avoids
# MaxAuthTries / confusing failures when many keys exist on the jump).
VCFA_SSH_OPTS="${VCFA_SSH_OPTS:--o StrictHostKeyChecking=no -o ConnectTimeout=15 -o IdentitiesOnly=yes -o IdentityFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no}"

# v2.4.1: outer SSH (laptop → jumphost) connection multiplexing. Without this, a single stabilizer
# run spawns dozens of TCP/SSH sessions in seconds and trips jumphost sshd MaxStartups (which
# manifests as "kex_exchange_identification: Connection reset by peer"). With ControlMaster all
# outer SSHes ride a single underlying connection.
# Always use /tmp (not $TMPDIR) — macOS TMPDIR is /var/folders/... which can blow past the
# 104-char Unix socket path limit when ssh appends %C (40 chars).
STABILIZER_SSH_CONTROL_DIR="${STABILIZER_SSH_CONTROL_DIR:-/tmp/.vcfa-stab-ssh-$$}"
STABILIZER_SSH_MUX_OPTS=(
    -o ControlMaster=auto
    -o ControlPath="${STABILIZER_SSH_CONTROL_DIR}/cm-%C"
    -o ControlPersist=60s
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=4
)
# Init/cleanup helpers used in main()
ssh_mux_init() {
    [[ -z "${STABILIZER_JUMP_HOST}" ]] && return 0
    mkdir -p "$STABILIZER_SSH_CONTROL_DIR" 2>/dev/null || true
    chmod 700 "$STABILIZER_SSH_CONTROL_DIR" 2>/dev/null || true
}
ssh_mux_cleanup() {
    [[ -z "${STABILIZER_JUMP_HOST}" ]] && return 0
    [[ -d "$STABILIZER_SSH_CONTROL_DIR" ]] || return 0
    # Send `-O exit` to any active masters so they tear down cleanly.
    for sock in "${STABILIZER_SSH_CONTROL_DIR}"/cm-*; do
        [[ -S "$sock" ]] || continue
        ssh -o ControlPath="$sock" -O exit dummy 2>/dev/null || true
    done
    rm -rf "$STABILIZER_SSH_CONTROL_DIR" 2>/dev/null || true
}
trap ssh_mux_cleanup EXIT INT TERM

# --- Risky patches (defaults changed Apr 2026 after gateway/SDS incidents) ---
# Merge-patching EnvoyProxy with extra volumeMounts broke dataplane Service generation on some labs.
STABILIZER_ENVOYPROXY_VOLUMES="${STABILIZER_ENVOYPROXY_VOLUMES:-0}"
# Patching envoy-gateway (operator) probes correlated with widespread probe storms; off by default.
STABILIZER_PATCH_ENVOY_GATEWAY_PROBES="${STABILIZER_PATCH_ENVOY_GATEWAY_PROBES:-0}"
STABILIZER_PATCH_CAPI_IPAM_PROBES="${STABILIZER_PATCH_CAPI_IPAM_PROBES:-0}"
STABILIZER_PATCH_PRELUDE_PROBES="${STABILIZER_PATCH_PRELUDE_PROBES:-1}"
# When prelude probe patches run, use >=10s in nested labs (5s was too aggressive under CPU steal).
STABILIZER_PROBE_TIMEOUT_SECONDS="${STABILIZER_PROBE_TIMEOUT_SECONDS:-10}"
# After core fixes, fail if hashed envoy dataplane Services never appear (0 = warn only).
STABILIZER_GATEWAY_PREFLIGHT_STRICT="${STABILIZER_GATEWAY_PREFLIGHT_STRICT:-0}"

# Lab CPU tuning (see VCFA_Complete_Stabilizer_README.md)
VMSP_POLICIES_NAMESPACE="${VMSP_POLICIES_NAMESPACE:-vmsp-policies}"
PROMETHEUS_NAME="${PROMETHEUS_NAME:-kube-prometheus-stack-prometheus}"
KYVERNO_ADMISSION_DEPLOY="${KYVERNO_ADMISSION_DEPLOY:-kyverno-admission-controller}"
KYVERNO_ADMISSION_REPLICAS_ROLLBACK="${KYVERNO_ADMISSION_REPLICAS_ROLLBACK:-3}"
PROVISIONING_DEPLOY="${PROVISIONING_DEPLOY:-provisioning-service-app}"
CPU_TUNE_SETTLE_SECONDS="${CPU_TUNE_SETTLE_SECONDS:-45}"
# Set to 1 to abort remaining tune steps if vcfa-control-plane-watch.sh exits non-zero
CPU_TUNE_STRICT="${CPU_TUNE_STRICT:-0}"
# Set to 1 to abort remaining tune steps if any /automation HTTP check is not 200
CPU_TUNE_REQUIRE_HTTP_200="${CPU_TUNE_REQUIRE_HTTP_200:-0}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}$1${NC}"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

info() {
    echo -e "${CYAN}[INFO]${NC} $1"
}

# Build the kubectl invocation prefix for inline probes (vcfa_ssh_nosudo) so callers don't have to
# remember whether to prepend "echo $pwq | sudo -S". When VCFA_USER == root we run it directly.
# Result: a string that can be concatenated with "kubectl ..." to form a full command line.
vcfa_kubectl_invoker() {
    local pwq
    if [[ "${VCFA_USER}" == "root" ]]; then
        echo ""
    else
        pwq=$(printf '%q' "$VCFA_PASSWORD")
        echo "echo ${pwq} | sudo -S "
    fi
}

# Run a shell snippet on the VCFA appliance (no execute_remote scaffolding). Used for kubectl-only probes.
# v2.4: switched to base64 transport to survive double-SSH quoting (jumphost -> VCFA strips one layer of
# shell quotes per hop, breaking curl -w "%{http_code}" and similar).
vcfa_ssh_nosudo() {
    local remote_cmd="$1"
    local b64 inner
    b64=$(printf '%s' "$remote_cmd" | tr -d '\r' | base64 -w0)
    # v2.4.1: pass the whole multi-statement pipeline directly to ssh (no outer `bash -c`).
    # Remote login shell parses pipes/redirs; otherwise `bash -c` only takes the first word.
    inner="echo ${b64} | base64 -d | bash --norc --noprofile"
    if [[ -n "${STABILIZER_VCFA_IDENTITY_FILE:-}" ]]; then
        if [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
            local id_q
            id_q=$(printf '%q' "$STABILIZER_VCFA_IDENTITY_FILE")
            ssh -q -T "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
                "ssh -i ${id_q} -q -T -o IdentitiesOnly=yes -o PubkeyAuthentication=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no ${VCFA_USER}@${VCFA_HOST} \"${inner//\"/\\\"}\""
        else
            # shellcheck disable=SC2086
            ssh -q -T -i "$STABILIZER_VCFA_IDENTITY_FILE" -o IdentitiesOnly=yes -o PubkeyAuthentication=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no \
                "${VCFA_USER}@${VCFA_HOST}" "$inner"
        fi
    elif [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
        local cred_q
        cred_q=$(printf '%q' "$CREDS_ON_JUMP")
        ssh -q -T "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
            "export SSHPASS=\$(head -1 ${cred_q} | tr -d '\r\n') && sshpass -e ssh -q -T ${VCFA_SSH_OPTS} ${VCFA_USER}@${VCFA_HOST} \"${inner//\"/\\\"}\""
    else
        sshpass -f "$CREDS_FILE" ssh -q -T ${VCFA_SSH_OPTS} "${VCFA_USER}@${VCFA_HOST}" "$inner"
    fi
}

# Return 3-digit HTTP code for https://auto-a.site-a.vcf.lab/automation (curl runs ON VCFA VM → hits VCFA_HTTP_VIP / kube-vip).
vcfa_curl_automation_code() {
    local raw out
    raw=$(vcfa_ssh_nosudo "curl -k -s -o /dev/null -w \"%{http_code}\" --connect-timeout 8 --resolve auto-a.site-a.vcf.lab:443:${VCFA_HTTP_VIP} https://auto-a.site-a.vcf.lab/automation 2>/dev/null || echo 000")
    out=$(printf '%s' "$raw" | tr -d '\r\n' | grep -oE '[0-9]{3}' | tail -1)
    [[ "$out" =~ ^[0-9]{3}$ ]] || out=000
    printf '%s' "$out"
}

# Function to execute commands on VCFA appliance (sudo bash).
# Transfers script via base64 (no scp dependency); strips CR; sudo password safely quoted for ssh.
execute_remote() {
    local command="$1"
    local description="$2"
    local suppress_errors="${3:-false}"
    local errsink=/dev/null
    [[ "$STABILIZER_DEBUG" == "1" ]] && errsink=/dev/stderr
    local rid="vcfa-stab-${RANDOM}-${RANDOM}.sh"
    local b64 pwq inner
    # Preserve newlines; drop CR so remote bash never sees stray ^M (breaks heredocs)
    b64=$(printf '%s' "$command" | tr -d '\r' | base64 -w0)
    pwq=$(printf '%q' "$VCFA_PASSWORD")
    # v2.4.1: send the whole pipeline as a single string to ssh (no `bash -c` wrapper) — ssh joins
    # remote argv into one cmd, so the remote login shell parses it natively. `bash -c` was eating
    # everything after the first word (pipe / redirect were lost), giving empty output.
    if [[ "${VCFA_USER}" == "root" ]]; then
        inner="echo ${b64} | base64 -d > /tmp/${rid} && bash --norc --noprofile /tmp/${rid}; ec=\$?; rm -f /tmp/${rid}; exit \$ec"
    else
        inner="echo ${b64} | base64 -d > /tmp/${rid} && echo ${pwq} | sudo -S bash --norc --noprofile /tmp/${rid}; ec=\$?; rm -f /tmp/${rid}; exit \$ec"
    fi

    log "$description"
    # shellcheck disable=SC2086
    if [[ -n "${STABILIZER_VCFA_IDENTITY_FILE:-}" ]]; then
        if [[ "$suppress_errors" == "true" ]]; then
            if [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
                local id_q
                id_q=$(printf '%q' "$STABILIZER_VCFA_IDENTITY_FILE")
                ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
                    "ssh -i ${id_q} -o IdentitiesOnly=yes -o PubkeyAuthentication=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no ${VCFA_USER}@${VCFA_HOST} \"${inner//\"/\\\"}\"" 2>"$errsink" \
                    || warning "Command failed (suppressed): $description — set STABILIZER_DEBUG=1 to see kubectl/SSH errors"
            else
                ssh -i "$STABILIZER_VCFA_IDENTITY_FILE" -o IdentitiesOnly=yes -o PubkeyAuthentication=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no \
                    "${VCFA_USER}@${VCFA_HOST}" "$inner" 2>"$errsink" \
                    || warning "Command failed (suppressed): $description — set STABILIZER_DEBUG=1 to see kubectl/SSH errors"
            fi
        else
            if [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
                local id_q
                id_q=$(printf '%q' "$STABILIZER_VCFA_IDENTITY_FILE")
                ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
                    "ssh -i ${id_q} -o IdentitiesOnly=yes -o PubkeyAuthentication=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no ${VCFA_USER}@${VCFA_HOST} \"${inner//\"/\\\"}\""
            else
                ssh -i "$STABILIZER_VCFA_IDENTITY_FILE" -o IdentitiesOnly=yes -o PubkeyAuthentication=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no \
                    "${VCFA_USER}@${VCFA_HOST}" "$inner"
            fi
        fi
    elif [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
        local cred_q
        cred_q=$(printf '%q' "$CREDS_ON_JUMP")
        if [[ "$suppress_errors" == "true" ]]; then
            ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
                "export SSHPASS=\$(head -1 ${cred_q} | tr -d '\r\n') && sshpass -e ssh ${VCFA_SSH_OPTS} ${VCFA_USER}@${VCFA_HOST} \"${inner//\"/\\\"}\"" 2>"$errsink" \
                || warning "Command failed (suppressed): $description — set STABILIZER_DEBUG=1 to see kubectl/SSH errors"
        else
            ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
                "export SSHPASS=\$(head -1 ${cred_q} | tr -d '\r\n') && sshpass -e ssh ${VCFA_SSH_OPTS} ${VCFA_USER}@${VCFA_HOST} \"${inner//\"/\\\"}\""
        fi
    else
        if [[ "$suppress_errors" == "true" ]]; then
            sshpass -f "$CREDS_FILE" ssh ${VCFA_SSH_OPTS} "${VCFA_USER}@${VCFA_HOST}" \
                "$inner" 2>"$errsink" \
                || warning "Command failed (suppressed): $description — set STABILIZER_DEBUG=1 to see kubectl/SSH errors"
        else
            sshpass -f "$CREDS_FILE" ssh ${VCFA_SSH_OPTS} "${VCFA_USER}@${VCFA_HOST}" \
                "$inner"
        fi
    fi
}

# --- Stability verification (inlined; same role as vcfa-verify-stability.sh) ---
# CPU_TUNE_STRICT=1: control-plane watch must pass. CPU_TUNE_REQUIRE_HTTP_200=1: all /automation checks must be 200.
run_stability_verification() {
    local tag="${1:-verify}"
    echo ""
    info "=== Stability verification (${tag}) ==="

    local cp_watch="${SCRIPT_DIR}/vcfa-control-plane-watch.sh"
    [[ -x "$cp_watch" ]] || cp_watch="/home/holuser/vcfa-control-plane-watch.sh"

    if [[ -x "$cp_watch" ]]; then
        echo "--- Control plane watch ---"
        if [[ "${CPU_TUNE_STRICT}" == "1" ]]; then
            if ! "$cp_watch"; then
                error "Control-plane watch failed (CPU_TUNE_STRICT=1)"
                return 1
            fi
        else
            "$cp_watch" || true
        fi
    fi

    echo "Testing VCFA /automation (3x) from VCFA VM → gateway LB ${VCFA_HTTP_VIP}..."
    local i http_fail=0 result
    for i in 1 2 3; do
        result=$(vcfa_curl_automation_code)
        echo "  Test ${i}: HTTP ${result}"
        [[ "$result" == "200" ]] || http_fail=1
    done
    if [[ "${CPU_TUNE_REQUIRE_HTTP_200}" == "1" ]] && [[ "$http_fail" -ne 0 ]]; then
        error "HTTP /automation not all 200 (CPU_TUNE_REQUIRE_HTTP_200=1)"
        return 1
    fi

    echo "Critical pod status (vmsp-platform gateways):"
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n $VMSP_NAMESPACE | grep -E '(vmsp-gateway|vcfa-gateway|envoy-gateway)'" \
        "Gateways" true
    echo "Critical pod status (prelude):"
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n $PRELUDE_NAMESPACE | grep -E '(encryption-manager|intent-server|vcfa-service-manager)'" \
        "Prelude subset" true
    echo "Problematic pods (cluster-wide):"
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -A | grep -E '(CrashLoopBackOff|ImagePullBackOff|Error)' || true" \
        "Problematic pods" true

    success "Stability verification completed: ${tag}"
    return 0
}

cpu_tune_settle() {
    local sec="${CPU_TUNE_SETTLE_SECONDS:-45}"
    log "Settling ${sec}s before next tune block..."
    sleep "$sec"
}

cpu_tune_apply() {
    log "=== LAB CPU TUNE: apply ==="
    local K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER"

    execute_remote "${K} patch prometheus ${PROMETHEUS_NAME} -n ${VMSP_NAMESPACE} --type merge -p '{\"spec\":{\"scrapeInterval\":\"60s\",\"evaluationInterval\":\"60s\",\"retentionSize\":\"4GiB\"}}'" \
        "CPU tune: Prometheus (60s scrape/eval, 4GiB retention cap)" true
    if ! run_stability_verification "after-prometheus"; then return 1; fi

    cpu_tune_settle

    local name
    for name in logging-operator logging-operator-filelogs logging-operator-systemlogs; do
        execute_remote "${K} patch fluentbitagent ${name} -n ${VMSP_NAMESPACE} --type merge -p '{\"spec\":{\"flush\":10}}'" \
            "CPU tune: FluentbitAgent ${name} flush=10" true
        execute_remote "${K} patch fluentbitagent ${name} -n ${VMSP_NAMESPACE} --type merge -p '{\"spec\":{\"metrics\":{\"interval\":\"120s\",\"path\":\"/metrics\",\"port\":2021,\"prometheusAnnotations\":false,\"serviceMonitor\":false}}}'" \
            "CPU tune: FluentbitAgent ${name} metrics.interval=120s" true
    done
    if ! run_stability_verification "after-fluent"; then return 1; fi

    cpu_tune_settle

    execute_remote "${K} scale deploy ${KYVERNO_ADMISSION_DEPLOY} -n ${VMSP_POLICIES_NAMESPACE} --replicas=1" \
        "CPU tune: Kyverno ${KYVERNO_ADMISSION_DEPLOY} replicas=1 (lab)" true
    if ! run_stability_verification "after-kyverno"; then return 1; fi

    cpu_tune_settle

    execute_remote "$(cat <<EOS
set -e
KUBECTL="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=${API_SERVER}"
if \$KUBECTL get deploy ${PROVISIONING_DEPLOY} -n ${PRELUDE_NAMESPACE} -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="JAVA_TOOL_OPTIONS")].value}' 2>/dev/null | grep -q .; then
  echo "JAVA_TOOL_OPTIONS already set — skipping"
  exit 0
fi
\$KUBECTL patch deploy ${PROVISIONING_DEPLOY} -n ${PRELUDE_NAMESPACE} --type json -p '[{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"JAVA_TOOL_OPTIONS","value":"-Dmanagement.prometheus.metrics.export.exemplars.enabled=false"}}]'
EOS
)" "CPU tune: JAVA_TOOL_OPTIONS on ${PROVISIONING_DEPLOY}" true

    cpu_tune_settle
    if ! run_stability_verification "after-java"; then return 1; fi
    success "Lab CPU tune apply finished"
    return 0
}

cpu_tune_rollback() {
    log "=== LAB CPU TUNE: rollback ==="
    local K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER"

    execute_remote "$(cat <<EOS
set -e
KUBECTL="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=${API_SERVER}"
command -v jq >/dev/null 2>&1 || { echo "jq required on appliance for Java rollback"; exit 1; }
\$KUBECTL get deploy ${PROVISIONING_DEPLOY} -n ${PRELUDE_NAMESPACE} -o json | \\
  jq '.spec.template.spec.containers[0].env |= map(select(.name != "JAVA_TOOL_OPTIONS"))' | \\
  \$KUBECTL replace -f -
EOS
)" "Rollback: remove JAVA_TOOL_OPTIONS from ${PROVISIONING_DEPLOY}" true

    run_stability_verification "rollback-after-java" || true

    cpu_tune_settle

    execute_remote "${K} scale deploy ${KYVERNO_ADMISSION_DEPLOY} -n ${VMSP_POLICIES_NAMESPACE} --replicas=${KYVERNO_ADMISSION_REPLICAS_ROLLBACK}" \
        "Rollback: Kyverno admission replicas=${KYVERNO_ADMISSION_REPLICAS_ROLLBACK}" true
    run_stability_verification "rollback-after-kyverno" || true

    cpu_tune_settle

    for name in logging-operator logging-operator-filelogs logging-operator-systemlogs; do
        execute_remote "${K} patch fluentbitagent ${name} -n ${VMSP_NAMESPACE} --type json -p '[{\"op\":\"remove\",\"path\":\"/spec/flush\"}]'" \
            "Rollback: remove FluentbitAgent ${name} spec.flush" true
        execute_remote "${K} patch fluentbitagent ${name} -n ${VMSP_NAMESPACE} --type merge -p '{\"spec\":{\"metrics\":{\"interval\":\"60s\",\"path\":\"/metrics\",\"port\":2021,\"prometheusAnnotations\":false,\"serviceMonitor\":false}}}'" \
            "Rollback: FluentbitAgent ${name} metrics.interval=60s" true
    done
    run_stability_verification "rollback-after-fluent" || true

    cpu_tune_settle

    execute_remote "${K} patch prometheus ${PROMETHEUS_NAME} -n ${VMSP_NAMESPACE} --type merge -p '{\"spec\":{\"scrapeInterval\":\"30s\",\"evaluationInterval\":\"30s\",\"retentionSize\":\"8GiB\"}}'" \
        "Rollback: Prometheus 30s scrape/eval, 8GiB retentionSize" true
    run_stability_verification "rollback-after-prometheus" || true

    success "Lab CPU tune rollback finished"
    return 0
}

# Function to check prerequisites
check_prerequisites() {
    log "Checking prerequisites..."
    ssh_mux_init

    if [[ -z "${STABILIZER_VCFA_IDENTITY_FILE:-}" ]]; then
        if ! command -v sshpass &> /dev/null; then
            error "sshpass is required for password auth (or set STABILIZER_VCFA_IDENTITY_FILE). Install: sudo apt-get install sshpass"
            exit 1
        fi
    fi

    if [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
        if ! ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" "test -f $(printf '%q' "$CREDS_ON_JUMP")" &>/dev/null; then
            error "Jump host $STABILIZER_JUMP_HOST cannot read CREDS_ON_JUMP=$CREDS_ON_JUMP (set CREDS_ON_JUMP to the password file path ON the jump host)."
            exit 1
        fi
        if [[ -z "${STABILIZER_VCFA_IDENTITY_FILE:-}" ]]; then
            if ! ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" "command -v sshpass" &>/dev/null; then
                error "sshpass must be installed ON $STABILIZER_JUMP_HOST for the second SSH hop."
                exit 1
            fi
        fi
    else
        if [[ ! -f "$CREDS_FILE" ]]; then
            error "Credentials file $CREDS_FILE not found"
            exit 1
        fi
    fi

    log "Testing SSH connectivity to VCFA appliance..."
    if [[ -n "${STABILIZER_VCFA_IDENTITY_FILE:-}" ]]; then
        if [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
            if ! ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
                "ssh -i $(printf '%q' "$STABILIZER_VCFA_IDENTITY_FILE") -o BatchMode=yes -o StrictHostKeyChecking=no ${VCFA_USER}@${VCFA_HOST} echo ok" &>/dev/null; then
                error "Cannot SSH (via jump + key) to VCFA at $VCFA_HOST"
                exit 1
            fi
        else
            if ! ssh -i "$STABILIZER_VCFA_IDENTITY_FILE" -o BatchMode=yes -o StrictHostKeyChecking=no "${VCFA_USER}@${VCFA_HOST}" "echo ok" &>/dev/null; then
                error "Cannot SSH to VCFA appliance at $VCFA_HOST"
                exit 1
            fi
        fi
    elif [[ -n "${STABILIZER_JUMP_HOST:-}" ]]; then
        local cred_q
        cred_q=$(printf '%q' "$CREDS_ON_JUMP")
        if ! ssh "${STABILIZER_SSH_MUX_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no "$STABILIZER_JUMP_HOST" \
            "export SSHPASS=\$(head -1 ${cred_q} | tr -d '\r\n') && sshpass -e ssh ${VCFA_SSH_OPTS} ${VCFA_USER}@${VCFA_HOST} echo ok" &>/dev/null; then
            error "Cannot SSH from ${STABILIZER_JUMP_HOST} to VCFA at $VCFA_HOST (wrong password, lockout, or add STABILIZER_VCFA_IDENTITY_FILE)."
            exit 1
        fi
    else
        if ! sshpass -f "$CREDS_FILE" ssh ${VCFA_SSH_OPTS} -o ConnectTimeout=10 "${VCFA_USER}@${VCFA_HOST}" "echo ok" &>/dev/null; then
            error "Cannot connect to VCFA appliance at $VCFA_HOST"
            exit 1
        fi
    fi

    log "Testing Kubernetes API connectivity..."
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER cluster-info | head -2" \
        "Testing Kubernetes API access" true

    success "Prerequisites check passed"
}
# Function to wait for API Server to be responsive
wait_for_api_server() {
    log "Waiting for Kubernetes API server to become responsive..."
    local max_wait=180
    local elapsed=0
    local kc
    kc=$(vcfa_kubectl_invoker)
    
    while [[ $elapsed -lt $max_wait ]]; do
        if vcfa_ssh_nosudo "${kc}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get --raw /healthz" &>/dev/null; then
            success "API server is responsive."
            return 0
        fi
        sleep 10
        elapsed=$((elapsed + 10))
        info "Still waiting for API server... (${elapsed}s)"
    done
    warning "Timed out waiting for API server after ${max_wait}s. Proceeding anyway, but subsequent steps may fail."
}

# Function to get current system status
get_system_status() {
    log "Getting current VCFA system status..."
    
    echo ""
    info "=== VCFA Core Components (vmsp-platform namespace) ==="
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n $VMSP_NAMESPACE | grep -E '(vmsp-gateway|vcfa-gateway|envoy-gateway|capi-ipam|synthetic-checker)'" \
        "Getting VCFA core pod status" true
    
    echo ""
    info "=== Authentication Services (prelude namespace) ==="
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n $PRELUDE_NAMESPACE | grep -E '(authentication|resource-manager|account-manager|encryption-manager|intent-server|vcfa-service-manager)'" \
        "Getting authentication services status" true
    
    echo ""
    info "=== VCFA Endpoint Test (via gateway LB ${VCFA_HTTP_VIP}) ==="
    local endpoint_result
    endpoint_result=$(vcfa_curl_automation_code)
    
    if [[ "$endpoint_result" == "200" ]]; then
        success "VCFA endpoint responding correctly (HTTP 200)"
    else
        warning "VCFA endpoint returned HTTP $endpoint_result"
    fi
}

# Function to fix authentication services with targeted approach
fix_authentication_services() {
    log "Applying authentication service stabilization fixes..."
    
    # List of critical authentication services that commonly fail
    local auth_services=(
        "encryption-manager"
        "intent-server"
        "vcfa-service-manager"
        "account-manager-server"
        "resource-manager-server"
    )
    
    for service in "${auth_services[@]}"; do
        log "Processing $service..."
        
        # Check if service exists and get current status
        local pod_info kc
        kc=$(vcfa_kubectl_invoker)
        pod_info=$(vcfa_ssh_nosudo "${kc}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n $PRELUDE_NAMESPACE | grep $service" 2>/dev/null || echo "")
        
        if [[ -z "$pod_info" ]]; then
            warning "$service not found, skipping..."
            continue
        fi
        
        # Check if service is in CrashLoopBackOff
        if echo "$pod_info" | grep -q "CrashLoopBackOff"; then
            warning "$service is in CrashLoopBackOff, restarting pod..."
            local pod_name
            pod_name=$(echo "$pod_info" | awk '{print $1}')
            execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER delete pod $pod_name -n $PRELUDE_NAMESPACE" \
                "Restarting $service pod" true
            sleep 2
        fi
        
        if [[ "${STABILIZER_PATCH_PRELUDE_PROBES}" == "1" ]]; then
            log "Applying probe timeout fixes to $service (timeoutSeconds=${STABILIZER_PROBE_TIMEOUT_SECONDS})..."
            execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get deployment $service -n $PRELUDE_NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].livenessProbe}' | grep -q . && kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment $service -n $PRELUDE_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]' || true" \
                "Applying liveness probe timeout fix to $service (skip if no livenessProbe)" true

            execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get deployment $service -n $PRELUDE_NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].readinessProbe}' | grep -q . && kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment $service -n $PRELUDE_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/readinessProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]' || true" \
                "Applying readiness probe timeout fix to $service (skip if no readinessProbe)" true

            execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get deployment $service -n $PRELUDE_NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].startupProbe}' | grep -q . && kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment $service -n $PRELUDE_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/startupProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]' || true" \
                "Applying startup probe timeout fix to $service (skip if no startupProbe)" true
        fi
    done
}

# Confirm north/south dataplane Services exist. Some builds expose kube-vip LBs (vcfa-gateway-configuration /
# vmsp-gateway) without separate hashed envoy-vmsp-platform-* Service names.
verify_envoy_dataplane_services() {
    log "Checking for gateway dataplane Services in ${VMSP_NAMESPACE}..."
    local kc lb hashed
    kc=$(vcfa_kubectl_invoker)
    # Count the two kube-vip LB rows from a plain table (first column = Service name).
    lb=$(vcfa_ssh_nosudo "${kc}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get svc -n $VMSP_NAMESPACE --no-headers 2>/dev/null | grep -cE '^(vcfa-gateway-configuration|vmsp-gateway)[[:space:]]' || true" 2>/dev/null | tr -d '[:space:]')
    hashed=$(vcfa_ssh_nosudo "${kc}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get svc -n $VMSP_NAMESPACE -o name 2>/dev/null | grep -c envoy-vmsp-platform || true" 2>/dev/null | tr -d '[:space:]')
    if [[ "${hashed:-0}" =~ ^[0-9]+$ ]] && [[ "$hashed" -ge 1 ]]; then
        success "Hashed Envoy dataplane Service(s) present (count=$hashed)."
        return 0
    fi
    if [[ "${lb:-0}" =~ ^[0-9]+$ ]] && [[ "$lb" -ge 2 ]]; then
        success "Gateway LoadBalancer Services present (vcfa-gateway-configuration + vmsp-gateway)."
        return 0
    fi
    warning "Gateway dataplane check inconclusive (lb_lines=${lb:-?} hashed=${hashed:-?})."
    if [[ "${STABILIZER_GATEWAY_PREFLIGHT_STRICT}" == "1" ]]; then
        error "STABILIZER_GATEWAY_PREFLIGHT_STRICT=1. See --repair-envoyproxy, --recover-gateway-503, and incident doc (SDS/upstream TLS)."
        return 1
    fi
    return 0
}

# When curl to /automation returns 503 but the LB and gateway pods are Ready, roll SDS / upstream stacks.
# Order: trust-manager + cert-manager → prelude SDS-backed backends (automation UI, tenant-manager) →
# provisioning → hashed dataplane envoys → vcfa/vmsp gateway → envoy-gateway operator.
recover_gateway_http_503() {
    log "Recover: rolling restarts for HTTP 503 / upstream-TLS (SDS) symptoms (see VCFA_Stabilizer_Incident_Apr2026.md)..."
    local K
    K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER"

    execute_remote "${K} get deployment trust-manager -n cert-manager &>/dev/null && ${K} rollout restart deployment/trust-manager -n cert-manager && ${K} rollout status deployment/trust-manager -n cert-manager --timeout=300s || true" \
        "Rollout restart cert-manager/trust-manager (if present)" true
    sleep 20
    execute_remote "${K} get deployment trust-manager -n ${VMSP_NAMESPACE} &>/dev/null && ${K} rollout restart deployment/trust-manager -n ${VMSP_NAMESPACE} && ${K} rollout status deployment/trust-manager -n ${VMSP_NAMESPACE} --timeout=300s || true" \
        "Rollout restart vmsp-platform/trust-manager (if present)" true
    sleep 15
    execute_remote "${K} get deployment trust-manager-sds-server -n cert-manager &>/dev/null && ${K} rollout restart deployment/trust-manager-sds-server -n cert-manager && ${K} rollout status deployment/trust-manager-sds-server -n cert-manager --timeout=300s || true" \
        "Rollout restart cert-manager/trust-manager-sds-server (if present)" true
    sleep 15
    execute_remote "${K} get deployment cert-manager -n cert-manager &>/dev/null && ${K} rollout restart deployment/cert-manager -n cert-manager && ${K} rollout status deployment/cert-manager -n cert-manager --timeout=300s || true" \
        "Rollout restart cert-manager/cert-manager controller (if present)" true
    sleep 15
    # Prelude apps that commonly appear as upstream_host in Envoy for /automation and /provider (SDS client certs to them).
    execute_remote "for d in cloud-automation-ui-app tenant-manager-server tenant-manager-app tenant-manager; do ${K} get deployment \$d -n ${PRELUDE_NAMESPACE} &>/dev/null && { echo restart prelude/\$d; ${K} rollout restart deployment/\$d -n ${PRELUDE_NAMESPACE}; ${K} rollout status deployment/\$d -n ${PRELUDE_NAMESPACE} --timeout=300s || true; }; sleep 8; done" \
        "Rollout restart prelude SDS upstream targets (cloud-automation-ui-app, tenant-manager*, if present)" true
    sleep 15
    execute_remote "${K} get deployment ${PROVISIONING_DEPLOY} -n ${PRELUDE_NAMESPACE} &>/dev/null && ${K} rollout restart deployment/${PROVISIONING_DEPLOY} -n ${PRELUDE_NAMESPACE} && ${K} rollout status deployment/${PROVISIONING_DEPLOY} -n ${PRELUDE_NAMESPACE} --timeout=300s || true" \
        "Rollout restart prelude/${PROVISIONING_DEPLOY} (if present)" true
    sleep 15
    execute_remote "for d in \$(${K} get deploy -n ${VMSP_NAMESPACE} -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | grep -E '^envoy-vmsp-platform' || true); do echo restart \$d; ${K} rollout restart deployment/\$d -n ${VMSP_NAMESPACE}; ${K} rollout status deployment/\$d -n ${VMSP_NAMESPACE} --timeout=300s || true; sleep 10; done" \
        "Rollout restart envoy-vmsp-platform-* dataplane deployments (if any)" true
    sleep 15
    execute_remote "${K} get deployment vcfa-gateway-configuration -n ${VMSP_NAMESPACE} &>/dev/null && ${K} rollout restart deployment/vcfa-gateway-configuration -n ${VMSP_NAMESPACE} && ${K} rollout status deployment/vcfa-gateway-configuration -n ${VMSP_NAMESPACE} --timeout=300s || true" \
        "Rollout restart vcfa-gateway-configuration" true
    sleep 15
    execute_remote "${K} get deployment vmsp-gateway -n ${VMSP_NAMESPACE} &>/dev/null && ${K} rollout restart deployment/vmsp-gateway -n ${VMSP_NAMESPACE} && ${K} rollout status deployment/vmsp-gateway -n ${VMSP_NAMESPACE} --timeout=300s || true" \
        "Rollout restart vmsp-gateway" true
    sleep 15
    execute_remote "${K} get deployment envoy-gateway -n ${VMSP_NAMESPACE} &>/dev/null && ${K} rollout restart deployment/envoy-gateway -n ${VMSP_NAMESPACE} && ${K} rollout status deployment/envoy-gateway -n ${VMSP_NAMESPACE} --timeout=300s || true" \
        "Rollout restart envoy-gateway operator" true
    sleep 25
    success "Gateway / SDS recovery rollouts finished (verify /automation with curl or get_system_status)."
}

# v2.4: Fix the envoy-gateway v1.5.0 + Envoy v1.34.x incompatibility around BackendTLSPolicy.validation.wellKnownCACertificates: System.
# Root cause: Envoy NACKs SDS resources whose validation_context has match_typed_subject_alt_names without
# trusted_ca, with the warning "SAN-based verification of peer certificates without trusted CA is insecure
# and not allowed". envoy-gateway v1.5.0 generates exactly that shape when wellKnownCACertificates: System
# is set on the BackendTLSPolicy because the operator does not also include the system CA bundle in the
# pushed Secret. The dataplane keeps every BackendTLSPolicy SDS resource in the warming state forever,
# every upstream cluster is published with transport_socket_matches but no resolved cert, and every HTTP
# request hits "upstream_reset_before_response_started{remote_connection_failure|TLS_error:_Secret_is_not_supplied_by_SDS}"
# (HTTP 503).
#
# Fix:
#   1) Make sure a ConfigMap named "platform-trust" with key "bundle.pem" exists in EVERY namespace that
#      hosts a BackendTLSPolicy (cross-namespace caCertificateRefs are not allowed by the Gateway API).
#      We create them by copying vmsp-platform/platform-trust (it is the appliance trust bundle that
#      both Envoy dataplanes mount as /etc/ssl/certs/ca-certificates.crt).
#   2) Patch every BackendTLSPolicy that uses wellKnownCACertificates: System so that it instead points
#      at the platform-trust ConfigMap via spec.validation.caCertificateRefs and clears
#      spec.validation.wellKnownCACertificates.
#   3) Bump the envoy-gateway operator memory limit to 4Gi by patching the helmrelease values (so
#      flux/helm don't revert it). The operator OOM-kills itself on 1Gi when re-translating ~30 policies
#      because each translation pulls the full HTTPRoute graph into memory.
#   4) Force a flux helm reconcile and roll the operator + dataplanes so the new SDS bundles get pushed
#      and Envoy resubscribes from a clean state.
fix_envoy_gateway_sds_san_nack() {
    log "Fix: SDS SAN-without-CA NACK (envoy-gateway v1.5.0 + Envoy v1.34.x)..."
    local fix_script
    # shellcheck disable=SC2016
    fix_script=$(cat <<REMOTE
set -u
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=${API_SERVER}"

echo "=== 1/5: ensure platform-trust ConfigMap exists in every BackendTLSPolicy namespace ==="
NS_LIST=\$(\$K get backendtlspolicy -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\\n"}{end}' 2>/dev/null | sort -u | grep -v '^\$' || true)
if [[ -z "\$NS_LIST" ]]; then
    echo "  no BackendTLSPolicy resources, skipping"
    exit 0
fi
SRC_NS="${VMSP_NAMESPACE}"
\$K get configmap -n "\$SRC_NS" platform-trust >/dev/null 2>&1 || { echo "  ERROR: vmsp-platform/platform-trust missing — abort"; exit 2; }
for NS in \$NS_LIST; do
    if [[ "\$NS" == "\$SRC_NS" ]]; then
        echo "  \$NS/platform-trust: source"; continue
    fi
    if \$K get configmap -n "\$NS" platform-trust >/dev/null 2>&1; then
        echo "  \$NS/platform-trust: exists"
    else
        echo "  copying \$SRC_NS/platform-trust -> \$NS/platform-trust"
        \$K get configmap -n "\$SRC_NS" platform-trust -o yaml \\
          | sed "s/namespace: \$SRC_NS/namespace: \$NS/" \\
          | sed '/uid:/d' | sed '/resourceVersion:/d' | sed '/creationTimestamp:/d' | sed '/ownerReferences:/,/^[^ ]/ d' \\
          | \$K apply -f - || echo "    WARN: copy failed for \$NS"
    fi
done

echo
echo "=== 2/5: patch every BackendTLSPolicy with wellKnownCACertificates: System -> caCertificateRefs ==="
PATCHED=0
\$K get backendtlspolicy -A -o jsonpath='{range .items[?(@.spec.validation.wellKnownCACertificates=="System")]}{.metadata.namespace} {.metadata.name}{"\\n"}{end}' \\
  | while read NS NAME; do
        [[ -z "\$NS" ]] && continue
        echo "  patch \$NS/\$NAME"
        \$K patch backendtlspolicy -n "\$NS" "\$NAME" --type=merge -p '{"spec":{"validation":{"caCertificateRefs":[{"group":"","kind":"ConfigMap","name":"platform-trust"}],"wellKnownCACertificates":null}}}' >/dev/null 2>&1 || echo "    WARN: patch failed"
    done
echo "  policies remaining with wellKnownCACertificates=System:"
\$K get backendtlspolicy -A -o jsonpath='{range .items[?(@.spec.validation.wellKnownCACertificates=="System")]}{.metadata.namespace}/{.metadata.name}{"\\n"}{end}' | head

echo
echo "=== 3/5: bump envoy-gateway operator memory limit (helmrelease values) ==="
if \$K get helmrelease -n "${VMSP_NAMESPACE}" envoyproxy-gateway >/dev/null 2>&1; then
    CUR_LIM=\$(\$K get deploy -n "${VMSP_NAMESPACE}" envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].resources.limits.memory}' 2>/dev/null || echo "")
    echo "  current operator memory limit: \${CUR_LIM:-<unset>}"
    if [[ "\$CUR_LIM" != "4Gi" ]]; then
        # Try the canonical key first, then the chart-flat key as a fallback. Either path is no-op
        # if the chart doesn't honour that key, but with v1.5.0-3 one of them lands.
        \$K patch helmrelease -n "${VMSP_NAMESPACE}" envoyproxy-gateway --type=merge \\
            -p '{"spec":{"values":{"deployment":{"envoyGateway":{"resources":{"limits":{"memory":"4Gi"},"requests":{"cpu":"100m","memory":"512Mi"}}}}}}}' >/dev/null 2>&1 || true
        \$K patch helmrelease -n "${VMSP_NAMESPACE}" envoyproxy-gateway --type=merge \\
            -p '{"spec":{"values":{"resources":{"limits":{"memory":"4Gi"},"requests":{"cpu":"100m","memory":"512Mi"}}}}}' >/dev/null 2>&1 || true
        # Force flux to apply the values change.
        \$K annotate helmrelease -n "${VMSP_NAMESPACE}" envoyproxy-gateway "reconcile.fluxcd.io/requestedAt=\$(date +%s)" --overwrite >/dev/null 2>&1 || true
        echo "  helmrelease patched + reconcile annotation set, waiting 35s..."
        sleep 35
        # Direct deployment patch as a belt-and-braces fallback if the helm values key isn't honoured.
        NEW_LIM=\$(\$K get deploy -n "${VMSP_NAMESPACE}" envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].resources.limits.memory}' 2>/dev/null || echo "")
        if [[ "\$NEW_LIM" != "4Gi" ]]; then
            echo "  helm values didn't take effect, falling back to direct deployment patch"
            \$K set resources deploy/envoy-gateway -n "${VMSP_NAMESPACE}" --limits=memory=4Gi --requests=memory=512Mi >/dev/null 2>&1 || echo "    WARN: deployment patch failed"
        fi
        \$K rollout status deploy/envoy-gateway -n "${VMSP_NAMESPACE}" --timeout=120s || true
    else
        echo "  already 4Gi, no change"
    fi
else
    echo "  helmrelease envoyproxy-gateway not found, falling back to direct deployment patch"
    \$K set resources deploy/envoy-gateway -n "${VMSP_NAMESPACE}" --limits=memory=4Gi --requests=memory=512Mi >/dev/null 2>&1 || true
fi

echo
echo "=== 4/5: install durable systemd watcher to re-assert envoy-gateway memory on drift ==="
# vmsp-operator owns spec.values on the HelmRelease and clobbers our 4Gi bump on every reconcile.
# This watcher (every 60s) re-applies the deployment-level memory limit if it drifts from 4Gi,
# and survives reboots. Idempotent install (overwrite ok).
mkdir -p /usr/local/bin /etc/systemd/system
cat > /usr/local/bin/vcfa-eg-mem-keeper.sh <<'EGKEEPER'
#!/usr/bin/env bash
set -u
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
NS="vmsp-platform"
WANT_LIM="4Gi"
WANT_REQ="512Mi"
\$K -n "\$NS" get deploy envoy-gateway >/dev/null 2>&1 || exit 0
CUR_LIM=\$(\$K -n "\$NS" get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].resources.limits.memory}' 2>/dev/null || echo "")
CUR_REQ=\$(\$K -n "\$NS" get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].resources.requests.memory}' 2>/dev/null || echo "")
if [[ "\$CUR_LIM" != "\$WANT_LIM" || "\$CUR_REQ" != "\$WANT_REQ" ]]; then
    \$K -n "\$NS" set resources deploy/envoy-gateway --limits=memory=\$WANT_LIM --requests=memory=\$WANT_REQ >/dev/null 2>&1 \
        && echo "\$(date -Is) drift: limits=\$CUR_LIM->\$WANT_LIM requests=\$CUR_REQ->\$WANT_REQ"
fi
EGKEEPER
chmod +x /usr/local/bin/vcfa-eg-mem-keeper.sh

cat > /etc/systemd/system/vcfa-eg-mem-keeper.service <<'EGSVC'
[Unit]
Description=VCFA: keep envoy-gateway operator memory limit at 4Gi (works around vmsp-operator/HR drift)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vcfa-eg-mem-keeper.sh
StandardOutput=journal
StandardError=journal
EGSVC

cat > /etc/systemd/system/vcfa-eg-mem-keeper.timer <<'EGTIMER'
[Unit]
Description=VCFA: run vcfa-eg-mem-keeper every 60s (drift watcher)

[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s
Unit=vcfa-eg-mem-keeper.service

[Install]
WantedBy=timers.target
EGTIMER

systemctl daemon-reload
systemctl enable --now vcfa-eg-mem-keeper.timer >/dev/null 2>&1 || true
systemctl --no-pager --full status vcfa-eg-mem-keeper.timer 2>&1 | head -10 || true

echo
echo "=== 5/5: roll dataplanes so they re-subscribe and pick up the new SDS bundles ==="
\$K rollout restart deploy/vcfa-gateway-configuration -n "${VMSP_NAMESPACE}" >/dev/null 2>&1 || true
\$K rollout restart deploy/vmsp-gateway -n "${VMSP_NAMESPACE}" >/dev/null 2>&1 || true
\$K rollout status deploy/vcfa-gateway-configuration -n "${VMSP_NAMESPACE}" --timeout=180s || true
\$K rollout status deploy/vmsp-gateway -n "${VMSP_NAMESPACE}" --timeout=180s || true
echo "  waiting 45s for SDS to settle..."
sleep 45

echo
echo "=== verify SDS state on dataplanes ==="
for DEP in vcfa-gateway-configuration vmsp-gateway; do
    DP=\$(\$K get pods -n "${VMSP_NAMESPACE}" --no-headers -o name 2>/dev/null | grep "^pod/\${DEP}-" | head -1 | sed 's|pod/||')
    [[ -z "\$DP" ]] && { echo "  \${DEP}: no pod yet"; continue; }
    echo "  \$DEP pod: \$DP"
    \$K exec -n "${VMSP_NAMESPACE}" "\$DP" -c envoy -- curl -s --max-time 15 'http://127.0.0.1:19000/config_dump' 2>/dev/null \\
      | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
except Exception as e:
    print('    parse error', e); sys.exit(0)
for c in d.get('configs',[]):
    if 'SecretsConfigDump' in c.get('@type',''):
        a=len(c.get('dynamic_active_secrets',[])); w=len(c.get('dynamic_warming_secrets',[]))
        print('    ACTIVE=%d  WARMING=%d' % (a, w))
        for s in c.get('dynamic_warming_secrets', [])[:5]:
            print('      WARMING:', s.get('name'))" 2>&1 || echo "    config_dump failed"
done
echo "=== done ==="
REMOTE
)
    execute_remote "$fix_script" "Fix SDS SAN-without-CA NACK (BackendTLSPolicy + operator memory)" false
}

# v2.5: Fix the "control plane overload death spiral" we hit on auto-platform-a-558jg (Apr 30 2026):
# Symptoms:
#   * load avg 500-900, single-node "control plane" with kubelet+kube-apiserver pinned at 99-230% CPU
#   * Kubernetes Lease objects get rewritten to leaseDurationSeconds=1 (kube-vip "plndr-cp-lock")
#   * kube-vip panics in client-go leaderelection.RunOrDie -> deletes the VIP (10.1.1.72) -> all
#     in-cluster controllers can't reach kubernetes.default.svc -> dial tcp 10.1.1.72:6443: no route to host
#   * kube-controller-manager / kube-scheduler / capi / metrics-server lose their leases every 5-10s
#   * CAPI HelmReleases / HelmRelease retrieval fail with the same "no route to host" -> reconcile churn
#   * kyverno admission webhooks (failurePolicy: Fail) reject all writes the moment one replica blips,
#     which in turn breaks kube-controller-manager job/replicaset/deployment reconciliation
#   * etcd db slack ratio climbs (we saw 83% slack on a 329MB db) -> every read/write is slow
# The fix below, in order, is the minimum that brings the cluster back without reboot:
#   1) defrag etcd
#   2) ensure 10.1.1.72/32 is on eth0 (kube-vip will own it once it's stable; until then we own it),
#      and ensure gateway VIPs (.69, .70) are also pinned non-deprecated as a backstop
#   3) bump kube-vip lease (60s) + renew (45s) + retry (10s), set vip_preserve_on_leadership_loss=true
#      and rebuild the corrupted plndr-cp-lock lease
#   4) bump probes on kube-apiserver / kube-controller-manager / kube-scheduler static manifests
#   5) durable kyverno webhook fix:
#      - SUSPEND kyverno HelmRelease in vmsp-platform (it lives there, NOT in vmsp-policies)
#        so flux/helm-controller will stop reconciling Deployment args back
#      - Patch kyverno-admission-controller Deployment with --forceFailurePolicyIgnore=true so
#        Kyverno's own reconciler keeps the resource webhook at failurePolicy: Ignore
#      - The 5 "policy-config" webhooks (cel-exception, cleanup, exception, global-context,
#        policy) stay at Fail intentionally — they only validate Kyverno's CRDs, not normal traffic
#      - Fall back to flipping individual webhook configs to Ignore if the deployment patch fails
#      - Set STABILIZER_KEEP_KYVERNO_FAIL=1 to skip step 5 entirely
# Lessons learned from the secondary-cascade observation on auto-platform-a-558jg:
#   * Rolling out the kyverno-admission-controller deployment (3 -> 2 -> 3) creates a burst of image
#     pulls + lease churn that briefly re-overloads kube-vip and zot. Expect a 1-2 minute load spike
#     after this fix runs; load returns to normal within ~5 minutes. Don't panic and re-run.
#   * After this fix, you should "ip addr replace" the manual VIPs to non-deprecated so they don't
#     get aged out by the kernel before kube-vip fully owns them again.
fix_overload_recovery() {
    log "Control-plane preflight (etcd defrag + kube-vip lease + control-plane probes + kyverno failurePolicy)..."
    local cp_vip="${VCFA_CP_VIP:-10.1.1.72}"
    local vmsp_gw_vip="${VMSP_GW_VIP:-10.1.1.69}"
    local vcfa_gw_vip="${VCFA_GW_VIP:-10.1.1.70}"
    local keep_fail="${STABILIZER_KEEP_KYVERNO_FAIL:-0}"
    # ETCD_DEFRAG_SLACK_PCT controls when defrag runs. Defaults to 30 (skip if slack <30%).
    # Set to 0 to always defrag, or 100 to never defrag.
    local etcd_slack_pct="${ETCD_DEFRAG_SLACK_PCT:-30}"
    # Build the remote script body using a quoted heredoc (no $/() escaping needed). We inject our
    # local variable values as bash assignments at the top of the body.
    local fix_prefix
    fix_prefix=$(printf 'set -u\nK="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=%s"\nCP_VIP="%s"\nVMSP_GW_VIP="%s"\nVCFA_GW_VIP="%s"\nETCD_SLACK_PCT="%s"\nKEEP_KYVERNO_FAIL="%s"\n' \
        "${API_SERVER}" "${cp_vip}" "${vmsp_gw_vip}" "${vcfa_gw_vip}" "${etcd_slack_pct}" "${keep_fail}")
    local fix_body
    fix_body=$(cat <<'REMOTE'

echo "=== preflight: starting load average:"
uptime
echo

echo "=== 1/5: ensure CP VIP ${CP_VIP}/32 + gateway VIPs are on eth0 (non-deprecated) ==="
# The control plane VIP must be reachable for in-cluster controllers (kubernetes.default).
# We also keep the gateway VIPs (.69, .70) pinned because kube-vip drops them during a panic too.
# 'ip addr replace ... preferred_lft forever' is idempotent and refreshes a deprecated address.
for v in "${CP_VIP}" "${VMSP_GW_VIP}" "${VCFA_GW_VIP}"; do
    [[ -z "$v" ]] && continue
    if ip -4 addr show dev eth0 2>/dev/null | grep -q "inet ${v}/32 .* preferred_lft forever"; then
        echo "  ${v}/32 already pinned non-deprecated on eth0 (no-op)"
    else
        ip addr replace ${v}/32 dev eth0 valid_lft forever preferred_lft forever 2>&1 | head -1 || true
        echo "  pinned ${v}/32 on eth0 (kube-vip will reclaim, this is a backstop)"
    fi
done

echo
echo "=== 2/5: etcd defrag (only if slack >= ${ETCD_SLACK_PCT}%) ==="
ETCDCTL='etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/peer.crt --key=/etc/kubernetes/pki/etcd/peer.key --endpoints=https://127.0.0.1:2379'
# Pull current slack% from etcdctl endpoint status JSON.
# If the apiserver/etcd is unreachable, skip silently.
SLACK=$($ETCDCTL endpoint status -w json 2>/dev/null | python3 -c "
import json, sys
try:
    d=json.load(sys.stdin)[0]['Status']
except Exception:
    sys.exit(0)
db=int(d.get('dbSize',0))
inuse=int(d.get('dbSizeInUse',0)) or db
slack = 0 if db==0 else int(100*(db-inuse)/db)
print(slack)
" 2>/dev/null || echo "")
if [[ -z "$SLACK" ]]; then
    echo "  (etcd unreachable or no metrics, skipping defrag)"
elif [[ "$SLACK" -ge "$ETCD_SLACK_PCT" ]]; then
    echo "  etcd slack=${SLACK}% >= ${ETCD_SLACK_PCT}%, defragging..."
    $ETCDCTL defrag --command-timeout=120s 2>&1 | tail -2 || echo "  (defrag failed; retry in a few minutes)"
    $ETCDCTL alarm disarm 2>&1 | head -1 || true
else
    echo "  etcd slack=${SLACK}% < ${ETCD_SLACK_PCT}%, defrag not needed (no-op)"
fi

echo
echo "=== 3/5: harden kube-vip lease/renew/retry/preserve (static pod manifest) ==="
M=/etc/kubernetes/manifests/kube-vip.yaml
if [[ -f "$M" ]]; then
    KV_NEEDS=$(python3 - <<'PY'
import re
p="/etc/kubernetes/manifests/kube-vip.yaml"
s=open(p).read()
desired={"vip_leaseduration":"60","vip_renewdeadline":"45","vip_retryperiod":"10","vip_preserve_on_leadership_loss":"true"}
def getv(text, name):
    m=re.search(r'- name: ' + re.escape(name) + r'\s*\n\s+value: "([^"]*)"', text, re.M)
    return m.group(1) if m else None
print("CHANGE" if any(getv(s,k)!=v for k,v in desired.items()) else "OK")
PY
)
    if [[ "$KV_NEEDS" == "CHANGE" ]]; then
        cp -a "$M" "${M}.bak.$(date +%s)"
        python3 - <<'PY'
import re
p="/etc/kubernetes/manifests/kube-vip.yaml"
s=open(p).read()
desired={"vip_leaseduration":"60","vip_renewdeadline":"45","vip_retryperiod":"10","vip_preserve_on_leadership_loss":"true"}
def setv(text, name, val):
    pat=re.compile(r'(- name: ' + re.escape(name) + r'\s*\n\s+value: ")[^"]*(")', re.M)
    new, n = pat.subn(r'\g<1>'+val+r'\g<2>', text)
    return new if n>0 else text
for k,v in desired.items(): s=setv(s,k,v)
open(p,"w").write(s)
PY
        echo "  kube-vip manifest updated: lease=60s renew=45s retry=10s preserve=true"
        # Nuke the corrupted plndr-cp-lock so kube-vip rebuilds it with the new lease duration.
        $K -n kube-system delete lease plndr-cp-lock --ignore-not-found 2>&1 | head -1 || true
        # Touch the manifest to force kubelet to re-read (env-var changes alone don't trigger restart).
        touch "$M" 2>/dev/null || true
        echo "  manifest touched, lease deleted; kubelet will recreate kube-vip pod within ~30s"
    else
        echo "  kube-vip manifest already at desired values (lease=60 renew=45 retry=10 preserve=true) -- no-op"
        # Only intervene if the lease is dangerously short (the death-spiral signature was
        # leaseDurationSeconds=1). Some kube-vip builds ignore vip_leaseduration and pin to the
        # default 15s -- that's healthy, leave it alone. Only act below 10s.
        LEASE_DUR=$($K -n kube-system get lease plndr-cp-lock -o jsonpath='{.spec.leaseDurationSeconds}' 2>/dev/null || echo "")
        if [[ -n "$LEASE_DUR" && "$LEASE_DUR" -lt 10 ]]; then
            echo "  WARN: plndr-cp-lock has leaseDurationSeconds=${LEASE_DUR} (death-spiral signature); deleting so kube-vip rebuilds it"
            $K -n kube-system delete lease plndr-cp-lock --ignore-not-found 2>&1 | head -1 || true
        elif [[ -n "$LEASE_DUR" ]]; then
            echo "  plndr-cp-lock leaseDurationSeconds=${LEASE_DUR} (>= 10, healthy) -- no action"
        fi
    fi
else
    echo "  /etc/kubernetes/manifests/kube-vip.yaml not found, skipping kube-vip hardening"
fi

echo
echo "=== 4/5: probe timeouts on kube-apiserver/kube-controller-manager/kube-scheduler ==="
for kind in kube-apiserver kube-controller-manager kube-scheduler; do
    M=/etc/kubernetes/manifests/${kind}.yaml
    [[ -f "$M" ]] || { echo "  (${kind} manifest not found, skipping)"; continue; }
    PR_NEEDS=$(python3 - "$M" <<'PY'
import re, sys
p=sys.argv[1]
s=open(p).read()
desired={"periodSeconds":"10","timeoutSeconds":"30","failureThreshold":"8"}
def get_probe_block(text, probe):
    m=re.search(r'(' + probe + r':\s*\n(?:[ \t]+[^\n]+\n){1,15})', text)
    return m.group(1) if m else None
def get_field(block, key):
    m=re.search(r'\b' + key + r':\s*(\d+)', block)
    return m.group(1) if m else None
needs=False
for probe in ("livenessProbe","readinessProbe","startupProbe"):
    blk=get_probe_block(s, probe)
    if blk is None: continue
    for k,v in desired.items():
        cur=get_field(blk, k)
        if cur is not None and cur != v:
            needs=True; break
    if needs: break
print("CHANGE" if needs else "OK")
PY
)
    if [[ "$PR_NEEDS" == "CHANGE" ]]; then
        cp -a "$M" "${M}.bak.$(date +%s)"
        python3 - "$M" <<'PY'
import re, sys
p=sys.argv[1]
s=open(p).read()
desired={"periodSeconds":"10","timeoutSeconds":"30","failureThreshold":"8"}
def bump(text, probe, key, newval):
    pat=re.compile(r'(' + probe + r':[\s\S]*?' + key + r': )\d+', re.M)
    return pat.sub(lambda m: m.group(1)+str(newval), text, count=1)
for probe in ("livenessProbe","readinessProbe","startupProbe"):
    for k,v in desired.items():
        s=bump(s, probe, k, v)
open(p,"w").write(s)
PY
        echo "  ${kind} probes bumped (period=10s timeout=30s failureThreshold=8)"
        touch "$M" 2>/dev/null || true
    else
        echo "  ${kind} probes already at desired values (period=10 timeout=30 failureThreshold=8) -- no-op"
    fi
done

echo
echo "=== 5/5: kyverno failurePolicy (only if needed: trouble detected or STABILIZER_FORCE_KYVERNO_FIX=1) ==="
# Why this is conditional, not unconditional:
# vmsp-operator owns the kyverno HelmRelease via the 'vsp' Component CR. Helm-controller reverts
# our --forceFailurePolicyIgnore=true patch on every helm upgrade reconcile (every ~10m) because
# the package default is forceFailurePolicyIgnore=false. So patching unconditionally on every run
# just triggers a continual cycle of rollouts -> revert -> rollouts.
# On a healthy cluster, kyverno responds in <1s and failurePolicy=Fail is fine. The problem is
# only when the cluster is overloaded -- so we only patch when we detect actual trouble.
TROUBLE=""
if [[ "$KEEP_KYVERNO_FAIL" == "1" ]]; then
    echo "  STABILIZER_KEEP_KYVERNO_FAIL=1 -- leaving kyverno webhooks at failurePolicy=Fail"
elif [[ "${FORCE_KYVERNO_FIX:-0}" == "1" ]]; then
    TROUBLE="forced (FORCE_KYVERNO_FIX=1)"
else
    # Trouble heuristics (any one triggers the fix):
    #   (a) load average over last 1m > 30
    #   (b) any kyverno-* pod NOT Ready in vmsp-policies
    #   (c) kube-controller-manager pod has restarted >5 times in last hour (crashloop signature)
    LOAD1=$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)
    if [[ "$LOAD1" -gt 30 ]]; then
        TROUBLE="load1=${LOAD1} > 30"
    fi
    if [[ -z "$TROUBLE" ]]; then
        UNREADY=$($K -n vmsp-policies get pods -l app.kubernetes.io/part-of=kyverno -o json 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except: sys.exit(0)
n=0
for p in d.get('items',[]):
    cs=p.get('status',{}).get('containerStatuses',[]) or []
    if cs and not all(c.get('ready') for c in cs):
        n+=1
print(n)" 2>/dev/null || echo 0)
        if [[ -n "$UNREADY" && "$UNREADY" -gt 0 ]]; then
            TROUBLE="${UNREADY} kyverno pod(s) not Ready"
        fi
    fi
    if [[ -z "$TROUBLE" ]]; then
        KCM_RESTARTS=$($K -n kube-system get pods -l component=kube-controller-manager -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)
        if [[ -n "$KCM_RESTARTS" && "$KCM_RESTARTS" -gt 5 ]]; then
            TROUBLE="kube-controller-manager restarts=${KCM_RESTARTS} > 5"
        fi
    fi
fi

if [[ "$KEEP_KYVERNO_FAIL" == "1" ]]; then
    : # already handled above
elif [[ -z "$TROUBLE" ]]; then
    WH_POLICY=$($K get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg -o jsonpath='{.webhooks[0].failurePolicy}' 2>/dev/null || echo "")
    echo "  no trouble detected (load fine, kyverno pods Ready, kcm not crashlooping)"
    echo "  kyverno-resource-validating-webhook-cfg current failurePolicy: ${WH_POLICY:-not-found}"
    echo "  skipping fix to avoid unnecessary deploy rollouts (vmsp-operator reverts on every reconcile)"
    echo "  (set FORCE_KYVERNO_FIX=1 to apply anyway, or use --fix-overload during an active incident)"
else
    echo "  TROUBLE DETECTED: ${TROUBLE} -- applying durable kyverno fix"
    # 5a. Suspend the kyverno HelmRelease so flux/helm-controller stops reverting our Deployment patch.
    # IMPORTANT: the HelmRelease lives in vmsp-platform, NOT vmsp-policies.
    HR_FOUND=""
    for hr_ns in vmsp-platform vmsp-policies; do
        if $K -n "$hr_ns" get helmrelease kyverno >/dev/null 2>&1; then
            CUR_SUSPEND=$($K -n "$hr_ns" get helmrelease kyverno -o jsonpath='{.spec.suspend}' 2>/dev/null)
            if [[ "$CUR_SUSPEND" == "true" ]]; then
                echo "  HelmRelease $hr_ns/kyverno already suspended (no-op)"
            else
                $K -n "$hr_ns" patch helmrelease kyverno --type=merge -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 \
                    && echo "  suspended HelmRelease $hr_ns/kyverno"
            fi
            HR_FOUND="$hr_ns"
            break
        fi
    done
    [[ -z "$HR_FOUND" ]] && echo "  (no kyverno HelmRelease found in vmsp-platform or vmsp-policies; flux may revert later steps)"

    # 5b. Patch the kyverno-admission-controller Deployment with --forceFailurePolicyIgnore=true.
    # The action ($KY_ACTION) is one of: NOOP, NOTFOUND, "REPLACE <idx>", "APPEND <idx>".
    KY_ACTION=$($K -n vmsp-policies get deploy kyverno-admission-controller -o json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('NOTFOUND'); sys.exit(0)
args=d.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[{}])[0].get('args',[]) or []
for i,a in enumerate(args):
    if a == '--forceFailurePolicyIgnore=true':
        print('NOOP'); sys.exit(0)
    if a.startswith('--forceFailurePolicyIgnore='):
        print('REPLACE %d' % i); sys.exit(0)
print('APPEND %d' % len(args))
" 2>/dev/null)
    if [[ "$KY_ACTION" == "NOOP" ]]; then
        echo "  kyverno-admission-controller: --forceFailurePolicyIgnore=true already set (no-op)"
    elif [[ "$KY_ACTION" == "NOTFOUND" ]]; then
        echo "  (kyverno-admission-controller deployment not found, skipping arg patch)"
    elif [[ "$KY_ACTION" == REPLACE\ * ]]; then
        IDX="${KY_ACTION#REPLACE }"
        $K -n vmsp-policies patch deploy kyverno-admission-controller --type=json \
            -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args/${IDX}\",\"value\":\"--forceFailurePolicyIgnore=true\"}]" >/dev/null 2>&1 \
            && echo "  kyverno-admission-controller: --forceFailurePolicyIgnore=true patched (replaced index ${IDX}, deploy will roll)" \
            || echo "  WARN: kyverno-admission-controller patch failed (will retry next run)"
    elif [[ "$KY_ACTION" == APPEND\ * ]]; then
        IDX="${KY_ACTION#APPEND }"
        $K -n vmsp-policies patch deploy kyverno-admission-controller --type=json \
            -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/${IDX}\",\"value\":\"--forceFailurePolicyIgnore=true\"}]" >/dev/null 2>&1 \
            && echo "  kyverno-admission-controller: --forceFailurePolicyIgnore=true appended (deploy will roll)" \
            || echo "  WARN: kyverno-admission-controller patch failed (will retry next run)"
    else
        echo "  (skipping: could not determine kyverno-admission-controller state: '$KY_ACTION')"
    fi

    # 5c. Belt-and-suspenders: also flip the resource webhook directly so it's Ignore right now.
    # The other 5 policy-config webhooks are NOT touched -- they intentionally stay at Fail.
    for vwc in kyverno-resource-validating-webhook-cfg; do
        WH_NEW=$($K get validatingwebhookconfiguration "$vwc" -o json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
changed=False
for w in d.get('webhooks', []) or []:
    if w.get('failurePolicy') == 'Fail':
        w['failurePolicy'] = 'Ignore'; changed=True
print(json.dumps(d) if changed else '__nochange__')")
        if [[ "$WH_NEW" == "__nochange__" || -z "$WH_NEW" ]]; then
            echo "  $vwc: already Ignore (or not found)"
        else
            echo "$WH_NEW" | $K replace -f - >/dev/null 2>&1 \
                && echo "  $vwc: flipped Fail -> Ignore" \
                || echo "  $vwc: WARN replace failed"
        fi
    done
fi

echo
echo "=== preflight: ending load average:"
uptime
echo "=== done ==="
REMOTE
)
    local fix_script="${fix_prefix}${fix_body}"
    execute_remote "$fix_script" "Recover from control-plane overload (etcd / kube-vip / probes / kyverno)" false
}

# Remove the v2.1 volumeMounts merge (shutdown-manager /tmp + ca bundle) from both EnvoyProxy objects. Requires jq on the appliance.
repair_envoyproxy_remove_stabilizer_mounts() {
    log "Repair: stripping stabilizer-added EnvoyProxy volumeMounts (requires jq on appliance)..."
    local jqfilter jqb64 ep
    jqfilter='.spec.provider.kubernetes.envoyDeployment.container.volumeMounts |= (if . == null then [] else [.[] | select((.mountPath=="/tmp" and .name=="shutdown-manager")|not) | select((.mountPath=="/etc/ssl/certs/ca-certificates.crt" and .name=="config-volume" and (.subPath=="ca-certificates.crt"))|not)] end)'
    jqb64=$(printf '%s' "$jqfilter" | base64 -w0 2>/dev/null || printf '%s' "$jqfilter" | base64 | tr -d '\n')
    for ep in vmsp-gateway-config vcfa-gateway-configuration-config; do
        execute_remote "command -v jq >/dev/null 2>&1 || { echo jq required on appliance >&2; exit 1; }
echo ${jqb64} | base64 -d > /tmp/vcfa-repair.jq
kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get envoyproxy ${ep} -n $VMSP_NAMESPACE -o json | jq -f /tmp/vcfa-repair.jq | kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER replace -f -
rm -f /tmp/vcfa-repair.jq" \
            "Repair EnvoyProxy ${ep}" false
    done
}

# Function to fix VCFA core components
fix_vcfa_core_components() {
    log "Applying VCFA core component fixes (EnvoyProxy volumes=${STABILIZER_ENVOYPROXY_VOLUMES}, prelude probes=${STABILIZER_PATCH_PRELUDE_PROBES}, capi=${STABILIZER_PATCH_CAPI_IPAM_PROBES}, egw-probes=${STABILIZER_PATCH_ENVOY_GATEWAY_PROBES})..."

    if [[ "${STABILIZER_ENVOYPROXY_VOLUMES}" == "1" ]]; then
        warning "STABILIZER_ENVOYPROXY_VOLUMES=1: applying legacy EnvoyProxy volume merge (disabled by default since v2.2 — known to break dataplane Services on some builds)."
        execute_remote "cat > /tmp/envoyproxy-fix.yaml << 'EOF'
spec:
  provider:
    kubernetes:
      envoyDeployment:
        container:
          volumeMounts:
          - mountPath: /tmp
            name: shutdown-manager
          - mountPath: /etc/ssl/certs/ca-certificates.crt
            name: config-volume
            subPath: ca-certificates.crt
EOF" "Creating EnvoyProxy patch file" true
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch envoyproxy vmsp-gateway-config -n $VMSP_NAMESPACE --type merge --patch-file /tmp/envoyproxy-fix.yaml" \
            "Patch vmsp-gateway-config EnvoyProxy" true
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch envoyproxy vcfa-gateway-configuration-config -n $VMSP_NAMESPACE --type merge --patch-file /tmp/envoyproxy-fix.yaml" \
            "Patch vcfa-gateway-configuration-config EnvoyProxy" true
    else
        info "Skipping EnvoyProxy volume merge (STABILIZER_ENVOYPROXY_VOLUMES=0, default)."
    fi

    if [[ "${STABILIZER_PATCH_CAPI_IPAM_PROBES}" == "1" ]]; then
        log "Patching CAPI IPAM probe timeouts to ${STABILIZER_PROBE_TIMEOUT_SECONDS}s..."
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment capi-ipam-in-cluster-controller-manager -n $VMSP_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]'" \
            "CAPI IPAM liveness probe" true
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment capi-ipam-in-cluster-controller-manager -n $VMSP_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/readinessProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]'" \
            "CAPI IPAM readiness probe" true
    else
        info "Skipping CAPI IPAM probe patches (STABILIZER_PATCH_CAPI_IPAM_PROBES=0, default)."
    fi

    if [[ "${STABILIZER_PATCH_ENVOY_GATEWAY_PROBES}" == "1" ]]; then
        log "Patching envoy-gateway operator probes to ${STABILIZER_PROBE_TIMEOUT_SECONDS}s..."
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment envoy-gateway -n $VMSP_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]'" \
            "envoy-gateway liveness probe" true
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER patch deployment envoy-gateway -n $VMSP_NAMESPACE --type='json' -p='[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/readinessProbe/timeoutSeconds\", \"value\": ${STABILIZER_PROBE_TIMEOUT_SECONDS}}]'" \
            "envoy-gateway readiness probe" true
    else
        info "Skipping envoy-gateway operator probe patches (STABILIZER_PATCH_ENVOY_GATEWAY_PROBES=0, default)."
    fi

    if ! verify_envoy_dataplane_services; then
        return 1
    fi
    return 0
}

# Function to wait for pods to stabilize
wait_for_stabilization() {
    log "Waiting for pods to stabilize..."
    
    local max_wait=300  # 5 minutes
    local wait_interval=15
    local elapsed=0
    
    while [[ $elapsed -lt $max_wait ]]; do
        log "Checking pod status (${elapsed}s elapsed)..."
        
        # Check for pods that are not running or have recent restarts
        local pods_output kc
        kc=$(vcfa_kubectl_invoker)
        pods_output=$(vcfa_ssh_nosudo "${kc}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -A 2>/dev/null || echo 'API_SERVER_DOWN'")
        
        if echo "$pods_output" | grep -q "API_SERVER_DOWN"; then
            info "API server unreachable, waiting..."
        else
            local problematic_pods
            problematic_pods=$(echo "$pods_output" | grep -E '(CrashLoopBackOff|ImagePullBackOff|Error|Terminating)' || true)
            
            if [[ -z "$problematic_pods" ]]; then
                success "No problematic pods found"
                break
            else
                info "Found some pods still stabilizing:"
                echo "$problematic_pods"
            fi
        fi
        
        sleep $wait_interval
        elapsed=$((elapsed + wait_interval))
    done
    
    if [[ $elapsed -ge $max_wait ]]; then
        warning "Timeout waiting for complete stabilization, but continuing..."
    fi
}

# Function to verify fixes
verify_fixes() {
    log "Verifying stabilization fixes..."

    echo ""
    info "=== Stability suite (control plane + /automation + critical pods) ==="
    run_stability_verification "phase-5" || true
    
    echo ""
    info "=== Final Pod Status ==="
    get_system_status
    
    echo ""
    info "=== Testing VCFA Functionality ==="
    
    # Test endpoint multiple times
    local success_count=0
    for i in {1..5}; do
        local result
        result=$(vcfa_curl_automation_code)
        
        if [[ "$result" == "200" ]]; then
            ((success_count++))
        fi
        info "Test $i: HTTP $result"
        sleep 1
    done
    
    if [[ $success_count -eq 5 ]]; then
        success "VCFA endpoint is consistently responding (5/5 tests passed)"
    elif [[ $success_count -gt 2 ]]; then
        warning "VCFA endpoint mostly stable ($success_count/5 tests passed)"
    else
        error "VCFA endpoint is unstable ($success_count/5 tests passed)"
    fi
    
    echo ""
    info "=== Checking Recent Events ==="
    execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get events -n $VMSP_NAMESPACE --sort-by=.lastTimestamp | tail -5" \
        "Getting recent events" true
}

# Function to generate verification script
generate_verification_script() {
    log "Generating verification script..."
    local out="${SCRIPT_DIR}/vcfa-verify-stability.sh"
    cat > "$out" << 'VFEOF'
#!/bin/bash

# VCFA Stability Verification Script (generated by vcfa-complete-stabilizer.sh)
# Run periodically from the jump host. For second-hop SSH issues, set on the jump host:
#   export VCFA_SSH_OPTS='-o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o IdentityFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VCFA_HOST="${VCFA_HOST:-10.1.1.73}"
VCFA_HTTP_VIP="${VCFA_HTTP_VIP:-10.1.1.70}"
VCFA_USER="${VCFA_USER:-vmware-system-user}"
CREDS_FILE="${CREDS_FILE:-/home/holuser/creds.txt}"
if [[ -z "${VCFA_PASSWORD:-}" ]] && [[ -f "$CREDS_FILE" ]]; then
  VCFA_PASSWORD=$(head -1 "$CREDS_FILE" | tr -d '\r\n')
fi
VCFA_PASSWORD="${VCFA_PASSWORD:-}"
API_SERVER="${API_SERVER:-https://10.1.1.73:6443}"
VCFA_SSH_OPTS="${VCFA_SSH_OPTS:--o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o IdentityFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no}"

echo "=== VCFA Stability Check - $(date) ==="

CPW="${SCRIPT_DIR}/vcfa-control-plane-watch.sh"
[[ -x "$CPW" ]] || CPW="/home/holuser/vcfa-control-plane-watch.sh"
if [[ -x "$CPW" ]]; then
  echo -e "\n--- Control plane pressure (non-fatal) ---"
  "$CPW" || true
fi

echo "Testing VCFA endpoint..."
for i in {1..3}; do
    result=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
        --resolve auto-a.site-a.vcf.lab:443:$VCFA_HTTP_VIP \
        https://auto-a.site-a.vcf.lab/automation 2>/dev/null || echo "000")
    echo "Test $i: HTTP $result"
done

PWQ=$(printf '%q' "$VCFA_PASSWORD")
# v2.4: when VCFA_USER is root we don't need sudo. Build the kubectl prefix once.
if [[ "${VCFA_USER}" == "root" ]]; then
    KC=""
else
    KC="echo ${PWQ} | sudo -S "
fi
echo -e "\nCritical pod status:"
sshpass -f "$CREDS_FILE" ssh -T ${VCFA_SSH_OPTS} "${VCFA_USER}@$VCFA_HOST" \
    "${KC}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n vmsp-platform | grep -E '(vmsp-gateway|vcfa-gateway|envoy-gateway)'" 2>/dev/null

sshpass -f "$CREDS_FILE" ssh -T ${VCFA_SSH_OPTS} "${VCFA_USER}@$VCFA_HOST" \
    "${KC}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -n prelude | grep -E '(encryption-manager|intent-server|vcfa-service-manager)'" 2>/dev/null

echo -e "\nChecking for problematic pods:"
problematic=$(sshpass -f "$CREDS_FILE" ssh -T ${VCFA_SSH_OPTS} "${VCFA_USER}@$VCFA_HOST" \
    "${KC}kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER get pods -A | grep -E '(CrashLoopBackOff|ImagePullBackOff|Error)'" 2>/dev/null || echo "None found")
echo "$problematic"

echo -e "\nVerification completed at $(date)"
VFEOF

    chmod +x "$out"
    success "Verification script created at $out"
}

# Detect and remediate stale service-tls certificates across all prelude deployments.
#
# cert-manager auto-renews the service-tls Secret (~90-day cert) but every pod
# that mounts it must restart to pick up the new cert data. Without that restart
# each pod keeps serving the expired cert → Envoy upstream TLS verification fails
# → HTTP 503 on any route whose backend has a stale pod. This affects 24+ prelude
# deployments including cloud-automation-ui-app (/automation), ccs-k3s-app (/cci/),
# and many service backends.
#
# Detection (pure kubectl, no network probe, ~2-3s on a healthy cluster):
#   1. Read cert notBefore epoch from the service-tls Secret.
#   2. For each affected deployment, get the running pod's startTime.
#   3. If pod.startTime < cert.notBefore → pod has stale cert → rollout restart.
#   Rollouts are issued in parallel (kubectl rollout restart is non-blocking).
#   Then we wait up to 120s for all restarted deployments to become ready.
#
# Called from main() BEFORE the idempotency early-exit so it runs on every
# lab startup, not just the first time the stabilizer is applied.
check_and_fix_ccs_k3s_cert() {
    local K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=${API_SERVER}"
    local kc
    kc=$(vcfa_kubectl_invoker)

    info "Checking service-tls certificate freshness across prelude deployments..."

    # --- Get cert notBefore from the service-tls Secret (epoch seconds) ---
    local cert_nbf cert_exp
    cert_nbf=$(vcfa_ssh_nosudo \
        "${kc}${K} get secret service-tls -n ${PRELUDE_NAMESPACE} \
         -o jsonpath='{.data.tls\.crt}' 2>/dev/null \
         | base64 -d \
         | openssl x509 -noout -startdate 2>/dev/null \
         | cut -d= -f2 \
         | xargs -I{} date -d '{}' +%s 2>/dev/null" \
        2>/dev/null | tr -d '[:space:]') || true

    if [[ -z "$cert_nbf" || ! "$cert_nbf" =~ ^[0-9]+$ ]]; then
        warning "service-tls: could not read cert from secret — skipping freshness check"
        return 0
    fi

    cert_exp=$(vcfa_ssh_nosudo \
        "${kc}${K} get secret service-tls -n ${PRELUDE_NAMESPACE} \
         -o jsonpath='{.data.tls\.crt}' 2>/dev/null \
         | base64 -d \
         | openssl x509 -noout -enddate 2>/dev/null \
         | cut -d= -f2" \
        2>/dev/null | tr -d '\r\n') || cert_exp="unknown"

    local cert_ts
    cert_ts=$(date -d "@${cert_nbf}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "${cert_nbf}")
    info "  service-tls cert: notBefore=${cert_ts}, notAfter=${cert_exp}"

    # --- All deployments in prelude that mount service-tls ---
    # This list covers every deployment confirmed to use the shared service-tls Secret.
    local service_tls_deployments=(
        abx-service-app
        approval-service-app
        catalog-service-app
        ccs-avi-eas-app
        ccs-gateway-app
        ccs-infra-eas-app
        ccs-k3s-app
        ccs-nsx-eas-app
        ccs-vksm-eas
        cgs-service-app
        cloud-automation-ui-app
        ebs-app
        encryption-manager
        extensibility-ui-app
        hcmp-service-app
        orchestration-ui-app
        provisioning-service-app
        provisioning-ui-app
        relocation-service-app
        relocation-ui-app
        tango-blueprint-service-app
        tango-uber-service-app
        terraform-service-app
        vcfa-service-manager
    )

    # --- Check each deployment and collect stale ones ---
    local stale_deployments=()

    for deploy in "${service_tls_deployments[@]}"; do
        local app_label pod_start pod_epoch
        app_label=$(vcfa_ssh_nosudo \
            "${kc}${K} get deployment ${deploy} -n ${PRELUDE_NAMESPACE} \
             -o jsonpath='{.spec.selector.matchLabels.app}' 2>/dev/null" \
            2>/dev/null | tr -d '[:space:]') || true
        [[ -z "$app_label" ]] && app_label="$deploy"

        pod_start=$(vcfa_ssh_nosudo \
            "${kc}${K} get pod -n ${PRELUDE_NAMESPACE} \
             -l app=${app_label} --field-selector='status.phase=Running' \
             -o jsonpath='{.items[0].status.startTime}' 2>/dev/null \
             | xargs -I{} date -d '{}' +%s 2>/dev/null" \
            2>/dev/null | tr -d '[:space:]') || true

        if [[ -z "$pod_start" || ! "$pod_start" =~ ^[0-9]+$ ]]; then
            continue  # No running pod (e.g. replicas=0) — skip silently
        fi

        if [[ "$pod_start" -lt "$cert_nbf" ]]; then
            local pod_ts
            pod_ts=$(date -d "@${pod_start}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "${pod_start}")
            warning "  STALE: ${deploy} (pod started ${pod_ts} < cert renewed ${cert_ts})"
            stale_deployments+=("$deploy")
        fi
    done

    if [[ ${#stale_deployments[@]} -eq 0 ]]; then
        success "service-tls: all prelude pods started after cert renewal — no restarts needed"
        return 0
    fi

    info "Issuing rollout restarts for ${#stale_deployments[@]} stale deployment(s)..."
    for deploy in "${stale_deployments[@]}"; do
        execute_remote \
            "${K} rollout restart deployment/${deploy} -n ${PRELUDE_NAMESPACE}" \
            "rollout restart ${deploy} (stale service-tls cert)" true
    done

    # --- Wait for all restarted deployments to become ready (max 120s) ---
    info "Waiting up to 120s for restarted deployments to become ready..."
    local waited=0
    while [[ $waited -lt 120 ]]; do
        sleep 10
        waited=$((waited + 10))
        local still_pending=0
        for deploy in "${stale_deployments[@]}"; do
            local ready
            ready=$(vcfa_ssh_nosudo \
                "${kc}${K} get deployment ${deploy} -n ${PRELUDE_NAMESPACE} \
                 -o jsonpath='{.status.readyReplicas}' 2>/dev/null" \
                2>/dev/null | tr -d '[:space:]' | grep -oE '^[0-9]+$' || echo "")
            local desired
            desired=$(vcfa_ssh_nosudo \
                "${kc}${K} get deployment ${deploy} -n ${PRELUDE_NAMESPACE} \
                 -o jsonpath='{.spec.replicas}' 2>/dev/null" \
                2>/dev/null | tr -d '[:space:]' | grep -oE '^[0-9]+$' || echo "1")
            [[ -z "$desired" ]] && desired=1
            if [[ "$ready" != "$desired" ]]; then
                still_pending=$((still_pending + 1))
            fi
        done
        if [[ $still_pending -eq 0 ]]; then
            success "service-tls: all ${#stale_deployments[@]} deployment(s) ready after ${waited}s — fresh certs mounted"
            return 0
        fi
        info "  ${still_pending}/${#stale_deployments[@]} deployment(s) still rolling out (${waited}s / 120s)..."
    done
    warning "service-tls: ${#stale_deployments[@]} deployment(s) restarted but rollout not confirmed within 120s — proceeding"
}

# Main execution function
main() {
    echo "======================================================================"
        echo "           VCFA Complete Stabilization Script v2.8"
    echo "======================================================================"
    echo "Comprehensive VCFA stability solution for nested environments"
    echo "Default run is prophylactic: control-plane preflight + auth + core + SDS"
    echo ""
    echo "Target VCFA VM (SSH): $VCFA_HOST"
    echo "Gateway LB VIP (curl): $VCFA_HTTP_VIP  (Service vcfa-gateway-configuration / kube-vip)"
    echo "API Server: $API_SERVER"
    echo "======================================================================"
    echo ""
    
    check_prerequisites

    # Always check ccs-k3s service-tls cert freshness — runs before the idempotency early-exit
    # because cert expiry is time-based and must be checked on every startup regardless of
    # whether the rest of the stabilizer has already been applied.
    check_and_fix_ccs_k3s_cert

    # Check if the stabilizer has already been applied by looking for persistent settings it creates:
    # 1. The durable systemd watcher script (from Phase 3.5)
    # 2. The kube-vip lease duration tuning (from Phase 1.5)
    local check_cmd="test -f /usr/local/bin/vcfa-eg-mem-keeper.sh || grep -A 1 'name: vip_leaseduration' /etc/kubernetes/manifests/kube-vip.yaml 2>/dev/null | grep -q 'value: \"60\"'"
    if vcfa_ssh_nosudo "$check_cmd" >/dev/null 2>&1; then
        echo "VCFA Stabilizer already applied..."
        exit 0
    fi
    
    echo ""
    info "=== PHASE 1: Initial System Assessment ==="
    get_system_status
    
    echo ""
    info "=== PHASE 1.5: Control-plane preflight (v2.6 prophylaxis) ==="
    # Apply every persistent control-plane fix we've ever needed in a single idempotent pass:
    #   - pin gateway+CP VIPs non-deprecated
    #   - defrag etcd if slack >= ETCD_DEFRAG_SLACK_PCT (default 30)
    #   - harden kube-vip plndr-cp-lock lease (60s/45s/10s, preserve_on_leadership_loss=true)
    #   - bump kube-apiserver/kcm/scheduler probe timeouts (period=10 timeout=30 failureThreshold=8)
    #   - suspend kyverno HelmRelease, set --forceFailurePolicyIgnore=true, flip resource webhook
    # Each step compares current vs desired and is a no-op when already correct, so this is safe to
    # run on every invocation (default-run, --full, --preflight).
    # Set STABILIZER_SKIP_OVERLOAD_PROPHYLAXIS=1 to skip this phase (legacy v2.5- behavior).
    if [[ "${STABILIZER_SKIP_OVERLOAD_PROPHYLAXIS:-0}" == "1" ]]; then
        info "STABILIZER_SKIP_OVERLOAD_PROPHYLAXIS=1 — skipping control-plane preflight"
    else
        fix_overload_recovery || warning "Control-plane preflight reported errors (continuing — see logs above)"
        # Static-pod manifest changes (kube-vip, kube-apiserver, kcm, scheduler) trigger kubelet to
        # restart those pods, which briefly disrupts API server availability. Wait for things to
        # settle before continuing, but only if any changes were actually applied.
        info "Waiting 20s for control-plane to settle after preflight..."
        sleep 20
        wait_for_api_server
    fi
    
    echo ""
    info "=== PHASE 2: Authentication Services Stabilization ==="
    fix_authentication_services
    
    echo ""
    info "=== PHASE 3: VCFA Core Components Stabilization ==="
    fix_vcfa_core_components
    
    echo ""
    info "=== PHASE 3.5: SDS NACK auto-fix (v2.4) ==="
    # Idempotent: if every BackendTLSPolicy already uses caCertificateRefs and operator already has 4Gi,
    # the helper is a no-op. Otherwise it applies the durable fix discovered during the Apr 2026 incident.
    if [[ "${STABILIZER_SKIP_SDS_FIX:-0}" == "1" ]]; then
        info "STABILIZER_SKIP_SDS_FIX=1 — skipping SDS NACK auto-fix"
    else
        fix_envoy_gateway_sds_san_nack || warning "SDS NACK auto-fix reported errors (continuing — see logs above)"
    fi
    
    echo ""
    info "=== PHASE 4: Waiting for Stabilization ==="
    wait_for_stabilization
    
    echo ""
    info "=== PHASE 5: Verification ==="
    verify_fixes
    
    echo ""
    info "=== PHASE 6: Setup Monitoring ==="
    generate_verification_script
    
    echo ""
    echo "======================================================================"
    success "VCFA Complete Stabilization Completed Successfully!"
    echo "======================================================================"
    echo ""
    echo "Next steps:"
    echo "1. Monitor system stability for 15-30 minutes"
    echo "2. Run ${SCRIPT_DIR}/vcfa-verify-stability.sh periodically"
    echo "3. Check for any remaining restart patterns"
    echo ""
    echo "If issues persist, check the logs of specific failing services:"
    echo "  kubectl logs -n prelude <pod-name>"
    echo "  kubectl logs -n vmsp-platform <pod-name>"
    echo ""
}

# Handle script arguments
case "${1:-}" in
    --help|-h)
        echo "VCFA Complete Stabilization Script v2.6"
        echo "See: ${SCRIPT_DIR}/VCFA_Stabilizer_Incident_Apr2026.md"
        echo ""
        echo "Usage: $0 [options]"
        echo ""
        echo "Default run (no options) = one-shot prophylactic stabilization. Run this on every boot;"
        echo "it's idempotent and only changes things that need changing."
        echo "Phases: 1 status, 1.5 control-plane preflight (NEW v2.6), 2 auth services, 3 core components,"
        echo "        3.5 SDS NACK auto-fix, 4 wait, 5 verify, 6 monitoring."
        echo ""
        echo "The control-plane preflight (1.5) applies these durable fixes idempotently:"
        echo "  - pin gateway+CP VIPs (.69/.70/.72) on eth0 non-deprecated (kube-vip backstop)"
        echo "  - defrag etcd if slack >= ETCD_DEFRAG_SLACK_PCT (default 30%)"
        echo "  - harden kube-vip plndr-cp-lock lease (60s renew=45s retry=10s, preserve_on_leadership_loss=true)"
        echo "  - bump kube-apiserver/kcm/scheduler probe timeouts (period=10 timeout=30 failureThreshold=8)"
        echo "  - kyverno --forceFailurePolicyIgnore=true (CONDITIONAL: only applied when trouble is"
        echo "    detected -- load1>30, kyverno pods not Ready, or kcm restarts>5 -- because"
        echo "    vmsp-operator reverts the patch on every helm reconcile)"
        echo "On a healthy cluster Phase 1.5 prints status and changes nothing (no kubelet churn)."
        echo ""
        echo "Options:"
        echo "  --help, -h              This help"
        echo "  --status                Only check current status (no fixes)"
        echo "  --preflight             v2.6: only Phase 1 + Phase 1.5 (control-plane preflight). Quick, idempotent."
        echo "                          Use after a fresh boot or before a known-busy run. ~2-3 min."
        echo "  --verify                Run stabilization verification (Phase 5-style)"
        echo "  --verify-stability      Run embedded stability suite only (watch + HTTP + pod grep)"
        echo "  --repair-envoyproxy     Strip v2.1 EnvoyProxy volumeMounts + restart envoy-gateway (needs jq on appliance)"
        echo "  --recover-gateway-503   Roll trust/cert-manager + prelude SDS backends + dataplane/operator gateways (HTTP 503 / SDS)"
        echo "  --fix-sds-sni           Fix envoy-gateway v1.5 + Envoy v1.34 SDS NACK by replacing"
        echo "                          BackendTLSPolicy.wellKnownCACertificates:System with caCertificateRefs:platform-trust"
        echo "                          and bumping operator memory to 4Gi (root cause of HTTP 503 'Secret_is_not_supplied_by_SDS')."
        echo "  --fix-overload          v2.5: control-plane overload recovery (same internals as Phase 1.5, with"
        echo "                          longer post-fix probe wait). Use when load avg is stuck in the hundreds and"
        echo "                          dial-tcp-no-route-to-host errors are everywhere; otherwise the default run"
        echo "                          already handles this prophylactically."
        echo "  --cpu-tune              Lab CPU tuning: Prometheus → Fluent → Kyverno → Java (with checks after each block)"
        echo "  --lab-cpu-tune          Same as --cpu-tune"
        echo "  --rollback-cpu-tune     Undo lab CPU tuning (reverse order)"
        echo "  --cpu-tune-rollback     Same as --rollback-cpu-tune"
        echo "  --full                  Run full stabilization, then --cpu-tune (defaults CPU_TUNE_REQUIRE_HTTP_200=1)"
        echo ""
        echo "SSH / transport:"
        echo "  STABILIZER_JUMP_HOST     e.g. jumphost — second hop uses sshpass + CREDS_ON_JUMP on that host"
        echo "  CREDS_ON_JUMP            Password file path ON the jump host (default: /home/holuser/creds.txt)"
        echo "  STABILIZER_VCFA_IDENTITY_FILE   If set, use key auth to VCFA (no sshpass for second hop)"
        echo "  VCFA_SSH_OPTS            Override ssh options for password hop (defaults force password auth)"
        echo ""
        echo "Risk / gateway knobs (defaults safe for Envoy Gateway dataplane):"
        echo "  STABILIZER_ENVOYPROXY_VOLUMES=1           Re-enable legacy EnvoyProxy merge (not recommended)"
        echo "  STABILIZER_PATCH_ENVOY_GATEWAY_PROBES=1   Patch envoy-gateway operator probes"
        echo "  STABILIZER_PATCH_CAPI_IPAM_PROBES=1       Patch CAPI IPAM probes"
        echo "  STABILIZER_PATCH_PRELUDE_PROBES=0         Skip prelude deployment probe patches"
        echo "  STABILIZER_PROBE_TIMEOUT_SECONDS          Default 10 (was 5 in v2.1)"
        echo "  STABILIZER_GATEWAY_PREFLIGHT_STRICT=1     Abort if no envoy-vmsp-platform-* Services after core phase"
        echo ""
        echo "Control-plane preflight (Phase 1.5, v2.6):"
        echo "  STABILIZER_SKIP_OVERLOAD_PROPHYLAXIS=1    Skip Phase 1.5 entirely (v2.5- behavior)"
        echo "  STABILIZER_KEEP_KYVERNO_FAIL=1            Never patch kyverno failurePolicy (skip step 5/5)"
        echo "  FORCE_KYVERNO_FIX=1                       Bypass trouble heuristic; always apply kyverno fix"
        echo "  ETCD_DEFRAG_SLACK_PCT=30                  Defrag etcd only when slack >= this % (0=always, 100=never)"
        echo "  VCFA_CP_VIP=10.1.1.72                     Control-plane VIP to pin as backstop"
        echo "  VMSP_GW_VIP=10.1.1.69                     vmsp-gateway VIP to pin as backstop"
        echo "  VCFA_GW_VIP=10.1.1.70                     vcfa-gateway-configuration VIP to pin as backstop"
        echo ""
        echo "Lab tuning env (optional):"
        echo "  CPU_TUNE_SETTLE_SECONDS          Sleep between blocks (default: 45)"
        echo "  CPU_TUNE_STRICT=1                Abort tune if vcfa-control-plane-watch.sh fails"
        echo "  CPU_TUNE_REQUIRE_HTTP_200=1      Abort tune if any /automation check != 200"
        echo "  KYVERNO_ADMISSION_REPLICAS_ROLLBACK  Default 3 (rollback scale)"
        echo ""
        echo "General env:"
        echo "  VCFA_HOST (SSH / kubectl on VM), VCFA_HTTP_VIP (kube-vip LB for /automation curl from VM)"
        echo "  VCFA_USER, CREDS_FILE, VCFA_PASSWORD, API_SERVER"
        echo "  VMSP_NAMESPACE, PRELUDE_NAMESPACE, VMSP_POLICIES_NAMESPACE"
        echo "  PROMETHEUS_NAME, KYVERNO_ADMISSION_DEPLOY, PROVISIONING_DEPLOY"
        echo "  STABILIZER_DEBUG=1               Show suppressed SSH/kubectl stderr"
        exit 0
        ;;
    --status)
        check_prerequisites
        get_system_status
        exit 0
        ;;
    --preflight)
        # Run only Phase 1 (status) + Phase 1.5 (control-plane preflight). Idempotent and quick;
        # use this when you don't need the full prelude/dataplane treatment but want to make sure
        # the control plane is hardened (e.g. shortly after a fresh boot or before a known-busy run).
        check_prerequisites
        echo ""
        info "=== Control-plane preflight (idempotent) ==="
        fix_overload_recovery
        info "Sleeping 30s for static-pod manifests to settle..."
        sleep 30
        get_system_status
        exit 0
        ;;
    --verify)
        check_prerequisites
        verify_fixes
        exit 0
        ;;
    --verify-stability)
        check_prerequisites
        run_stability_verification "standalone" || true
        exit 0
        ;;
    --cpu-tune|--lab-cpu-tune)
        check_prerequisites
        cpu_tune_apply || exit 1
        exit 0
        ;;
    --rollback-cpu-tune|--cpu-tune-rollback)
        check_prerequisites
        cpu_tune_rollback || exit 1
        exit 0
        ;;
    --full)
        main
        echo ""
        info "=== Post-stabilization: lab CPU tune (HTTP 200 required between steps unless CPU_TUNE_REQUIRE_HTTP_200=0) ==="
        export CPU_TUNE_REQUIRE_HTTP_200="${CPU_TUNE_REQUIRE_HTTP_200:-1}"
        cpu_tune_apply || exit 1
        exit 0
        ;;
    --repair-envoyproxy)
        check_prerequisites
        repair_envoyproxy_remove_stabilizer_mounts
        log "Restarting envoy-gateway operator to force reconcile..."
        execute_remote "kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=$API_SERVER rollout restart deployment/envoy-gateway -n $VMSP_NAMESPACE" \
            "rollout restart envoy-gateway" true
        sleep 25
        verify_envoy_dataplane_services || true
        get_system_status
        exit 0
        ;;
    --fix-sds-sni)
        check_prerequisites
        fix_envoy_gateway_sds_san_nack
        get_system_status
        info "Re-check /automation (5x) after SDS fix..."
        c=0
        for i in 1 2 3 4 5; do
            sleep 12
            r=$(vcfa_curl_automation_code)
            info "  probe $i: HTTP $r"
            [[ "$r" == "200" ]] && c=$((c + 1))
        done
        if [[ "$c" -ge 3 ]]; then
            success "/automation returned 200 on at least 3 of 5 probes after SDS fix."
        else
            warning "/automation still not consistently 200 ($c/5) after SDS fix. Inspect: kubectl logs -n vmsp-platform deploy/vcfa-gateway-configuration --tail=120; check 'kubectl exec ... curl 127.0.0.1:19000/config_dump' for warming secrets."
        fi
        exit 0
        ;;
    --fix-overload)
        check_prerequisites
        fix_overload_recovery
        info "Sleeping 90s for control plane to settle (kube-vip restart, controllers reconnect)..."
        sleep 90
        get_system_status
        info "Re-check /automation (3x) after overload recovery..."
        c=0
        for i in 1 2 3; do
            sleep 15
            r=$(vcfa_curl_automation_code)
            info "  probe $i: HTTP $r"
            [[ "$r" == "200" ]] && c=$((c + 1))
        done
        if [[ "$c" -ge 2 ]]; then
            success "/automation returned 200 on at least 2 of 3 probes after overload recovery."
        else
            warning "/automation not yet 200 ($c/3). Allow 5-10 minutes more, then re-run --status; if still failing, run --recover-gateway-503."
        fi
        exit 0
        ;;
    --recover-gateway-503)
        check_prerequisites
        verify_envoy_dataplane_services || true
        # v2.4: the SDS SAN-without-CA NACK is the most common 503 root cause; address it first so the
        # subsequent rollouts have valid SDS payloads to push.
        fix_envoy_gateway_sds_san_nack || warning "fix_envoy_gateway_sds_san_nack failed (continuing with rollouts)"
        recover_gateway_http_503
        verify_envoy_dataplane_services || true
        get_system_status
        info "Waiting for gateway dataplane to settle after rollouts..."
        sleep 45
        info "Re-check /automation (5x) after recovery rollouts..."
        c=0
        for i in 1 2 3 4 5; do
            sleep 15
            r=$(vcfa_curl_automation_code)
            info "  probe $i: HTTP $r"
            [[ "$r" == "200" ]] && c=$((c + 1))
        done
        if [[ "$c" -ge 3 ]]; then
            success "/automation returned 200 on at least 3 of 5 probes."
        else
            warning "/automation still not consistently 200 ($c/5). Check: kubectl logs -n vmsp-platform deploy/vcfa-gateway-configuration --tail=120; prelude app pods; trust-manager logs."
        fi
        exit 0
        ;;
    *)
        main
        ;;
esac
