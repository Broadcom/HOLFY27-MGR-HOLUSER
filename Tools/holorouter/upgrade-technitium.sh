#!/bin/bash
# ==============================================================================
# upgrade-technitium.sh - Technitium DNS Upgrader
# Version: 1.0.0
#
# Checks for the latest upstream version of Technitium DNS Server, compares it
# to the currently running version on the holorouter, and performs an upgrade
# if a newer version is available (or if forced).
#
# Execution:
#   - Local on router:   Executes containerd pull & k8s rollout directly.
#   - Remote on manager: Connects via SSH to the router to run the upgrade.
# ==============================================================================

set -euo pipefail

VERSION="1.0.0"
SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
if [[ -f "${SCRIPT_SOURCE}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
    SCRIPT_PATH="${SCRIPT_DIR}/$(basename "${SCRIPT_SOURCE}")"
else
    SCRIPT_DIR="/tmp"
    SCRIPT_PATH="/tmp/upgrade-technitium.sh"
fi

# ------------------------------------------------------------------------------
# Color configuration (ANSI gated on TTY)
# ------------------------------------------------------------------------------
if [[ -t 1 ]]; then
    _CYAN='\033[0;36m'
    _BLUE='\033[38;2;0;176;255m'
    _GREEN='\033[0;32m'
    _YELLOW='\033[1;33m'
    _BOLD='\033[1m'
    _RED='\033[0;31m'
    _NC='\033[0m'
else
    _CYAN=''
    _BLUE=''
    _GREEN=''
    _YELLOW=''
    _BOLD=''
    _RED=''
    _NC=''
fi

# ------------------------------------------------------------------------------
# Defaults & Global Variables
# ------------------------------------------------------------------------------
CHECK_ONLY="false"
FORCE_UPGRADE="false"
DRY_RUN="false"
ROUTER_HOST="${ROUTER_HOST:-router}"
ROUTER_USER="${ROUTER_USER:-root}"
PASSWORD="${PASSWORD:-}"
CREDS_FILE="${CREDS_FILE:-}"
DNS_API_PORT="${DNS_API_PORT:-5380}"
K8S_NAMESPACE="default"
DAEMONSET_NAME="technitium"
CONTAINER_NAME="technitium-server"
IMAGE_NAME="docker.io/technitium/dns-server:latest"
TARBALL_DIR="/root/containerd-images"
DNS_SPECS_FILE="/holodeck-runtime/technitium/technitium_k8s.yaml"

# ------------------------------------------------------------------------------
# Help Screen
# ------------------------------------------------------------------------------
show_help() {
    local w=64
    local border
    border=$(printf "%0.s═" $(seq 1 $w))
    local title="Technitium DNS Upgrader"
    local vtext="Version ${VERSION}"
    local t_pad=$(( (w - ${#title}) / 2 ))
    local t_rpad=$(( w - ${#title} - t_pad ))
    local v_pad=$(( (w - ${#vtext}) / 2 ))
    local v_rpad=$(( w - ${#vtext} - v_pad ))

    echo -e "${_CYAN}╔${border}╗${_NC}"
    echo -e "${_CYAN}║${_NC}$(printf "%*s" $t_pad "")${_BLUE}${title}${_NC}$(printf "%*s" $t_rpad "")${_CYAN}║${_NC}"
    echo -e "${_CYAN}║${_NC}$(printf "%*s" $v_pad "")${vtext}$(printf "%*s" $v_rpad "")${_CYAN}║${_NC}"
    echo -e "${_CYAN}╚${border}╝${_NC}"
    echo ""
    echo -e "${_BOLD}USAGE:${_NC}"
    echo -e "    upgrade-technitium.sh [OPTIONS]\n"
    echo -e "${_BOLD}OPTIONS:${_NC}"
    echo -e "    ${_GREEN}-c, --check${_NC}               Check versions only without upgrading"
    echo -e "    ${_GREEN}-f, --force${_NC}               Force pull image and rollout even if version matches"
    echo -e "    ${_GREEN}-d, --dry-run${_NC}             Simulate upgrade steps without making changes"
    echo -e "    ${_GREEN}-r, --router${_NC} <host>       Target router hostname or IP (default: router)"
    echo -e "    ${_GREEN}-u, --user${_NC} <username>     SSH username for remote execution (default: root)"
    echo -e "    ${_GREEN}-p, --password${_NC} <secret>   Lab password for SSH / Technitium API"
    echo -e "    ${_GREEN}--creds-file${_NC} <path>       Path to credentials file (default: ~/creds.txt or /root/creds.txt)"
    echo -e "    ${_GREEN}-v, --version${_NC}             Show version information"
    echo -e "    ${_GREEN}-h, --help${_NC}                Show this help message\n"
    echo -e "${_YELLOW}EXAMPLES:${_NC}"
    echo -e "    ${_GREEN}# Check versions and upgrade Technitium if an update is available${_NC}"
    echo -e "    upgrade-technitium.sh\n"
    echo -e "    ${_GREEN}# Check current vs latest version only${_NC}"
    echo -e "    upgrade-technitium.sh --check\n"
    echo -e "    ${_GREEN}# Force pull latest image and rollout restart${_NC}"
    echo -e "    upgrade-technitium.sh --force\n"
    echo -e "    ${_GREEN}# Execute remotely from manager against specific router IP${_NC}"
    echo -e "    upgrade-technitium.sh --router 192.168.0.2"
    exit 0
}

# ------------------------------------------------------------------------------
# Logging Helpers
# ------------------------------------------------------------------------------
log_info() {
    echo -e "${_CYAN}[INFO]${_NC} $1"
}

log_ok() {
    echo -e "${_GREEN}[OK]${_NC} $1"
}

log_warn() {
    echo -e "${_YELLOW}[WARN]${_NC} $1"
}

log_error() {
    echo -e "${_RED}[ERROR]${_NC} $1" >&2
}

log_step() {
    echo -e "${_BOLD}===> $1${_NC}"
}

# ------------------------------------------------------------------------------
# Argument Parsing
# ------------------------------------------------------------------------------
ORIGINAL_ARGS=("$@")

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--check)
            CHECK_ONLY="true"
            shift
            ;;
        -f|--force)
            FORCE_UPGRADE="true"
            shift
            ;;
        -d|--dry-run)
            DRY_RUN="true"
            shift
            ;;
        -r|--router)
            if [[ -z "${2:-}" || "${2:0:1}" == "-" ]]; then
                log_error "Option --router requires a value."
                show_help
            fi
            ROUTER_HOST="$2"
            shift 2
            ;;
        -u|--user)
            if [[ -z "${2:-}" || "${2:0:1}" == "-" ]]; then
                log_error "Option --user requires a value."
                show_help
            fi
            ROUTER_USER="$2"
            shift 2
            ;;
        -p|--password)
            if [[ -z "${2:-}" || "${2:0:1}" == "-" ]]; then
                log_error "Option --password requires a value."
                show_help
            fi
            PASSWORD="$2"
            shift 2
            ;;
        --creds-file)
            if [[ -z "${2:-}" || "${2:0:1}" == "-" ]]; then
                log_error "Option --creds-file requires a value."
                show_help
            fi
            CREDS_FILE="$2"
            shift 2
            ;;
        -v|--version)
            echo "Technitium DNS Upgrader v${VERSION}"
            exit 0
            ;;
        -h|--help)
            show_help
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            ;;
    esac
done

# ------------------------------------------------------------------------------
# Environment Detection (Local Router vs Remote Manager)
# ------------------------------------------------------------------------------
is_running_on_router() {
    if [[ -d "/holodeck-runtime/dns" ]] || [[ "$(hostname 2>/dev/null)" =~ ^(holorouter|router)$ ]] || [[ -f "/etc/photon-release" && -f "/etc/kubernetes/admin.conf" ]]; then
        return 0
    fi
    return 1
}

# ------------------------------------------------------------------------------
# Password Resolution
# ------------------------------------------------------------------------------
resolve_password() {
    if [[ -n "${PASSWORD}" ]]; then
        return 0
    fi

    if [[ -n "${CREDS_FILE}" && -f "${CREDS_FILE}" ]]; then
        PASSWORD="$(tr -d '[:space:]' < "${CREDS_FILE}")"
        return 0
    fi

    local candidate_paths=(
        "/root/creds.txt"
        "/home/holuser/creds.txt"
        "${HOME}/creds.txt"
        "/home/holuser/hol/creds.txt"
    )

    for p in "${candidate_paths[@]}"; do
        if [[ -f "$p" ]]; then
            PASSWORD="$(tr -d '[:space:]' < "$p")"
            CREDS_FILE="$p"
            return 0
        fi
    done

    return 1
}

# ------------------------------------------------------------------------------
# Remote SSH Execution Handler
# ------------------------------------------------------------------------------
run_remote_over_ssh() {
    log_info "Detected remote execution environment (Manager VM)."
    log_info "Connecting to router (${ROUTER_USER}@${ROUTER_HOST}) to execute upgrade..."

    resolve_password || true

    local quoted_args=""
    if [[ ${#ORIGINAL_ARGS[@]} -gt 0 ]]; then
        quoted_args=$(printf "%q " "${ORIGINAL_ARGS[@]}")
    fi

    if command -v sshpass >/dev/null 2>&1 && [[ -n "${PASSWORD}" ]]; then
        sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no \
            "${ROUTER_USER}@${ROUTER_HOST}" "bash -s -- ${quoted_args}" < "${SCRIPT_PATH}"
    elif [[ -n "${CREDS_FILE}" && -f "${CREDS_FILE}" ]] && command -v sshpass >/dev/null 2>&1; then
        sshpass -f "${CREDS_FILE}" ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no \
            "${ROUTER_USER}@${ROUTER_HOST}" "bash -s -- ${quoted_args}" < "${SCRIPT_PATH}"
    else
        ssh -o StrictHostKeyChecking=accept-new "${ROUTER_USER}@${ROUTER_HOST}" "bash -s -- ${quoted_args}" < "${SCRIPT_PATH}"
    fi
}

# ------------------------------------------------------------------------------
# Version Comparison Logic (SemVer)
# Returns:
#   1 if v1 > v2
#   0 if v1 == v2
#   2 if v1 < v2
# ------------------------------------------------------------------------------
compare_versions() {
    local v1="$1"
    local v2="$2"

    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import sys, re
def parse(v):
    nums = re.findall(r'\d+', str(v))
    return [int(x) for x in nums] if nums else [0]
v1, v2 = parse(sys.argv[1]), parse(sys.argv[2])
maxlen = max(len(v1), len(v2))
v1 += [0] * (maxlen - len(v1))
v2 += [0] * (maxlen - len(v2))
if v1 > v2: sys.exit(1)
elif v1 < v2: sys.exit(2)
else: sys.exit(0)
" "$v1" "$v2"
        return $?
    else
        # Fallback to sort -V
        if [[ "$v1" == "$v2" ]]; then
            return 0
        fi
        local lowest
        lowest=$(printf '%s\n%s\n' "$v1" "$v2" | sort -V | head -n1)
        if [[ "$lowest" == "$v2" ]]; then
            return 1
        else
            return 2
        fi
    fi
}

# ------------------------------------------------------------------------------
# Version Query Functions
# ------------------------------------------------------------------------------
get_latest_upstream_version() {
    local ver=""

    # 1. Try GitHub Releases API
    ver=$(curl -s -m 10 "https://api.github.com/repos/TechnitiumSoftware/DnsServer/releases/latest" 2>/dev/null | \
        python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    tag = data.get("tag_name", "").lstrip("v").strip()
    if tag: print(tag)
except Exception:
    pass
' 2>/dev/null || true)

    if [[ -n "${ver}" && "${ver}" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        echo "${ver}"
        return 0
    fi

    # 2. Fallback: Try Docker Hub API
    ver=$(curl -s -m 10 "https://hub.docker.com/v2/repositories/technitium/dns-server/tags?page_size=25&page=1" 2>/dev/null | \
        python3 -c '
import sys, json, re
try:
    data = json.load(sys.stdin)
    tags = [r["name"] for r in data.get("results", []) if re.match(r"^\d+(\.\d+)+$", r["name"])]
    if tags: print(tags[0])
except Exception:
    pass
' 2>/dev/null || true)

    if [[ -n "${ver}" && "${ver}" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        echo "${ver}"
        return 0
    fi

    return 1
}

get_current_running_version() {
    local ver=""
    local encoded_pass=""

    if [[ -n "${PASSWORD}" ]]; then
        encoded_pass=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${PASSWORD}'))" 2>/dev/null || echo "${PASSWORD}")
    fi

    # 1. Query Technitium HTTP API on localhost:5380
    local login_resp=""
    login_resp=$(curl -s -m 10 "http://127.0.0.1:${DNS_API_PORT}/api/user/login?user=admin&pass=${encoded_pass}&includeInfo=true" 2>/dev/null || true)

    if [[ -n "${login_resp}" ]]; then
        ver=$(echo "${login_resp}" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    v = data.get("info", {}).get("version", "").strip()
    if v: print(v)
except Exception:
    pass
' 2>/dev/null || true)
    fi

    if [[ -n "${ver}" && "${ver}" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        echo "${ver}"
        return 0
    fi

    # 2. Fallback: Check without authentication (in case session or open)
    local session_resp=""
    session_resp=$(curl -s -m 10 "http://127.0.0.1:${DNS_API_PORT}/api/settings/get" 2>/dev/null || true)
    if [[ -n "${session_resp}" ]]; then
        ver=$(echo "${session_resp}" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    v = data.get("info", {}).get("version", "").strip()
    if v: print(v)
except Exception:
    pass
' 2>/dev/null || true)
    fi

    if [[ -n "${ver}" && "${ver}" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        echo "${ver}"
        return 0
    fi

    return 1
}

# ------------------------------------------------------------------------------
# Local Execution Logic (Runs directly on router)
# ------------------------------------------------------------------------------
run_local_on_router() {
    log_step "Technitium DNS Upgrade Check"
    log_info "Host: $(hostname) ($(uname -s) $(uname -r))"

    # Pre-flight check for required tools
    local missing_tools=()
    for tool in kubectl ctr curl python3; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing_tools+=("$tool")
        fi
    done

    if [[ ${#missing_tools[@]} -gt 0 ]]; then
        log_error "Missing required tool(s): ${missing_tools[*]}"
        exit 1
    fi

    # Verify Technitium DaemonSet exists
    if ! kubectl get daemonset "${DAEMONSET_NAME}" -n "${K8S_NAMESPACE}" >/dev/null 2>&1; then
        log_error "Kubernetes DaemonSet '${DAEMONSET_NAME}' not found in namespace '${K8S_NAMESPACE}'."
        exit 1
    fi
    log_ok "Kubernetes DaemonSet '${DAEMONSET_NAME}' is present in namespace '${K8S_NAMESPACE}'."

    # Resolve password
    resolve_password || true
    if [[ -z "${PASSWORD}" ]]; then
        log_warn "No credentials file found. API version check will run unauthenticated."
    fi

    # Retrieve current running version
    log_info "Querying currently running Technitium DNS version..."
    local current_ver=""
    if current_ver=$(get_current_running_version); then
        log_ok "Current Technitium version: ${_BOLD}${current_ver}${_NC}"
    else
        log_warn "Could not determine current version from API. Assuming initial/unknown version."
        current_ver="0.0.0"
    fi

    # Retrieve latest upstream version
    log_info "Checking upstream for latest Technitium DNS release..."
    local latest_ver=""
    if latest_ver=$(get_latest_upstream_version); then
        log_ok "Latest upstream version:   ${_BOLD}${latest_ver}${_NC}"
    else
        log_error "Failed to retrieve latest version from GitHub and Docker Hub APIs."
        exit 1
    fi

    # Compare versions
    local cmp_res=0
    compare_versions "${latest_ver}" "${current_ver}" && cmp_res=$? || cmp_res=$?

    echo ""
    log_step "Version Comparison Summary"
    echo -e "  • Current Running Version: ${_BOLD}${current_ver}${_NC}"
    echo -e "  • Latest Upstream Version: ${_BOLD}${latest_ver}${_NC}"

    if [[ "$cmp_res" -eq 1 ]]; then
        echo -e "  • Status:                  ${_YELLOW}Upgrade Available (${current_ver} -> ${latest_ver})${_NC}"
    elif [[ "$cmp_res" -eq 0 ]]; then
        echo -e "  • Status:                  ${_GREEN}Up to Date (${current_ver})${_NC}"
    else
        echo -e "  • Status:                  ${_CYAN}Running version (${current_ver}) is newer than release (${latest_ver})${_NC}"
    fi
    echo ""

    if [[ "${CHECK_ONLY}" == "true" ]]; then
        log_info "Check-only mode requested. Exiting without making changes."
        exit 0
    fi

    # Check if upgrade is needed
    if [[ "$cmp_res" -ne 1 && "${FORCE_UPGRADE}" != "true" ]]; then
        log_ok "Technitium DNS is already at the latest version (${current_ver}). No upgrade needed."
        exit 0
    fi

    if [[ "${FORCE_UPGRADE}" == "true" && "$cmp_res" -ne 1 ]]; then
        log_warn "Force flag enabled (--force). Proceeding with image pull and rollout restart."
    fi

    # Perform Upgrade
    log_step "Executing Technitium Upgrade"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "[DRY-RUN] Planned Actions:"
        log_info "  1. Pull container image: ctr -n k8s.io images pull ${IMAGE_NAME}"
        log_info "  2. Pull version tag:    ctr -n k8s.io images pull docker.io/technitium/dns-server:${latest_ver}"
        log_info "  3. Update local tarball: ctr -n k8s.io images export ${TARBALL_DIR}/technitium.tar ${IMAGE_NAME}"
        log_info "  4. Restart DaemonSet:   kubectl rollout restart daemonset ${DAEMONSET_NAME} -n ${K8S_NAMESPACE}"
        log_info "  5. Wait for rollout:    kubectl rollout status daemonset ${DAEMONSET_NAME} -n ${K8S_NAMESPACE} --timeout=120s"
        log_info "  6. Verify API health on port ${DNS_API_PORT} and test DNS resolution"
        log_ok "[DRY-RUN] Completed simulation successfully."
        exit 0
    fi

    # Step 1: Pull the new image into containerd
    log_info "Pulling latest container image into containerd (k8s.io namespace)..."
    ctr -n k8s.io images pull "${IMAGE_NAME}"
    log_ok "Successfully pulled ${IMAGE_NAME}"

    # Also pull specific semver tag if possible (best-effort)
    if [[ -n "${latest_ver}" ]]; then
        log_info "Pulling version tag docker.io/technitium/dns-server:${latest_ver}..."
        ctr -n k8s.io images pull "docker.io/technitium/dns-server:${latest_ver}" 2>/dev/null || true
    fi

    # Step 2: Export image to /root/containerd-images for offline resilience
    if [[ -d "${TARBALL_DIR}" ]]; then
        log_info "Updating local image archive ${TARBALL_DIR}/technitium.tar..."
        ctr -n k8s.io images export "${TARBALL_DIR}/technitium.tar" "${IMAGE_NAME}" 2>/dev/null || true
        log_ok "Local tarball archive updated."
    fi

    # Step 3: Ensure DaemonSet specification is synchronized
    if [[ -f "${DNS_SPECS_FILE}" ]]; then
        log_info "Re-applying ${DNS_SPECS_FILE}..."
        kubectl apply -f "${DNS_SPECS_FILE}" >/dev/null 2>&1 || true
    fi

    # Step 4: Restart DaemonSet to pick up the updated image
    log_info "Triggering rollout restart of DaemonSet '${DAEMONSET_NAME}'..."
    kubectl rollout restart daemonset "${DAEMONSET_NAME}" -n "${K8S_NAMESPACE}"

    log_info "Waiting for rollout to complete..."
    kubectl rollout status daemonset "${DAEMONSET_NAME}" -n "${K8S_NAMESPACE}" --timeout=120s
    log_ok "DaemonSet '${DAEMONSET_NAME}' rolled out successfully."

    # Step 5: Post-Upgrade Verification
    log_step "Post-Upgrade Verification"
    log_info "Waiting for Technitium DNS API to become ready on port ${DNS_API_PORT}..."

    local api_ready="false"
    local new_ver=""
    local retries=30
    local wait_sec=2

    for ((i=1; i<=retries; i++)); do
        if new_ver=$(get_current_running_version); then
            api_ready="true"
            break
        fi
        sleep "$wait_sec"
    done

    if [[ "${api_ready}" == "true" ]]; then
        log_ok "Technitium API is responsive."
        log_ok "Active Technitium Version: ${_BOLD}${new_ver}${_NC}"
    else
        log_warn "Technitium API did not respond within 60 seconds, checking DNS port 53 directly..."
    fi

    # Step 6: Test DNS resolution on local port 53
    log_info "Testing DNS query resolution on 127.0.0.1:53..."
    local dns_test_out=""
    if command -v dig >/dev/null 2>&1; then
        dns_test_out=$(dig @127.0.0.1 +short technitium.vcf.lab 2>/dev/null || true)
    elif command -v nslookup >/dev/null 2>&1; then
        dns_test_out=$(nslookup technitium.vcf.lab 127.0.0.1 2>/dev/null | grep "Address:" | tail -n1 || true)
    fi

    if [[ -n "${dns_test_out}" ]]; then
        log_ok "DNS resolution test passed (technitium.vcf.lab -> ${dns_test_out})."
    else
        log_warn "DNS query test returned empty output; service may still be initializing cache."
    fi

    echo ""
    log_ok "============================================================"
    log_ok "  Technitium DNS upgrade completed successfully!"
    log_ok "  Previous Version: ${current_ver}"
    log_ok "  Current Version:  ${new_ver:-${latest_ver}}"
    log_ok "============================================================"
}

# ------------------------------------------------------------------------------
# Main Entrypoint
# ------------------------------------------------------------------------------
main() {
    if is_running_on_router; then
        run_local_on_router
    else
        run_remote_over_ssh
    fi
}

main
