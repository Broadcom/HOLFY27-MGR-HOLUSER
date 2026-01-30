#!/usr/bin/env python3
# confighol.py - HOLFY27 vApp HOLification Tool
# Version 2.1 - January 2026
# Author - Burke Azbill and HOL Core Team
#
# This script automates the "HOLification" process for vApp templates
# that will be used in VMware Hands-on Labs. It must be run after the
# Holodeck factory build process completes.
#
# OVERVIEW:
# The HOL team leverages the Holodeck factory build process (documented
# elsewhere) and adjusts ("HOLifies") the deliverable for HOL use. This
# script automates as much of the HOLification process as possible.
#
# PREREQUISITES:
# - Complete a successful LabStartup reaching Ready state
# - Edit /hol/vPod.txt on the Console and set to appropriate test vPod_SKU
# - Valid /tmp/config.ini with all required resources defined
# - 'expect' utility installed on the Manager (/usr/bin/expect)
# - SSH access to all target systems using lab password
#
# CAPABILITIES:
# 0. Vault Root CA Import (runs first with SKIP/RETRY/FAIL options):
#    - Checks if HashiCorp Vault PKI is accessible
#    - Downloads root CA certificate from Vault
#    - Imports CA as trusted authority in Firefox on console VM
#    - Requires: libnss3-tools package (provides certutil)
#
# 1. ESXi Host Configuration:
#    - Enable SSH service on each ESXi host
#    - Configure SSH to start automatically with host
#    - Copy holuser public keys for passwordless SSH access
#    - Set password expiration to non-expiring
#    - Update session timeout settings
#
# 2. vCenter Configuration:
#    - Enable bash shell for root user
#    - Configure SSH authorized_keys for passwordless access
#    - Enable browser support and MOB (Managed Object Browser)
#    - Set password policies (9999 days expiration)
#    - Disable HA Admission Control
#    - Configure DRS to PartiallyAutomated
#    - Clear ARP cache
#
# 3. NSX Configuration:
#    - Enable SSH via API on NSX Managers (where supported)
#    - Configure SSH authorized_keys for passwordless access
#    - Remove password expiration for admin, root, audit users
#
# 4. SDDC Manager Configuration:
#    - Configure SSH authorized_keys
#    - Set non-expiring passwords for vcf, backup, root accounts
#
# 5. Operations VMs:
#    - Set non-expiring passwords
#    - Configure SSH authorized_keys
#
# 6. Final Steps:
#    - Clear ARP cache on console and router
#    - Run vpodchecker.py to update L2 VMs (uuid, typematicdelay)
#
# USAGE:
#    python3 confighol.py                    # Full interactive HOLification
#    python3 confighol.py --dry-run          # Preview what would be done
#    python3 confighol.py --skip-vcshell     # Skip vCenter shell configuration
#    python3 confighol.py --skip-nsx         # Skip NSX configuration
#    python3 confighol.py --esx-only         # Only configure ESXi hosts
#
# NOTE: Some operations (vCenter shell, NSX Edge SSH) require manual
#       confirmation or may need to be performed via the vSphere client.
#       See HOLIFICATION.md for details on manual steps.

"""
HOLification Tool for vApp Templates

This tool consolidates and automates the following legacy scripts:
- esx-config.py: ESXi SSH configuration
- configvsphere.ps1: vCenter password policies and cluster settings
- Legacy confighol.py: SSH key distribution, password expiration, etc.

All functionality has been merged into this single script using modern
Python patterns and the lsfunctions library for consistency.
"""

import os
import sys
import glob
import argparse
import time
import ssl
import json
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

import requests

# Add hol directory to path for imports
sys.path.insert(0, '/home/holuser/hol')

from pyVim import connect
from pyVmomi import vim
from pyVim.task import WaitForTask

# Import lsfunctions for common operations
import lsfunctions as lsf

#==============================================================================
# CONFIGURATION CONSTANTS
#==============================================================================

SCRIPT_VERSION = '2.0'
SCRIPT_NAME = 'confighol.py'

# SSH key paths
PUBLIC_KEY_FILE = '/home/holuser/.ssh/id_rsa.pub'
LMC_PUBLIC_KEY_FILE = '/lmchol/home/holuser/.ssh/id_rsa.pub'
LOCAL_AUTH_FILE = '/tmp/authorized_keys'

# ESXi SSH configuration
ESX_AUTH_KEYS_PATH = '/etc/ssh/keys-root/authorized_keys'
ESX_USERNAME = 'root'
SSH_SERVICE_NAME = 'TSM-SSH'  # Technical Support Mode - SSH

# Linux/vCenter SSH configuration
LINUX_AUTH_FILE = '/root/.ssh/authorized_keys'
VPXD_CONFIG = '/etc/vmware-vpx/vpxd.cfg'
LOCAL_VPXD_CONFIG = '/tmp/vpxd.cfg'

# NSX users to configure
NSX_USERS = ['admin', 'root', 'audit']

# Password expiration setting (9999 days ~ 27 years)
PASSWORD_MAX_DAYS = 9999

#==============================================================================
# HELPER FUNCTIONS - FILE OPERATIONS
#==============================================================================

def get_file_contents(filepath: str) -> str:
    """
    Read and return the contents of a file.
    
    :param filepath: Full path to file
    :return: File contents as string, or empty string if not found
    """
    if not os.path.isfile(filepath):
        lsf.write_output(f'WARNING: File not found: {filepath}')
        return ''
    
    try:
        with open(filepath, 'r') as f:
            return f.read().strip()
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to read {filepath}: {e}')
        return ''


def create_authorized_keys_file() -> str:
    """
    Create the combined authorized_keys file with both Manager and LMC keys.
    
    The authorized_keys file contains public SSH keys that allow passwordless
    authentication. We combine keys from both the Manager and the Linux Main
    Console (LMC) so that SSH works from both systems.
    
    :return: Path to the created authorized_keys file
    """
    lsf.write_output('Creating combined authorized_keys file...')
    
    manager_key = get_file_contents(PUBLIC_KEY_FILE)
    lmc_key = get_file_contents(LMC_PUBLIC_KEY_FILE)
    
    if not manager_key:
        lsf.write_output('WARNING: Manager public key not found')
    if not lmc_key:
        lsf.write_output('WARNING: LMC public key not found')
    
    try:
        with open(LOCAL_AUTH_FILE, 'w') as f:
            if manager_key:
                f.write(manager_key + '\n')
            if lmc_key:
                f.write(lmc_key + '\n')
        
        lsf.write_output(f'Created authorized_keys file: {LOCAL_AUTH_FILE}')
        return LOCAL_AUTH_FILE
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to create authorized_keys: {e}')
        return ''


#==============================================================================
# HELPER FUNCTIONS - SSH CONFIG SETUP
#==============================================================================

def setup_ssh_environment():
    """
    Set up the SSH environment for passwordless authentication.
    
    This function:
    1. Renames the Firefox SSL certificate state file to prevent issues
    2. Removes stale known_hosts files on Manager and LMC
    3. Creates SSH config to auto-accept new host keys
    
    These steps are critical for reliable SSH connectivity in lab environments
    where VMs are frequently rebuilt with new host keys.
    """
    lsf.write_output('Setting up SSH environment...')
    
    # Handle Firefox SSL certificates (prevents certificate errors in browser)
    # The SiteSecurityServiceState.bin file can cause SSL issues after lab rebuild
    firefox_pattern = '/lmchol/home/holuser/snap/firefox/common/.mozilla/firefox/*/SiteSecurityServiceState.bin'
    filepath = glob.glob(firefox_pattern)
    if len(filepath) == 1:
        backup_path = f'{filepath[0]}.bak'
        lsf.write_output(f'Backing up Firefox SSL state: {filepath[0]}')
        os.rename(filepath[0], backup_path)
    
    # Remove known_hosts files to prevent host key verification failures
    # Lab VMs are frequently rebuilt with new host keys, so stale entries cause issues
    known_hosts_files = [
        '/home/holuser/.ssh/known_hosts',
        '/lmchol/home/holuser/.ssh/known_hosts'
    ]
    for known_hosts in known_hosts_files:
        if os.path.exists(known_hosts):
            lsf.write_output(f'Removing stale known_hosts: {known_hosts}')
            os.remove(known_hosts)
    
    # Create SSH config to auto-accept new host keys
    # This prevents interactive prompts during SSH connections
    ssh_config_content = "Host *\n\tStrictHostKeyChecking=no\n"
    ssh_config_files = [
        '/home/holuser/.ssh/config',
        '/lmchol/home/holuser/.ssh/config'
    ]
    for ssh_config in ssh_config_files:
        try:
            ssh_dir = os.path.dirname(ssh_config)
            os.makedirs(ssh_dir, exist_ok=True)
            with open(ssh_config, 'w') as f:
                f.write(ssh_config_content)
            lsf.write_output(f'Created SSH config: {ssh_config}')
        except Exception as e:
            lsf.write_output(f'WARNING: Failed to create {ssh_config}: {e}')
    
    lsf.write_output('SSH environment setup complete')


#==============================================================================
# ESXI HOST CONFIGURATION FUNCTIONS
#==============================================================================

def get_ssh_service(host_system):
    """
    Get the SSH service object from an ESXi host.
    
    ESXi's SSH service is named 'TSM-SSH' (Technical Support Mode - SSH).
    This function retrieves the service object which can then be used to
    check status, start/stop the service, or modify its startup policy.
    
    :param host_system: vim.HostSystem object
    :return: SSH service object or None if not found
    """
    try:
        service_system = host_system.configManager.serviceSystem
        services = service_system.serviceInfo.service
        
        for service in services:
            if service.key == SSH_SERVICE_NAME:
                return service
        
        lsf.write_output(f'{host_system.name}: SSH service not found')
        return None
    except Exception as e:
        lsf.write_output(f'{host_system.name}: Error getting SSH service: {e}')
        return None


def enable_ssh_on_esxi_via_api(host_system, dry_run: bool = False) -> bool:
    """
    Enable SSH service on an ESXi host via the vSphere API.
    
    This function:
    1. Starts the SSH service if not running
    2. Sets the service policy to 'on' (auto-start with host)
    
    Using the API is more reliable than CLI-based approaches and works
    through vCenter without needing direct SSH access to the host.
    
    :param host_system: vim.HostSystem object
    :param dry_run: If True, show what would be done without making changes
    :return: True if successful
    """
    hostname = host_system.name
    
    try:
        service_system = host_system.configManager.serviceSystem
        ssh_service = get_ssh_service(host_system)
        
        if ssh_service is None:
            return False
        
        # Start SSH service if not running
        if ssh_service.running:
            lsf.write_output(f'{hostname}: SSH service already running')
        else:
            if dry_run:
                lsf.write_output(f'{hostname}: Would start SSH service')
            else:
                lsf.write_output(f'{hostname}: Starting SSH service')
                service_system.StartService(SSH_SERVICE_NAME)
        
        # Set SSH to start automatically with host (policy: on)
        if ssh_service.policy != 'on':
            if dry_run:
                lsf.write_output(f'{hostname}: Would set SSH to auto-start')
            else:
                lsf.write_output(f'{hostname}: Setting SSH to auto-start on boot')
                service_system.UpdateServicePolicy(SSH_SERVICE_NAME, 'on')
        else:
            lsf.write_output(f'{hostname}: SSH already configured to auto-start')
        
        return True
        
    except Exception as e:
        lsf.write_output(f'{hostname}: Error enabling SSH - {e}')
        return False


def update_esxi_session_timeout(hostname: str, timeout: int = 0, dry_run: bool = False) -> bool:
    """
    Update the shell session timeout on an ESXi host.
    
    Setting timeout to 0 disables the session timeout, which is useful for
    lab environments where sessions should not time out during debugging.
    
    :param hostname: ESXi host FQDN
    :param timeout: Timeout in seconds (0 = no timeout)
    :param dry_run: If True, show what would be done
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would set session timeout to {timeout}')
        return True
    
    # ESXi stores timeout in /etc/profile
    # esxcli system settings advanced set -o /UserVars/ESXiShellInteractiveTimeOut -i <value>
    cmd = f'esxcli system settings advanced set -o /UserVars/ESXiShellInteractiveTimeOut -i {timeout}'
    result = lsf.ssh(cmd, f'{ESX_USERNAME}@{hostname}', lsf.get_password())
    
    if result.returncode == 0:
        lsf.write_output(f'{hostname}: Set session timeout to {timeout}')
        return True
    else:
        lsf.write_output(f'{hostname}: Failed to set session timeout')
        return False


def configure_esxi_host(hostname: str, host_system, auth_keys_file: str, 
                        dry_run: bool = False) -> bool:
    """
    Perform complete ESXi host configuration for HOLification.
    
    This includes:
    1. Enable SSH via vSphere API
    2. Copy authorized_keys for passwordless access
    3. Set session timeout to 0 (no timeout)
    4. Set password expiration to non-expiring (9999 days)
    
    :param hostname: ESXi host FQDN
    :param host_system: vim.HostSystem object (or None for direct connection)
    :param auth_keys_file: Path to local authorized_keys file
    :param dry_run: If True, show what would be done
    :return: True if all steps successful
    """
    lsf.write_output('')
    lsf.write_output(f'Configuring ESXi host: {hostname}')
    lsf.write_output('-' * 50)
    
    password = lsf.get_password()
    success = True
    
    # Step 1: Enable SSH via API (if we have a host_system object)
    if host_system:
        if not enable_ssh_on_esxi_via_api(host_system, dry_run):
            lsf.write_output(f'{hostname}: WARNING - Failed to enable SSH via API')
            # Don't fail completely - try via direct SSH later
    
    # Give SSH time to start if we just enabled it
    if not dry_run:
        time.sleep(2)
    
    # Step 2: Copy authorized_keys for passwordless SSH access
    if not dry_run:
        lsf.write_output(f'{hostname}: Copying authorized_keys for passwordless SSH')
        result = lsf.scp(auth_keys_file, f'{ESX_USERNAME}@{hostname}:{ESX_AUTH_KEYS_PATH}', password)
        if result.returncode != 0:
            lsf.write_output(f'{hostname}: WARNING - Failed to copy authorized_keys')
            success = False
        else:
            # Set proper permissions on the authorized_keys file
            lsf.ssh(f'chmod 600 {ESX_AUTH_KEYS_PATH}', f'{ESX_USERNAME}@{hostname}', password)
    else:
        lsf.write_output(f'{hostname}: Would copy authorized_keys to {ESX_AUTH_KEYS_PATH}')
    
    # Step 3: Set session timeout to 0 (no timeout)
    update_esxi_session_timeout(hostname, 0, dry_run)
    
    # Step 4: Set password expiration to non-expiring
    if not dry_run:
        lsf.write_output(f'{hostname}: Setting non-expiring password for root')
        result = lsf.ssh(f'chage -M {PASSWORD_MAX_DAYS} root', f'{ESX_USERNAME}@{hostname}', password)
        if result.returncode != 0:
            lsf.write_output(f'{hostname}: WARNING - Failed to set password expiration')
    else:
        lsf.write_output(f'{hostname}: Would set password expiration to {PASSWORD_MAX_DAYS} days')
    
    if success:
        lsf.write_output(f'{hostname}: ESXi configuration complete')
    else:
        lsf.write_output(f'{hostname}: ESXi configuration completed with warnings')
    
    return success


def configure_all_esxi_hosts(esx_hosts: list, auth_keys_file: str, 
                             dry_run: bool = False) -> dict:
    """
    Configure all ESXi hosts from the config.ini.
    
    :param esx_hosts: List of ESXi host entries from config
    :param auth_keys_file: Path to authorized_keys file
    :param dry_run: If True, preview only
    :return: Dict with success/fail counts
    """
    results = {'success': 0, 'failed': 0, 'hosts': []}
    
    if not esx_hosts:
        lsf.write_output('No ESXi hosts defined in config.ini')
        return results
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('ESXi Host Configuration')
    lsf.write_output('=' * 60)
    
    for entry in esx_hosts:
        # Skip comments and empty lines
        if not entry or entry.strip().startswith('#'):
            continue
        
        # Parse entry format: hostname:maintenance_mode_flag
        parts = entry.split(':')
        hostname = parts[0].strip()
        
        # Wait for host to be reachable
        if not lsf.test_ping(hostname):
            lsf.write_output(f'{hostname}: Host not reachable, skipping')
            results['failed'] += 1
            results['hosts'].append({'host': hostname, 'status': 'unreachable'})
            continue
        
        # Get host object from connected sessions
        host_system = lsf.get_host(hostname)
        
        # Configure the host
        if configure_esxi_host(hostname, host_system, auth_keys_file, dry_run):
            results['success'] += 1
            results['hosts'].append({'host': hostname, 'status': 'success'})
        else:
            results['failed'] += 1
            results['hosts'].append({'host': hostname, 'status': 'failed'})
    
    return results


#==============================================================================
# VCENTER CONFIGURATION FUNCTIONS
#==============================================================================

def enable_vcenter_shell(hostname: str, password: str, dry_run: bool = False) -> bool:
    """
    Enable bash shell for root user on vCenter Server.
    
    By default, vCenter uses the VAMI shell which is limited. This function
    uses an expect script to change root's shell to /bin/bash for easier
    command-line access.
    
    NOTE: This operation can only be run once per vCenter instance.
    Subsequent runs will fail as the shell is already changed.
    
    :param hostname: vCenter hostname
    :param password: Root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would enable bash shell for root')
        return True
    
    expect_script = os.path.expanduser('~/hol/Tools/vcshell.exp')
    if not os.path.isfile(expect_script):
        lsf.write_output(f'{hostname}: vcshell.exp not found, skipping shell config')
        return False
    
    lsf.write_output(f'{hostname}: Enabling bash shell for root...')
    result = lsf.run_command(f'/usr/bin/expect {expect_script} {hostname} {password}')
    
    if result.returncode == 0:
        lsf.write_output(f'{hostname}: Shell enabled successfully')
        return True
    else:
        lsf.write_output(f'{hostname}: Shell enable failed (may already be configured)')
        return False


def configure_vcenter_browser_support(hostname: str, password: str, 
                                      dry_run: bool = False) -> bool:
    """
    Configure browser support message and MOB on vCenter.
    
    This function:
    1. Runs the vcbrowser.sh script to configure browser settings
    2. Enables the Managed Object Browser (MOB) by editing vpxd.cfg
    
    The MOB is useful for API development and troubleshooting but is
    disabled by default for security reasons.
    
    :param hostname: vCenter hostname
    :param password: Root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would configure browser support and MOB')
        return True
    
    # Run browser support script if it exists
    browser_script = os.path.expanduser('~/hol/Tools/vcbrowser.sh')
    if os.path.isfile(browser_script):
        lsf.write_output(f'{hostname}: Configuring browser support...')
        lsf.run_command(f'{browser_script} {hostname}')
    
    # Enable the Managed Object Browser (MOB)
    # Edit /etc/vmware-vpx/vpxd.cfg to add <enableDebugBrowse>true</enableDebugBrowse>
    lsf.write_output(f'{hostname}: Configuring Managed Object Browser...')
    
    try:
        # Download vpxd.cfg from vCenter
        lsf.scp(f'root@{hostname}:{VPXD_CONFIG}', LOCAL_VPXD_CONFIG, password)
        
        # Parse and modify the XML
        tree = ET.parse(LOCAL_VPXD_CONFIG)
        root = tree.getroot()
        
        # Find or create the vpxd element
        vpxd_element = root.find('vpxd')
        if vpxd_element is None:
            vpxd_element = ET.SubElement(root, 'vpxd')
        
        # Check if MOB is already enabled
        mob_element = vpxd_element.find('enableDebugBrowse')
        if mob_element is None:
            # Create and add the MOB enable element
            mob_element = ET.Element('enableDebugBrowse')
            mob_element.text = 'true'
            vpxd_element.append(mob_element)
            
            # Write modified config
            tree.write(LOCAL_VPXD_CONFIG)
            
            # Upload and restart vpxd service
            lsf.scp(LOCAL_VPXD_CONFIG, f'root@{hostname}:{VPXD_CONFIG}', password)
            lsf.write_output(f'{hostname}: Restarting vpxd service...')
            lsf.ssh('service-control --restart vmware-vpxd', f'root@{hostname}', password)
            
            lsf.write_output(f'{hostname}: MOB enabled successfully')
        else:
            lsf.write_output(f'{hostname}: MOB already enabled')
        
        return True
        
    except Exception as e:
        lsf.write_output(f'{hostname}: Failed to configure MOB: {e}')
        return False


def configure_vcenter_password_policies(hostname: str, user: str, password: str,
                                         dry_run: bool = False) -> bool:
    """
    Configure vCenter password policies and cluster settings.
    
    This replaces the PowerShell script configvsphere.ps1 with native Python
    implementation using the vSphere REST API. It configures:
    
    1. Local account password expiration (9999 days)
    2. SSO password policy (previous password count = 1)
    3. DRS automation level (PartiallyAutomated)
    4. HA Admission Control (disabled)
    
    :param hostname: vCenter hostname
    :param user: vCenter user (e.g., administrator@vsphere.local)
    :param password: vCenter password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    if dry_run:
        lsf.write_output(f'{hostname}: Would configure password policies and cluster settings')
        return True
    
    lsf.write_output(f'{hostname}: Configuring password policies and cluster settings...')
    
    # Create session for REST API
    session = requests.Session()
    session.verify = False
    
    try:
        # Get API session token
        auth_url = f'https://{hostname}/api/session'
        response = session.post(auth_url, auth=(user, password))
        
        if response.status_code != 201:
            lsf.write_output(f'{hostname}: Failed to authenticate to REST API: {response.status_code}')
            # Fall back to PowerShell if available
            return configure_vcenter_password_policies_powershell(hostname, user, password)
        
        token = response.json()
        session.headers.update({'vmware-api-session-id': token})
        
        # Configure local accounts password policy (9999 days)
        lsf.write_output(f'{hostname}: Setting password expiration to {PASSWORD_MAX_DAYS} days')
        policy_url = f'https://{hostname}/api/appliance/local-accounts/global-policy'
        policy_data = {'max_days': PASSWORD_MAX_DAYS}
        
        response = session.put(policy_url, json=policy_data)
        if response.status_code not in [200, 204]:
            lsf.write_output(f'{hostname}: WARNING - Failed to set password policy: {response.status_code}')
        
        # Get all clusters and configure DRS/HA settings
        # Note: This uses the pyVmomi connection we already have
        if hostname in lsf.sisvc:
            si = lsf.sisvc[hostname]
            content = si.RetrieveContent()
            
            # Get all clusters
            container = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.ClusterComputeResource], True
            )
            
            for cluster in container.view:
                lsf.write_output(f'{hostname}: Configuring cluster {cluster.name}')
                
                try:
                    # Create cluster config spec
                    spec = vim.cluster.ConfigSpecEx()
                    
                    # Configure DRS to PartiallyAutomated
                    drs_spec = vim.cluster.DrsConfigInfo()
                    drs_spec.enabled = True
                    drs_spec.defaultVmBehavior = vim.cluster.DrsConfigInfo.DrsBehavior.partiallyAutomated
                    spec.drsConfig = drs_spec
                    
                    # Disable HA Admission Control
                    das_spec = vim.cluster.DasConfigInfo()
                    das_spec.admissionControlEnabled = False
                    spec.dasConfig = das_spec
                    
                    # Apply configuration
                    task = cluster.ReconfigureComputeResource_Task(spec, True)
                    WaitForTask(task)
                    
                    lsf.write_output(f'{hostname}: Cluster {cluster.name} configured')
                    
                except Exception as e:
                    lsf.write_output(f'{hostname}: Failed to configure cluster {cluster.name}: {e}')
            
            container.Destroy()
        
        # End the REST API session
        session.delete(auth_url)
        
        lsf.write_output(f'{hostname}: Password policies and cluster settings configured')
        return True
        
    except Exception as e:
        lsf.write_output(f'{hostname}: Error configuring password policies: {e}')
        return False


def configure_vcenter_password_policies_powershell(hostname: str, user: str, 
                                                    password: str) -> bool:
    """
    Fallback to PowerShell script for vCenter password policy configuration.
    
    This is used when the REST API approach fails or PowerCLI is available.
    
    :param hostname: vCenter hostname
    :param user: vCenter user
    :param password: vCenter password
    :return: True if successful
    """
    script_path = os.path.expanduser('~/hol/Tools/configvsphere.ps1')
    
    if not os.path.isfile(script_path):
        lsf.write_output(f'{hostname}: PowerShell script not found, skipping')
        return False
    
    lsf.write_output(f'{hostname}: Using PowerShell fallback for password policies')
    result = lsf.run_command(f'pwsh -File {script_path} {hostname} {user} {password}')
    
    return result.returncode == 0


def configure_vcenter(entry: str, auth_keys_file: str, password: str,
                      skip_shell: bool = False, dry_run: bool = False) -> bool:
    """
    Perform complete vCenter configuration for HOLification.
    
    This function handles all vCenter-related configuration:
    1. Enable bash shell (optional, interactive prompt)
    2. Configure SSH authorized_keys
    3. Enable browser support and MOB
    4. Configure password policies and cluster settings
    5. Set password expiration for root
    6. Clear ARP cache
    
    :param entry: vCenter entry from config.ini (hostname:type:user)
    :param auth_keys_file: Path to authorized_keys file
    :param password: Root password
    :param skip_shell: Skip shell configuration
    :param dry_run: If True, preview only
    :return: True if successful
    """
    # Parse entry format: hostname:type:user
    parts = entry.split(':')
    hostname = parts[0].strip()
    vc_type = parts[1].strip() if len(parts) > 1 else 'linux'
    user = parts[2].strip() if len(parts) > 2 else 'administrator@vsphere.local'
    
    lsf.write_output('')
    lsf.write_output(f'Configuring vCenter: {hostname}')
    lsf.write_output('-' * 50)
    
    success = True
    
    # Step 1: Enable shell and browser support (interactive)
    if not skip_shell:
        if not dry_run:
            answer = input(f'Enable shell and browser support on {hostname}? (y/n): ')
            if answer.lower().startswith('y'):
                # Enable bash shell
                enable_vcenter_shell(hostname, password, dry_run)
                
                # Configure SSH authorized_keys
                lsf.write_output(f'{hostname}: Copying authorized_keys')
                lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', password)
                lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', password)
                
                # Configure browser support and MOB
                configure_vcenter_browser_support(hostname, password, dry_run)
        else:
            lsf.write_output(f'{hostname}: Would configure shell and browser support')
    
    # Step 2: Set password expiration for root
    if not dry_run:
        lsf.write_output(f'{hostname}: Setting non-expiring password for root')
        lsf.ssh('chage -M -1 root', f'root@{hostname}', password)
    else:
        lsf.write_output(f'{hostname}: Would set non-expiring password for root')
    
    # Step 3: Configure password policies and cluster settings
    configure_vcenter_password_policies(hostname, user, password, dry_run)
    
    # Step 4: Clear ARP cache
    if not dry_run:
        lsf.write_output(f'{hostname}: Clearing ARP cache')
        lsf.ssh('ip -s -s neigh flush all', f'root@{hostname}', password)
    else:
        lsf.write_output(f'{hostname}: Would clear ARP cache')
    
    lsf.write_output(f'{hostname}: vCenter configuration complete')
    return success


#==============================================================================
# NSX CONFIGURATION FUNCTIONS
#==============================================================================

def enable_nsx_ssh_via_api(hostname: str, user: str, password: str,
                            dry_run: bool = False) -> bool:
    """
    Enable SSH service on NSX Manager or Edge via the REST API.
    
    Uses the NSX-T API endpoint:
    POST /api/v1/node/services/ssh?action=start
    
    NOTE: This starts the SSH service but does NOT persist across reboots.
    For persistent SSH, the CLI command 'set service ssh start-on-boot'
    must be run via SSH after the service is started.
    
    :param hostname: NSX Manager/Edge hostname
    :param user: API user (usually 'admin')
    :param password: API password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    if dry_run:
        lsf.write_output(f'{hostname}: Would enable SSH via API')
        return True
    
    lsf.write_output(f'{hostname}: Enabling SSH via NSX API...')
    
    try:
        # Start SSH service via API
        url = f'https://{hostname}/api/v1/node/services/ssh?action=start'
        response = requests.post(
            url,
            auth=(user, password),
            verify=False,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            runtime_state = result.get('runtime_state', 'unknown')
            lsf.write_output(f'{hostname}: SSH service state: {runtime_state}')
            return True
        else:
            lsf.write_output(f'{hostname}: Failed to start SSH: HTTP {response.status_code}')
            return False
            
    except Exception as e:
        lsf.write_output(f'{hostname}: Error enabling SSH via API: {e}')
        return False


def configure_nsx_ssh_start_on_boot(hostname: str, password: str,
                                     dry_run: bool = False) -> bool:
    """
    Configure NSX SSH service to start on boot via CLI command.
    
    The NSX API does not support setting start-on-boot directly, so we
    must use SSH to run the CLI command:
    - set service ssh start-on-boot
    
    PREREQUISITE: SSH must already be enabled on the NSX appliance.
    
    :param hostname: NSX hostname
    :param password: Admin password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would configure SSH start-on-boot')
        return True
    
    lsf.write_output(f'{hostname}: Configuring SSH start-on-boot...')
    
    # Run the CLI command to enable start-on-boot
    result = lsf.ssh('set service ssh start-on-boot', f'admin@{hostname}', password)
    
    if result.returncode == 0:
        lsf.write_output(f'{hostname}: SSH start-on-boot configured')
        return True
    else:
        lsf.write_output(f'{hostname}: WARNING - Failed to configure start-on-boot')
        return False


def configure_nsx_node(hostname: str, auth_keys_file: str, password: str,
                       dry_run: bool = False) -> bool:
    """
    Configure an NSX Manager or Edge node for HOLification.
    
    This function:
    1. Attempts to enable SSH via API (if not already enabled)
    2. Copies authorized_keys for passwordless SSH access
    3. Configures SSH to start on boot
    4. Removes password expiration for admin, root, audit users
    
    NOTE: If SSH is not already enabled, the API call will work but SSH
    access is still required for start-on-boot and password configuration.
    
    :param hostname: NSX node hostname
    :param auth_keys_file: Path to authorized_keys file
    :param password: Admin/root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    lsf.write_output(f'{hostname}: Configuring NSX node...')
    
    success = True
    
    # Step 1: Try to enable SSH via API
    enable_nsx_ssh_via_api(hostname, 'admin', password, dry_run)
    
    if not dry_run:
        # Give SSH service time to start
        time.sleep(3)
        
        # Step 2: Copy authorized_keys for root user
        lsf.write_output(f'{hostname}: Copying authorized_keys')
        result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', password)
        if result.returncode != 0:
            lsf.write_output(f'{hostname}: WARNING - Failed to copy authorized_keys')
            success = False
        else:
            lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', password)
        
        # Step 3: Configure SSH to start on boot
        configure_nsx_ssh_start_on_boot(hostname, password, dry_run)
        
        # Step 4: Remove password expiration for NSX users
        for user in NSX_USERS:
            lsf.write_output(f'{hostname}: Removing password expiration for {user}')
            result = lsf.ssh(f'clear user {user} password-expiration', f'admin@{hostname}', password)
            if result.returncode != 0:
                lsf.write_output(f'{hostname}: WARNING - Failed for {user}')
    else:
        lsf.write_output(f'{hostname}: Would copy authorized_keys')
        lsf.write_output(f'{hostname}: Would configure SSH start-on-boot')
        for user in NSX_USERS:
            lsf.write_output(f'{hostname}: Would remove password expiration for {user}')
    
    return success


def configure_nsx_components(auth_keys_file: str, password: str,
                              skip_nsx: bool = False, dry_run: bool = False) -> bool:
    """
    Configure all NSX components from config.ini.
    
    Processes both NSX Managers (vcfnsxmgr) and NSX Edges (vcfnsxedges)
    defined in the [VCF] section of config.ini.
    
    NOTE: SSH must be manually enabled first on each NSX component via
    the vSphere console before this function can configure them.
    See HOLIFICATION.md for the manual steps required.
    
    :param auth_keys_file: Path to authorized_keys file
    :param password: Admin password
    :param skip_nsx: Skip NSX configuration entirely
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if skip_nsx:
        lsf.write_output('Skipping NSX configuration (--skip-nsx)')
        return True
    
    if 'VCF' not in lsf.config:
        lsf.write_output('No VCF section in config.ini, skipping NSX')
        return True
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('NSX Configuration')
    lsf.write_output('=' * 60)
    
    success = True
    
    # Process NSX Managers
    if 'vcfnsxmgr' in lsf.config['VCF']:
        lsf.write_output('Processing NSX Managers...')
        vcfnsxmgrs = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
        
        for entry in vcfnsxmgrs:
            if not entry or entry.strip().startswith('#'):
                continue
            
            # Format: nsxmgr_hostname:esxhost
            parts = entry.split(':')
            nsxmgr = parts[0].strip()
            
            if not dry_run:
                # Interactive prompt - SSH must be enabled manually first
                answer = input(f'Is SSH enabled on {nsxmgr}? (y/n): ')
                if not answer.lower().startswith('y'):
                    lsf.write_output(f'{nsxmgr}: Skipping - SSH not enabled')
                    continue
            
            if not configure_nsx_node(nsxmgr, auth_keys_file, password, dry_run):
                success = False
    
    # Process NSX Edges
    if 'vcfnsxedges' in lsf.config['VCF']:
        lsf.write_output('Processing NSX Edges...')
        vcfnsxedges = lsf.config.get('VCF', 'vcfnsxedges').split('\n')
        
        for entry in vcfnsxedges:
            if not entry or entry.strip().startswith('#'):
                continue
            
            # Format: nsxedge_hostname:esxhost
            parts = entry.split(':')
            nsxedge = parts[0].strip()
            
            if not dry_run:
                # Interactive prompt - SSH must be enabled manually first
                answer = input(f'Is SSH enabled on {nsxedge}? (y/n): ')
                if not answer.lower().startswith('y'):
                    lsf.write_output(f'{nsxedge}: Skipping - SSH not enabled')
                    continue
            
            if not configure_nsx_node(nsxedge, auth_keys_file, password, dry_run):
                success = False
    
    return success


#==============================================================================
# SDDC MANAGER CONFIGURATION
#==============================================================================

def configure_sddc_manager(auth_keys_file: str, password: str,
                           dry_run: bool = False) -> bool:
    """
    Configure SDDC Manager for HOLification.
    
    This function:
    1. Copies authorized_keys for the vcf user
    2. Sets non-expiring passwords for vcf, root, backup accounts
    
    The expect script sddcmgr.exp is used to handle the interactive su command
    required to modify root account settings.
    
    :param auth_keys_file: Path to authorized_keys file (LMC key only)
    :param password: VCF password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    sddcmgr = 'sddcmanager-a.site-a.vcf.lab'
    
    lsf.write_output('')
    lsf.write_output(f'Configuring SDDC Manager: {sddcmgr}')
    lsf.write_output('-' * 50)
    
    if dry_run:
        lsf.write_output(f'{sddcmgr}: Would configure authorized_keys')
        lsf.write_output(f'{sddcmgr}: Would set non-expiring passwords')
        return True
    
    # Copy authorized_keys for vcf user
    # Note: Only the LMC key works for SDDC Manager, not the Manager key
    lmc_key_file = '/lmchol/home/holuser/.ssh/id_rsa.pub'
    lsf.write_output(f'{sddcmgr}: Copying LMC authorized_keys for vcf user')
    
    result = lsf.scp(lmc_key_file, f'vcf@{sddcmgr}:{LINUX_AUTH_FILE}', password)
    if result.returncode != 0:
        lsf.write_output(f'{sddcmgr}: WARNING - Failed to copy authorized_keys')
    else:
        lsf.ssh(f'chmod 600 ~/.ssh/authorized_keys', f'vcf@{sddcmgr}', password)
    
    # Run expect script to configure password expiration
    # This handles the interactive su command needed to modify root settings
    expect_script = os.path.expanduser('~/hol/Tools/sddcmgr.exp')
    if os.path.isfile(expect_script):
        lsf.write_output(f'{sddcmgr}: Configuring non-expiring passwords...')
        result = lsf.run_command(f'/usr/bin/expect {expect_script} {sddcmgr} {password}')
        if result.returncode == 0:
            lsf.write_output(f'{sddcmgr}: Password expiration configured')
        else:
            lsf.write_output(f'{sddcmgr}: WARNING - Password config may have failed')
    else:
        lsf.write_output(f'{sddcmgr}: WARNING - sddcmgr.exp not found')
    
    return True


#==============================================================================
# OPERATIONS VMS CONFIGURATION
#==============================================================================

def configure_operations_vms(auth_keys_file: str, password: str,
                              dry_run: bool = False) -> bool:
    """
    Configure Operations VMs (vRealize/Aria Operations) for HOLification.
    
    Finds VMs with "ops" in the name from the config.ini [RESOURCES] VMs
    section and configures:
    1. Non-expiring password for root
    2. SSH authorized_keys for passwordless access
    
    :param auth_keys_file: Path to authorized_keys file
    :param password: Root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if 'VMs' not in lsf.config['RESOURCES']:
        lsf.write_output('No Operations VMs defined in config')
        return True
    
    vms = lsf.config.get('RESOURCES', 'VMs').split('\n')
    ops_vms = []
    
    for vm in vms:
        if not vm or vm.strip().startswith('#'):
            continue
        if 'ops' in vm.lower():
            # Format: vmname:vcenter
            parts = vm.split(':')
            ops_vms.append(parts[0].strip())
    
    if not ops_vms:
        lsf.write_output('No Operations VMs found in config')
        return True
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('Operations VMs Configuration')
    lsf.write_output('=' * 60)
    
    for opsvm in ops_vms:
        lsf.write_output(f'{opsvm}: Configuring...')
        
        if not dry_run:
            # Set non-expiring password for root
            lsf.write_output(f'{opsvm}: Setting non-expiring password')
            lsf.ssh('chage -M -1 root', f'root@{opsvm}', password)
            
            # Copy authorized_keys
            lsf.write_output(f'{opsvm}: Copying authorized_keys')
            lsf.scp(auth_keys_file, f'root@{opsvm}:{LINUX_AUTH_FILE}', password)
            lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{opsvm}', password)
        else:
            lsf.write_output(f'{opsvm}: Would configure password and SSH keys')
    
    return True


#==============================================================================
# VAULT CA CERTIFICATE IMPORT
#==============================================================================

# Vault PKI Configuration (same as cert-replacement.py)
VAULT_URL = 'http://10.1.1.1:32000'
VAULT_CA_PATH = '/v1/pki/ca/pem'
VAULT_CA_NAME = 'vcf.lab Root Authority'

# Firefox profile paths on the console VM
LMC_FIREFOX_PROFILE_BASE = '/lmchol/home/holuser/snap/firefox/common/.mozilla/firefox'
LMC_ROOT = '/lmchol'

# certutil tool (from libnss3-tools package)
CERTUTIL_BINARY = 'certutil'


def check_certutil_installed() -> bool:
    """
    Check if certutil is installed on the system.
    
    certutil is part of the libnss3-tools package and is required to
    manage Firefox's certificate store (cert9.db).
    
    :return: True if certutil is available
    """
    import shutil
    return shutil.which(CERTUTIL_BINARY) is not None


def install_certutil(dry_run: bool = False) -> bool:
    """
    Install libnss3-tools package which provides certutil.
    
    certutil is the official tool for managing NSS certificate databases
    used by Firefox, Thunderbird, and other Mozilla-based applications.
    
    REQUIRED PACKAGE: libnss3-tools
    
    To install manually (if apt is unavailable):
        sudo apt-get install libnss3-tools
    
    Or download from Ubuntu archives:
        wget http://archive.ubuntu.com/ubuntu/pool/main/n/nss/libnss3-tools_3.98-1build1_amd64.deb
        sudo dpkg -i libnss3-tools_3.98-1build1_amd64.deb
    
    :param dry_run: If True, only show what would be done
    :return: True if installation successful
    """
    if check_certutil_installed():
        lsf.write_output('certutil is already installed')
        return True
    
    if dry_run:
        lsf.write_output('Would install libnss3-tools package (provides certutil)')
        return True
    
    lsf.write_output('Installing libnss3-tools package (provides certutil)...')
    
    try:
        result = lsf.run_command('sudo apt-get update && sudo apt-get install -y libnss3-tools')
        if result.returncode == 0:
            lsf.write_output('libnss3-tools installed successfully')
            return True
        else:
            lsf.write_output('ERROR: Failed to install libnss3-tools via apt')
            lsf.write_output('')
            lsf.write_output('To install manually, run:')
            lsf.write_output('  sudo apt-get install libnss3-tools')
            lsf.write_output('')
            lsf.write_output('Or download and install the .deb package:')
            lsf.write_output('  wget http://archive.ubuntu.com/ubuntu/pool/main/n/nss/libnss3-tools_3.98-1build1_amd64.deb')
            lsf.write_output('  sudo dpkg -i libnss3-tools_3.98-1build1_amd64.deb')
            return False
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to install libnss3-tools: {e}')
        return False


def check_vault_accessible(vault_url: str = VAULT_URL, 
                           ca_path: str = VAULT_CA_PATH,
                           timeout: int = 5) -> Tuple[bool, str]:
    """
    Check if the Vault PKI CA certificate is accessible.
    
    This performs a quick check to see if the Vault server is reachable
    and the PKI CA endpoint is responding before attempting the full import.
    
    :param vault_url: Vault server URL
    :param ca_path: Path to CA certificate endpoint
    :param timeout: Connection timeout in seconds
    :return: Tuple of (accessible: bool, message: str)
    """
    url = f"{vault_url.rstrip('/')}{ca_path}"
    
    try:
        response = requests.get(url, timeout=timeout)
        
        if response.status_code == 200:
            ca_pem = response.text.strip()
            if ca_pem.startswith('-----BEGIN CERTIFICATE-----'):
                return True, "Vault PKI CA is accessible"
            else:
                return False, "Vault responded but CA certificate format is invalid"
        else:
            return False, f"Vault responded with HTTP {response.status_code}"
            
    except requests.exceptions.ConnectTimeout:
        return False, f"Connection timeout - Vault server not responding at {vault_url}"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection error - Cannot reach Vault at {vault_url}"
    except Exception as e:
        return False, f"Error checking Vault: {e}"


def prompt_vault_unavailable(message: str) -> str:
    """
    Prompt user for action when Vault CA is not accessible.
    
    Presents options to:
    - [S]kip: Continue without importing Vault CA
    - [R]etry: Try checking Vault again (user may have fixed it)
    - [F]ail: Exit the script with an error
    
    :param message: Error message describing why Vault is not accessible
    :return: User's choice: 'skip', 'retry', or 'fail'
    """
    print('')
    print('!' * 60)
    print('  WARNING: Vault PKI CA Certificate Not Accessible')
    print('!' * 60)
    print('')
    print(f'  {message}')
    print('')
    print('  The Vault root CA certificate is used to establish trust')
    print('  for VCF component certificates in Firefox on the console VM.')
    print('')
    print('  Options:')
    print('    [S]kip  - Continue without importing Vault CA')
    print('              (Firefox will show certificate warnings)')
    print('    [R]etry - Check Vault again (if you have fixed the issue)')
    print('    [F]ail  - Exit the script with an error')
    print('')
    
    while True:
        choice = input('  Enter choice [S/R/F]: ').strip().upper()
        if choice in ['S', 'SKIP']:
            return 'skip'
        elif choice in ['R', 'RETRY']:
            return 'retry'
        elif choice in ['F', 'FAIL']:
            return 'fail'
        else:
            print('  Invalid choice. Please enter S, R, or F.')


def download_vault_ca_certificate(vault_url: str = VAULT_URL, 
                                   ca_path: str = VAULT_CA_PATH) -> Optional[str]:
    """
    Download the root CA certificate from HashiCorp Vault PKI.
    
    The Vault PKI secrets engine exposes the CA certificate at /v1/pki/ca/pem.
    This endpoint does not require authentication.
    
    :param vault_url: Vault server URL (default: http://10.1.1.1:32000)
    :param ca_path: Path to CA certificate endpoint (default: /v1/pki/ca/pem)
    :return: PEM-encoded CA certificate or None on failure
    """
    url = f"{vault_url.rstrip('/')}{ca_path}"
    lsf.write_output(f'Downloading root CA from Vault: {url}')
    
    try:
        response = requests.get(url, timeout=30)
        
        if response.status_code == 200:
            ca_pem = response.text.strip()
            if ca_pem.startswith('-----BEGIN CERTIFICATE-----'):
                lsf.write_output('Successfully downloaded Vault root CA certificate')
                return ca_pem
            else:
                lsf.write_output('ERROR: Invalid certificate format received from Vault')
                return None
        else:
            lsf.write_output(f'ERROR: Failed to download CA from Vault: HTTP {response.status_code}')
            return None
            
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to connect to Vault: {e}')
        return None


def find_firefox_profiles(profile_base: str = LMC_FIREFOX_PROFILE_BASE) -> list:
    """
    Find all Firefox profile directories containing a cert9.db file.
    
    Firefox uses NSS (Network Security Services) for certificate management.
    The certificate database is stored in cert9.db within each profile directory.
    
    :param profile_base: Base path to Firefox profiles
    :return: List of profile directory paths
    """
    profiles = []
    
    if not os.path.isdir(profile_base):
        lsf.write_output(f'WARNING: Firefox profile directory not found: {profile_base}')
        return profiles
    
    # Look for directories containing cert9.db
    for entry in os.listdir(profile_base):
        profile_path = os.path.join(profile_base, entry)
        cert_db = os.path.join(profile_path, 'cert9.db')
        
        if os.path.isdir(profile_path) and os.path.isfile(cert_db):
            profiles.append(profile_path)
            lsf.write_output(f'Found Firefox profile: {entry}')
    
    return profiles


def import_ca_to_firefox_profile(ca_pem: str, profile_path: str, 
                                  ca_name: str = VAULT_CA_NAME,
                                  dry_run: bool = False) -> bool:
    """
    Import a CA certificate into a Firefox profile's NSS certificate store.
    
    Uses certutil to add the certificate as a trusted CA for:
    - SSL/TLS server authentication (C)
    - Email signing (not enabled)
    - Code signing (not enabled)
    
    The trust flags "CT,," mean:
    - C: Valid CA for SSL/TLS connections
    - T: Trusted for client authentication (allows the CA to issue client certs)
    - (empty): Not trusted for email or code signing
    
    :param ca_pem: PEM-encoded CA certificate
    :param profile_path: Path to Firefox profile directory
    :param ca_name: Friendly name for the certificate
    :param dry_run: If True, only show what would be done
    :return: True if import successful
    """
    import tempfile
    import subprocess
    
    if dry_run:
        lsf.write_output(f'Would import "{ca_name}" to Firefox profile: {profile_path}')
        return True
    
    # Write CA to a temporary file
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
            f.write(ca_pem)
            ca_file = f.name
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to create temp file for CA certificate: {e}')
        return False
    
    try:
        # Check if certificate already exists and delete it first (to update)
        check_cmd = [
            CERTUTIL_BINARY, '-L',
            '-d', f'sql:{profile_path}',
            '-n', ca_name
        ]
        
        result = subprocess.run(check_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            # Certificate exists, delete it first to allow update
            lsf.write_output(f'Certificate "{ca_name}" already exists, updating...')
            delete_cmd = [
                CERTUTIL_BINARY, '-D',
                '-d', f'sql:{profile_path}',
                '-n', ca_name
            ]
            subprocess.run(delete_cmd, capture_output=True)
        
        # Import the CA certificate
        # Trust flags: C,, = trusted CA for SSL/TLS, not for email or code signing
        import_cmd = [
            CERTUTIL_BINARY, '-A',
            '-d', f'sql:{profile_path}',
            '-n', ca_name,
            '-t', 'CT,,',  # Trusted CA for SSL and client auth
            '-i', ca_file
        ]
        
        lsf.write_output(f'Importing CA to Firefox profile: {os.path.basename(profile_path)}')
        result = subprocess.run(import_cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            lsf.write_output(f'Successfully imported "{ca_name}" to Firefox')
            return True
        else:
            lsf.write_output(f'ERROR: certutil failed: {result.stderr}')
            return False
            
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to import CA certificate: {e}')
        return False
        
    finally:
        # Clean up temp file
        try:
            os.unlink(ca_file)
        except:
            pass


def configure_vault_ca_for_firefox(dry_run: bool = False, 
                                    skip_vault_check: bool = False) -> bool:
    """
    Download the Vault root CA and import it into Firefox on the console VM.
    
    This function:
    1. Checks if Vault PKI CA is accessible (with SKIP/RETRY/FAIL options)
    2. Ensures certutil is installed (from libnss3-tools package)
    3. Downloads the root CA certificate from the Vault PKI endpoint
    4. Finds all Firefox profiles on the console VM (/lmchol filesystem)
    5. Imports the CA as a trusted authority in each profile
    
    After running this function, Firefox on the console VM will trust
    certificates signed by the Vault PKI without showing security warnings.
    
    PREREQUISITES:
    - libnss3-tools package must be installed (provides certutil)
    - Vault server must be accessible at VAULT_URL
    - Firefox profile must exist on the console VM
    
    :param dry_run: If True, preview what would be done
    :param skip_vault_check: If True, skip the initial Vault accessibility check
    :return: True if successful, False if failed, None if skipped
    """
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('Vault Root CA Import for Firefox')
    lsf.write_output('=' * 60)
    
    # Step 1: Check if Vault is accessible (with retry loop)
    if not dry_run and not skip_vault_check:
        lsf.write_output(f'Checking Vault PKI accessibility at {VAULT_URL}...')
        
        while True:
            accessible, message = check_vault_accessible()
            
            if accessible:
                lsf.write_output(f'✓ {message}')
                break
            else:
                # Vault not accessible - prompt user for action
                choice = prompt_vault_unavailable(message)
                
                if choice == 'skip':
                    lsf.write_output('Skipping Vault CA import (user choice)')
                    lsf.write_output('NOTE: Firefox will show certificate warnings for VCF components')
                    return True  # Return True to not fail the overall process
                elif choice == 'retry':
                    lsf.write_output('Retrying Vault accessibility check...')
                    continue  # Loop and check again
                elif choice == 'fail':
                    lsf.write_output('Exiting due to Vault unavailability (user choice)')
                    return False
    elif dry_run:
        lsf.write_output(f'Would check Vault PKI accessibility at {VAULT_URL}')
    
    # Step 2: Ensure certutil is installed
    if not dry_run:
        if not check_certutil_installed():
            if not install_certutil(dry_run):
                lsf.write_output('ERROR: Cannot proceed without certutil')
                lsf.write_output('Please install libnss3-tools: sudo apt-get install libnss3-tools')
                return False
    else:
        if not check_certutil_installed():
            lsf.write_output('Would install libnss3-tools package if not present')
    
    # Step 3: Download the root CA from Vault
    if dry_run:
        lsf.write_output(f'Would download root CA from: {VAULT_URL}{VAULT_CA_PATH}')
        ca_pem = None
    else:
        ca_pem = download_vault_ca_certificate()
        if not ca_pem:
            lsf.write_output('ERROR: Failed to download Vault root CA')
            return False
    
    # Step 4: Find Firefox profiles on the console VM
    profiles = find_firefox_profiles()
    
    if not profiles:
        lsf.write_output('WARNING: No Firefox profiles found on console VM')
        lsf.write_output(f'Expected location: {LMC_FIREFOX_PROFILE_BASE}')
        return False
    
    lsf.write_output(f'Found {len(profiles)} Firefox profile(s)')
    
    # Step 5: Import CA to each Firefox profile
    success_count = 0
    for profile_path in profiles:
        if dry_run:
            lsf.write_output(f'Would import CA to: {profile_path}')
            success_count += 1
        else:
            if import_ca_to_firefox_profile(ca_pem, profile_path, VAULT_CA_NAME, dry_run):
                success_count += 1
    
    if success_count == len(profiles):
        lsf.write_output('')
        lsf.write_output(f'Successfully imported Vault root CA to {success_count} Firefox profile(s)')
        lsf.write_output('Firefox will now trust certificates signed by the Vault PKI')
        return True
    else:
        lsf.write_output(f'WARNING: Only imported to {success_count}/{len(profiles)} profiles')
        return False


#==============================================================================
# FINAL CLEANUP
#==============================================================================

def perform_final_cleanup(dry_run: bool = False) -> bool:
    """
    Perform final cleanup tasks after HOLification.
    
    This includes:
    1. Clear ARP cache on console and router
    2. Run vpodchecker.py to update L2 VM settings
    
    :param dry_run: If True, preview only
    :return: True if successful
    """
    password = lsf.get_password()
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('Final Cleanup')
    lsf.write_output('=' * 60)
    
    # Clear ARP cache on console and router
    for machine in ['console', 'router']:
        if not dry_run:
            lsf.write_output(f'{machine}: Clearing ARP cache')
            lsf.ssh('ip -s -s neigh flush all', f'root@{machine}', password)
        else:
            lsf.write_output(f'{machine}: Would clear ARP cache')
    
    # Run vpodchecker.py to update L2 VMs
    vpodchecker = os.path.expanduser('~/hol/Tools/vpodchecker.py')
    if os.path.isfile(vpodchecker):
        if not dry_run:
            lsf.write_output('Running vpodchecker.py to update L2 VM settings...')
            os.system(f'python3 {vpodchecker}')
        else:
            lsf.write_output('Would run vpodchecker.py')
    else:
        lsf.write_output('WARNING: vpodchecker.py not found')
    
    return True


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main():
    """
    Main entry point for HOLification tool.
    
    Orchestrates all HOLification steps in the correct order:
    0. Vault root CA import to Firefox on console VM (with SKIP/RETRY/FAIL options)
    1. Pre-checks and environment setup
    2. ESXi host configuration
    3. vCenter configuration
    4. NSX configuration
    5. SDDC Manager configuration
    6. Operations VMs configuration
    7. Final cleanup
    """
    parser = argparse.ArgumentParser(
        description='HOLFY27 vApp HOLification Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script automates the HOLification process for vApp templates.
It must be run after the Holodeck factory build completes.

Examples:
  python3 confighol.py                    Full interactive HOLification
  python3 confighol.py --dry-run          Preview what would be done
  python3 confighol.py --skip-vcshell     Skip vCenter shell configuration
  python3 confighol.py --skip-nsx         Skip NSX configuration
  python3 confighol.py --esx-only         Only configure ESXi hosts

Prerequisites:
  - Complete successful LabStartup reaching Ready state
  - Valid /tmp/config.ini with all resources defined
  - 'expect' utility installed (/usr/bin/expect)

NOTE: Some NSX operations require manual steps first.
      See HOLIFICATION.md for complete instructions.
        """
    )
    
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview what would be done without making changes')
    parser.add_argument('--skip-vcshell', action='store_true',
                        help='Skip vCenter shell configuration')
    parser.add_argument('--skip-nsx', action='store_true',
                        help='Skip NSX configuration')
    parser.add_argument('--esx-only', action='store_true',
                        help='Only configure ESXi hosts')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {SCRIPT_VERSION}')
    
    args = parser.parse_args()
    
    # Print banner
    print('')
    print('=' * 60)
    print('  HOLFY27 vApp HOLification Tool')
    print(f'  Version {SCRIPT_VERSION}')
    print('=' * 60)
    print('')
    
    if args.dry_run:
        print('DRY RUN MODE - No changes will be made')
        print('')
    
    # Initialize lsfunctions
    lsf.init(router=False)
    password = lsf.get_password()
    
    # Pre-checks
    if not os.path.exists('/usr/bin/expect'):
        lsf.write_output("ERROR: 'expect' utility not found. Please install expect.")
        sys.exit(1)
    
    lsf.write_output("Pre-check: 'expect' utility is present")
    
    # Step 0: Import Vault root CA to Firefox on console VM (at the beginning)
    # This allows the user to skip/retry/fail early if Vault is not accessible
    if not configure_vault_ca_for_firefox(args.dry_run):
        lsf.write_output('ERROR: Failed to configure Vault CA for Firefox')
        sys.exit(1)
    
    # Setup SSH environment
    setup_ssh_environment()
    
    # Create authorized_keys file
    auth_keys_file = create_authorized_keys_file()
    if not auth_keys_file:
        lsf.write_output('ERROR: Failed to create authorized_keys file')
        sys.exit(1)
    
    # Get configuration sections
    vcenters = []
    if 'vCenters' in lsf.config['RESOURCES']:
        vcenters = lsf.config.get('RESOURCES', 'vCenters').split('\n')
    
    esx_hosts = []
    if 'ESXiHosts' in lsf.config['RESOURCES']:
        esx_hosts = lsf.config.get('RESOURCES', 'ESXiHosts').split('\n')
    
    # Connect to vCenters (needed for ESXi API access)
    if vcenters and not args.dry_run:
        lsf.connect_vcenters(vcenters)
    
    # Step 1: Configure ESXi hosts
    esx_results = configure_all_esxi_hosts(esx_hosts, auth_keys_file, args.dry_run)
    
    if args.esx_only:
        # Print summary and exit
        print('')
        print('=' * 60)
        print('ESXi Configuration Summary')
        print('=' * 60)
        print(f'  Successful: {esx_results["success"]}')
        print(f'  Failed: {esx_results["failed"]}')
        sys.exit(0 if esx_results['failed'] == 0 else 1)
    
    # Ensure password is available (exit if not)
    if not password:
        lsf.write_output('ERROR: No password available from creds.txt')
        sys.exit(1)
    
    # Step 2: Configure vCenters
    for entry in vcenters:
        if not entry or entry.strip().startswith('#'):
            continue
        configure_vcenter(entry, auth_keys_file, password, 
                         args.skip_vcshell, args.dry_run)
    
    # Step 3: Configure NSX components
    configure_nsx_components(auth_keys_file, password, args.skip_nsx, args.dry_run)
    
    # Step 4: Configure SDDC Manager
    configure_sddc_manager(auth_keys_file, password, args.dry_run)
    
    # Step 5: Configure Operations VMs
    configure_operations_vms(auth_keys_file, password, args.dry_run)
    
    # Step 6: Final cleanup
    perform_final_cleanup(args.dry_run)
    
    # Print summary
    print('')
    print('=' * 60)
    print('HOLification Complete')
    print('=' * 60)
    print('')
    print('IMPORTANT: Review HOLIFICATION.md for any manual steps required,')
    print('particularly for NSX Edge SSH configuration which must be done')
    print('via the vSphere console.')
    print('')


if __name__ == '__main__':
    main()
