#!/bin/bash
# log_functions.sh - HOLFY27 Shared Bash Logging Library
# Version 1.0 - February 2026
# Author - Burke Azbill and HOL Core Team
#
# Provides consistent timestamped logging across all bash scripts.
# Timestamp format: [YYYY-MM-DD HH:MM:SS]
#
# Usage:
#   source /home/holuser/hol/Tools/log_functions.sh
#
#   # Explicit file arguments (highest priority):
#   log_msg  "message" "/path/to/logfile"
#   log_msg  "message" "/path/to/logfile" "/path/to/consolelog"
#
#   # Or set environment variables before calling (used as defaults):
#   LOG_FILE="/path/to/logfile"                 # or LOGFILE
#   CONSOLELOG="/path/to/consolelog"
#   log_msg  "message"                          # uses LOG_FILE/LOGFILE + CONSOLELOG
#
# STDOUT_ONLY mode:
#   When the environment variable STDOUT_ONLY is set to "true", log functions
#   emit plain text to stdout/stderr with NO timestamp and NO file writes.
#   This is used when the caller (e.g. VCFfinal.py) adds its own timestamp.

_log_ts() {
    date '+%Y-%m-%d %H:%M:%S'
}

# Resolve the effective log file paths.
# Explicit args ($1, $2) take priority; fall back to env vars.
_log_resolve_files() {
    _LOG_PRIMARY="${1:-${LOG_FILE:-${LOGFILE:-}}}"
    _LOG_SECONDARY="${2:-${CONSOLELOG:-}}"
}

# log_msg - Log an informational message
#   $1 = message text
#   $2 = primary log file path (optional, falls back to LOG_FILE/LOGFILE)
#   $3 = secondary log file path (optional, falls back to CONSOLELOG)
log_msg() {
    local msg="$1"
    if [[ "${STDOUT_ONLY}" == "true" ]]; then
        echo "$msg"
        return
    fi
    _log_resolve_files "$2" "$3"
    local formatted="[$(_log_ts)] $msg"
    echo "$formatted"
    [[ -n "$_LOG_PRIMARY" ]]   && echo "$formatted" >> "$_LOG_PRIMARY"   || true
    [[ -n "$_LOG_SECONDARY" ]] && echo "$formatted" >> "$_LOG_SECONDARY" || true
}

# log_error - Log an error message (output goes to stderr)
#   $1 = message text
#   $2 = primary log file path (optional, falls back to LOG_FILE/LOGFILE)
#   $3 = secondary log file path (optional, falls back to CONSOLELOG)
log_error() {
    local msg="$1"
    if [[ "${STDOUT_ONLY}" == "true" ]]; then
        echo "ERROR: $msg" >&2
        return
    fi
    _log_resolve_files "$2" "$3"
    local formatted="[$(_log_ts)] ERROR: $msg"
    echo "$formatted" >&2
    [[ -n "$_LOG_PRIMARY" ]]   && echo "$formatted" >> "$_LOG_PRIMARY"   || true
    [[ -n "$_LOG_SECONDARY" ]] && echo "$formatted" >> "$_LOG_SECONDARY" || true
}

# log_warn - Log a warning message
#   $1 = message text
#   $2 = primary log file path (optional, falls back to LOG_FILE/LOGFILE)
#   $3 = secondary log file path (optional, falls back to CONSOLELOG)
log_warn() {
    local msg="$1"
    if [[ "${STDOUT_ONLY}" == "true" ]]; then
        echo "WARNING: $msg"
        return
    fi
    _log_resolve_files "$2" "$3"
    local formatted="[$(_log_ts)] WARNING: $msg"
    echo "$formatted"
    [[ -n "$_LOG_PRIMARY" ]]   && echo "$formatted" >> "$_LOG_PRIMARY"   || true
    [[ -n "$_LOG_SECONDARY" ]] && echo "$formatted" >> "$_LOG_SECONDARY" || true
}
