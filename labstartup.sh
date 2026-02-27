#!/bin/bash
# labstartup.sh - HOLFY27 Lab Startup Shell Wrapper
# Version 3.5 - 2026-02-12
# Author - Burke Azbill and HOL Core Team
# Enhanced with NFS-based router communication, DNS import support

#==============================================================================
# INITIALIZATION
#==============================================================================

holuser_home=/home/holuser
holroot=${holuser_home}/hol
gitdrive=/vpodrepo
lmcholroot=/lmchol/hol
configini=/tmp/config.ini
logfile=/tmp/labstartupsh.log
touch ${logfile} && chmod 666 ${logfile} 2>/dev/null || true

# Source shared logging library
source "${holroot}/Tools/log_functions.sh"
# Lab environment: disable strict host key checking to handle key changes
# sshoptions='StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
LMC=false
router='holorouter'
holorouterdir=/tmp/holorouter
#password=$(cat /home/holuser/creds.txt)

# Testing flag file - if this exists, skip git clone/pull operations
# This allows local testing without overwriting changes
# IMPORTANT: Delete this file before capturing the lab to the catalog!
# Check both the NFS path (Console) and the local path (Manager)
TESTING_FLAG_LMC="/lmchol/hol/testing"
TESTING_FLAG_LOCAL="${holroot}/testing"

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
# FUNCTIONS
#==============================================================================

check_testing_mode() {
    if [ -f "${TESTING_FLAG_LOCAL}" ] || [ -f "${TESTING_FLAG_LMC}" ]; then
        log_msg "*** TESTING MODE ENABLED - Skipping git operations ***" "${logfile}"
        log_msg "*** Delete testing flag before capturing to catalog! ***" "${logfile}"
        log_msg "***   rm -f ${TESTING_FLAG_LOCAL} ${TESTING_FLAG_LMC} ***" "${logfile}"
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
    # Used for non-HOL SKUs and for HOL-BADSKU (base template with no vpodrepo)
    #
    # Search priority for ${sku}.ini:
    #   1. /home/holuser/{labtype}/holodeck/${sku}.ini  (external team repo)
    #   2. /home/holuser/hol/{labtype}/holodeck/${sku}.ini  (in-repo labtype)
    #   3. /home/holuser/hol/holodeck/${sku}.ini  (core default)
    #
    # If no ${sku}.ini found, fall back to defaultconfig.ini with same priority:
    #   1. /home/holuser/{labtype}/holodeck/defaultconfig.ini
    #   2. /home/holuser/hol/{labtype}/holodeck/defaultconfig.ini
    #   3. /home/holuser/hol/holodeck/defaultconfig.ini
    
    local sku="$1"
    local ini_file=""
    
    # Search for ${sku}.ini across override hierarchy
    if [ -f "${holuser_home}/${labtype}/holodeck/${sku}.ini" ]; then
        ini_file="${holuser_home}/${labtype}/holodeck/${sku}.ini"
    elif [ -f "${holroot}/${labtype}/holodeck/${sku}.ini" ]; then
        ini_file="${holroot}/${labtype}/holodeck/${sku}.ini"
    elif [ -f "${holroot}/holodeck/${sku}.ini" ]; then
        ini_file="${holroot}/holodeck/${sku}.ini"
    fi
    
    if [ -n "${ini_file}" ]; then
        log_msg "Using local holodeck config: ${ini_file}" "${logfile}"
        cp "${ini_file}" ${configini}
        return 0  # Success
    fi
    
    log_msg "No local holodeck config found for ${sku}" "${logfile}"
    
    # Fall back to defaultconfig.ini with SKU substitution
    # Search across override hierarchy
    local default_ini=""
    if [ -f "${holuser_home}/${labtype}/holodeck/defaultconfig.ini" ]; then
        default_ini="${holuser_home}/${labtype}/holodeck/defaultconfig.ini"
    elif [ -f "${holroot}/${labtype}/holodeck/defaultconfig.ini" ]; then
        default_ini="${holroot}/${labtype}/holodeck/defaultconfig.ini"
    elif [ -f "${holroot}/holodeck/defaultconfig.ini" ]; then
        default_ini="${holroot}/holodeck/defaultconfig.ini"
    fi
    
    if [ -n "${default_ini}" ]; then
        log_msg "Using defaultconfig.ini from ${default_ini} with SKU substitution" "${logfile}"
        cat "${default_ini}" | sed s/HOL-BADSKU/"${sku}"/ > ${configini}
        return 0  # Still success - we have a config
    fi
    
    log_error "No defaultconfig.ini found in any holodeck directory" "${logfile}"
    return 1  # No config found anywhere
}

git_repo_exists() {
    # Validate that a remote git repository exists and is accessible.
    # Uses curl with a 15-second timeout to check HTTP status of the repo URL.
    # Returns 0 if the repo exists (HTTP 200), 1 otherwise.
    local repo_url="$1"
    local check_url="${repo_url%.git}"
    local http_status
    http_status=$(curl -s --max-time 15 -o /dev/null -w "%{http_code}" "$check_url" 2>/dev/null)
    if [ "$http_status" = "200" ]; then
        return 0
    else
        return 1
    fi
}

git_pull() {
    cd "$1" || exit
    ctr=0

    # Validate the remote repo exists before attempting pull
    log_msg "Validating remote repository: ${gitproject}" "${logfile}"
    if ! git_repo_exists "$gitproject"; then
        log_msg "Remote repository not found or not accessible: ${gitproject}" "${logfile}"
        log_msg "Will attempt LabStartup with existing code." "${logfile}"
        return 1
    fi

    # stash uncommitted changes if not running in HOL-Dev
    if [ "$branch" = "main" ]; then
        log_msg "git stash local changes for prod." "${logfile}"
        git stash >> ${logfile}
    else
        log_msg "Not doing git stash due to HOL-Dev." "${logfile}"
    fi
    while true; do
        if [ "$ctr" -gt 30 ]; then
            log_msg "Could not perform git pull. Will attempt LabStartup with existing code." "${logfile}"
            break
        fi
        git checkout $branch >> ${logfile} 2>&1
        if timeout 30 env GIT_TERMINAL_PROMPT=0 git pull origin $branch >> ${logfile} 2>&1; then
            break
        else
            if grep -q 'could not be found' ${logfile}; then
                log_msg "The git project ${gitproject} does not exist." "${logfile}"
                echo "FAIL - No GIT Project" > "$startupstatus"
                exit 1
            else
                log_msg "Could not complete git pull. Will try again." "${logfile}"
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
            log_msg "Could not perform git clone after 10 attempts." "${logfile}"
            # For non-HOL SKUs, return failure status instead of exiting
            # The caller will handle the fallback to local holodeck/*.ini
            if is_hol_sku "$vPod_SKU"; then
                log_msg "HOL SKU requires git repo. Failing vpod." "${logfile}"
                echo "FAIL - Could not clone GIT Project" > "$startupstatus"
                exit 1
            else
                log_msg "Non-HOL SKU: Will attempt fallback to local config." "${logfile}"
                return 1  # Return failure, let caller handle fallback
            fi
        fi
        log_msg "Performing git clone for repo ${vpodgit}" "${logfile}"
        if ! git_repo_exists "$gitproject"; then
            log_msg "Git repository does not exist: ${gitproject}" "${logfile}"
            if is_hol_sku "$vPod_SKU"; then
                log_msg "HOL SKU requires git repo. Failing vpod." "${logfile}"
                echo "FAIL - No GIT Project" > "$startupstatus"
                exit 1
            else
                log_msg "Non-HOL SKU: Will attempt fallback to local config." "${logfile}"
                return 1  # Return failure, let caller handle fallback
            fi
        fi
        log_msg "git clone -b $branch $gitproject $vpodgitdir" "${logfile}"
        if timeout 120 env GIT_TERMINAL_PROMPT=0 git clone -b $branch "$gitproject" "$vpodgitdir" >> ${logfile} 2>&1; then
            return 0  # Success
        else
            # Check for permanent failures (repo not found)
            if grep -qE 'Repository not found|remote: Not Found|fatal: repository.*not found' ${logfile} 2>/dev/null; then
                log_msg "Git repository does not exist: ${gitproject}" "${logfile}"
                if is_hol_sku "$vPod_SKU"; then
                    log_msg "HOL SKU requires git repo. Failing vpod." "${logfile}"
                    echo "FAIL - No GIT Project" > "$startupstatus"
                    exit 1
                else
                    log_msg "Non-HOL SKU: Will attempt fallback to local config." "${logfile}"
                    return 1  # Return failure, let caller handle fallback
                fi
            fi
            # Check for DNS issues (temporary, retry)
            if grep -q 'Could not resolve host' ${logfile}; then
                log_msg "DNS did not resolve, will try again" "${logfile}"
            else
                log_msg "Could not complete git clone. Will try again." "${logfile}"
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
        log_msg "Starting ${holroot}/labstartup.py ${mode}" "${logfile}" "${holroot}/labstartup.log"
        log_msg "Starting ${holroot}/labstartup.py ${mode}" "${lmcholroot}/labstartup.log"
        
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
        DISCOVERY)
            # Discovery uses name-based pattern (no year extraction)
            # e.g., Discovery-Demo -> /vpodrepo/Discovery-labs/Demo
            yearrepo="${gitdrive}/Discovery-labs"
            vpodgitdir="${yearrepo}/${suffix}"
            gitproject="https://github.com/Broadcom/${vPod_SKU}.git"
            log_msg "Using Discovery naming pattern for ${vPod_SKU}" "${logfile}"
            ;;
        HOL|ATE|VXP|EDU)
            # Standard format: PREFIX-XXYY where XX=year, YY=index
            # e.g., ATE-2701 -> /vpodrepo/2027-labs/2701
            year=$(echo "${suffix}" | cut -c1-2)
            index=$(echo "${suffix}" | cut -c3-4)
            yearrepo="${gitdrive}/20${year}-labs"
            vpodgitdir="${yearrepo}/${year}${index}"
            gitproject="https://github.com/Broadcom/${prefix}-${year}${index}.git"
            log_msg "Using standard naming pattern for ${vPod_SKU} (prefix: ${prefix})" "${logfile}"
            ;;
        *)
            # Fallback to HOL pattern for unknown lab types
            year=$(echo "${vPod_SKU}" | cut -c5-6)
            index=$(echo "${vPod_SKU}" | cut -c7-8)
            yearrepo="${gitdrive}/20${year}-labs"
            vpodgitdir="${yearrepo}/${year}${index}"
            gitproject="https://github.com/Broadcom/HOL-${year}${index}.git"
            log_msg "Using fallback HOL pattern for unknown labtype: ${labtype}" "${logfile}"
            ;;
    esac
    
    vpodgit="${vpodgitdir}/.git"
    log_msg "Git project: ${gitproject}" "${logfile}"
    log_msg "Year repo: ${yearrepo}" "${logfile}"
    log_msg "VPod git dir: ${vpodgitdir}" "${logfile}"
}

clone_or_pull_labtype_overrides() {
    # Clone or pull the labtype team override repo if applicable.
    # External team repos are cloned to /home/holuser/{labtype}/ as a sibling
    # to the core /home/holuser/hol/ directory.
    #
    # Only applies to labtypes that have their own external repo.
    # Core team labtypes (HOL, VXP) keep overrides inside hol/{labtype}/.
    #
    # Input: labtype, branch (global variables)
    # Output: /home/holuser/{labtype}/ directory with override content
    
    local labtype_repo_url=""
    
    case "$labtype" in
        ATE)
            labtype_repo_url="https://github.com/Broadcom/HOLFY27-MGR-ATE.git"
            ;;
        EDU)
            labtype_repo_url="https://github.com/Broadcom/HOLFY27-MGR-EDU.git"
            ;;
        *)
            # HOL, VXP, Discovery,and others use in-repo overrides - no external repo
            log_msg "Labtype ${labtype}: using in-repo overrides (no external repo)" "${logfile}"
            return 0
            ;;
    esac
    
    local labtype_dir="${holuser_home}/${labtype}"
    
    log_msg "Checking for ${labtype} team override repo..." "${logfile}"
    
    # Validate the remote repo exists before attempting any git operations
    if ! git_repo_exists "$labtype_repo_url"; then
        log_warn "${labtype} override repo not found or not accessible: ${labtype_repo_url}" "${logfile}"
        log_warn "${labtype} overrides not available - using core defaults" "${logfile}"
        return 0
    fi
    
    if [ -d "${labtype_dir}/.git" ]; then
        # Repo already cloned - pull latest
        log_msg "Pulling latest ${labtype} overrides from ${labtype_repo_url}" "${logfile}"
        cd "${labtype_dir}" || return 1
        git checkout ${branch} >> ${logfile} 2>&1
        if timeout 30 env GIT_TERMINAL_PROMPT=0 git pull origin ${branch} >> ${logfile} 2>&1; then
            log_msg "${labtype} overrides updated successfully" "${logfile}"
        else
            log_warn "${labtype} override pull failed - using existing content" "${logfile}"
        fi
        cd "${holroot}" || return 1
    else
        # First boot - clone the repo
        log_msg "Cloning ${labtype} overrides from ${labtype_repo_url}" "${logfile}"
        if timeout 120 env GIT_TERMINAL_PROMPT=0 git clone -b ${branch} "${labtype_repo_url}" "${labtype_dir}" >> ${logfile} 2>&1; then
            log_msg "${labtype} overrides cloned successfully" "${logfile}"
        else
            log_warn "${labtype} override clone failed - ${labtype} overrides not available" "${logfile}"
        fi
    fi
    
    return 0
}

push_router_files_nfs() {
    # Push router files via NFS instead of SCP
    # Note: vpodgitdir must be set before calling this function
    #
    # Override priority (highest to lowest):
    #   1. /vpodrepo/20XX-labs/XXXX/holorouter/     (Lab-specific vpodrepo)
    #   2. /home/holuser/{labtype}/holorouter/       (External team override repo)
    #   3. /home/holuser/hol/{labtype}/holorouter/   (In-repo labtype override)
    #   4. /home/holuser/hol/holorouter/             (Default core)
    #
    log_msg "Pushing router files via NFS to ${holorouterdir}..." "${logfile}"
    
    # Ensure NFS export directory exists
    mkdir -p ${holorouterdir}
    
    # Layer 1 (lowest): Copy core team router files
    if [ -d "${holroot}/${router}" ]; then
        cp -r "${holroot}/${router}"/* ${holorouterdir}/ 2>/dev/null
        log_msg "Copied core team router files" "${logfile}"
    fi
    
    # Layer 2: Overlay labtype-specific router files if present
    # Check both in-repo ({holroot}/{labtype}) and external ({holuser_home}/{labtype})
    # External team repo takes precedence over in-repo override
    labtyperouter=""
    if [ -d "${holuser_home}/${labtype}/${router}" ]; then
        labtyperouter="${holuser_home}/${labtype}/${router}"
    elif [ -d "${holroot}/${labtype}/${router}" ]; then
        labtyperouter="${holroot}/${labtype}/${router}"
    fi
    if [ -n "${labtyperouter}" ] && [ -d "${labtyperouter}" ]; then
        log_msg "Merging labtype (${labtype}) router files from ${labtyperouter}" "${logfile}"
        
        # Merge allowlist files (core + labtype)
        if [ -f "${holorouterdir}/allowlist" ] && [ -f "${labtyperouter}/allowlist" ]; then
            cat "${holorouterdir}/allowlist" "${labtyperouter}/allowlist" | sort | uniq > ${holorouterdir}/allowlist.tmp
            mv ${holorouterdir}/allowlist.tmp ${holorouterdir}/allowlist
            log_msg "Merged labtype allowlist files" "${logfile}"
        fi
        
        # Copy other files (override)
        for file in "${labtyperouter}"/*; do
            filename=$(basename "$file")
            if [ "$filename" != "allowlist" ] && [ "$filename" != ".gitkeep" ]; then
                cp "$file" ${holorouterdir}/ 2>/dev/null
            fi
        done
    fi
    
    # Layer 3 (highest): Merge lab-specific router files if present in vpodrepo
    # Use vpodgitdir which is set by get_git_project_info()
    skurouterfiles="${vpodgitdir}/${router}"
    if [ -d "${skurouterfiles}" ]; then
        log_msg "Merging lab-specific router files from ${skurouterfiles}" "${logfile}"
        
        # Merge allowlist files (accumulated + lab-specific)
        if [ -f "${holorouterdir}/allowlist" ] && [ -f "${skurouterfiles}/allowlist" ]; then
            cat "${holorouterdir}/allowlist" "${skurouterfiles}/allowlist" | sort | uniq > ${holorouterdir}/allowlist.tmp
            mv ${holorouterdir}/allowlist.tmp ${holorouterdir}/allowlist
            log_msg "Merged lab-specific allowlist files" "${logfile}"
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
    date > ${holorouterdir}/gitdone
    log_msg "Signaled router: gitdone" "${logfile}"
}

push_console_files_nfs() {
    # Push console files via NFS to the Main Console VM
    #
    # Override priority (highest to lowest):
    #   1. /vpodrepo/20XX-labs/XXXX/console/     (Lab-specific vpodrepo)
    #   2. /home/holuser/{labtype}/console/       (External team override repo)
    #   3. /home/holuser/hol/{labtype}/console/   (In-repo labtype override)
    #   4. /home/holuser/hol/console/             (Default core)
    #
    # Target directories on the console (via NFS mount at /lmchol):
    #   /lmchol/home/holuser/desktop-hol/  -> conkywatch.sh, VMware.config
    #   /lmchol/home/holuser/.conky/       -> conky-startup.sh
    #
    # This allows:
    #   - Core team to update conkywatch.sh, conky-startup.sh, VMware.config
    #     via the hol repo (console/ directory)
    #   - LabType teams to override for their program via their own repo or
    #     in-repo {labtype}/console/
    #   - Individual labs to override VMware.config (or any file) by placing
    #     their version in their vpodrepo's console/ directory
    
    local console_src="${holroot}/console"
    local desktop_dest="/lmchol/home/holuser/desktop-hol"
    local conky_dest="/lmchol/home/holuser/.conky"
    
    log_msg "Pushing console files via NFS..." "${logfile}"
    
    if [ ! -d "${console_src}" ]; then
        log_msg "No console/ directory in hol repo - skipping console file push" "${logfile}"
        return
    fi
    
    # Helper function to deploy a single console file to the correct destination
    _deploy_console_file() {
        local src_file="$1"
        local src_label="$2"
        local filename=$(basename "$src_file")
        case "$filename" in
            conky-startup.sh)
                if cp "$src_file" "${conky_dest}/${filename}" 2>/dev/null; then
                    log_msg "${src_label}: console/${filename} -> .conky/" "${logfile}"
                fi
                ;;
            .gitkeep)
                # Skip placeholder files
                ;;
            *)
                if cp "$src_file" "${desktop_dest}/${filename}" 2>/dev/null; then
                    log_msg "${src_label}: console/${filename} -> desktop-hol/" "${logfile}"
                fi
                ;;
        esac
    }
    
    # Layer 1 (lowest): Copy core team console files
    for file in "${console_src}"/*; do
        [ -f "$file" ] && _deploy_console_file "$file" "Core"
    done
    
    # Layer 2: Overlay labtype-specific console files if present
    # Check external team repo first, then in-repo override
    local labtype_console=""
    if [ -d "${holuser_home}/${labtype}/console" ]; then
        labtype_console="${holuser_home}/${labtype}/console"
    elif [ -d "${holroot}/${labtype}/console" ]; then
        labtype_console="${holroot}/${labtype}/console"
    fi
    if [ -n "${labtype_console}" ] && [ -d "${labtype_console}" ]; then
        log_msg "Merging labtype (${labtype}) console files from ${labtype_console}" "${logfile}"
        for file in "${labtype_console}"/*; do
            [ -f "$file" ] && _deploy_console_file "$file" "LabType override"
        done
    fi
    
    # Layer 3 (highest): Overlay with SKU-specific console files from vpodrepo
    local sku_console="${vpodgitdir}/console"
    if [ -d "${sku_console}" ]; then
        log_msg "Merging SKU-specific console files from ${sku_console}" "${logfile}"
        for file in "${sku_console}"/*; do
            [ -f "$file" ] && _deploy_console_file "$file" "SKU override"
        done
    fi
    
    # Ensure scripts are executable on the console
    chmod +x "${desktop_dest}/conkywatch.sh" 2>/dev/null
    chmod +x "${conky_dest}/conky-startup.sh" 2>/dev/null
    
    log_msg "Console file push complete" "${logfile}"
}

#==============================================================================
# MAIN EXECUTION
#==============================================================================

#==============================================================================
# WAIT FOR CONSOLE MOUNT
#==============================================================================

log_msg "Starting labstartup.sh" "${logfile}"

while true; do
    if [ -d ${lmcholroot} ]; then
        log_msg "LMC detected." "${logfile}"
        LMC=true
        break
    fi
    log_msg "Waiting for Main Console mount to complete..." "${logfile}"
    sleep 5
done

startupstatus=${lmcholroot}/startup_status.txt

# Handle labcheck mode
if [ "$1" = "labcheck" ]; then
    runlabstartup labcheck
    exit 0
else
    log_msg "Main Console mount is present. Clearing labstartup logs." "${logfile}"
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
# Wait for vPod.txt to appear on the Main Console NFS mount.
# The LMC mount point may be detected before NFS attribute caches are fully
# populated, causing the file to appear a few seconds after the directory.
# Retry up to 60 seconds (12 attempts x 5 seconds) before giving up.

vpod_found=false
vpod_wait=0
vpod_max_wait=60

while [ "$vpod_wait" -lt "$vpod_max_wait" ]; do
    if [ -f "${lmcholroot}/vPod.txt" ]; then
        vpod_found=true
        break
    fi
    vpod_wait=$((vpod_wait + 5))
    log_msg "Waiting for vPod.txt on Main Console... (${vpod_wait}/${vpod_max_wait}s)" "${logfile}"
    sleep 5
done

if [ "$vpod_found" = true ]; then
    log_msg "vPod.txt found after ${vpod_wait}s. Copying to /tmp/vPod.txt..." "${logfile}"
    cp "${lmcholroot}"/vPod.txt /tmp/vPod.txt
    labtype=$(grep labtype /tmp/vPod.txt | cut -f2 -d '=' | sed 's/\r$//' | xargs)
    # Normalize labtype to uppercase to avoid case-sensitivity issues
    labtype=$(echo "${labtype}" | tr '[:lower:]' '[:upper:]')
    
    if [ "$labtype" != "HOL" ]; then
        vPod_SKU=$(grep vPod_SKU /tmp/vPod.txt | cut -f2 -d '=' | sed 's/\r$//' | xargs)
        
        # Search for ${vPod_SKU}.ini across override hierarchy:
        #   1. /home/holuser/{labtype}/holodeck/  (external team repo)
        #   2. /home/holuser/hol/{labtype}/holodeck/  (in-repo labtype)
        #   3. /home/holuser/hol/holodeck/  (core default)
        sku_ini=""
        if [ -f "${holuser_home}/${labtype}/holodeck/${vPod_SKU}.ini" ]; then
            sku_ini="${holuser_home}/${labtype}/holodeck/${vPod_SKU}.ini"
        elif [ -f "${holroot}/${labtype}/holodeck/${vPod_SKU}.ini" ]; then
            sku_ini="${holroot}/${labtype}/holodeck/${vPod_SKU}.ini"
        elif [ -f "${holroot}/holodeck/${vPod_SKU}.ini" ]; then
            sku_ini="${holroot}/holodeck/${vPod_SKU}.ini"
        fi
        
        if [ -n "${sku_ini}" ]; then
            log_msg "Copying ${sku_ini} to ${configini}" "${logfile}"
            cp "${sku_ini}" ${configini}
        else
            # Fall back to defaultconfig.ini with SKU substitution
            default_ini=""
            if [ -f "${holuser_home}/${labtype}/holodeck/defaultconfig.ini" ]; then
                default_ini="${holuser_home}/${labtype}/holodeck/defaultconfig.ini"
            elif [ -f "${holroot}/${labtype}/holodeck/defaultconfig.ini" ]; then
                default_ini="${holroot}/${labtype}/holodeck/defaultconfig.ini"
            elif [ -f "${holroot}/holodeck/defaultconfig.ini" ]; then
                default_ini="${holroot}/holodeck/defaultconfig.ini"
            fi
            
            if [ -n "${default_ini}" ]; then
                log_msg "Copying updated ${default_ini} to ${configini}" "${logfile}"
                cat "${default_ini}" | sed s/HOL-BADSKU/"${vPod_SKU}"/ > ${configini}
            else
                log_error "No holodeck config found for ${vPod_SKU}" "${logfile}"
            fi
        fi
    fi
else
    log_msg "No vPod.txt on Main Console after ${vpod_max_wait}s. Abort." "${logfile}"
    # Write failure status and verify the write succeeded
    echo "FAIL - No vPod_SKU" > "$startupstatus"
    sync  # Force NFS write to flush
    # Verify the write - retry if NFS is slow
    for i in 1 2 3; do
        if grep -q "FAIL" "$startupstatus" 2>/dev/null; then
            break
        fi
        log_msg "Retrying status file write (attempt $i)..." "${logfile}"
        sleep 1
        echo "FAIL - No vPod_SKU" > "$startupstatus"
        sync
    done
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

if [ -f "${holroot}/.vlp-disabled" ]; then
    log_msg "VLP Agent disabled by offline-ready.py marker. Skipping." "${logfile}"
elif ! pgrep -f VLPagent.sh > /dev/null 2>&1; then
    cloud=$(/usr/bin/vmtoolsd --cmd "info-get guestinfo.ovfenv" 2>&1 | grep vlp_org_name | cut -f3 -d: | cut -f2 -d\\)
    if [ "${cloud}" = "" ]; then
        log_msg "Dev environment. Not starting VLP Agent." "${logfile}"
        echo "NOT REPORTED" > /tmp/cloudinfo.txt
    else
        log_msg "Prod environment. Starting VLP Agent." "${logfile}"
        echo "$cloud" > /tmp/cloudinfo.txt
        /home/holuser/hol/Tools/VLPagent.sh &
    fi
fi

#==============================================================================
# CHECK FOR PRIOR PROXY FAILURE
#==============================================================================
# gitpull.sh runs before labstartup.sh and checks proxy availability.
# If it wrote a FAIL status, check if the proxy has recovered since then.
# If not, fail the startup immediately rather than proceeding with a broken proxy.

if [ -f "$startupstatus" ] && grep -q "FAIL.*Proxy" "$startupstatus" 2>/dev/null; then
    log_msg "gitpull.sh reported proxy failure. Checking if proxy has recovered..." "${logfile}"
    proxy_recovered=false
    proxy_recheck_max=12
    proxy_recheck=0
    while [ $proxy_recheck -lt $proxy_recheck_max ]; do
        if nc -z -w3 proxy.site-a.vcf.lab 3128 > /dev/null 2>&1; then
            proxy_recovered=true
            log_msg "Proxy has recovered (squid listening on port 3128). Clearing failure." "${logfile}"
            # Clear the failure status since proxy is now available
            echo "STARTING" > "$startupstatus"
            break
        fi
        proxy_recheck=$((proxy_recheck + 1))
        log_msg "Proxy still unavailable (recheck ${proxy_recheck}/${proxy_recheck_max})..." "${logfile}"
        sleep 5
    done
    if [ "$proxy_recovered" = "false" ]; then
        log_msg "Proxy remains unavailable after recheck. Lab startup FAILED." "${logfile}"
        echo "FAIL - Proxy Unavailable" > "$startupstatus"
        sync
        # Update the HTML dashboard with failure
        if [ -f "${holroot}/Tools/status_dashboard.py" ]; then
            /usr/bin/python3 -c "
import sys
sys.path.insert(0, '${holroot}/Tools')
try:
    from status_dashboard import StatusDashboard
    dashboard = StatusDashboard('STARTUP')
    dashboard.set_failed('Proxy Unavailable - squid not listening on port 3128')
    dashboard.generate_html()
except Exception as e:
    pass
" >> ${logfile} 2>&1
        fi
        exit 1
    fi
fi

#==============================================================================
# WAIT FOR VPODREPO MOUNT
#==============================================================================

while [ ! -d ${gitdrive}/lost+found ]; do
    log_msg "Waiting for ${gitdrive}..." "${logfile}"
    sleep 5
    gitmount=$(mount | grep ${gitdrive})
    if [ "${gitmount}" = "" ]; then
        log_msg "External ${gitdrive} not found. Abort." "${logfile}"
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
    log_msg "labtype: $labtype" "${logfile}"
elif [ -f /tmp/vPod.txt ]; then
    log_msg "Getting vPod_SKU from /tmp/vPod.txt" "${logfile}"
    vPod_SKU=$(grep vPod_SKU /tmp/vPod.txt | cut -f2 -d '=' | sed 's/\r$//' | xargs)
    log_msg "vPod_SKU is ${vPod_SKU}" "${logfile}"
fi

echo "$vPod_SKU" > /tmp/vPod_SKU.txt

# Check for BAD SKU - no vpodrepo exists for this SKU
# Fall back to defaultconfig.ini so labstartup.py has a valid config
if [ "$vPod_SKU" = "HOL-BADSKU" ]; then
    log_msg "No vpodrepo for ${vPod_SKU}. Falling back to defaultconfig.ini..." "${logfile}"
    if ! use_local_holodeck_ini "$vPod_SKU"; then
        log_error "No defaultconfig.ini found. labstartup.py will run without config." "${logfile}"
    fi
    date > ${holorouterdir}/gitdone
    push_router_files_nfs
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
    log_msg "TESTING MODE: Skipping git operations for ${vPod_SKU}" "${logfile}"
    git_success=true  # Consider testing mode as success (use existing files)
else
    log_msg "Ready to pull updates for ${vPod_SKU} from ${gitproject}." "${logfile}"
    
    if [ ! -e "${yearrepo}" ] || [ ! -e "${vpodgitdir}" ]; then
        log_msg "Creating new git repo for ${vPod_SKU}..." "${logfile}"
        mkdir -p "$yearrepo" > /dev/null 2>&1
        if git_clone "$yearrepo"; then
            git_success=true
            log_msg "${vPod_SKU} git clone was successful." "${logfile}"
        else
            # git_clone already handles HOL SKU failure (exits)
            # If we reach here, it's a non-HOL SKU that needs fallback
            log_msg "Git clone failed for non-HOL SKU ${vPod_SKU}. Using local config fallback." "${logfile}"
            git_success=false
        fi
    else
        log_msg "Performing git pull for repo ${vpodgit}" "${logfile}"
        if git_pull "$vpodgitdir"; then
            git_success=true
            log_msg "${vPod_SKU} git pull completed." "${logfile}"
        else
            log_msg "Git pull failed for ${vPod_SKU} (repo not found or not accessible)." "${logfile}"
            git_success=true  # Local clone exists; proceed with existing code
        fi
    fi
fi

# Clone or pull labtype team override repo (ATE, EDU, Discovery)
# This runs after the core repo is up to date and labtype is known
if ! check_testing_mode; then
    clone_or_pull_labtype_overrides
fi

# Copy config.ini from vpodrepo if present and git succeeded
# Otherwise, for non-HOL SKUs, use local holodeck/*.ini fallback
if [ "$git_success" = true ] && [ -f "${vpodgitdir}"/config.ini ]; then
    log_msg "Using config.ini from git repo: ${vpodgitdir}/config.ini" "${logfile}"
    cp "${vpodgitdir}"/config.ini ${configini}
elif [ "$git_success" = false ]; then
    # Fallback for non-HOL SKUs when git repo doesn't exist
    log_msg "Git operations failed. Attempting local holodeck config fallback for ${vPod_SKU}..." "${logfile}"
    if use_local_holodeck_ini "$vPod_SKU"; then
        log_msg "Successfully loaded local holodeck config for ${vPod_SKU}" "${logfile}"
    else
        log_msg "Failed to load local holodeck config for ${vPod_SKU}" "${logfile}"
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
    log_msg "Pushing $labtype router files via NFS..." "${logfile}"
    mkdir -p ${holorouterdir}
    # In dev environment, keep the default iptablescfg.sh from git
    # In prod environment, use nofirewall.sh for non-HOL labs
    if [ "$branch" = "dev" ]; then
        log_msg "Dev environment: keeping default iptablescfg.sh from holorouter" "${logfile}"
        cp ${holroot}/${router}/iptablescfg.sh ${holorouterdir}/iptablescfg.sh 2>/dev/null
    else
        log_msg "Prod environment: using nofirewall.sh for non-HOL labtype" "${logfile}"
        cp ${holroot}/${router}/nofirewall.sh ${holorouterdir}/iptablescfg.sh 2>/dev/null
    fi
    cp ${holroot}/${router}/allowall ${holorouterdir}/allowlist 2>/dev/null
    
    # Overlay labtype-specific router overrides if present
    # Check external team repo first, then in-repo override
    labtyperouter=""
    if [ -d "${holuser_home}/${labtype}/${router}" ]; then
        labtyperouter="${holuser_home}/${labtype}/${router}"
    elif [ -d "${holroot}/${labtype}/${router}" ]; then
        labtyperouter="${holroot}/${labtype}/${router}"
    fi
    if [ -n "${labtyperouter}" ] && [ -d "${labtyperouter}" ]; then
        log_msg "Merging labtype (${labtype}) router files from ${labtyperouter}" "${logfile}"
        for file in "${labtyperouter}"/*; do
            filename=$(basename "$file")
            if [ "$filename" != ".gitkeep" ]; then
                cp "$file" ${holorouterdir}/ 2>/dev/null
            fi
        done
    fi
    
    # Overlay vpodrepo-specific router overrides if present
    if [ -d "${vpodgitdir}/${router}" ]; then
        log_msg "Merging vpodrepo router files from ${vpodgitdir}/${router}" "${logfile}"
        for file in "${vpodgitdir}/${router}"/*; do
            filename=$(basename "$file")
            cp "$file" ${holorouterdir}/ 2>/dev/null
        done
    fi
    
fi

#==============================================================================
# PUSH CONSOLE FILES VIA NFS
#==============================================================================
# Deploy console files (conkywatch.sh, conky-startup.sh, VMware.config)
# to the Main Console via the /lmchol NFS mount.
# Core team files from hol/console/ are copied first, then any SKU-specific
# overrides from vpodrepo/<sku>/console/ are overlaid on top.
# This allows labs to customize the conky layout by placing a custom
# VMware.config (or any console file) in their repo's console/ directory.

if [ "$LMC" = true ] && [ "$1" != "labcheck" ]; then
    push_console_files_nfs
fi

# Set up Cursor IDE symlinks (skills, rules, MCP) from hol repo
if [ -x "${holroot}/console/setup-cursor.sh" ] && [ "$1" != "labcheck" ]; then
    ${holroot}/console/setup-cursor.sh >> ${logfile} 2>&1
fi

# Signal the router that the git pull is complete so files are applied
date > /tmp/gitdone
date > ${holorouterdir}/gitdone

#==============================================================================
# RUN LABSTARTUP
#==============================================================================

if [ -f ${configini} ]; then
    runlabstartup
    /home/holuser/hol/Tools/VLPagent.sh &
    log_msg "$0 finished." "${logfile}"
else
    log_msg "No config.ini on Main Console or vpodrepo. Abort." "${logfile}"
    echo "FAIL - No Config" > "$startupstatus"
    exit 1
fi
