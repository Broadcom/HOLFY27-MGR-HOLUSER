#!/bin/bash
# VLPagent.sh - HOLFY27 VLP Agent Management
# Version 2.0 - January 2026
# Author - Burke Azbill and HOL Core Team
#
# Manages the VLP VM Agent installation and event handling

set -e

#==============================================================================
# Configuration
#==============================================================================

LOGFILE='/tmp/VLPagentsh.log'
HOLROOT='/home/holuser/hol'
GITDRIVE='/vpodrepo'
VLP_AGENT_DIR="${HOLROOT}/vlp-agent"
VLP_AGENT_VERSION='1.0.10'

# Event trigger files
PREPOP_START='/tmp/prepop.txt'
LAB_START='/tmp/labstart.txt'

# Source shared logging library
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/log_functions.sh"

#==============================================================================
# Functions
#==============================================================================

get_vpod_repo() {
    # Calculate the git repo based on the vPod_SKU
    year=$(echo "${vPod_SKU}" | cut -c5-6)
    index=$(echo "${vPod_SKU}" | cut -c7-8)
    yearrepo="${GITDRIVE}/20${year}-labs"
    vpodgitdir="${yearrepo}/${year}${index}"
}

install_vlp_agent() {
    log_msg "Installing VLP Agent version ${VLP_AGENT_VERSION}..." "$LOGFILE"
    
    # Clean up old agent versions
    if [ -d "${VLP_AGENT_DIR}" ]; then
        for jar in "${VLP_AGENT_DIR}"/vlp-agent-*.jar; do
            [ -e "${jar}" ] || continue
            if [ "${jar}" != "${VLP_AGENT_DIR}/vlp-agent-${VLP_AGENT_VERSION}.jar" ]; then
                rm -f "${jar}"
                log_msg "Removed old agent: ${jar}" "$LOGFILE"
            fi
        done
    fi
    
    # Install the VLP Agent
    cd "${HOLROOT}"
    Tools/vlp-vm-agent-cli.sh install --platform linux-x64 --version ${VLP_AGENT_VERSION} >> ${LOGFILE} 2>&1
    
    # Kill any existing agent process
    pkill -f -9 "java -jar vlp-agent-${VLP_AGENT_VERSION}.jar" 2>/dev/null || true
}

start_vlp_agent() {
    # Check if already running
    if pgrep -f "vlp-agent-${VLP_AGENT_VERSION}.jar" > /dev/null; then
        log_msg "VLP Agent already running" "$LOGFILE"
        return 0
    fi
    
    log_msg "Starting VLP Agent..." "$LOGFILE"
    cd "${HOLROOT}"
    Tools/vlp-vm-agent-cli.sh start
    
    if [ $? -eq 0 ]; then
        log_msg "VLP Agent started successfully" "$LOGFILE"
    else
        log_msg "Failed to start VLP Agent" "$LOGFILE"
        return 1
    fi
}

handle_prepop_start() {
    log_msg "Received prepop start notification" "$LOGFILE"
    
    if [ -f "${vpodgitdir}/prepopstart.sh" ] && [ -x "${vpodgitdir}/prepopstart.sh" ]; then
        log_msg "Running ${vpodgitdir}/prepopstart.sh" "$LOGFILE"
        /bin/bash "${vpodgitdir}/prepopstart.sh"
    fi
}

handle_lab_start() {
    log_msg "Received lab start notification" "$LOGFILE"
    
    # Kill any running labcheck process
    pid=$(pgrep -f 'labstartup.py' 2>/dev/null || true)
    if [ -n "${pid}" ]; then
        log_msg "Stopping current LabStartup processes..." "$LOGFILE"
        pkill -P ${pid} 2>/dev/null || true
        kill ${pid} 2>/dev/null || true
    fi
    
    # Clear scheduled labcheck jobs
    for job in $(atq 2>/dev/null | awk '{print $1}'); do
        atrm ${job} 2>/dev/null || true
    done
    
    # Run lab start script if exists
    if [ -f "${vpodgitdir}/labstart.sh" ] && [ -x "${vpodgitdir}/labstart.sh" ]; then
        log_msg "Running ${vpodgitdir}/labstart.sh" "$LOGFILE"
        /bin/bash "${vpodgitdir}/labstart.sh"
    fi
}

#==============================================================================
# Main
#==============================================================================

# Source environment
. /home/holuser/.bashrc 2>/dev/null || true
. /home/holuser/noproxy.sh 2>/dev/null || true

# Initialize log
log_msg "=== VLP Agent Script Started ===" "$LOGFILE"

# Check for offline/partner export disable marker (set by offline-ready.py)
if [ -f "${HOLROOT}/.vlp-disabled" ]; then
    echo "VLP Agent disabled by offline-ready.py marker: ${HOLROOT}/.vlp-disabled" >> ${LOGFILE}
    echo "Exiting without starting agent."  >> ${LOGFILE}
    exit 0
fi

# Get vPod SKU
if [ -f /tmp/vPod_SKU.txt ]; then
    vPod_SKU=$(cat /tmp/vPod_SKU.txt)
else
    vPod_SKU="HOL-UNKNOWN"
fi

log_msg "vPod SKU: ${vPod_SKU}" "$LOGFILE"

# Determine VPod repository
get_vpod_repo
log_msg "VPod Repo: ${vpodgitdir}" "$LOGFILE"

# Wait before installing agent
log_msg "Waiting 15 seconds before installing VLP Agent..." "$LOGFILE"
sleep 15

# Install and start VLP Agent
install_vlp_agent

log_msg "Waiting 15 seconds before starting VLP Agent..." "$LOGFILE"
sleep 15

start_vlp_agent

#==============================================================================
# Event Loop
#==============================================================================

log_msg "Starting event loop..." "$LOGFILE"

while true; do
    if [ -f "${PREPOP_START}" ]; then
        handle_prepop_start
        rm -f "${PREPOP_START}"
    fi
    
    if [ -f "${LAB_START}" ]; then
        handle_lab_start
        rm -f "${LAB_START}"
    fi
    
    sleep 2
done
