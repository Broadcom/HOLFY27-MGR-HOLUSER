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

echo "=== AutoCheck Started: $(date) ===" | tee $LOGFILE
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

# Wait 90 seconds for proxy filter to be disabled
sleep 90
# Check if proxy is disabled
if curl -s --max-time 5 -x http://proxy.site-a.vcf.lab:3128 https://github.com > /dev/null 2>&1; then
    echo "Proxy is not disabled" | tee -a $LOGFILE
    exit 1
else
    echo "Proxy is disabled" | tee -a $LOGFILE
fi
# Attempt to clone the repository
echo "Cloning AutoCheck from GitHub: ${autorepo}" | tee -a $LOGFILE
if git clone -b main "${autorepo}" "${autocheckdir}" > /dev/null 2>&1; then
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
    # This line should only be reached if autocheck.py is not found in the cloned repository

    # # Check if PowerShell autocheck.ps1 exists in cloned repo
    # if [ -f "${autocheckdir}/autocheck.ps1" ]; then
    #     echo "Running PowerShell AutoCheck from GitHub clone" | tee -a $LOGFILE
        
    #     # Install required PowerShell modules
    #     echo "Installing PowerShell modules..." | tee -a $LOGFILE
    #     pwsh -Command 'Install-Module PSSQLite -Confirm:$false -Force' 2>/dev/null || true
        
    #     # Configure PowerCLI
    #     echo "Configuring PowerCLI..." | tee -a $LOGFILE
    #     pwsh -Command 'Set-PowerCLIConfiguration -Scope User -ParticipateInCEIP $false -Confirm:$false' 2>/dev/null || true
    #     pwsh -Command 'Set-PowerCLIConfiguration -InvalidCertificateAction Ignore -Confirm:$false' 2>/dev/null || true
    #     pwsh -Command 'Set-PowerCLIConfiguration -DefaultVIServerMode multiple -Confirm:$false' 2>/dev/null || true
        
    #     # Run AutoCheck
    #     echo "Starting AutoCheck..." | tee -a $LOGFILE
    #     cd "${autocheckdir}"
    #     pwsh -File autocheck.ps1 2>&1 | tee -a $LOGFILE
    #     exit $?
    # fi
    
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
