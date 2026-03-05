#!/usr/bin/bash
# VLPagent.sh - HOLFY27 VLP Agent Management
# Version 2.3 - 2026-03-05
# Author - Burke Azbill and HOL Core Team
#
# Manages the VLP VM Agent installation and event handling
# Uses flock for atomic single-instance enforcement and watchdog for agent health

#==============================================================================
# Configuration
#==============================================================================

LOGFILE='/tmp/VLPagentsh.log'
HOLROOT='/home/holuser/hol'
GITDRIVE='/vpodrepo'
VLP_AGENT_DIR="${HOLROOT}/vlp-agent"
VLP_AGENT_VERSION='1.0.11'
LOCKFILE='/tmp/VLPagent.lock'

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
    
    # Install the VLP Agent with retry
    cd "${HOLROOT}" || return 1
    local attempt=1
    local max_attempts=3
    while [ $attempt -le $max_attempts ]; do
        if Tools/vlp-vm-agent-cli.sh install --platform linux-x64 --version ${VLP_AGENT_VERSION} >> ${LOGFILE} 2>&1; then
            log_msg "VLP Agent install succeeded on attempt ${attempt}" "$LOGFILE"
            break
        fi
        log_msg "VLP Agent install failed (attempt ${attempt}/${max_attempts})" "$LOGFILE"
        if [ $attempt -eq $max_attempts ]; then
            log_msg "ERROR: VLP Agent install failed after ${max_attempts} attempts" "$LOGFILE"
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 5
    done
    
    # Kill any existing agent process
    pkill -f -9 "java -jar vlp-agent-${VLP_AGENT_VERSION}.jar" 2>/dev/null || true
}

start_vlp_agent() {
    # Check if already running
    if pgrep -f "vlp-agent-${VLP_AGENT_VERSION}.jar" > /dev/null; then
        log_msg "VLP Agent already running (pid $(pgrep -f "vlp-agent-${VLP_AGENT_VERSION}.jar"))" "$LOGFILE"
        return 0
    fi
    
    cd "${HOLROOT}" || return 1
    local attempt=1
    local max_attempts=3
    while [ $attempt -le $max_attempts ]; do
        log_msg "Starting VLP Agent (attempt ${attempt}/${max_attempts})..." "$LOGFILE"
        Tools/vlp-vm-agent-cli.sh start >> ${LOGFILE} 2>&1
        
        # Give the JVM a moment to launch, then verify it's actually running
        sleep 3
        if pgrep -f "vlp-agent-${VLP_AGENT_VERSION}.jar" > /dev/null; then
            log_msg "VLP Agent started successfully (pid $(pgrep -f "vlp-agent-${VLP_AGENT_VERSION}.jar"))" "$LOGFILE"
            return 0
        fi
        
        log_msg "VLP Agent process not found after start attempt ${attempt}" "$LOGFILE"
        attempt=$((attempt + 1))
        sleep 5
    done
    
    log_msg "ERROR: Failed to start VLP Agent after ${max_attempts} attempts" "$LOGFILE"
    return 1
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

# Ensure only one instance runs at a time using flock (atomic, race-free)
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') VLPagent.sh is already running (lock held). Exiting duplicate instance." >> "$LOGFILE"
    exit 0
fi

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

# Brief pause to let the system settle after boot
log_msg "Waiting 5 seconds before installing VLP Agent..." "$LOGFILE"
sleep 5

# Install and start VLP Agent
if ! install_vlp_agent; then
    log_msg "WARNING: Agent install had errors, attempting start anyway..." "$LOGFILE"
fi

sleep 2

if ! start_vlp_agent; then
    log_msg "WARNING: Agent failed to start, will retry in event loop watchdog" "$LOGFILE"
fi

#==============================================================================
# Event Loop (with agent health watchdog)
#==============================================================================

log_msg "Starting event loop..." "$LOGFILE"
WATCHDOG_INTERVAL=30
LOOP_COUNT=0

while true; do
    if [ -f "${PREPOP_START}" ]; then
        handle_prepop_start
        rm -f "${PREPOP_START}"
    fi
    
    if [ -f "${LAB_START}" ]; then
        handle_lab_start
        rm -f "${LAB_START}"
    fi
    
    # Watchdog: verify agent process is alive every WATCHDOG_INTERVAL loops (~60s)
    LOOP_COUNT=$((LOOP_COUNT + 1))
    if [ $((LOOP_COUNT % WATCHDOG_INTERVAL)) -eq 0 ]; then
        if ! pgrep -f "vlp-agent-${VLP_AGENT_VERSION}.jar" > /dev/null 2>&1; then
            log_msg "WATCHDOG: VLP Agent process not found, restarting..." "$LOGFILE"
            start_vlp_agent || log_msg "WATCHDOG: Restart failed, will retry next cycle" "$LOGFILE"
        fi
    fi
    
    sleep 2
done
