#!/usr/bin/env bash
# import-firefox-bookmarks.sh — Install a bookmarks JSON file into the Firefox profile
#
# Copies a bookmarks*.json file (sourced from the vpodrepo) into the Firefox
# profile's bookmarkbackups/ directory, removes places.sqlite so Firefox rebuilds
# its database from the backup on the next launch, and sets the required user.js
# preference to suppress Firefox's built-in default bookmarks.
#
# IMPORTANT: Removing places.sqlite also removes browsing history (not just
# bookmarks). This is intentional for a lab environment — the goal is a clean,
# known bookmark state on each boot.
#
# If no --bookmark-file is provided or the file does not exist, the script exits
# cleanly without touching the profile (existing bookmarks are preserved).
#
# If Firefox is currently running (.parentlock detected), the script skips the
# import and logs a warning rather than corrupting the live database.
#
# Version 1.0 - 2026-06-30
# Author - Burke Azbill and HOL Core Team
#
# USAGE:
#   import-firefox-bookmarks.sh --bookmark-file FILE [options]
#
# OPTIONS:
#   --bookmark-file FILE   Path to the bookmarks JSON file to import
#   --mc-base PATH         NFS mount prefix for accessing the console from the
#                          manager VM (default: /, i.e. running locally)
#   --dry-run              Print what would happen; make no changes
#   -h, --help             Show this help

set -euo pipefail

TOOL_NAME="import-firefox-bookmarks.sh"
VERSION="1.0"
VERSION_STR="Version ${VERSION}"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[38;2;0;176;255m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' NC=''
fi

print_header() {
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}${BLUE}$(printf '%*s' $(( (64 + ${#TOOL_NAME}) / 2 )) "$TOOL_NAME")$(printf '%*s' $(( (64 - ${#TOOL_NAME}) / 2 )) '')${NC}${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}$(printf '%*s' $(( (64 + ${#VERSION_STR}) / 2 )) "$VERSION_STR")$(printf '%*s' $(( (64 - ${#VERSION_STR}) / 2 )) '')${NC}${CYAN}║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
}

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

show_help() {
    print_header
    echo ""
    echo -e "${BOLD}USAGE:${NC}"
    echo -e "    ${TOOL_NAME} --bookmark-file FILE [options]"
    echo ""
    echo -e "${BOLD}DESCRIPTION:${NC}"
    echo -e "    Installs a bookmarks JSON backup into the Firefox profile so that"
    echo -e "    Firefox rebuilds its bookmark database from it on the next launch."
    echo -e "    Existing bookmarks and history are replaced by the JSON content."
    echo -e "    If the file is not found, exits cleanly — no profile changes are made."
    echo ""
    echo -e "${BOLD}OPTIONS:${NC}"
    echo -e "    ${GREEN}--bookmark-file FILE${NC}   ${BOLD}(required)${NC} Path to the bookmarks JSON to import"
    echo -e "    ${GREEN}--mc-base PATH${NC}          NFS mount prefix for console home dir"
    echo -e "                           (default: /, i.e. running locally on console)"
    echo -e "    ${GREEN}--dry-run${NC}               Print actions only; make no changes"
    echo -e "    ${GREEN}-h, --help${NC}              Show this help"
    echo ""
    echo -e "${YELLOW}EXAMPLES:${NC}"
    echo -e "    ${GREEN}# Run from the manager VM (via NFS, called by labstartup.sh)${NC}"
    echo -e "    ${TOOL_NAME} --bookmark-file /lmchol/home/holuser/bookmarks-lab.json --mc-base /lmchol"
    echo ""
    echo -e "    ${GREEN}# Run locally on the console VM${NC}"
    echo -e "    ${TOOL_NAME} --bookmark-file ~/bookmarks-lab.json"
    echo ""
    echo -e "    ${GREEN}# Preview without making changes${NC}"
    echo -e "    ${TOOL_NAME} --bookmark-file ~/bookmarks-lab.json --dry-run"
    echo ""
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BOOKMARK_FILE=""
MC_BASE="/"
DRY_RUN=0

if [[ $# -eq 0 ]]; then
    show_help
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)        show_help ;;
        --bookmark-file)
            BOOKMARK_FILE="${2:-}"
            shift 2 || die "--bookmark-file requires a value"
            ;;
        --mc-base)
            MC_BASE="${2:-}"
            shift 2 || die "--mc-base requires a value"
            ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) die "Unknown option: $1 (use --help)" ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------
if [[ -z "$BOOKMARK_FILE" ]]; then
    die "--bookmark-file is required (use --help for usage)"
fi

# Strip trailing slash from mc_base for consistent path construction
MC_BASE="${MC_BASE%/}"

# ---------------------------------------------------------------------------
# No-op exit: bookmark file not present — existing profile is untouched
# ---------------------------------------------------------------------------
if [[ ! -f "$BOOKMARK_FILE" ]]; then
    info "Bookmark file not found: ${BOOKMARK_FILE}"
    info "No changes made — existing Firefox bookmarks are preserved."
    exit 0
fi

# ---------------------------------------------------------------------------
# Resolve Firefox profile directory
# Mirrors the logic in firefox_lmchol_tuning.py _resolve_ff_base():
#   apt Firefox (deb): ${MC_BASE}/home/holuser/.mozilla/firefox
#   snap Firefox:      ${MC_BASE}/home/holuser/snap/firefox/common/.mozilla/firefox
# Apt path is preferred; snap is the fallback for pre-migration environments.
# ---------------------------------------------------------------------------
_FF_APT_BASE="${MC_BASE}/home/holuser/.mozilla/firefox"
_FF_SNAP_BASE="${MC_BASE}/home/holuser/snap/firefox/common/.mozilla/firefox"

if [[ -d "$_FF_APT_BASE" ]]; then
    FF_BASE="$_FF_APT_BASE"
elif [[ -d "$_FF_SNAP_BASE" ]]; then
    FF_BASE="$_FF_SNAP_BASE"
else
    info "No Firefox profile directory found (checked apt and snap paths)."
    info "No changes made — Firefox has not been set up yet on this console."
    exit 0
fi

# Find the active profile: the subdirectory containing cert9.db (or places.sqlite).
# Uses the same heuristic as migrate-firefox-snap-to-apt.sh.
PROFILE_DIR=""
for _d in "${FF_BASE}"/*/; do
    if [[ -f "${_d}cert9.db" ]] || [[ -f "${_d}places.sqlite" ]]; then
        PROFILE_DIR="${_d%/}"
        break
    fi
done

if [[ -z "$PROFILE_DIR" ]]; then
    info "No active Firefox profile found under ${FF_BASE}."
    info "No changes made."
    exit 0
fi

info "Firefox profile: ${PROFILE_DIR}"

# ---------------------------------------------------------------------------
# Guard: do not modify the profile while Firefox is running.
# Firefox holds .parentlock for the duration of its session.
# ---------------------------------------------------------------------------
if [[ -f "${PROFILE_DIR}/.parentlock" ]]; then
    warn "Firefox appears to be running (.parentlock found in profile)."
    warn "Skipping bookmark import to avoid corrupting the live database."
    warn "Re-run after Firefox has been closed, or on the next lab boot."
    exit 0
fi

# ---------------------------------------------------------------------------
# Dry-run summary
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
    print_header
    echo ""
    info "DRY-RUN mode — no changes will be made."
    info "Bookmark file:   ${BOOKMARK_FILE}"
    info "Profile dir:     ${PROFILE_DIR}"
    _ts_preview=$(date +%Y-%m-%dT%H%M%S)
    info "Would copy to:   ${PROFILE_DIR}/bookmarkbackups/bookmarks-${_ts_preview}.json"
    info "Would remove:    ${PROFILE_DIR}/places.sqlite (and -shm, -wal if present)"
    info "Would update:    ${PROFILE_DIR}/user.js (browser.bookmarks.restore_default_bookmark_count = 0)"
    exit 0
fi

print_header
echo ""

# ---------------------------------------------------------------------------
# Step 1: Copy the JSON into bookmarkbackups/ with a timestamped filename.
# Firefox picks up the most recently modified bookmarks-*.json file from this
# directory when rebuilding a missing places.sqlite.
# ---------------------------------------------------------------------------
BACKUPS_DIR="${PROFILE_DIR}/bookmarkbackups"
mkdir -p "$BACKUPS_DIR"

TS=$(date +%Y-%m-%dT%H%M%S)
DEST_JSON="${BACKUPS_DIR}/bookmarks-${TS}.json"

cp "$BOOKMARK_FILE" "$DEST_JSON"
info "Installed: $(basename "$BOOKMARK_FILE") → bookmarkbackups/bookmarks-${TS}.json"

# ---------------------------------------------------------------------------
# Step 2: Remove places.sqlite (and WAL/SHM files if present).
# On the next Firefox launch, the absence of places.sqlite causes Firefox to
# create a fresh database and immediately restore from the newest backup.
# NOTE: This also clears browsing history — intentional for lab environments.
# ---------------------------------------------------------------------------
rm -f "${PROFILE_DIR}/places.sqlite" \
      "${PROFILE_DIR}/places.sqlite-shm" \
      "${PROFILE_DIR}/places.sqlite-wal"
info "Removed places.sqlite (Firefox will rebuild from bookmark backup on next launch)."

# ---------------------------------------------------------------------------
# Step 3: Write the user.js preference that suppresses Firefox's built-in
# default bookmarks, ensuring the restore uses our JSON rather than inserting
# Mozilla's defaults alongside it.
# ---------------------------------------------------------------------------
USER_JS="${PROFILE_DIR}/user.js"
touch "$USER_JS"

# Remove any existing instance of this pref (idempotent)
# shellcheck disable=SC2016
sed -i '/browser\.bookmarks\.restore_default_bookmark_count/d' "$USER_JS"
echo 'user_pref("browser.bookmarks.restore_default_bookmark_count", 0);' >> "$USER_JS"
info "Updated user.js: browser.bookmarks.restore_default_bookmark_count = 0"

echo ""
echo -e "${GREEN}Bookmark import queued.${NC} Firefox will restore bookmarks on next launch."
echo -e "  Source: ${BOOKMARK_FILE}"
echo -e "  Backup: ${DEST_JSON}"
