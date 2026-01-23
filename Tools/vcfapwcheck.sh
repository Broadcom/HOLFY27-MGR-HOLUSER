#!/bin/bash

# Author: Burke Azbill
# Purpose: Check VCF Automation appliance to see if the password has expired. If so, use expect script to reset the password
# Version: 1.0 Date: October, 2025
# Version: 1.1 Date: November, 2025 - re-wrote to account for failed connections, incorrect password, and successful connection.
# Version: 1.2 Date: November, 2025 - Updated logging
# Configuration
# Replace these with your actual values or pass them as arguments
HOST="10.1.1.71"
USER="vmware-system-user"
# For some reason, outputing to LOGFILE only shows up in the manager log, so attempting to log to both files...
LOGFILE="/home/holuser/hol/labstartup.log"
CONSOLELOG="/lmchol/hol/labstartup.log"

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
        echo "$(date +"%m/%d/%Y %T") Password has expired for user $USER on host $HOST, launching password reset script..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        /home/holuser/hol/Tools/vcfapass.sh $(cat /home/holuser/creds.txt) $(/home/holuser/hol/Tools/holpwgen.sh)
        exit 0
    fi

    # 2. Check for incorrect password
    #    Note: "Permission denied, please try again." is standard, but sometimes it's just "Permission denied".
    #    Using -F for fixed string matching on the specific message provided.
    if echo "$OUTPUT" | grep -F -q "Permission denied, please try again."; then
        echo "$(date +"%m/%d/%Y %T") Incorrect password for user $USER on host $HOST . Unable to continue, exiting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        exit 1
    fi
    
    # Fallback check for standard "Permission denied" (publickey,password,keyboard-interactive) 
    # which might occur if password auth fails entirely or after max attempts.
    if echo "$OUTPUT" | grep -F -q "Permission denied ("; then
        echo "$(date +"%m/%d/%Y %T") Authentication failed (Incorrect password or method)" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        exit 1
    fi

    # 3. Check for successful connection (Exit Code 0)
    if [ $RET -eq 0 ]; then
        echo "$(date +"%m/%d/%Y %T") Successful SSH connection to host $HOST detected, exiting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        exit 0
    fi

    # If failed and retries remain, wait 30 seconds
    if [ $i -lt 10 ]; then
        echo "$(date +"%m/%d/%Y %T") Trying SSH connection to $HOST again in 30s... OUTPUT: $OUTPUT" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        sleep 30
    fi
done

echo "$(date +"%m/%d/%Y %T") The vcfapwcheck.sh script made it to the end of the file - this shouldn't have happened, maybe the host is not powered on! OUTPUT: $OUTPUT" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"