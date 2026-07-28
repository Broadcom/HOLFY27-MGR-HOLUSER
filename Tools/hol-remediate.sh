#!/usr/bin/env bash
# =============================================================================
# hol-remediate.sh   (v3.0.0 - 2026-07-25, HOL Core Team)
#
# Unified Console Orchestrator for Holodeck Lab Remediation & Stabilization.
# Delegates specialized stabilization tasks to:
#   1. VCFA Node (10.1.1.70):  Tools/vcfa-stabilizer.sh (v2.20)
#   2. VSP Control Plane (10.1.1.142): Tools/vsp-stabilizer.sh (v1.0.0)
#
# Idempotent and safe to run on-demand or via automated post-boot routines.
# =============================================================================
set -euo pipefail

VSP_CP_IP="${VSP_CP_IP:-10.1.1.142}"
AUTOA_IP="${AUTOA_IP:-10.1.1.70}"
CREDS_FILE="${CREDS_FILE:-/home/holuser/creds.txt}"
NODE_USER="vmware-system-user"

DO_VSP=1
DO_VCFA=1
ACTION="install"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VCFA_SCRIPT="${SCRIPT_DIR}/vcfa-stabilizer.sh"
VSP_SCRIPT="${SCRIPT_DIR}/vsp-stabilizer.sh"

log()     { echo "[INFO]  $(date +'%Y-%m-%d %H:%M:%S') $1"; }
warning() { echo "[WARN]  $(date +'%Y-%m-%d %H:%M:%S') $1"; }
error()   { echo "[ERROR] $(date +'%Y-%m-%d %H:%M:%S') $1"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --vsp-cp) VSP_CP_IP="$2"; shift 2;;
    --autoa)  AUTOA_IP="$2";  shift 2;;
    --vsp-only)  DO_VCFA=0; shift;;
    --vcfa-only) DO_VSP=0;  shift;;
    --status) ACTION="status"; shift;;
    --remove) ACTION="remove"; shift;;
    --apply-lease) ACTION="apply-lease"; shift;;
    --revert-lease) ACTION="revert-lease"; shift;;
    --etcd-compaction) ACTION="etcd-compaction"; shift;;
    --kube-vip-apply) ACTION="kube-vip-apply"; shift;;
    -h|--help)
      echo "hol-remediate.sh (v3.0.0 - 2026-07-25)"
      echo "Usage: $0 [options]"
      echo ""
      echo "Options:"
      echo "  (no args)           Run full remediation on both VCFA and VSP Control Plane"
      echo "  --vsp-only          Target VSP Control Plane node only"
      echo "  --vcfa-only         Target VCFA node only"
      echo "  --vsp-cp IP         Override VSP CP IP (default: 10.1.1.142)"
      echo "  --autoa IP          Override VCFA IP (default: 10.1.1.70)"
      echo "  --status            Check status across both targets"
      echo "  --remove            Uninstall drift keepers on both targets"
      echo "  --apply-lease       Apply static manifest lease tuning only"
      echo "  --revert-lease      Revert static manifest lease tuning"
      echo "  --etcd-compaction   Run etcd auto-compaction and defrag"
      echo "  --kube-vip-apply    Safe file-based kube-vip update on both targets"
      echo "  --help, -h          Show this help message"
      exit 0
      ;;
    *) error "Unknown arg: $1"; exit 1;;
  esac
done

# Ensure required scripts exist
[ -f "$VCFA_SCRIPT" ] || { error "VCFA stabilizer script not found at $VCFA_SCRIPT"; exit 2; }
[ -f "$VSP_SCRIPT" ]  || { error "VSP stabilizer script not found at $VSP_SCRIPT"; exit 2; }

SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"

run_vcfa() {
  local act="$1"
  log "=== VCFA Node (${AUTOA_IP}) Action: ${act} ==="
  if [ "$act" = "install" ]; then
    bash "$VCFA_SCRIPT"
  elif [ "$act" = "status" ]; then
    bash "$VCFA_SCRIPT" --status
  else
    # Map common flags to vcfa-stabilizer options
    case "$act" in
      remove)
        # To remove keepers on VCFA
        if command -v sshpass >/dev/null 2>&1 && [ -f "$CREDS_FILE" ]; then
          sshpass -f "$CREDS_FILE" ssh $SSHO "${NODE_USER}@${AUTOA_IP}" "echo '$(cat "$CREDS_FILE")' | sudo -S -i systemctl disable --now vcfa-eg-mem-keeper.timer vcfa-vip-watchdog.service vcfa-support-bundle-keeper.timer 2>/dev/null || true"
          log "VCFA keepers disabled."
        fi
        ;;
      apply-lease|etcd-compaction|kube-vip-apply)
        bash "$VCFA_SCRIPT"
        ;;
      revert-lease)
        log "Revert lease requested for VCFA -- re-running vcfa-stabilizer default flow"
        bash "$VCFA_SCRIPT"
        ;;
    esac
  fi
}

run_vsp() {
  local act="$1"
  log "=== VSP Fleet Control-Plane Node (${VSP_CP_IP}) Action: ${act} ==="
  case "$act" in
    install)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP"
      ;;
    status)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP" --status
      ;;
    remove)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP" --remove
      ;;
    apply-lease)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP" --apply-lease
      ;;
    revert-lease)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP" --revert-lease
      ;;
    etcd-compaction)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP" --etcd-compaction
      ;;
    kube-vip-apply)
      bash "$VSP_SCRIPT" --vsp-cp "$VSP_CP_IP" --kube-vip-apply
      ;;
  esac
}

RC=0
if [ "$DO_VCFA" -eq 1 ]; then
  run_vcfa "$ACTION" || RC=$?
fi

if [ "$DO_VSP" -eq 1 ]; then
  run_vsp "$ACTION" || RC=$?
fi

log "hol-remediate execution completed with exit code $RC."
exit "$RC"
