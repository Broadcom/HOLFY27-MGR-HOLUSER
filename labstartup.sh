#!/bin/bash
# labstartup.sh - HOLFY27 Lab Startup Shell Wrapper
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Enhanced with NFS-based router communication, DNS import support

#==============================================================================
# TESTING FLAG FILE
#==============================================================================
# If /lmchol/hol/testing exists, skip git clone/pull operations
# This allows local testing without overwriting changes
# IMPORTANT: Delete this file before capturing the lab to the catalog!
TESTING_FLAG_FILE="/lmchol/hol/testing"

check_testing_mode() {
    if [ -f "${TESTING_FLAG_FILE}" ]; then
        echo "*** TESTING MODE ENABLED - Skipping git operations ***" >> ${logfile}
        echo "*** Delete ${TESTING_FLAG_FILE} before capturing to catalog! ***" >> ${logfile}
        return 0  # True - testing mode enabled
    fi
    return 1  # False - normal mode
}

is_hol_sku() {
    # Check if the vPod_SKU starts with "HOL-"
    # Returns 0 (true) if it's an HOL SKU, 1 (false) otherwise
    local sku="$1"
    case "$sku" in
        HOL-*)
            return 0  # True - is HOL SKU
            ;;
        *)
            return 1  # False - not HOL SKU
            ;;
    esac
}

use_local_holodeck_ini() {
    # Fallback to using local holodeck/*.ini file when git repo not available
    # For non-HOL SKUs only
    local sku="$1"
    local ini_file="${holroot}/holodeck/${sku}.ini"
    
    if [ -f "${ini_file}" ]; then
        echo "Using local holodeck config: ${ini_file}" >> ${logfile}
        cp "${ini_file}" ${configini}
        return 0  # Success
    else
        echo "No local holodeck config found at ${ini_file}" >> ${logfile}
        # Fall back to defaultconfig.ini with SKU substitution
        echo "Using defaultconfig.ini with SKU substitution" >> ${logfile}
        cat ${holroot}/holodeck/defaultconfig.ini | sed s/HOL-BADSKU/"${sku}"/ > ${configini}
        return 0  # Still success - we have a config
    fi
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
        GIT_TERMINAL_PROMPT=0 git pull origin $branch >> ${logfile} 2>&1
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
        if [ "$ctr" -gt 10 ]; then
            echo "Could not perform git clone after 10 attempts." >> ${logfile}
            # For non-HOL SKUs, return failure status instead of exiting
            # The caller will handle the fallback to local holodeck/*.ini
            if is_hol_sku "$vPod_SKU"; then
                echo "HOL SKU requires git repo. Failing vpod." >> ${logfile}
                echo "FAIL - Could not clone GIT Project" > "$startupstatus"
                exit 1
            else
                echo "Non-HOL SKU: Will attempt fallback to local config." >> ${logfile}
                return 1  # Return failure, let caller handle fallback
            fi
        fi
        echo "Performing git clone for repo ${vpodgit}" >> ${logfile}
        # Confirm that $gitproject url is valid
        git ls-remote $gitproject > /dev/null 2>&1
        if [ $? != 0 ]; then
            echo "Git repository does not exist: ${gitproject}" >> ${logfile}
            if is_hol_sku "$vPod_SKU"; then
                echo "HOL SKU requires git repo. Failing vpod." >> ${logfile}
                echo "FAIL - No GIT Project" > "$startupstatus"
                exit 1
            else
                echo "Non-HOL SKU: Will attempt fallback to local config." >> ${logfile}
                return 1  # Return failure, let caller handle fallback
            fi
        fi
        echo "git clone -b $branch $gitproject $vpodgitdir" >> ${logfile}
        GIT_TERMINAL_PROMPT=0 git clone -b $branch "$gitproject" "$vpodgitdir" >> ${logfile} 2>&1
        if [ $? = 0 ]; then
            return 0  # Success
        else
            # Check for permanent failures (repo not found)
            gitresult=$(grep -E 'Repository not found|remote: Not Found|fatal: repository.*not found' ${logfile} 2>/dev/null)
            if [ $? = 0 ]; then
                echo "Git repository does not exist: ${gitproject}" >> ${logfile}
                if is_hol_sku "$vPod_SKU"; then
                    echo "HOL SKU requires git repo. Failing vpod." >> ${logfile}
                    echo "FAIL - No GIT Project" > "$startupstatus"
                    exit 1
                else
                    echo "Non-HOL SKU: Will attempt fallback to local config." >> ${logfile}
                    return 1  # Return failure, let caller handle fallback
                fi
            fi
            # Check for DNS issues (temporary, retry)
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
    # Start the Python labstartup.py script with optional "labcheck" argument
    # We only want one labstartup.py running at a time
    # 
    # Python lsf.write_output() writes directly to BOTH log files:
    #   - /home/holuser/hol/labstartup.log (Manager local)
    #   - /lmchol/hol/labstartup.log (Main Console via NFS)
    # 
    # We redirect stderr to the local log to catch any Python errors/exceptions
    # Console output from write_output is disabled to avoid duplicates
    local mode="${1:-startup}"
    
    if ! pgrep -f "labstartup.py"; then
        echo "[$(date)] Starting ${holroot}/labstartup.py ${mode}" >> ${logfile}
        echo "[$(date)] Starting ${holroot}/labstartup.py ${mode}" >> "${holroot}/labstartup.log"
        echo "[$(date)] Starting ${holroot}/labstartup.py ${mode}" >> "${lmcholroot}/labstartup.log"
        
        # Run Python with unbuffered output (-u)
        # Redirect stderr to local log to capture any errors/exceptions
        # write_output() handles writing to both log files directly
        /usr/bin/python3 -u ${holroot}/labstartup.py "${mode}" 2>> "${holroot}/labstartup.log" &
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
# Lab environment: disable strict host key checking to handle key changes
sshoptions='StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
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
        desktopfile=/lmchol/home/holuser/desktop-hol/VMware.config
        [ "$1" != "labcheck" ] && cp /home/holuser/hol/Tools/VMware.config $desktopfile 2>/dev/null
        LMC=true
        break
    fi
    echo "Waiting for Main Console mount to complete..." >> ${logfile}
    sleep 5
done

startupstatus=${lmcholroot}/startup_status.txt

# Handle labcheck mode
if [ "$1" = "labcheck" ]; then
    runlabstartup labcheck
    exit 0
else
    echo "Main Console mount is present. Clearing labstartup logs." >> ${logfile}
    # Initialize the status dashboard to clear previous run info
    /usr/bin/python3 ${holroot}/Tools/status_dashboard.py --clear >> ${logfile} 2>&1
    echo "" > "${holroot}"/labstartup.log
    chmod 666 "${holroot}"/labstartup.log 2>/dev/null || true
    echo "" > "${lmcholroot}"/labstartup.log
    chmod 666 "${lmcholroot}"/labstartup.log 2>/dev/null || true
    if [ -f ${holorouterdir}/gitdone ]; then
        rm ${holorouterdir}/gitdone
    fi
fi

#==============================================================================
# COPY VPOD.TXT AND DETERMINE LAB TYPE
#==============================================================================

if [ -f "${lmcholroot}"/vPod.txt ]; then
    echo "Copying ${lmcholroot}/vPod.txt to /tmp/vPod.txt..." >> ${logfile}
    cp "${lmcholroot}"/vPod.txt /tmp/vPod.txt
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

# ubuntu=$(grep DISTRIB_RELEASE /etc/lsb-release | cut -f2 -d '=')

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
    date > ${holorouterdir}/gitdone
    runlabstartup
    exit 0
fi

# Determine branch
cloud=$(/usr/bin/vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>&1)
holdev=$(echo "${cloud}" | grep -i dev)
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
# For non-HOL SKUs, if git repo doesn't exist, fall back to local holodeck/*.ini
git_success=false

if check_testing_mode; then
    echo "TESTING MODE: Skipping git operations for ${vPod_SKU}" >> ${logfile}
    git_success=true  # Consider testing mode as success (use existing files)
else
    echo "Ready to pull updates for ${vPod_SKU} from ${gitproject}." >> ${logfile}
    
    if [ ! -e "${yearrepo}" ] || [ ! -e "${vpodgitdir}" ]; then
        echo "Creating new git repo for ${vPod_SKU}..." >> ${logfile}
        mkdir -p "$yearrepo" > /dev/null 2>&1
        git_clone "$yearrepo"
        # shellcheck disable=SC2181
        if [ $? = 0 ]; then
            git_success=true
            echo "${vPod_SKU} git clone was successful." >> ${logfile}
        else
            # git_clone already handles HOL SKU failure (exits)
            # If we reach here, it's a non-HOL SKU that needs fallback
            echo "Git clone failed for non-HOL SKU ${vPod_SKU}. Using local config fallback." >> ${logfile}
            git_success=false
        fi
    else
        echo "Performing git pull for repo ${vpodgit}" >> ${logfile}
        git_pull "$vpodgitdir"
        git_success=true
        echo "${vPod_SKU} git pull completed." >> ${logfile}
    fi
fi

# Copy config.ini from vpodrepo if present and git succeeded
# Otherwise, for non-HOL SKUs, use local holodeck/*.ini fallback
if [ "$git_success" = true ] && [ -f "${vpodgitdir}"/config.ini ]; then
    echo "Using config.ini from git repo: ${vpodgitdir}/config.ini" >> ${logfile}
    cp "${vpodgitdir}"/config.ini ${configini}
elif [ "$git_success" = false ]; then
    # Fallback for non-HOL SKUs when git repo doesn't exist
    echo "Git operations failed. Attempting local holodeck config fallback for ${vPod_SKU}..." >> ${logfile}
    use_local_holodeck_ini "$vPod_SKU"
    if [ $? = 0 ]; then
        echo "Successfully loaded local holodeck config for ${vPod_SKU}" >> ${logfile}
    else
        echo "Failed to load local holodeck config for ${vPod_SKU}" >> ${logfile}
        echo "FAIL - No Config Available" > "$startupstatus"
        exit 1
    fi
fi

#==============================================================================
# PUSH ROUTER FILES VIA NFS
#==============================================================================

if [ "${labtype}" = "HOL" ]; then
    push_router_files_nfs
else
    echo "Pushing $labtype router files via NFS..." >> ${logfile}
    mkdir -p ${holorouterdir}
    # In dev environment, keep the default iptablescfg.sh from git
    # In prod environment, use nofirewall.sh for non-HOL labs
    if [ "$branch" = "dev" ]; then
        echo "Dev environment: keeping default iptablescfg.sh from holorouter" >> ${logfile}
        cp ${holroot}/${router}/iptablescfg.sh ${holorouterdir}/iptablescfg.sh 2>/dev/null
    else
        echo "Prod environment: using nofirewall.sh for non-HOL labtype" >> ${logfile}
        cp ${holroot}/${router}/nofirewall.sh ${holorouterdir}/iptablescfg.sh 2>/dev/null
    fi
    cp ${holroot}/${router}/allowall ${holorouterdir}/allowlist 2>/dev/null
    date > ${holorouterdir}/gitdone
fi

date > /tmp/gitdone

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
