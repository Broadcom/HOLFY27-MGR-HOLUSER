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

#==============================================================================
# Functions
#==============================================================================

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> ${LOGFILE}
}

get_vpod_repo() {
    # Calculate the git repo based on the vPod_SKU
    year=$(echo "${vPod_SKU}" | cut -c5-6)
    index=$(echo "${vPod_SKU}" | cut -c7-8)
    yearrepo="${GITDRIVE}/20${year}-labs"
    vpodgitdir="${yearrepo}/${year}${index}"
}

install_vlp_agent() {
    log "Installing VLP Agent version ${VLP_AGENT_VERSION}..."
    
    # Clean up old agent versions
    if [ -d "${VLP_AGENT_DIR}" ]; then
        for jar in $(ls ${VLP_AGENT_DIR}/vlp-agent-*.jar 2>/dev/null); do
            if [ "${jar}" != "${VLP_AGENT_DIR}/vlp-agent-${VLP_AGENT_VERSION}.jar" ]; then
                rm -f "${jar}"
                log "Removed old agent: ${jar}"
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
        log "VLP Agent already running"
        return 0
    fi
    
    log "Starting VLP Agent..."
    cd "${HOLROOT}"
    Tools/vlp-vm-agent-cli.sh start
    
    if [ $? -eq 0 ]; then
        log "VLP Agent started successfully"
    else
        log "Failed to start VLP Agent"
        return 1
    fi
}

handle_prepop_start() {
    log "Received prepop start notification"
    
    if [ -f "${vpodgitdir}/prepopstart.sh" ] && [ -x "${vpodgitdir}/prepopstart.sh" ]; then
        log "Running ${vpodgitdir}/prepopstart.sh"
        /bin/bash "${vpodgitdir}/prepopstart.sh"
    fi
}

handle_lab_start() {
    log "Received lab start notification"
    
    # Kill any running labcheck process
    pid=$(pgrep -f 'labstartup.py' 2>/dev/null || true)
    if [ -n "${pid}" ]; then
        log "Stopping current LabStartup processes..."
        pkill -P ${pid} 2>/dev/null || true
        kill ${pid} 2>/dev/null || true
    fi
    
    # Clear scheduled labcheck jobs
    for job in $(atq 2>/dev/null | awk '{print $1}'); do
        atrm ${job} 2>/dev/null || true
    done
    
    # Run lab start script if exists
    if [ -f "${vpodgitdir}/labstart.sh" ] && [ -x "${vpodgitdir}/labstart.sh" ]; then
        log "Running ${vpodgitdir}/labstart.sh"
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
echo "=== VLP Agent Script Started: $(date) ===" > ${LOGFILE}

# Get vPod SKU
if [ -f /tmp/vPod_SKU.txt ]; then
    vPod_SKU=$(cat /tmp/vPod_SKU.txt)
else
    vPod_SKU="HOL-UNKNOWN"
fi

log "vPod SKU: ${vPod_SKU}"

# Determine VPod repository
get_vpod_repo
log "VPod Repo: ${vpodgitdir}"

# Wait before installing agent
log "Waiting 15 seconds before installing VLP Agent..."
sleep 15

# Install and start VLP Agent
install_vlp_agent

log "Waiting 15 seconds before starting VLP Agent..."
sleep 15

start_vlp_agent

#==============================================================================
# Event Loop
#==============================================================================

log "Starting event loop..."

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
