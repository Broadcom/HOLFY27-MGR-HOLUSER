#!/bin/bash

# Author: Burke Azbill
# Purpose: Check VCF Automation appliance to see if the password has expired. If so, use expect script to reset the password
# Version: 1.0 Date: October, 2025
# Version: 1.1 Date: November, 2025 - re-wrote to account for failed connections, incorrect password, and successful connection.
# Version: 1.2 Date: November, 2025 - Updated logging
# Configuration
VCFA_FQDN="auto-a.site-a.vcf.lab"
USER="vmware-system-user"
CREDS_FILE="/home/holuser/creds.txt"
# For some reason, outputing to LOGFILE only shows up in the manager log, so attempting to log to both files...
LOGFILE="/home/holuser/hol/labstartup.log"
CONSOLELOG="/lmchol/hol/labstartup.log"

# Source shared logging library
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/log_functions.sh"

# Resolve the VCFA FQDN to an IP
detect_vcfa_host() {
    local resolved_ip
    resolved_ip=$(getent hosts "${VCFA_FQDN}" 2>/dev/null | awk '{print $1}' | head -1)

    if [ -z "${resolved_ip}" ]; then
        log_warn "Cannot resolve ${VCFA_FQDN} via DNS, falling back to 10.1.1.71" "$LOGFILE" "$CONSOLELOG"
        HOST="10.1.1.71"
    else
        HOST="${resolved_ip}"
        log_msg "${VCFA_FQDN} resolves to ${HOST}" "$LOGFILE" "$CONSOLELOG"
    fi
}

detect_vcfa_host

# Loop for 10 total attempts (1 Initial + 9 Retries) for a total of 5 minutes
for i in {0..10}; do
    # Attempt SSH connection using sshpass
    # -o StrictHostKeyChecking=no: Auto-accept host keys
    # -o UserKnownHostsFile=/dev/null: Don't save host keys (prevents known_hosts pollution)
    # -o ConnectTimeout=10: Fail fast if host is unreachable
    # -o PreferredAuthentications=password: Force password auth
    # -o PubkeyAuthentication=no: Disable public key auth
    # "exit": Command to run if connection succeeds (immediately closes session)
    # 2>&1: Capture both stdout and stderr to catch the expiration message
    OUTPUT=$(sshpass -f /home/holuser/creds.txt ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o PreferredAuthentications=password -o PubkeyAuthentication=no "$USER@$HOST" "exit" 2>&1)
    RET=$?

    # Check specifically for the password expiration message
    # grep -F uses fixed string matching (safer/faster than regex)
    # 1. Check for password expiration
    if echo "$OUTPUT" | grep -F -q "You are required to change your password immediately"; then
        log_msg "Password has expired for user $USER on host $HOST, launching password reset script..." "$LOGFILE" "$CONSOLELOG"
        /home/holuser/hol/Tools/vcfapass.sh "$HOST" "$(cat ${CREDS_FILE})" "$(/home/holuser/hol/Tools/holpwgen.sh)"
        exit 0
    fi

    # 2. Check for incorrect password
    #    Note: "Permission denied, please try again." is standard, but sometimes it's just "Permission denied".
    #    Using -F for fixed string matching on the specific message provided.
    if echo "$OUTPUT" | grep -F -q "Permission denied, please try again."; then
        log_error "Incorrect password for user $USER on host $HOST . Unable to continue, exiting..." "$LOGFILE" "$CONSOLELOG"
        exit 1
    fi
    
    # Fallback check for standard "Permission denied" (publickey,password,keyboard-interactive) 
    # which might occur if password auth fails entirely or after max attempts.
    if echo "$OUTPUT" | grep -F -q "Permission denied ("; then
        log_error "Authentication failed (Incorrect password or method)" "$LOGFILE" "$CONSOLELOG"
        exit 1
    fi

    # 3. Check for successful connection (Exit Code 0)
    if [ $RET -eq 0 ]; then
        log_msg "Successful SSH connection to host $HOST detected, exiting..." "$LOGFILE" "$CONSOLELOG"
        exit 0
    fi

    # If failed and retries remain, wait 30 seconds
    if [ $i -lt 10 ]; then
        log_msg "Trying SSH connection to $HOST again in 30s... OUTPUT: $OUTPUT" "$LOGFILE" "$CONSOLELOG"
        sleep 30
    fi
done

log_error "The vcfapwcheck.sh script made it to the end of the file - this shouldn't have happened, maybe the host is not powered on! OUTPUT: $OUTPUT" "$LOGFILE" "$CONSOLELOG"
