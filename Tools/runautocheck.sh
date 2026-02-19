#!/bin/bash
# runautocheck.sh - HOLFY27 AutoCheck Runner
# Version 2.2 - February 2026
# Author - Burke Azbill and HOL Core Team
#
# This script runs the AutoCheck validation suite for the lab
#
# Search order:
#   1. Lab-specific autocheck.py from vpodrepo (allows lab customization)
#   2. Core HOLFY27-MGR-AUTOCHECK/autocheck.py (standard checks)
#   3. Legacy PowerShell autocheck.ps1 from vpodrepo
#   4. GitHub clone fallback (clone HOLFY27-MGR-AUTOCHECK from GitHub)
#   5. Final fallback: vpodchecker.py (if GitHub is unreachable)

set -e

LOGFILE='/home/holuser/hol/autocheck.log'
HOLROOT='/home/holuser/hol'

# Source shared logging library
source "${HOLROOT}/Tools/log_functions.sh"
AUTOCHECK_DIR='/home/holuser/hol/HOLFY27-MGR-AUTOCHECK'

# Get vPod SKU
if [ -f /tmp/vPod_SKU.txt ]; then
    vPod_SKU=$(cat /tmp/vPod_SKU.txt)
elif [ -f /tmp/config.ini ]; then
    vPod_SKU=$(grep -m1 'vPod_SKU' /tmp/config.ini | cut -d'=' -f2 | tr -d ' ')
else
    vPod_SKU="HOL-UNKNOWN"
fi

# Calculate vpod repository path
year=$(echo "${vPod_SKU}" | cut -c5-6)
index=$(echo "${vPod_SKU}" | cut -c7-8)
VPOD_REPO="/vpodrepo/20${year}-labs/${year}${index}"

log_msg "=== AutoCheck Started ===" "$LOGFILE"
echo "Lab SKU: ${vPod_SKU}" | tee -a $LOGFILE
echo "VPod Repo: ${VPOD_REPO}" | tee -a $LOGFILE

#==============================================================================
# Priority 1: Lab-specific Python AutoCheck from vpodrepo
#==============================================================================

if [ -f "${VPOD_REPO}/autocheck.py" ]; then
    echo "Running lab-specific AutoCheck from vpodrepo" | tee -a $LOGFILE
    /usr/bin/python3 "${VPOD_REPO}/autocheck.py" 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# Priority 2: Core HOLFY27-MGR-AUTOCHECK (standard Python AutoCheck)
#==============================================================================

if [ -f "${AUTOCHECK_DIR}/autocheck.py" ]; then
    echo "Running HOLFY27 Core AutoCheck" | tee -a $LOGFILE
    cd "${AUTOCHECK_DIR}"
    /usr/bin/python3 "${AUTOCHECK_DIR}/autocheck.py" 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# Priority 3: Legacy PowerShell AutoCheck from vpodrepo
#==============================================================================

if [ -f "${VPOD_REPO}/autocheck.ps1" ]; then
    echo "Running PowerShell AutoCheck from vpodrepo (legacy)" | tee -a $LOGFILE
    /usr/bin/pwsh "${VPOD_REPO}/autocheck.ps1" 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# Priority 4: GitHub AutoCheck fallback (clone from remote repository)
#==============================================================================

echo "No local AutoCheck found, attempting to clone from GitHub..." | tee -a $LOGFILE

autocheckdir="${HOME}/autocheck"
[ -d "${autocheckdir}" ] && rm -rf "${autocheckdir}"

autorepo="https://github.com/broadcom/HOLFY27-MGR-AUTOCHECK.git"

# Disable proxy filter for GitHub access
${HOLROOT}/Tools/proxyfilteroff.sh 2>/dev/null || true

# Wait for proxy filter to be disabled (check up to 120 seconds)
echo "Waiting for proxy filter to allow GitHub access..." | tee -a $LOGFILE
PROXY_CHECK_ATTEMPTS=0
PROXY_CHECK_MAX=12
GITHUB_ACCESSIBLE=false

while [ $PROXY_CHECK_ATTEMPTS -lt $PROXY_CHECK_MAX ]; do
    PROXY_CHECK_ATTEMPTS=$((PROXY_CHECK_ATTEMPTS + 1))
    # Check if GitHub is accessible through the proxy using HTTP status code
    # When proxy filter is DISABLED, GitHub should return HTTP 200
    # When proxy filter is ENABLED, GitHub is blocked (returns 403, 503, or connection fails)
    HTTP_STATUS=$(curl -s --max-time 10 -o /dev/null -w "%{http_code}" -x http://proxy.site-a.vcf.lab:3128 https://github.com 2>&1)
    
    if [ "$HTTP_STATUS" == "200" ]; then
        echo "GitHub is accessible through proxy (HTTP ${HTTP_STATUS}, filter disabled)" | tee -a $LOGFILE
        GITHUB_ACCESSIBLE=true
        break
    else
        echo "Attempt ${PROXY_CHECK_ATTEMPTS}/${PROXY_CHECK_MAX}: GitHub returned HTTP ${HTTP_STATUS}, waiting 10s..." | tee -a $LOGFILE
        sleep 10
    fi
done

if [ "$GITHUB_ACCESSIBLE" != "true" ]; then
    echo "ERROR: GitHub not accessible after ${PROXY_CHECK_MAX} attempts (last status: ${HTTP_STATUS})." | tee -a $LOGFILE
    echo "Proxy filter may still be enabled. Continuing with fallback options..." | tee -a $LOGFILE
fi
# Attempt to clone the repository (only if GitHub is accessible)
if [ "$GITHUB_ACCESSIBLE" == "true" ]; then
    echo "Cloning AutoCheck from GitHub: ${autorepo}" | tee -a $LOGFILE
fi

if [ "$GITHUB_ACCESSIBLE" == "true" ] && GIT_TERMINAL_PROMPT=0 git clone -b main "${autorepo}" "${autocheckdir}" > /dev/null 2>&1; then
    echo "Successfully cloned AutoCheck repository" | tee -a $LOGFILE
    
    # Re-enable proxy filter
    ${HOLROOT}/Tools/proxyfilteron.sh 2>/dev/null || true
    
    # Check if Python autocheck.py exists in cloned repo
    if [ -f "${autocheckdir}/autocheck.py" ]; then
        echo "Running Python AutoCheck from GitHub clone" | tee -a $LOGFILE
        cd "${autocheckdir}"
        /usr/bin/python3 "${autocheckdir}/autocheck.py" 2>&1 | tee -a $LOGFILE
        exit $?
    fi
    
    echo "ERROR: Cloned repository does not contain autocheck.py" | tee -a $LOGFILE
else
    echo "WARNING: Failed to clone AutoCheck from GitHub (network unreachable or repo not found)" | tee -a $LOGFILE
    
    # Re-enable proxy filter
    ${HOLROOT}/Tools/proxyfilteron.sh 2>/dev/null || true
fi

#==============================================================================
# Final Fallback: Run vpodchecker.py as minimal AutoCheck
#==============================================================================

if [ -f "${HOLROOT}/Tools/vpodchecker.py" ]; then
    echo "Running vpodchecker.py as fallback AutoCheck" | tee -a $LOGFILE
    /usr/bin/python3 "${HOLROOT}/Tools/vpodchecker.py" 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# No AutoCheck Available
#==============================================================================

echo "No AutoCheck script found" | tee -a $LOGFILE
echo "Checked:" | tee -a $LOGFILE
echo "  - ${VPOD_REPO}/autocheck.py (lab-specific)" | tee -a $LOGFILE
echo "  - ${AUTOCHECK_DIR}/autocheck.py (core)" | tee -a $LOGFILE
echo "  - ${VPOD_REPO}/autocheck.ps1 (legacy)" | tee -a $LOGFILE
echo "  - GitHub: ${autorepo} (fallback)" | tee -a $LOGFILE
echo "  - ${HOLROOT}/Tools/vpodchecker.py (final fallback)" | tee -a $LOGFILE

exit 0
