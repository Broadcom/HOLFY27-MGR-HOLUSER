#!/usr/bin/env bash
#
# Copyright (c) 2026 Broadcom. All Rights Reserved.
# Broadcom Confidential. The term "Broadcom" refers to Broadcom Inc.
# and/or its subsidiaries.
#
# vcf_services_runtime_shutdown.sh
#
# Orchestrates a safe, ordered shutdown of the VCF Services Runtime
# system via the management REST API, then powers off the underlying
# VMs via vSphere.  No kubectl access is required.
#
# Shutdown order:
#   The system shutdown API is invoked, which handles scaling down all
#   tenant workloads and platform controllers, and creates the global
#   power-off-marker for automatic recovery on boot.
#
# Usage:
#   vcf_services_runtime_shutdown.sh [OPTIONS]
#
# Options:
#   --node-ip IP            IP address of any reachable cluster node
#                           (API server listens on port 5480).
#   --password PASSWORD     Breakglass password for vmware-system-user.
#                           Omit to be prompted interactively.
#   --dry-run               Log planned actions without executing any
#                           shutdown or power-off operations.
#   --skip-poweroff         Shut down components but leave VMs running.
#   --skip-snapshot-check   Bypass the VM snapshot pre-flight check.
#   --help                  Show this help message and exit.
#
# Environment Variables (override defaults):
#   NODE_IP                 IP address of any reachable cluster node.
#   NODE_PORT               Node HTTPS port to host management APIs(default: 5480).
#   VMSP_PASSWORD           Breakglass password (avoids interactive prompt).
#   GOVC_URL                vCenter server URL. Auto-discovered from the vsp
#                           component config if not set.
#   VCENTER_USERNAME        vCenter username. If unset, automated VM power-off
#                           is skipped and the VM list is printed for manual action.
#   VCENTER_PASSWORD        vCenter password. If unset, automated VM power-off
#                           is skipped and the VM list is printed for manual action.
#   GOVC_INSECURE           Set to "true" to skip TLS verification (default: true).
#   TASK_POLL_INTERVAL      Seconds between task status polls (default: 15).
#   TASK_TIMEOUT_SECONDS    Seconds to wait for a component shutdown task
#                           (default: 600).
#   POWEROFF_WAIT_SECONDS   Seconds to wait between VM power-off calls
#                           (default: 5).
#

set -o errexit
set -o nounset
set -o pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_NAME="$(basename "${0}")"
readonly SCRIPT_NAME
readonly SCRIPT_VERSION="1.0.0"
readonly DEFAULT_NODE_PORT=5480
readonly DEFAULT_TASK_POLL_INTERVAL=15
readonly DEFAULT_TASK_TIMEOUT=600
readonly DEFAULT_POWEROFF_WAIT=5
readonly API_USER="vmware-system-user"
readonly LOGIN_PATH="/api/v1/auth/login"
readonly COMPONENTS_PATH="/api/v1/components"
readonly TASKS_PATH="/api/v1/tasks"
readonly NODES_PATH="/api/v1/system/inventory/nodes"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
DRY_RUN="false"
SKIP_POWEROFF="false"
SKIP_SNAPSHOT_CHECK="false"
NODE_IP="${NODE_IP:-}"
NODE_PORT="${NODE_PORT:-${DEFAULT_NODE_PORT}}"
VMSP_PASSWORD="${VMSP_PASSWORD:-}"
VMSP_API_TMPFILE="$(mktemp)"
TASK_POLL_INTERVAL="${TASK_POLL_INTERVAL:-${DEFAULT_TASK_POLL_INTERVAL}}"
TASK_TIMEOUT="${TASK_TIMEOUT_SECONDS:-${DEFAULT_TASK_TIMEOUT}}"
POWEROFF_WAIT="${POWEROFF_WAIT_SECONDS:-${DEFAULT_POWEROFF_WAIT}}"

# Populated after login
API_BASE=""
AUTH_TOKEN=""

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log() {
  echo "[$(date +'%Y-%m-%dT%H:%M:%SZ')] [INFO]  ${*}"
}

log_warn() {
  echo "[$(date +'%Y-%m-%dT%H:%M:%SZ')] [WARN]  ${*}" >&2
}

log_error() {
  echo "[$(date +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] ${*}" >&2
}

log_step() {
  echo ""
  echo "=========================================================="
  echo "[$(date +'%Y-%m-%dT%H:%M:%SZ')] [STEP]  ${*}"
  echo "=========================================================="
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
  local -r exit_code="${?}"
  rm -f "${VMSP_API_TMPFILE}" 2> /dev/null || true
  if [[ "${exit_code}" -ne 0 ]]; then
    log_error "Script exited with code ${exit_code}."
    log_error "Review the output above and consult the KB article for recovery steps."
  fi
  exit "${exit_code}"
}

trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
  grep '^#' "${0}" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
  exit 0
}

parse_args() {
  while [[ "${#}" -gt 0 ]]; do
    case "${1}" in
      --node-ip)
        NODE_IP="${2:?'--node-ip requires a value'}"
        shift 2
        ;;
      --password)
        VMSP_PASSWORD="${2:?'--password requires a value'}"
        shift 2
        ;;
      --dry-run)
        DRY_RUN="true"
        shift
        ;;
      --skip-poweroff)
        SKIP_POWEROFF="true"
        shift
        ;;
      --skip-snapshot-check)
        SKIP_SNAPSHOT_CHECK="true"
        shift
        ;;
      --help | -h)
        usage
        ;;
      *)
        log_error "Unknown option: ${1}"
        log_error "Run '${SCRIPT_NAME} --help' for usage."
        exit 1
        ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
vcenter_credentials_available() {
  [[ -n "${VCENTER_USERNAME:-}" && -n "${VCENTER_PASSWORD:-}" ]]
}

check_prerequisites() {
  log_step "Checking prerequisites"

  local -a required_tools=("curl" "jq")
  if [[ "${SKIP_POWEROFF}" == "false" ]] && vcenter_credentials_available; then
    required_tools+=("govc")
  fi

  local missing=0
  for tool in "${required_tools[@]}"; do
    if ! command -v "${tool}" &> /dev/null; then
      log_error "Required tool not found: ${tool}"
      missing=$((missing + 1))
    else
      log "Found: ${tool} ($(command -v "${tool}"))"
    fi
  done

  if [[ "${missing}" -gt 0 ]]; then
    log_error "${missing} required tool(s) are missing. Install them before running this script."
    exit 1
  fi

  if [[ -z "${NODE_IP}" ]]; then
    log_error "Node IP is required. Use --node-ip or set NODE_IP."
    exit 1
  fi

  API_BASE="https://${NODE_IP}:${NODE_PORT}"
  log "Management API base: ${API_BASE}"
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
prompt_for_password() {
  if [[ -n "${VMSP_PASSWORD}" ]]; then
    return 0
  fi
  log "Enter the breakglass password for '${API_USER}':"
  read -r -s VMSP_PASSWORD
  echo ""
  if [[ -z "${VMSP_PASSWORD}" ]]; then
    log_error "Password cannot be empty."
    exit 1
  fi
}

api_login() {
  log_step "Authenticating with API server"

  prompt_for_password

  local payload
  payload=$(jq -n --arg u "${API_USER}" --arg p "${VMSP_PASSWORD}" \
    '{"username": $u, "password": $p}')

  local http_code body
  http_code=$(curl -sk -o "${VMSP_API_TMPFILE}" \
    -w "%{http_code}" \
    -X POST "${API_BASE}${LOGIN_PATH}" \
    -H "Content-Type: application/json" \
    -d "${payload}" 2> /dev/null) || {
    log_error "Failed to reach API server at ${API_BASE}. Check --node-ip and network connectivity."
    exit 1
  }
  body=$(cat "${VMSP_API_TMPFILE}")

  if [[ "${http_code}" != "200" ]]; then
    log_error "Authentication failed (HTTP ${http_code}): ${body}"
    exit 1
  fi

  AUTH_TOKEN=$(echo "${body}" | jq -r '.token // empty')
  if [[ -z "${AUTH_TOKEN}" ]]; then
    log_error "No token in login response: ${body}"
    exit 1
  fi

  log "Authentication successful."
}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

# api_get <path> — returns response body; exits on HTTP error
api_get() {
  local path="${1}"
  local http_code body

  http_code=$(curl -sk -o "${VMSP_API_TMPFILE}" \
    -w "%{http_code}" \
    -X GET "${API_BASE}${path}" \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Accept: application/json" 2> /dev/null) || {
    log_error "GET ${path} — connection failed."
    exit 1
  }
  body=$(cat "${VMSP_API_TMPFILE}")

  if [[ "${http_code}" == "401" ]]; then
    log "Token expired — re-authenticating..."
    if ! api_login; then
      log_error "Re-authentication failed. Cannot retry GET ${path}."
      exit 1
    fi
    api_get "${path}"
    return
  fi

  if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
    log_error "GET ${path} failed (HTTP ${http_code}): ${body}"
    exit 1
  fi

  echo "${body}"
}

# api_post <path> [body] — returns response body; exits on HTTP error
api_post() {
  local path="${1}"
  local post_body="${2:-}"
  local http_code body

  local curl_args=(-sk -o "${VMSP_API_TMPFILE}" -w "%{http_code}"
    -X POST "${API_BASE}${path}"
    -H "Authorization: Bearer ${AUTH_TOKEN}"
    -H "Content-Type: application/json"
    -H "Accept: application/json")

  if [[ -n "${post_body}" ]]; then
    curl_args+=(-d "${post_body}")
  fi

  http_code=$(curl "${curl_args[@]}" 2> /dev/null) || {
    log_error "POST ${path} — connection failed."
    exit 1
  }
  body=$(cat "${VMSP_API_TMPFILE}")

  if [[ "${http_code}" == "401" ]]; then
    log "Token expired — re-authenticating..."
    if ! api_login; then
      log_error "Re-authentication failed. Cannot retry POST ${path}."
      exit 1
    fi
    api_post "${path}" "${post_body}"
    return
  fi

  if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
    log_error "POST ${path} failed (HTTP ${http_code}): ${body}"
    exit 1
  fi

  echo "${body}"
}

# ---------------------------------------------------------------------------
# Snapshot pre-check
# ---------------------------------------------------------------------------
check_no_snapshots() {
  log_step "Checking for VM snapshots on VCF Services Runtime nodes"

  if [[ "${SKIP_SNAPSHOT_CHECK}" == "true" ]]; then
    log_warn "Snapshot check skipped (--skip-snapshot-check). Ensure no snapshots exist on cluster node VMs before proceeding."
    return 0
  fi

  if [[ "${DRY_RUN}" == "true" ]]; then
    log "[DRY-RUN] Would check for VM snapshots via API."
    return 0
  fi

  # The actual snapshot check is enforced by the component shutdown precheck
  # workflow triggered by the API. This step is informational only.
  log "Snapshot pre-check is enforced by the component shutdown precheck workflow."
  log "Use --skip-snapshot-check only if you have manually confirmed no snapshots exist."
  log "Proceeding — snapshots will be detected during component shutdown prechecks."
}

# ---------------------------------------------------------------------------
# Wait for a task to reach a terminal state
# ---------------------------------------------------------------------------
wait_for_task() {
  local -r task_id="${1}"
  local -r target_name="${2}"
  local elapsed=0

  log "  Waiting for task ${task_id} (${target_name})...."

  while true; do
    local task_body status
    task_body=$(api_get "${TASKS_PATH}/${task_id}")
    status=$(echo "${task_body}" | jq -r '.status // .phase // "Unknown"')

    log "  Task ${task_id} status: ${status}"

    case "${status}" in
      Succeeded)
        log "  '${target_name}' shutdown succeeded."
        return 0
        ;;
      Failed)
        log_error "  '${target_name}' shutdown task failed."
        log_error "  Task details: $(echo "${task_body}" | jq -c '.messages // [] | map(.default) | join("; ")')"
        return 1
        ;;
    esac

    if [[ "${elapsed}" -ge "${TASK_TIMEOUT}" ]]; then
      log_error "  Timed out waiting for task ${task_id} (${target_name}) after ${TASK_TIMEOUT}s."
      return 1
    fi

    sleep "${TASK_POLL_INTERVAL}"
    elapsed=$((elapsed + TASK_POLL_INTERVAL))
  done
}

# ---------------------------------------------------------------------------
# Shutdown the system
# ---------------------------------------------------------------------------
shutdown_system() {
  log_step "Shutting down VCF Services Runtime system"

  if [[ "${DRY_RUN}" == "true" ]]; then
    log "  [DRY-RUN] Would POST /api/v1/system?action=shutdown"
    return 0
  fi

  local response task_id
  response=$(api_post "/api/v1/system?action=shutdown")
  task_id=$(echo "${response}" | jq -r '.id // empty')

  if [[ -z "${task_id}" ]]; then
    log_error "  No task ID returned for system shutdown: ${response}"
    return 1
  fi

  log "  System shutdown task created: ${task_id}"
  if ! wait_for_task "${task_id}" "system"; then
    log_error "System shutdown failed. Resolve the failures above before powering off VMs."
    exit 1
  fi

  log "System shut down successfully."
}

# ---------------------------------------------------------------------------
# Retrieve cluster nodes
# ---------------------------------------------------------------------------
get_cluster_nodes() {
  local response
  if ! response=$(api_get "${NODES_PATH}"); then
    log_error "Failed to retrieve cluster nodes from ${NODES_PATH}."
    return 1
  fi

  local count
  count=$(echo "${response}" | jq '.nodes // [] | length')
  # Use stderr so this log line does not pollute the base64 output consumed by callers.
  echo "[$(date +'%Y-%m-%dT%H:%M:%SZ')] [INFO]  Returned ${count} node(s)." >&2

  echo "${response}" | jq -r '.nodes // [] | .[] | @base64'
}

# ---------------------------------------------------------------------------
# Auto-discover vCenter URL from the vsp component configuration
# ---------------------------------------------------------------------------
discover_vcenter_url() {
  log "Auto-discovering vCenter URL from vsp component configuration..."

  local response vcenter_url
  response=$(api_get "${COMPONENTS_PATH}?type=vsp") || return 1
  # The vCenter server is exposed in two possible shapes depending on whether
  # the alias ConfigMap is active:
  #   - nested:  .spec.configuration.infrastructure.vsphere.server  (aliased)
  #   - flat:    .spec.configuration["provider.vsphere.server"]      (canonical)
  vcenter_url=$(echo "${response}" |
    jq -r '(.components // []) | .[0].spec.configuration |
               (.infrastructure.vsphere.server // .["provider.vsphere.server"]) // empty')

  if [[ -z "${vcenter_url}" ]]; then
    log_warn "Could not determine vCenter URL from vsp component config."
    return 1
  fi

  log "Discovered vCenter URL: ${vcenter_url}"
  GOVC_URL="https://${vcenter_url}"
  export GOVC_URL
}

# ---------------------------------------------------------------------------
# vCenter / govc setup
# ---------------------------------------------------------------------------
setup_govc() {
  log_step "Setting up vCenter connection"

  if [[ -z "${GOVC_URL:-}" ]]; then
    if ! discover_vcenter_url; then
      log_error "GOVC_URL could not be determined. Set GOVC_URL manually and re-run."
      return 1
    fi
  fi

  export GOVC_USERNAME="${VCENTER_USERNAME}"
  export GOVC_PASSWORD="${VCENTER_PASSWORD}"
  export GOVC_INSECURE="${GOVC_INSECURE:-true}"

  log "Testing vCenter connection at ${GOVC_URL}..."
  if ! govc about &> /dev/null; then
    log_error "Failed to connect to vCenter at ${GOVC_URL}. Check credentials and network."
    return 1
  fi
  log "vCenter connection established."
}

# ---------------------------------------------------------------------------
# Power off VMs
# ---------------------------------------------------------------------------
poweroff_vms() {
  log_step "VM Power-Off"

  if [[ "${SKIP_POWEROFF}" == "true" ]]; then
    log "Skipping VM power-off (--skip-poweroff)."
    return 0
  fi

  local nodes
  # get_cluster_nodes logs the node count; failure is non-fatal here —
  # we fall through to the manual-poweroff warning path.
  nodes=$(get_cluster_nodes) || true

  # The API returns vm.moRef (e.g. "vm-1234") for each node.
  # govc accepts MoRef references in the form "VirtualMachine:<moRef>".
  local -a vm_refs=()
  local -a node_names=()

  if [[ -n "${nodes}" ]]; then
    while IFS= read -r encoded; do
      local node node_name vm_moref
      node=$(echo "${encoded}" | base64 -d)
      node_name=$(echo "${node}" | jq -r '.name // empty')
      vm_moref=$(echo "${node}" | jq -r '.vm.moRef // empty')

      if [[ -z "${vm_moref}" ]]; then
        log_warn "  Node '${node_name}' has no VM MoRef — skipping."
        continue
      fi

      local vm_ref="VirtualMachine:${vm_moref}"
      log "  Node '${node_name}' → VM MoRef: ${vm_ref}"
      vm_refs+=("${vm_ref}")
      node_names+=("${node_name}")
    done <<< "${nodes}"
  else
    log_warn "No nodes returned by API. Cannot determine VMs to power off."
  fi

  if ! vcenter_credentials_available; then
    log_warn "VCENTER_USERNAME and VCENTER_PASSWORD are not set — skipping automated VM power-off."
    log_warn "Power off the following VMs manually in vCenter before considering the shutdown complete:"
    if [[ "${#vm_refs[@]}" -eq 0 ]]; then
      log_warn "  (no VM MoRefs could be determined from the API)"
    else
      for i in "${!vm_refs[@]}"; do
        log_warn "  ${node_names[${i}]:-unknown}  →  ${vm_refs[${i}]}"
      done
    fi
    return 0
  fi

  if [[ "${#vm_refs[@]}" -eq 0 ]]; then
    log_warn "No VM MoRefs found. Skipping power-off."
    return 0
  fi

  if ! setup_govc; then
    log_error "vCenter connection could not be established. Power off VMs manually:"
    for i in "${!vm_refs[@]}"; do
      log_error "  ${node_names[${i}]:-unknown}  →  ${vm_refs[${i}]}"
    done
    exit 1
  fi

  for i in "${!vm_refs[@]}"; do
    local vm_ref="${vm_refs[${i}]}"
    local power_state
    power_state=$(govc vm.info -json "${vm_ref}" 2> /dev/null |
      jq -r '.virtualMachines[0].runtime.powerState // "unknown"' || true)

    if [[ "${power_state}" == "poweredOff" ]]; then
      log "  VM '${vm_ref}' is already powered off — skipping."
      continue
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
      log "  [DRY-RUN] Would power off VM: ${vm_ref}"
      continue
    fi

    log "  Powering off VM: ${vm_ref} (${node_names[${i}]:-unknown})"
    if ! govc vm.power -off -force "${vm_ref}" &> /dev/null; then
      log_warn "  Failed to power off VM '${vm_ref}' — it may already be off or inaccessible."
    else
      log "  VM '${vm_ref}' powered off."
    fi

    sleep "${POWEROFF_WAIT}"
  done

  log "VM power-off sequence complete."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  parse_args "${@}"

  log ""
  log "VCF Services Runtime Shutdown Script v${SCRIPT_VERSION}"
  log "Mode: $([ "${DRY_RUN}" == "true" ] && echo "DRY-RUN" || echo "LIVE")"
  log ""

  check_prerequisites
  api_login
  check_no_snapshots
  shutdown_system
  poweroff_vms

  log ""
  log "VCF Services Runtimes shutdown completed successfully."
}

main "${@}"
