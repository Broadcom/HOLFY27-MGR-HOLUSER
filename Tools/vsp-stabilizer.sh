#!/usr/bin/env bash
# =============================================================================
# vsp-stabilizer.sh   (v1.1.0 - 2026-08-03, HOL Core Team)
#
# Dedicated stabilization script for the VSP Fleet Control-Plane Node (10.1.1.142).
# Enforces durable remediation across two core areas:
#
#   (A) Drift-keeper systemd timer ('vsp-fleet-depot-keeper.timer') --
#       Probe-timeout and memory patches on Helm/reconciler-managed objects
#       that revert on reconciles (depot-service, fleetbuild, envoy-gateway,
#       vidb-service, sddcbuild, sddcupgrade, prometheus, kube-state-metrics,
#       node-exporter).
#   (B) Static-manifest lease & resource tuning --
#       kube-controller-manager and kube-scheduler lease tuning (60s/40s/6s),
#       etcd CPU request enforcement (2500m) + auto-compaction + defrag,
#       and kube-vip static pod manifest hardening.
#
# v1.1.0: etcd CPU request raised from 1000m to 2500m to match BenS's later,
#         empirically-validated value for the VSP CP's etcd (cgroup weight
#         ~=98 vs ~=39) -- the drift-keeper timer was reverting BenS's
#         manually-applied 2500m fix back to 1000m every 60s.
#
# Can be run locally on the VSP CP node or remotely from the Console VM via SSH.
# =============================================================================
set -euo pipefail

VSP_CP_IP="${VSP_CP_IP:-10.1.1.142}"
CREDS_FILE="${CREDS_FILE:-/home/holuser/creds.txt}"
NODE_USER="vmware-system-user"
ACTION="install"
LOCKFILE="${STABILIZER_LOCKFILE:-/tmp/vsp-stabilizer.lock}"

LEASE_DURATION="60s";  RENEW_DEADLINE="40s";  RETRY_PERIOD="6s"
VIP_LEASE_DURATION="60"; VIP_RENEW_DEADLINE="40"; VIP_RETRY_PERIOD="6"
ETCD_CPU_REQUEST="2500m"

log()     { echo "[INFO]  $(date +'%Y-%m-%d %H:%M:%S') $1"; }
warning() { echo "[WARN]  $(date +'%Y-%m-%d %H:%M:%S') $1"; }
error()   { echo "[ERROR] $(date +'%Y-%m-%d %H:%M:%S') $1"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --vsp-cp) VSP_CP_IP="$2"; shift 2;;
    --status) ACTION="status"; shift;;
    --remove) ACTION="remove"; shift;;
    --apply-lease) ACTION="apply-lease"; shift;;
    --revert-lease) ACTION="revert-lease"; shift;;
    --etcd-compaction) ACTION="etcd-compaction"; shift;;
    --kube-vip-apply) ACTION="kube-vip-apply"; shift;;
    -h|--help)
      echo "vsp-stabilizer.sh (v1.0.0 - 2026-07-25)"
      echo "Usage: $0 [options]"
      echo ""
      echo "Options:"
      echo "  (no args)           Install/refresh drift keeper timer + apply static manifest lease/etcd/kube-vip tuning"
      echo "  --vsp-cp IP         Override VSP CP node IP (default: 10.1.1.142)"
      echo "  --status            Check current status of keepers, pods, lease tuning, etcd, and kube-vip"
      echo "  --remove            Uninstall systemd keeper timer and service"
      echo "  --apply-lease       Apply static-manifest leader election, etcd CPU, and lease tuning only"
      echo "  --revert-lease      Revert static-manifest edits from backups"
      echo "  --etcd-compaction   Apply etcd auto-compaction and run defrag"
      echo "  --kube-vip-apply    Safe file-based update of /etc/kubernetes/manifests/kube-vip.yaml"
      echo "  --help, -h          Show this help message"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

acquire_lock() {
  exec 200>"$LOCKFILE"
  if ! flock -n 200; then
    error "Another vsp-stabilizer.sh process holds $LOCKFILE. Aborting."
    exit 1
  fi
}

# Determine execution mode: local (running on 10.1.1.142) vs remote (SSH from Console)
IS_LOCAL=0
MY_IPS="$(ip -4 addr show 2>/dev/null | grep -oE 'inet [0-9\.]+' | awk '{print $2}' || true)"
if echo "$MY_IPS" | grep -qE "^${VSP_CP_IP}$"; then
  IS_LOCAL=1
fi

SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"

run_node_root() {
  if [ "$IS_LOCAL" -eq 1 ]; then
    if [ "$(id -u)" -eq 0 ]; then
      bash -c "$*" 2>&1
    else
      [ -f "$CREDS_FILE" ] || { error "Creds file $CREDS_FILE not found"; exit 2; }
      echo "$(cat "$CREDS_FILE")" | sudo -S -i bash -c "$*" 2>&1 | grep -vaE "password for|Welcome to Photon|Last login" || true
    fi
  else
    [ -f "$CREDS_FILE" ] || { error "Creds file $CREDS_FILE not found"; exit 2; }
    PW="$(cat "$CREDS_FILE")"
    sshpass -f "$CREDS_FILE" ssh $SSHO "${NODE_USER}@${VSP_CP_IP}" "echo '$PW' | sudo -S -i bash -c '$*'" 2>&1 | grep -vaE "password for|Welcome to Photon|Last login" || true
  fi
}

copy_to_node() {
  local src="$1" dest="$2"
  if [ "$IS_LOCAL" -eq 1 ]; then
    cp "$src" "$dest"
  else
    sshpass -f "$CREDS_FILE" scp $SSHO "$src" "${NODE_USER}@${VSP_CP_IP}:$dest" >/dev/null
  fi
}

test_reachable() {
  if [ "$IS_LOCAL" -eq 1 ]; then
    return 0
  else
    sshpass -f "$CREDS_FILE" ssh $SSHO "${NODE_USER}@${VSP_CP_IP}" "echo ok" >/dev/null 2>&1
  fi
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Generate remote helper script for static manifest actions
cat > "$TMP/remediate-lease.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
MDIR=/etc/kubernetes/manifests
FAMB_ACTION="\$1"
LEASE_DURATION="${LEASE_DURATION}"
RENEW_DEADLINE="${RENEW_DEADLINE}"
RETRY_PERIOD="${RETRY_PERIOD}"
ETCD_CPU_REQUEST="${ETCD_CPU_REQUEST}"

mkdir -p /root/manifest-bak
if ls /etc/kubernetes/manifests/*.bak.* >/dev/null 2>&1; then
  mv /etc/kubernetes/manifests/*.bak.* /root/manifest-bak/ 2>/dev/null || true
fi

patch_leader_elect() {
  local file="\$MDIR/\$1.yaml"
  [ -f "\$file" ] || { echo "  \$1: manifest not found at \$file -- skipping"; return; }
  local cur_lease cur_renew cur_retry
  cur_lease="\$(grep -oE -- '--leader-elect-lease-duration=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_renew="\$(grep -oE -- '--leader-elect-renew-deadline=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retry="\$(grep -oE -- '--leader-elect-retry-period=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ "\$cur_lease" = "\${LEASE_DURATION}" ] && [ "\$cur_renew" = "\${RENEW_DEADLINE}" ] && [ "\$cur_retry" = "\${RETRY_PERIOD}" ]; then
    echo "  \$1: already at target (\${LEASE_DURATION}/\${RENEW_DEADLINE}/\${RETRY_PERIOD}) -- no-op"
    return
  fi
  if ! grep -q -- '--leader-elect=true' "\$file"; then
    echo "  \$1: '--leader-elect=true' not found in \$file -- skipping"
    return
  fi
  cp "\$file" "/root/manifest-bak/\$1.yaml.bak.\$(date +%s)"
  sed -i -E "/--leader-elect-lease-duration=/d; /--leader-elect-renew-deadline=/d; /--leader-elect-retry-period=/d" "\$file"
  sed -i "/--leader-elect=true/a\\\\    - --leader-elect-lease-duration=\${LEASE_DURATION}\\\\n    - --leader-elect-renew-deadline=\${RENEW_DEADLINE}\\\\n    - --leader-elect-retry-period=\${RETRY_PERIOD}" "\$file"
  echo "  \$1: patched leader election lease settings (\${LEASE_DURATION}/\${RENEW_DEADLINE}/\${RETRY_PERIOD})"
}

revert_leader_elect() {
  local name="\$1" file="\$MDIR/\$1.yaml"
  local latest
  latest="\$(ls -t /root/manifest-bak/\$1.yaml.bak.* 2>/dev/null | head -1 || true)"
  if [ -z "\$latest" ]; then
    echo "  \$name: no backup found -- nothing to revert"
    return
  fi
  cp "\$latest" "\$file"
  echo "  \$name: reverted from \$latest"
}

revert_etcd() {
  local latest
  latest="\$(ls -t /root/manifest-bak/etcd.yaml.bak.* 2>/dev/null | head -1 || true)"
  if [ -z "\$latest" ]; then
    echo "  etcd: no backup found -- nothing to revert"
    return
  fi
  cp "\$latest" "\$MDIR/etcd.yaml"
  echo "  etcd: reverted from \$latest"
}

status_leader_elect() {
  local file="\$MDIR/\$1.yaml"
  [ -f "\$file" ] || { echo "  \$1: manifest not found"; return; }
  local cur_lease cur_renew cur_retry
  cur_lease="\$(grep -oE -- '--leader-elect-lease-duration=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_renew="\$(grep -oE -- '--leader-elect-renew-deadline=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retry="\$(grep -oE -- '--leader-elect-retry-period=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ -z "\$cur_lease\$cur_renew\$cur_retry" ]; then
    echo "  \$1: NOT patched (using defaults)"
  elif [ "\$cur_lease" = "\${LEASE_DURATION}" ] && [ "\$cur_renew" = "\${RENEW_DEADLINE}" ] && [ "\$cur_retry" = "\${RETRY_PERIOD}" ]; then
    echo "  \$1: PATCHED, at target (\${cur_lease}/\${cur_renew}/\${cur_retry})"
  else
    echo "  \$1: DRIFTED -- current \${cur_lease:-unset}/\${cur_renew:-unset}/\${cur_retry:-unset} (target \${LEASE_DURATION}/\${RENEW_DEADLINE}/\${RETRY_PERIOD})"
  fi
}

etcd_cpu_status() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found"; return; }
  local cur
  cur="\$(grep -A1 'requests:' "\$file" | grep 'cpu:' | awk '{print \$2}' || true)"
  echo "  etcd cpu request: \${cur:-unset} (target \${ETCD_CPU_REQUEST})"
}

etcd_cpu_apply() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found -- skipping"; return; }
  local cur
  cur="\$(grep -A1 'requests:' "\$file" | grep 'cpu:' | awk '{print \$2}' || true)"
  if [ "\$cur" = "\${ETCD_CPU_REQUEST}" ]; then
    echo "  etcd cpu request: already \${ETCD_CPU_REQUEST} -- no-op"
    return
  fi
  cp "\$file" "/root/manifest-bak/etcd.yaml.bak.\$(date +%s)"
  sed -i "0,/cpu: \$cur/s//cpu: \${ETCD_CPU_REQUEST}/" "\$file"
  echo "  etcd cpu request: \$cur -> \${ETCD_CPU_REQUEST}"
}

etcd_compaction_apply() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found -- skipping"; return; }
  local cur_mode cur_retention
  cur_mode="\$(grep -oE -- '--auto-compaction-mode=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retention="\$(grep -oE -- '--auto-compaction-retention=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ "\$cur_mode" = "periodic" ] && [ "\$cur_retention" = "1h" ]; then
    echo "  etcd auto-compaction: already at target (periodic, 1h) -- no-op"
  else
    if grep -q -- '--election-timeout=' "\$file"; then
      cp "\$file" "/root/manifest-bak/etcd.yaml.bak.\$(date +%s)"
      sed -i -E "/--auto-compaction-mode=/d; /--auto-compaction-retention=/d" "\$file"
      sed -i "/--election-timeout=/a\\\\    - --auto-compaction-mode=periodic\\\\n    - --auto-compaction-retention=1h" "\$file"
      echo "  etcd auto-compaction: enabled (periodic, 1h retention)"
    fi
  fi
  echo "  Running etcd defrag..."
  ETCDCTL_API=3 etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key --endpoints=https://127.0.0.1:2379 defrag && echo "  etcd defrag: OK" || echo "  etcd defrag: FAILED"
}

kube_vip_status() {
  echo "kube-vip:"
  local file="\$MDIR/kube-vip.yaml"
  if [ ! -f "\$file" ]; then echo "  manifest not found"; return; fi
  echo "  on-disk file settings:"
  grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod\|vip_preserve_on_leadership_loss' "\$file" | grep -E 'name|value' | paste - - | sed 's/^/    /'
}

case "\$FAMB_ACTION" in
  status)
    echo "kube-controller-manager / kube-scheduler:"
    status_leader_elect kube-controller-manager
    status_leader_elect kube-scheduler
    echo ""
    echo "etcd:"
    etcd_cpu_status
    echo ""
    kube_vip_status
    ;;
  apply-lease)
    echo "kube-controller-manager / kube-scheduler:"
    patch_leader_elect kube-controller-manager
    patch_leader_elect kube-scheduler
    echo ""
    echo "etcd:"
    etcd_cpu_apply
    ;;
  revert-lease)
    echo "kube-controller-manager / kube-scheduler:"
    revert_leader_elect kube-controller-manager
    revert_leader_elect kube-scheduler
    echo ""
    echo "etcd:"
    revert_etcd
    ;;
  etcd-compaction)
    etcd_compaction_apply
    ;;
  kube-vip-status)
    kube_vip_status
    ;;
esac
REMOTE

# Generate remote helper script for safe kube-vip static pod manifest edit
cat > "$TMP/kube-vip-apply.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
VIP_LEASE_DURATION="${VIP_LEASE_DURATION}"
VIP_RENEW_DEADLINE="${VIP_RENEW_DEADLINE}"
VIP_RETRY_PERIOD="${VIP_RETRY_PERIOD}"
FILE=/etc/kubernetes/manifests/kube-vip.yaml

[ -f "\$FILE" ] || { echo "ERROR: \$FILE not found -- skipping."; exit 0; }

mkdir -p /root/manifest-bak
if ls /etc/kubernetes/manifests/kube-vip.yaml.bak.* >/dev/null 2>&1; then
  mv /etc/kubernetes/manifests/kube-vip.yaml.bak.* /root/manifest-bak/ 2>/dev/null || true
fi

CUR="\$(cat "\$FILE")"
NEW="\$(echo "\$CUR" | sed -E "/name: vip_leaseduration/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_LEASE_DURATION}\"/}" \
                     | sed -E "/name: vip_renewdeadline/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_RENEW_DEADLINE}\"/}" \
                     | sed -E "/name: vip_retryperiod/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_RETRY_PERIOD}\"/}")"

if [ "\$NEW" = "\$CUR" ]; then
  echo "kube-vip manifest already at target values (\${VIP_LEASE_DURATION}/\${VIP_RENEW_DEADLINE}/\${VIP_RETRY_PERIOD}) -- no-op"
  exit 0
fi

cp "\$FILE" "/root/manifest-bak/kube-vip.yaml.bak.\$(date +%s)"
TMPFILE="/root/.vsp-kubevip-\$\$"
printf '%s\n' "\$NEW" > "\$TMPFILE"
cat "\$TMPFILE" > "\$FILE"
rm -f "\$TMPFILE"

POD_ID="\$(crictl pods --name '^kube-vip-'"\$(hostname)"'\$' -q 2>/dev/null | head -1 || true)"
if [ -n "\$POD_ID" ]; then
  CID="\$(crictl ps --pod "\$POD_ID" -q 2>/dev/null | head -1 || true)"
  [ -n "\$CID" ] && crictl stop "\$CID" >/dev/null 2>&1 || true
fi
echo "kube-vip manifest updated in-place and pod restarted."
REMOTE

famb_run() {
  local action="$1"
  log "Executing static-manifest action: ${action} on VSP CP (${VSP_CP_IP})..."
  test_reachable || { error "Cannot reach VSP CP ${VSP_CP_IP}"; return 2; }
  if [ "$action" = "kube-vip-apply" ]; then
    copy_to_node "$TMP/kube-vip-apply.sh" "/tmp/kube-vip-apply.sh"
    run_node_root "install -m 0755 /tmp/kube-vip-apply.sh /usr/local/bin/kube-vip-apply.sh && /usr/local/bin/kube-vip-apply.sh; rm -f /tmp/kube-vip-apply.sh /usr/local/bin/kube-vip-apply.sh"
  else
    copy_to_node "$TMP/remediate-lease.sh" "/tmp/remediate-lease.sh"
    run_node_root "install -m 0755 /tmp/remediate-lease.sh /usr/local/bin/remediate-lease.sh && /usr/local/bin/remediate-lease.sh $action; rm -f /tmp/remediate-lease.sh /usr/local/bin/remediate-lease.sh"
  fi
}

if [ "$ACTION" = "apply-lease" ] || [ "$ACTION" = "revert-lease" ] || [ "$ACTION" = "etcd-compaction" ] || [ "$ACTION" = "kube-vip-apply" ]; then
  acquire_lock
  famb_run "$ACTION"
  exit 0
fi

if [ "$ACTION" = "status" ]; then
  log "=== VSP Control Plane (${VSP_CP_IP}) Status ==="
  if test_reachable; then
    run_node_root "systemctl --no-pager is-active vsp-fleet-depot-keeper.timer 2>/dev/null || echo 'keeper timer inactive'"
    echo "-- Pod Statuses --"
    run_node_root "kubectl --request-timeout=20s -n vcf-fleet-depot get pods 2>/dev/null | grep -E 'NAME|depot|distribution' || true"
    run_node_root "kubectl --request-timeout=20s -n vcf-fleet-lcm get pods 2>/dev/null | grep -E 'NAME|fleetbuild' || true"
    run_node_root "kubectl --request-timeout=20s -n vmsp-platform get pods 2>/dev/null | grep -E 'NAME|envoy-gateway|vmsp-gateway|ops-logs-gateway' || true"
    run_node_root "kubectl --request-timeout=20s -n vidb-external get pods 2>/dev/null | grep -E 'NAME|vidb-service' || true"
    run_node_root "kubectl --request-timeout=20s -n vcf-sddc-lcm get pods 2>/dev/null | grep -E 'NAME|sddcbuild|sddcupgrade' || true"
    run_node_root "kubectl --request-timeout=20s -n vmsp-platform get pods 2>/dev/null | grep -E 'NAME|prometheus-kube-prometheus|kube-state-metrics|node-exporter' || true"
    echo "-- Static Manifest & Lease Status --"
    famb_run "status"
  else
    error "Cannot reach VSP CP node at ${VSP_CP_IP}"
    exit 2
  fi
  exit 0
fi

if [ "$ACTION" = "remove" ]; then
  acquire_lock
  log "Removing VSP keeper timer and service from ${VSP_CP_IP}..."
  if test_reachable; then
    run_node_root "systemctl disable --now vsp-fleet-depot-keeper.timer 2>/dev/null || true"
    run_node_root "rm -f /etc/systemd/system/vsp-fleet-depot-keeper.{service,timer} /usr/local/bin/vsp-fleet-depot-keeper.sh /usr/local/etc/vsp-*.yaml"
    run_node_root "systemctl daemon-reload"
    log "VSP keeper removed."
  fi
  exit 0
fi

# ==================== DEFAULT ACTION: INSTALL KEEPERS & TUNING ====================
acquire_lock
log "Installing / refreshing VSP Fleet Control Plane keepers and static manifest tuning on ${VSP_CP_IP}..."
test_reachable || { error "Cannot reach VSP CP node at ${VSP_CP_IP}"; exit 2; }

# Generate patch files and keeper script
cat > "$TMP/vsp-fleet-depot-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: download-service
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60}
        resources: {limits: {memory: 2Gi}, requests: {cpu: 300m, memory: 512Mi}}
      - name: file-server
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
      - name: proxy-forwarder
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

cat > "$TMP/vsp-fleet-lcm-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: fleetbuild
        livenessProbe:  {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

cat > "$TMP/vsp-envoy-gateway-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: envoy-gateway
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 20}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
        startupProbe:
          httpGet: {path: /healthz, port: 8081, scheme: HTTP}
          timeoutSeconds: 10
          failureThreshold: 60
          periodSeconds: 10
PATCH

cat > "$TMP/vsp-vidb-service-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: vidb-service
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
PATCH

cat > "$TMP/vsp-sddcbuild-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: sddcbuild
        livenessProbe:  {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

cat > "$TMP/vsp-sddcupgrade-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: sddcupgrade
        livenessProbe:  {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

cat > "$TMP/vsp-prometheus-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: prometheus
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 8, periodSeconds: 10}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 8, periodSeconds: 10}
        resources: {limits: {memory: 4Gi}, requests: {memory: 1Gi}}
PATCH

cat > "$TMP/vsp-ksm-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: kube-state-metrics
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6}
PATCH

cat > "$TMP/vsp-node-exporter-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: node-exporter
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6}
PATCH

cat > "$TMP/vsp-fleet-depot-keeper.sh" <<'KEEPER'
#!/bin/bash
export KUBECONFIG=/etc/kubernetes/admin.conf
KB=/usr/local/bin/kubectl
PROBE_TARGETS='
vcf-fleet-depot deployment/depot-service download-service 10 /usr/local/etc/vsp-fleet-depot-patch.yaml
vcf-fleet-lcm deployment/vcf-fleet-build-service-fleetbuild fleetbuild 15 /usr/local/etc/vsp-fleet-lcm-patch.yaml
vidb-external deployment/vidb-service vidb-service 10 /usr/local/etc/vsp-vidb-service-patch.yaml
vcf-sddc-lcm deployment/vcf-sddc-build-service-sddcbuild sddcbuild 15 /usr/local/etc/vsp-sddcbuild-patch.yaml
vcf-sddc-lcm deployment/vcf-sddc-upgrade-service-sddcupgrade sddcupgrade 15 /usr/local/etc/vsp-sddcupgrade-patch.yaml
vmsp-platform statefulset/prometheus-kube-prometheus-stack-prometheus prometheus 10 /usr/local/etc/vsp-prometheus-patch.yaml
vmsp-platform deployment/kube-prometheus-stack-kube-state-metrics kube-state-metrics 10 /usr/local/etc/vsp-ksm-patch.yaml
vmsp-platform daemonset/kube-prometheus-stack-prometheus-node-exporter node-exporter 10 /usr/local/etc/vsp-node-exporter-patch.yaml
'
echo "$PROBE_TARGETS" | while read -r NS REF CON WANT PF; do
  [ -z "$NS" ] && continue
  CUR=$($KB -n "$NS" get "$REF" -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$CON\")].livenessProbe.timeoutSeconds}" 2>/dev/null || echo "")
  if [ "$CUR" != "$WANT" ]; then
    $KB -n "$NS" patch "$REF" --type=strategic --patch-file "$PF" >/dev/null 2>&1 \
      && logger -t vsp-fleet-depot-keeper "drift corrected: $NS/$REF probes (livenessTimeout was '${CUR:-unset}')"
  fi
done
EGMEM=$($KB -n vmsp-platform get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null || echo "")
if [ "$EGMEM" != "4Gi" ]; then
  $KB -n vmsp-platform set resources deploy/envoy-gateway --limits=memory=4Gi --requests=memory=512Mi >/dev/null 2>&1 \
    && logger -t vsp-fleet-depot-keeper "drift corrected: envoy-gateway memory -> 4Gi (was '${EGMEM:-unset}')"
fi
EGPROBE=$($KB -n vmsp-platform get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].livenessProbe.timeoutSeconds}' 2>/dev/null || echo "")
if [ "$EGPROBE" != "10" ]; then
  $KB -n vmsp-platform patch deploy envoy-gateway --type=strategic --patch-file /usr/local/etc/vsp-envoy-gateway-patch.yaml >/dev/null 2>&1 \
    && logger -t vsp-fleet-depot-keeper "drift corrected: envoy-gateway probes (livenessTimeout was '${EGPROBE:-unset}')"
fi
PROMMEM=$($KB -n vmsp-platform get statefulset prometheus-kube-prometheus-stack-prometheus -o jsonpath='{.spec.template.spec.containers[?(@.name=="prometheus")].resources.limits.memory}' 2>/dev/null || echo "")
if [ "$PROMMEM" != "4Gi" ]; then
  $KB -n vmsp-platform patch statefulset prometheus-kube-prometheus-stack-prometheus --type=strategic --patch-file /usr/local/etc/vsp-prometheus-patch.yaml >/dev/null 2>&1 \
    && logger -t vsp-fleet-depot-keeper "drift corrected: prometheus memory -> 4Gi (was '${PROMMEM:-unset}')"
fi
KEEPER

cat > "$TMP/vsp-fleet-depot-keeper.service" <<'SVC'
[Unit]
Description=VSP: re-apply vcf-fleet-depot + fleet-lcm + envoy-gateway fixes (drift keeper)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vsp-fleet-depot-keeper.sh
SVC

cat > "$TMP/vsp-fleet-depot-keeper.timer" <<'TIMER'
[Unit]
Description=VSP: run vsp-fleet-depot-keeper every 60s (drift watcher)
[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
Unit=vsp-fleet-depot-keeper.service
[Install]
WantedBy=timers.target
TIMER

# Deploy keeper files to VSP CP node
copy_to_node "$TMP/vsp-fleet-depot-keeper.sh" "/tmp/vsp-fleet-depot-keeper.sh"
copy_to_node "$TMP/vsp-fleet-depot-patch.yaml" "/tmp/vsp-fleet-depot-patch.yaml"
copy_to_node "$TMP/vsp-fleet-lcm-patch.yaml" "/tmp/vsp-fleet-lcm-patch.yaml"
copy_to_node "$TMP/vsp-envoy-gateway-patch.yaml" "/tmp/vsp-envoy-gateway-patch.yaml"
copy_to_node "$TMP/vsp-vidb-service-patch.yaml" "/tmp/vsp-vidb-service-patch.yaml"
copy_to_node "$TMP/vsp-sddcbuild-patch.yaml" "/tmp/vsp-sddcbuild-patch.yaml"
copy_to_node "$TMP/vsp-sddcupgrade-patch.yaml" "/tmp/vsp-sddcupgrade-patch.yaml"
copy_to_node "$TMP/vsp-prometheus-patch.yaml" "/tmp/vsp-prometheus-patch.yaml"
copy_to_node "$TMP/vsp-ksm-patch.yaml" "/tmp/vsp-ksm-patch.yaml"
copy_to_node "$TMP/vsp-node-exporter-patch.yaml" "/tmp/vsp-node-exporter-patch.yaml"
copy_to_node "$TMP/vsp-fleet-depot-keeper.service" "/tmp/vsp-fleet-depot-keeper.service"
copy_to_node "$TMP/vsp-fleet-depot-keeper.timer" "/tmp/vsp-fleet-depot-keeper.timer"

run_node_root "
  install -m 0755 /tmp/vsp-fleet-depot-keeper.sh /usr/local/bin/vsp-fleet-depot-keeper.sh &&
  install -d /usr/local/etc &&
  install -m 0644 /tmp/vsp-fleet-depot-patch.yaml /usr/local/etc/vsp-fleet-depot-patch.yaml &&
  install -m 0644 /tmp/vsp-fleet-lcm-patch.yaml   /usr/local/etc/vsp-fleet-lcm-patch.yaml &&
  install -m 0644 /tmp/vsp-envoy-gateway-patch.yaml /usr/local/etc/vsp-envoy-gateway-patch.yaml &&
  install -m 0644 /tmp/vsp-vidb-service-patch.yaml /usr/local/etc/vsp-vidb-service-patch.yaml &&
  install -m 0644 /tmp/vsp-sddcbuild-patch.yaml /usr/local/etc/vsp-sddcbuild-patch.yaml &&
  install -m 0644 /tmp/vsp-sddcupgrade-patch.yaml /usr/local/etc/vsp-sddcupgrade-patch.yaml &&
  install -m 0644 /tmp/vsp-prometheus-patch.yaml /usr/local/etc/vsp-prometheus-patch.yaml &&
  install -m 0644 /tmp/vsp-ksm-patch.yaml /usr/local/etc/vsp-ksm-patch.yaml &&
  install -m 0644 /tmp/vsp-node-exporter-patch.yaml /usr/local/etc/vsp-node-exporter-patch.yaml &&
  install -m 0644 /tmp/vsp-fleet-depot-keeper.service /etc/systemd/system/vsp-fleet-depot-keeper.service &&
  install -m 0644 /tmp/vsp-fleet-depot-keeper.timer   /etc/systemd/system/vsp-fleet-depot-keeper.timer &&
  systemctl daemon-reload && systemctl enable --now vsp-fleet-depot-keeper.timer &&
  /usr/local/bin/vsp-fleet-depot-keeper.sh ;
  rm -f /tmp/vsp-fleet-depot-* /tmp/vsp-envoy-gateway-* /tmp/vsp-vidb-service-* /tmp/vsp-sddc* /tmp/vsp-prometheus-* /tmp/vsp-ksm-* /tmp/vsp-node-exporter-*
"

log "VSP keeper timer active."

# Apply static manifest lease and etcd tuning
famb_run "apply-lease"
famb_run "etcd-compaction"
famb_run "kube-vip-apply"

log "VSP Fleet Control Plane stabilization completed successfully."
