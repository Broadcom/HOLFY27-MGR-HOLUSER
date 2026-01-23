#!/bin/bash
# runautocheck.sh - HOLFY27 AutoCheck Runner
# Version 2.0 - January 2026
# Author - Burke Azbill and HOL Core Team
#
# This script runs the AutoCheck validation suite for the lab

set -e

LOGFILE='/home/holuser/hol/autocheck.log'
HOLROOT='/home/holuser/hol'

# Get vPod SKU
if [ -f /tmp/vPod_SKU.txt ]; then
    vPod_SKU=$(cat /tmp/vPod_SKU.txt)
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
# Check for Python AutoCheck (preferred)
#==============================================================================

if [ -f "${VPOD_REPO}/autocheck.py" ]; then
    echo "Running Python AutoCheck from vpodrepo" | tee -a $LOGFILE
    /usr/bin/python3 "${VPOD_REPO}/autocheck.py" 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# Check for PowerShell AutoCheck
#==============================================================================

if [ -f "${VPOD_REPO}/autocheck.ps1" ]; then
    echo "Running PowerShell AutoCheck from vpodrepo" | tee -a $LOGFILE
    /usr/bin/pwsh "${VPOD_REPO}/autocheck.ps1" 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# Check for CD-based AutoCheck (legacy)
#==============================================================================

if [ -f "/media/cdrom0/autocheck.ps1" ]; then
    echo "Running PowerShell AutoCheck from CD" | tee -a $LOGFILE
    
    # Clone AutoCheck repository if configured
    autocheckdir="${HOME}/autocheck"
    [ -d "${autocheckdir}" ] && rm -rf "${autocheckdir}"
    
    autorepo="https://github.com/broadcom/HOLFY27-MGR-AUTOCHECK.git"
    
    echo "Cloning AutoCheck from GitHub..." | tee -a $LOGFILE
    git clone -b main "${autorepo}" "${autocheckdir}" > /dev/null 2>&1
    
    # Disable proxy filter for module installation
    ${HOLROOT}/Tools/proxyfilteroff.sh
    
    # Install required PowerShell modules
    echo "Installing PowerShell modules..." | tee -a $LOGFILE
    pwsh -Command 'Install-Module PSSQLite -Confirm:$false -Force' 2>/dev/null
    
    # Configure PowerCLI
    echo "Configuring PowerCLI..." | tee -a $LOGFILE
    pwsh -Command 'Set-PowerCLIConfiguration -Scope User -ParticipateInCEIP $false -Confirm:$false' 2>/dev/null
    pwsh -Command 'Set-PowerCLIConfiguration -InvalidCertificateAction Ignore -Confirm:$false' 2>/dev/null
    pwsh -Command 'Set-PowerCLIConfiguration -DefaultVIServerMode multiple -Confirm:$false' 2>/dev/null
    
    # Re-enable proxy filter
    ${HOLROOT}/Tools/proxyfilteron.sh
    
    # Run AutoCheck
    echo "Starting AutoCheck..." | tee -a $LOGFILE
    cd "${autocheckdir}"
    pwsh -File autocheck.ps1 2>&1 | tee -a $LOGFILE
    exit $?
fi

#==============================================================================
# No AutoCheck Found
#==============================================================================

echo "No AutoCheck script found" | tee -a $LOGFILE
echo "Checked:" | tee -a $LOGFILE
echo "  - ${VPOD_REPO}/autocheck.py" | tee -a $LOGFILE
echo "  - ${VPOD_REPO}/autocheck.ps1" | tee -a $LOGFILE
echo "  - /media/cdrom0/autocheck.ps1" | tee -a $LOGFILE

exit 0
