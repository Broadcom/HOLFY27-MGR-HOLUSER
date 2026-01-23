#!/bin/bash
# labstartup.sh - HOLFY27 Lab Startup Shell Wrapper
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Enhanced with NFS-based router communication, DNS import support

#==============================================================================
# TESTING FLAG FILE
#==============================================================================
# If /lmchol/home/holuser/hol/testing exists, skip git clone/pull operations
# This allows local testing without overwriting changes
# IMPORTANT: Delete this file before capturing the lab to the catalog!
TESTING_FLAG_FILE="/lmchol/home/holuser/hol/testing"

check_testing_mode() {
    if [ -f "${TESTING_FLAG_FILE}" ]; then
        echo "*** TESTING MODE ENABLED - Skipping git operations ***" >> ${logfile}
        echo "*** Delete ${TESTING_FLAG_FILE} before capturing to catalog! ***" >> ${logfile}
        return 0  # True - testing mode enabled
    fi
    return 1  # False - normal mode
}

#==============================================================================
# FUNCTIONS
#==============================================================================

git_pull() {
    cd "$1" || exit
    ctr=0
    # stash uncommitted changes if not running in HOL-Dev
    if [ "$branch" = "main" ]; then
        echo "git stash local changes for prod." >> ${logfile}
        git stash >> ${logfile}
    else
        echo "Not doing git stash due to HOL-Dev." >> ${logfile}
    fi
    while true; do
        if [ "$ctr" -gt 30 ]; then
            echo "Could not perform git pull. Will attempt LabStartup with existing code." >> ${logfile}
            break
        fi
        git checkout $branch >> ${logfile} 2>&1
        git pull origin $branch >> ${logfile} 2>&1
        if [ $? = 0 ]; then
            break
        else
            gitresult=$(grep 'could not be found' ${logfile})
            if [ $? = 0 ]; then
                echo "The git project ${gitproject} does not exist." >> ${logfile}
                echo "FAIL - No GIT Project" > "$startupstatus"
                exit 1
            else
                echo "Could not complete git pull. Will try again." >> ${logfile}
            fi
        fi
        ctr=$((ctr + 1))
        sleep 5
    done
}

git_clone() {
    cd "$1" || exit
    ctr=0
    while true; do
        if [ "$ctr" -gt 30 ]; then
            echo "Could not perform git clone. failing vpod." >> ${logfile}
            echo "FAIL - Could not clone GIT Project" > "$startupstatus"
            exit 1
        fi
        echo "Performing git clone for repo ${vpodgit}" >> ${logfile}
        echo "git clone -b $branch $gitproject $vpodgitdir" >> ${logfile}
        git clone -b $branch "$gitproject" "$vpodgitdir" >> ${logfile} 2>&1
        if [ $? = 0 ]; then
            break
        else
            gitresult=$(grep 'Could not resolve host' ${logfile})
            if [ $? = 0 ]; then
                echo "DNS did not resolve, will try again" >> ${logfile}
            else
                echo "Could not complete git clone. Will try again." >> ${logfile}
            fi
        fi
        ctr=$((ctr + 1))
        sleep 5
    done
}

runlabstartup() {
    # start the Python labstartup.py script with optional "labcheck" argument
    # we only want one labstartup.py running
    # 
    # The Python scripts use lsf.write_output() which writes directly to the
    # labstartup.log files in holroot and mcholroot. However, any print() 
    # statements, logging output, or errors would be lost without this capture.
    # We use tee to write to both log files simultaneously.
    if ! pgrep -f "labstartup.py"; then
        echo "Starting ${holroot}/labstartup.py $1" >> ${logfile}
        echo "[$(date)] Starting ${holroot}/labstartup.py $1" >> "${holroot}/labstartup.log"
        echo "[$(date)] Starting ${holroot}/labstartup.py $1" >> "${mcholroot}/labstartup.log"
        
        # Run Python with unbuffered output (-u) and write to both log files
        # Using tee with multiple output files to capture stdout/stderr
        /usr/bin/python3 -u ${holroot}/labstartup.py "$1" 2>&1 | tee -a "${holroot}/labstartup.log" "${mcholroot}/labstartup.log" &
    fi
}

get_vpod_repo() {
    # calculate the git repo based on the vPod_SKU
    year=$(echo "${vPod_SKU}" | cut -c5-6)
    index=$(echo "${vPod_SKU}" | cut -c7-8)
    yearrepo="${gitdrive}/20${year}-labs"
    vpodgitdir="${yearrepo}/${year}${index}"
}

get_git_project_info() {
    # Calculate git repository URL and local path based on labtype and SKU
    # Supports multiple SKU patterns:
    # - Standard (HOL, ATE, VXP, EDU): PREFIX-XXYY format
    # - Named (Discovery): PREFIX-Name format
    #
    # Input: vPod_SKU, labtype (global variables)
    # Output: gitproject, yearrepo, vpodgitdir (global variables)
    
    local prefix=$(echo "${vPod_SKU}" | cut -f1 -d'-')
    local suffix=$(echo "${vPod_SKU}" | cut -f2- -d'-')
    
    case "$labtype" in
        Discovery)
            # Discovery uses name-based pattern (no year extraction)
            # e.g., Discovery-Demo -> /vpodrepo/Discovery-labs/Demo
            yearrepo="${gitdrive}/Discovery-labs"
            vpodgitdir="${yearrepo}/${suffix}"
            gitproject="https://github.com/Broadcom/${vPod_SKU}.git"
            echo "Using Discovery naming pattern for ${vPod_SKU}" >> ${logfile}
            ;;
        HOL|ATE|VXP|EDU)
            # Standard format: PREFIX-XXYY where XX=year, YY=index
            # e.g., ATE-2701 -> /vpodrepo/2027-labs/2701
            year=$(echo "${suffix}" | cut -c1-2)
            index=$(echo "${suffix}" | cut -c3-4)
            yearrepo="${gitdrive}/20${year}-labs"
            vpodgitdir="${yearrepo}/${year}${index}"
            gitproject="https://github.com/Broadcom/${prefix}-${year}${index}.git"
            echo "Using standard naming pattern for ${vPod_SKU} (prefix: ${prefix})" >> ${logfile}
            ;;
        *)
            # Fallback to HOL pattern for unknown lab types
            year=$(echo "${vPod_SKU}" | cut -c5-6)
            index=$(echo "${vPod_SKU}" | cut -c7-8)
            yearrepo="${gitdrive}/20${year}-labs"
            vpodgitdir="${yearrepo}/${year}${index}"
            gitproject="https://github.com/Broadcom/HOL-${year}${index}.git"
            echo "Using fallback HOL pattern for unknown labtype: ${labtype}" >> ${logfile}
            ;;
    esac
    
    vpodgit="${vpodgitdir}/.git"
    echo "Git project: ${gitproject}" >> ${logfile}
    echo "Year repo: ${yearrepo}" >> ${logfile}
    echo "VPod git dir: ${vpodgitdir}" >> ${logfile}
}

push_router_files_nfs() {
    # Push router files via NFS instead of SCP
    # Note: vpodgitdir must be set before calling this function
    echo "Pushing router files via NFS to ${holorouterdir}..." >> ${logfile}
    
    # Ensure NFS export directory exists
    mkdir -p ${holorouterdir}
    
    # Copy core team router files
    if [ -d "${holroot}/${router}" ]; then
        cp -r "${holroot}/${router}"/* ${holorouterdir}/ 2>/dev/null
        echo "Copied core team router files" >> ${logfile}
    fi
    
    # Merge lab-specific router files if present
    # Use vpodgitdir which is set by get_git_project_info()
    skurouterfiles="${vpodgitdir}/${router}"
    if [ -d "${skurouterfiles}" ]; then
        echo "Merging lab-specific router files from ${skurouterfiles}" >> ${logfile}
        
        # Merge allowlist files
        if [ -f "${holroot}/${router}/allowlist" ] && [ -f "${skurouterfiles}/allowlist" ]; then
            cat "${holroot}/${router}/allowlist" "${skurouterfiles}/allowlist" | sort | uniq > ${holorouterdir}/allowlist
            echo "Merged allowlist files" >> ${logfile}
        fi
        
        # Copy other files (override)
        for file in "${skurouterfiles}"/*; do
            filename=$(basename "$file")
            if [ "$filename" != "allowlist" ]; then
                cp "$file" ${holorouterdir}/ 2>/dev/null
            fi
        done
    fi
    
    # Signal router that files are ready
    echo "$(date)" > ${holorouterdir}/gitdone
    echo "Signaled router: gitdone" >> ${logfile}
}

#==============================================================================
# INITIALIZATION
#==============================================================================

holroot=/home/holuser/hol
gitdrive=/vpodrepo
lmcholroot=/lmchol/hol
configini=/tmp/config.ini
logfile=/tmp/labstartupsh.log
touch ${logfile} && chmod 666 ${logfile} 2>/dev/null || true
sshoptions='StrictHostKeyChecking=accept-new'
LMC=false
router='holorouter'
holorouterdir=/tmp/holorouter
password=$(cat /home/holuser/creds.txt)

# Generate new password file if not exists
if [ ! -f /home/holuser/NEWPASSWORD.txt ]; then
    /bin/bash /home/holuser/hol/Tools/holpwgen.sh > /home/holuser/NEWPASSWORD.txt
fi

# Source environment variables
. /home/holuser/.bashrc

# If no command line argument, clean up old config
if [ -z "$1" ]; then
    rm ${configini} > /dev/null 2>&1
fi

# Remove all at jobs before starting
for i in $(atq | awk '{print $1}'); do atrm "$i"; done

# Ensure holorouter NFS directory exists
mkdir -p ${holorouterdir}
chmod 775 ${holorouterdir}

#==============================================================================
# WAIT FOR CONSOLE MOUNT
#==============================================================================

echo "[$(date)] Starting labstartup.sh" >> ${logfile}

while true; do
    if [ -d ${lmcholroot} ]; then
        echo "LMC detected." >> ${logfile}
        mcholroot=${lmcholroot}
        desktopfile=/lmchol/home/holuser/desktop-hol/VMware.config
        [ "$1" != "labcheck" ] && cp /home/holuser/hol/Tools/VMware.config $desktopfile 2>/dev/null
        LMC=true
        break
    fi
    echo "Waiting for Main Console mount to complete..." >> ${logfile}
    sleep 5
done

startupstatus=${mcholroot}/startup_status.txt

# Handle labcheck mode
if [ "$1" = "labcheck" ]; then
    runlabstartup labcheck
    exit 0
else
    echo "Main Console mount is present. Clearing labstartup logs." >> ${logfile}
    echo "" > "${holroot}"/labstartup.log
    chmod 666 "${holroot}"/labstartup.log 2>/dev/null || true
    echo "" > "${mcholroot}"/labstartup.log
    chmod 666 "${mcholroot}"/labstartup.log 2>/dev/null || true
    if [ -f ${holorouterdir}/gitdone ]; then
        rm ${holorouterdir}/gitdone
    fi
fi

#==============================================================================
# COPY VPOD.TXT AND DETERMINE LAB TYPE
#==============================================================================

if [ -f "${mcholroot}"/vPod.txt ]; then
    echo "Copying ${mcholroot}/vPod.txt to /tmp/vPod.txt..." >> ${logfile}
    cp "${mcholroot}"/vPod.txt /tmp/vPod.txt
    labtype=$(grep labtype /tmp/vPod.txt | cut -f2 -d '=' | sed 's/\r$//' | xargs)
    
    if [ "$labtype" != "HOL" ]; then
        vPod_SKU=$(grep vPod_SKU /tmp/vPod.txt | cut -f2 -d '=' | sed 's/\r$//' | xargs)
        if [ -f "${holroot}/holodeck/${vPod_SKU}.ini" ]; then
            echo "Copying ${holroot}/holodeck/${vPod_SKU}.ini to ${configini}" >> ${logfile}
            cp ${holroot}/holodeck/"${vPod_SKU}".ini ${configini}
        else
            echo "Copying updated ${holroot}/holodeck/defaultconfig.ini to ${configini}" >> ${logfile}
            cat ${holroot}/holodeck/defaultconfig.ini | sed s/HOL-BADSKU/"${vPod_SKU}"/ > ${configini}
        fi
    fi
else
    echo "No vPod.txt on Main Console. Abort." >> ${logfile}
    echo "FAIL - No vPod_SKU" > "$startupstatus"
    exit 1
fi

# Get vPod_SKU
vPod_SKU=$(grep vPod_SKU /tmp/vPod.txt | grep -v \# | cut -f2 -d= | sed 's/\r$//' | xargs)
echo "$vPod_SKU" > /tmp/vPod_SKU.txt

# Copy password to console
[ -d ${lmcholroot} ] && cp /home/holuser/creds.txt /lmchol/home/holuser/creds.txt 2>/dev/null
[ -d ${lmcholroot} ] && cp /home/holuser/creds.txt /lmchol/home/holuser/Desktop/PASSWORD.txt 2>/dev/null

#==============================================================================
# START VLP AGENT
#==============================================================================

startagent=$(ps -ef | grep VLPagent.sh | grep -v grep)
if [ "${startagent}" = "" ]; then
    cloud=$(/usr/bin/vmtoolsd --cmd "info-get guestinfo.ovfenv" 2>&1 | grep vlp_org_name | cut -f3 -d: | cut -f2 -d\\)
    if [ "${cloud}" = "" ]; then
        echo "Dev environment. Not starting VLP Agent." >> ${logfile}
        echo "NOT REPORTED" > /tmp/cloudinfo.txt
    else
        echo "Prod environment. Starting VLP Agent." >> ${logfile}
        echo "$cloud" > /tmp/cloudinfo.txt
        /home/holuser/hol/Tools/VLPagent.sh &
    fi
fi

#==============================================================================
# WAIT FOR VPODREPO MOUNT
#==============================================================================

while [ ! -d ${gitdrive}/lost+found ]; do
    echo "Waiting for ${gitdrive}..." >> ${logfile}
    sleep 5
    gitmount=$(mount | grep ${gitdrive})
    if [ "${gitmount}" = "" ]; then
        echo "External ${gitdrive} not found. Abort." >> ${logfile}
        echo "FAIL - No GIT Drive" > "$startupstatus"
        exit 1
    fi
done

#==============================================================================
# GIT OPERATIONS
#==============================================================================

ubuntu=$(grep DISTRIB_RELEASE /etc/lsb-release | cut -f2 -d '=')

if [ -f ${configini} ]; then
    [ "${labtype}" = "" ] && labtype="HOL"
    echo "labtype: $labtype" >> ${logfile}
elif [ -f /tmp/vPod.txt ]; then
    echo "Getting vPod_SKU from /tmp/vPod.txt" >> ${logfile}
    vPod_SKU=$(grep vPod_SKU /tmp/vPod.txt | cut -f2 -d '=' | sed 's/\r$//' | xargs)
    echo "vPod_SKU is ${vPod_SKU}" >> ${logfile}
fi

echo "$vPod_SKU" > /tmp/vPod_SKU.txt

# Check for BAD SKU
if [ "$vPod_SKU" = "HOL-BADSKU" ]; then
    echo "LabStartup not implemented." >> ${logfile}
    echo "$(date)" > ${holorouterdir}/gitdone
    runlabstartup
    exit 0
fi

# Determine branch
cloud=$(/usr/bin/vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>&1)
holdev=$(echo "${cloud}" | grep -i hol-dev)
if [ "${cloud}" = "No value found" ] || [ -n "${holdev}" ]; then
    branch="dev"
else
    branch="main"
fi

# Calculate git repos from vPod_SKU using labtype-aware function
# This sets: gitproject, yearrepo, vpodgitdir, vpodgit
get_git_project_info

# Perform git operations for all lab types (unless in testing mode)
# Supports: HOL, ATE, VXP, EDU, Discovery
if check_testing_mode; then
    echo "TESTING MODE: Skipping git operations for ${vPod_SKU}" >> ${logfile}
else
    echo "Ready to pull updates for ${vPod_SKU} from ${gitproject}." >> ${logfile}
    
    if [ ! -e "${yearrepo}" ] || [ ! -e "${vpodgitdir}" ]; then
        echo "Creating new git repo for ${vPod_SKU}..." >> ${logfile}
        mkdir -p "$yearrepo" > /dev/null 2>&1
        git_clone "$yearrepo" > /dev/null 2>&1
        # shellcheck disable=SC2181
        if [ $? != 0 ]; then
            echo "The git project ${vpodgit} does not exist." >> ${logfile}
            echo "FAIL - No GIT Project" > "$startupstatus"
            exit 1
        fi
    else
        echo "Performing git pull for repo ${vpodgit}" >> ${logfile}
        git_pull "$vpodgitdir"
    fi
    
    # shellcheck disable=SC2181
    if [ $? = 0 ]; then
        echo "${vPod_SKU} git operations were successful." >> ${logfile}
    else
        echo "Could not complete ${vPod_SKU} git operations." >> ${logfile}
    fi
fi

# Copy config.ini from vpodrepo if present
if [ -f "${vpodgitdir}"/config.ini ]; then
    cp "${vpodgitdir}"/config.ini ${configini}
fi

#==============================================================================
# PUSH ROUTER FILES VIA NFS
#==============================================================================

if [ "${labtype}" = "HOL" ]; then
    push_router_files_nfs
else
    echo "Pushing $labtype router files via NFS..." >> ${logfile}
    mkdir -p ${holorouterdir}
    cp ${holroot}/${router}/nofirewall.sh ${holorouterdir}/iptablescfg.sh 2>/dev/null
    cp ${holroot}/${router}/allowall ${holorouterdir}/allowlist 2>/dev/null
    echo "$(date)" > ${holorouterdir}/gitdone
fi

echo "$(date)" > /tmp/gitdone

#==============================================================================
# RUN LABSTARTUP
#==============================================================================

if [ -f ${configini} ]; then
    runlabstartup
    echo "$0 finished." >> ${logfile}
else
    echo "No config.ini on Main Console or vpodrepo. Abort." >> ${logfile}
    echo "FAIL - No Config" > "$startupstatus"
    exit 1
fi
