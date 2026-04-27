#!/usr/bin/env bash
# rebuild-firefox-profile.sh — In-place Firefox profile rebuild (same directory path)
#
# Backs up the entire profile, removes regenerable/corruption-prone data, then restores:
#   logins (key4.db + logins.json), bookmarks (places/favicons if integrity OK), prefs.js,
#   user.js, bookmarkbackups/, handlers, permissions, form history, containers, NSS cert DB.
#
# By default does NOT restore: extensions/, storage/ (IndexedDB), caches, sessionstore.
#   Use --keep-extensions / --keep-storage to include them (higher risk of re-importing corruption).
#
# Version 1.1 - 2026-04-27: --rebuild-count N writes ~/.local/state/firefox_profile_rebuild.count (labstartup gate).
set -euo pipefail

VERSION="1.1"
TOOL_NAME="rebuild-firefox-profile.sh"
VERSION_STR="Version ${VERSION}"

if [[ -t 1 ]]; then
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  BLUE='\033[38;2;0;176;255m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  RED='\033[0;31m'
  NC='\033[0m'
else
  RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' NC=''
fi

PROFILE_DIR=""
FIREFOX_MOZILLA_DIR=""
DRY_RUN=0
YES=0
KEEP_EXTENSIONS=0
KEEP_STORAGE=0
NO_CERT_DB=0
ALLOW_EXOTIC=0
STAGE=""
SKIP_PLACES=0
SKIP_FAVICONS=0
# Optional: after successful rebuild, write this integer to ~/.local/state/firefox_profile_rebuild.count
REBUILD_COUNT=""
REBUILD_COUNT_FLAG_DIR="${HOME}/.local/state"
REBUILD_COUNT_FLAG_FILE="${REBUILD_COUNT_FLAG_DIR}/firefox_profile_rebuild.count"

cleanup() {
  if [[ -n "${STAGE:-}" && -d "${STAGE:-}" ]]; then
    rm -rf "$STAGE"
  fi
}
trap cleanup EXIT

die() {
  echo -e "${RED}ERROR:${NC} $*" >&2
  exit 1
}

print_header() {
  echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${CYAN}║${NC}${BLUE}$(printf '%*s' $(((64 + ${#TOOL_NAME}) / 2)) "$TOOL_NAME")$(printf '%*s' $(((64 - ${#TOOL_NAME}) / 2)) '')${NC}${CYAN}║${NC}"
  echo -e "${CYAN}║${NC}$(printf '%*s' $(((64 + ${#VERSION_STR}) / 2)) "$VERSION_STR")$(printf '%*s' $(((64 - ${#VERSION_STR}) / 2)) '')${NC}${CYAN} ║${NC}"
  echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
}

show_help() {
  print_header
  echo ""
  echo -e "${BOLD}USAGE:${NC}"
  echo -e "    ${TOOL_NAME} ${GREEN}--profile-dir PATH${NC} [options]"
  echo -e "    ${TOOL_NAME} ${GREEN}--default-profile${NC} [options]"
  echo ""
  echo -e "${BOLD}DESCRIPTION:${NC}"
  echo -e "    Full rsync backup of the profile, then wipe profile contents and restore only"
  echo -e "    bookmarks, logins, prefs, and related settings files (see script header)."
  echo -e "    ${BOLD}Firefox must not be running.${NC}"
  echo ""
  echo -e "${BOLD}OPTIONS:${NC}"
  echo -e "    ${GREEN}--profile-dir PATH${NC}     ${BOLD}(one of)${NC} Absolute profile directory (…/firefox/xxxxx.default)"
  echo -e "    ${GREEN}--default-profile${NC}       Use Default=1 profile from snap firefox profiles.ini"
  echo -e "    ${GREEN}--firefox-dir PATH${NC}      With --default-profile: parent of profiles.ini (default: \$HOME/snap/firefox/common/.mozilla/firefox)"
  echo -e "    ${GREEN}--dry-run${NC}                 Print actions only; no backup/wipe/restore"
  echo -e "    ${GREEN}--yes${NC}                     Skip interactive confirmation (required if not a TTY)"
  echo -e "    ${GREEN}--rebuild-count N${NC}         On success, write N to ${REBUILD_COUNT_FLAG_FILE} (labstartup gate)"
  echo -e "    ${GREEN}--keep-extensions${NC}         Also restore extensions/ + extensions.json (+ addonStartup.json.lz4 if present)"
  echo -e "    ${GREEN}--keep-storage${NC}            Also restore storage/ (IndexedDB; may re-import corruption)"
  echo -e "    ${GREEN}--no-cert-db${NC}              Do not restore cert9.db / pkcs11.txt"
  echo -e "    ${GREEN}--i-know-the-risk${NC}         Allow PROFILE_DIR not containing .mozilla/firefox"
  echo -e "    ${GREEN}-h, --help${NC}                Show this help"
  echo ""
  echo -e "${YELLOW}EXAMPLES:${NC}"
  echo -e "    ${GREEN}# Preview${NC}"
  echo -e "    ${TOOL_NAME} --default-profile --dry-run"
  echo ""
  echo -e "    ${GREEN}# Rebuild snap default profile (destructive)${NC}"
  echo -e "    ${TOOL_NAME} --default-profile --yes"
  echo ""
  echo -e "    ${GREEN}# Explicit path${NC}"
  echo -e "    ${TOOL_NAME} --profile-dir \"\\\$HOME/snap/firefox/common/.mozilla/firefox/xxxxx.default\" --yes"
  echo ""
  echo -e "    ${GREEN}# Labstartup / automation (record generation after success)${NC}"
  echo -e "    ${TOOL_NAME} --default-profile --yes --rebuild-count 1"
  echo ""
  exit 0
}

firefox_running() {
  pgrep -f '/snap/firefox/.*/usr/lib/firefox/firefox' &>/dev/null && return 0
  pgrep -x firefox &>/dev/null && return 0
  return 1
}

resolve_default_profile_dir() {
  local base="${1:-$HOME/snap/firefox/common/.mozilla/firefox}"
  local ini="${base}/profiles.ini"
  [[ -f "$ini" ]] || die "profiles.ini not found: $ini (set --firefox-dir)"
  python3 - "$ini" "$base" <<'PY' || die "Could not parse profiles.ini"
import configparser
import os
import sys

ini, base = sys.argv[1], sys.argv[2]
cfg = configparser.ConfigParser()
cfg.read(ini)
for sec in cfg.sections():
    if not sec.startswith("Profile"):
        continue
    if cfg.get(sec, "Default", fallback="0").strip() != "1":
        continue
    path = cfg.get(sec, "Path", fallback="").strip()
    if not path:
        continue
    rel = cfg.get(sec, "IsRelative", fallback="1").strip()
    if rel == "1":
        print(os.path.join(base, path))
    else:
        print(path)
    sys.exit(0)
print("", end="")
sys.exit(2)
PY
}

sqlite_ok() {
  local db="$1"
  [[ -f "$db" ]] || return 1
  command -v sqlite3 &>/dev/null || return 0
  sqlite3 "$db" 'PRAGMA integrity_check;' 2>/dev/null | grep -qx 'ok'
}

stage_copy_file() {
  local src_root="$1"
  local rel="$2"
  local dst_stage="$3"
  [[ -f "${src_root}/${rel}" ]] || return 0
  cp -p "${src_root}/${rel}" "${dst_stage}/${rel}"
  echo "  staged file: ${rel}"
}

stage_copy_optional() {
  local src_root="$1"
  local rel="$2"
  local dst_stage="$3"
  if [[ -e "${src_root}/${rel}" ]]; then
    cp -a "${src_root}/${rel}" "${dst_stage}/"
    echo "  staged: ${rel}"
  fi
}

parse_args() {
  FIREFOX_MOZILLA_DIR="${HOME}/snap/firefox/common/.mozilla/firefox"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h | --help) show_help ;;
      --profile-dir)
        PROFILE_DIR="${2:-}"
        shift 2 || die "--profile-dir requires a value"
        ;;
      --default-profile)
        PROFILE_DIR="__DEFAULT__"
        shift
        ;;
      --firefox-dir)
        FIREFOX_MOZILLA_DIR="${2:-}"
        shift 2 || die "--firefox-dir requires a value"
        ;;
      --dry-run) DRY_RUN=1; shift ;;
      --yes) YES=1; shift ;;
      --keep-extensions) KEEP_EXTENSIONS=1; shift ;;
      --keep-storage) KEEP_STORAGE=1; shift ;;
      --no-cert-db) NO_CERT_DB=1; shift ;;
      --i-know-the-risk) ALLOW_EXOTIC=1; shift ;;
      --rebuild-count)
        REBUILD_COUNT="${2:-}"
        shift 2 || die "--rebuild-count requires a value"
        [[ "$REBUILD_COUNT" =~ ^[1-9][0-9]*$ ]] || die "--rebuild-count must be a positive integer"
        ;;
      *) die "Unknown option: $1 (use --help)" ;;
    esac
  done

  if [[ -z "$PROFILE_DIR" ]]; then
    die "Specify --profile-dir PATH or --default-profile"
  fi
  if [[ "$PROFILE_DIR" == "__DEFAULT__" ]]; then
    PROFILE_DIR="$(resolve_default_profile_dir "$FIREFOX_MOZILLA_DIR")"
    [[ -n "$PROFILE_DIR" && -d "$PROFILE_DIR" ]] || die "Could not resolve default profile under $FIREFOX_MOZILLA_DIR"
  fi
  PROFILE_DIR="$(readlink -f "$PROFILE_DIR")"
}

validate_profile_dir() {
  [[ -n "$PROFILE_DIR" ]] || die "PROFILE_DIR empty"
  [[ -d "$PROFILE_DIR" ]] || die "Not a directory: $PROFILE_DIR"
  [[ "$PROFILE_DIR" != "/" ]] || die "Refusing root"
  local home_real
  home_real="$(readlink -f "$HOME")"
  [[ "$PROFILE_DIR" != "$home_real" ]] || die "Refusing HOME directory"
  if [[ "$ALLOW_EXOTIC" -eq 0 ]]; then
    if [[ "$PROFILE_DIR" != *".mozilla/firefox"* ]]; then
      die "Path must contain '.mozilla/firefox' (or pass --i-know-the-risk)"
    fi
  fi
}

confirm_destructive() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  if [[ "$YES" -eq 1 ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "Not a TTY: pass --yes or set REBUILD_FIREFOX_PROFILE_CONFIRM=1 after reviewing --dry-run"
  fi
  echo -e "${YELLOW}This will DELETE all files in:${NC}"
  echo "  $PROFILE_DIR"
  echo -e "${YELLOW}after a full backup. Type ${BOLD}REBUILD${NC}${YELLOW} to continue:${NC}"
  read -r line
  [[ "$line" == "REBUILD" ]] || die "Aborted (you did not type REBUILD)"
}

main() {
  parse_args "$@"
  validate_profile_dir

  if firefox_running; then
    die "Firefox is running. Quit Firefox completely, then retry."
  fi

  print_header
  echo ""
  echo -e "${BOLD}Profile:${NC} $PROFILE_DIR"
  echo -e "${BOLD}Dry-run:${NC} $DRY_RUN  ${BOLD}Keep extensions:${NC} $KEEP_EXTENSIONS  ${BOLD}Keep storage:${NC} $KEEP_STORAGE"
  echo ""

  local ts backup_dir
  ts="$(date +%Y%m%d%H%M%S)"
  backup_dir="$(dirname "$PROFILE_DIR")/$(basename "$PROFILE_DIR").rebuild-backup-${ts}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo -e "${CYAN}[dry-run]${NC} Would rsync backup to: $backup_dir"
    echo -e "${CYAN}[dry-run]${NC} Would stage preserved files, wipe profile, restore staged files."
    confirm_destructive
    exit 0
  fi

  confirm_destructive

  echo -e "${BOLD}Backup${NC} (full tree) → $backup_dir"
  mkdir -p "$backup_dir"
  rsync -a "${PROFILE_DIR}/" "${backup_dir}/"

  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ff-profile-rebuild.XXXXXX")"
  echo -e "${BOLD}Stage${NC} → $STAGE"

  SKIP_PLACES=0
  SKIP_FAVICONS=0
  if [[ -f "${PROFILE_DIR}/places.sqlite" ]]; then
    if sqlite_ok "${PROFILE_DIR}/places.sqlite"; then
      cp -p "${PROFILE_DIR}/places.sqlite" "${STAGE}/places.sqlite"
      echo "  staged: places.sqlite"
    else
      SKIP_PLACES=1
      echo -e "  ${YELLOW}SKIP${NC} places.sqlite (integrity check failed). Use bookmarkbackups/ in Firefox: Library → Restore."
    fi
  fi
  if [[ -f "${PROFILE_DIR}/favicons.sqlite" ]]; then
    if sqlite_ok "${PROFILE_DIR}/favicons.sqlite"; then
      cp -p "${PROFILE_DIR}/favicons.sqlite" "${STAGE}/favicons.sqlite"
      echo "  staged: favicons.sqlite"
    else
      SKIP_FAVICONS=1
      echo -e "  ${YELLOW}SKIP${NC} favicons.sqlite (integrity check failed)"
    fi
  fi

  stage_copy_file "$PROFILE_DIR" "logins.json" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "key4.db" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "prefs.js" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "user.js" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "handlers.json" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "permissions.sqlite" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "content-prefs.sqlite" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "formhistory.sqlite" "$STAGE"
  stage_copy_file "$PROFILE_DIR" "containers.json" "$STAGE"

  if [[ -d "${PROFILE_DIR}/bookmarkbackups" ]]; then
    cp -a "${PROFILE_DIR}/bookmarkbackups" "${STAGE}/"
    echo "  staged: bookmarkbackups/"
  fi

  if [[ "$NO_CERT_DB" -eq 0 ]]; then
    stage_copy_file "$PROFILE_DIR" "cert9.db" "$STAGE"
    stage_copy_file "$PROFILE_DIR" "pkcs11.txt" "$STAGE"
  fi

  if [[ "$KEEP_EXTENSIONS" -eq 1 ]]; then
    stage_copy_optional "$PROFILE_DIR" "extensions.json" "$STAGE"
    stage_copy_optional "$PROFILE_DIR" "addonStartup.json.lz4" "$STAGE"
    if [[ -d "${PROFILE_DIR}/extensions" ]]; then
      cp -a "${PROFILE_DIR}/extensions" "${STAGE}/"
      echo "  staged: extensions/"
    fi
  fi

  if [[ "$KEEP_STORAGE" -eq 1 && -d "${PROFILE_DIR}/storage" ]]; then
    cp -a "${PROFILE_DIR}/storage" "${STAGE}/"
    echo "  staged: storage/"
  fi

  echo -e "${BOLD}Wipe${NC} profile directory (contents only)"
  shopt -s dotglob nullglob
  local item
  for item in "$PROFILE_DIR"/*; do
    rm -rf "$item"
  done
  for item in "$PROFILE_DIR"/.[!.]* "$PROFILE_DIR"/..?*; do
    [[ -e "$item" ]] || continue
    rm -rf "$item"
  done
  shopt -u dotglob nullglob

  echo -e "${BOLD}Restore${NC} staged files"
  shopt -s dotglob
  if compgen -G "${STAGE}/*" &>/dev/null; then
    cp -a "${STAGE}/." "${PROFILE_DIR}/"
  fi
  shopt -u dotglob

  rm -rf "$STAGE"
  STAGE=""
  trap - EXIT

  echo ""
  echo -e "${GREEN}Done.${NC} Backup: $backup_dir"
  if [[ "$SKIP_PLACES" -eq 1 ]] || [[ "$SKIP_FAVICONS" -eq 1 ]]; then
    echo -e "${YELLOW}Note:${NC} Open Firefox → Bookmarks → Manage Bookmarks → Import and Backup → Restore if bookmarks are missing."
  fi
  echo "Start Firefox and verify logins (about:logins), bookmarks, and settings."

  if [[ -n "$REBUILD_COUNT" ]]; then
    mkdir -p "$REBUILD_COUNT_FLAG_DIR"
    printf '%s\n' "$REBUILD_COUNT" > "$REBUILD_COUNT_FLAG_FILE"
    echo "Recorded rebuild count ${REBUILD_COUNT} → ${REBUILD_COUNT_FLAG_FILE}"
  fi
}

if [[ "${REBUILD_FIREFOX_PROFILE_CONFIRM:-}" == "1" ]]; then
  YES=1
fi

if [[ $# -eq 0 ]]; then
  show_help
fi
for a in "$@"; do
  if [[ "$a" == "-h" || "$a" == "--help" ]]; then
    show_help
  fi
done

main "$@"
