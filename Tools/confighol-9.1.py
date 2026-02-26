#!/usr/bin/env python3
# confighol-9.1.py - HOLFY27 vApp HOLification Tool
# Version 2.3 - February 26, 2026
# Author - Burke Azbill and HOL Core Team
#
# Script Naming Convention:
# This script is named according to the VCF version it was developed and
# tested against: confighol-9.1.py for VCF 9.1.x. Future VCF versions may
# require a new script version (e.g., confighol-9.5.py for VCF 9.5.x).
#
# CHANGELOG:
# v2.4 - 2026-02-26:
#   - NSX Edge SSH now enabled via NSX Manager transport node API
#     (fixes systemctl exit code 5 on Edge appliances where sshd is
#     managed by the NSX control plane, not systemd)
#   - NSX CLI SSH commands now use -T flag to disable PTY allocation
#     (fixes connection reset on Edge nodes for set service/user commands)
#   - VCF Automation (auto-platform-a) sudo now uses -S flag to pipe password
#     (fixes "a terminal is required to read the password" error)
# v2.3 - 2026-02-26:
#   - NSX Edge SSH initially attempted via vSphere Guest Operations API
#   - Operations VMs SSH now enabled via Guest Operations when port 22 is closed
#   - NSX Manager root password: auto-recovers SDDC Manager rotated passwords
#     (queries SDDC Manager credentials API, resets to standard lab password)
#   - Unreachable Operations VMs (e.g. not deployed) are skipped gracefully
#   - Removed interactive prompt for NSX Edge SSH confirmation
# v2.2 - 2026-02:
#   - Initial release for VCF 9.1
#
# This script automates the "HOLification" process for vApp templates
# that will be used in VMware Hands-on Labs. It must be run after the
# Holodeck 9.x factory build process completes.
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
# - 'libnss3-tools' package installed on the Manager (provides certutil)
# - SSH access to all target systems using lab password
#
# CAPABILITIES:
# 0a. Vault Root CA Import (runs first with SKIP/RETRY/FAIL options):
#    - Checks if HashiCorp Vault PKI is accessible
#    - Downloads root CA certificate from Vault
#    - Imports CA as trusted authority in Firefox on console VM
#    - Requires: libnss3-tools package (provides certutil)
#
# 0b. vCenter CA Import (runs after Vault CA, with SKIP/RETRY/FAIL options):
#    - Reads vCenter list from /tmp/config.ini
#    - Downloads CA certificates from each vCenter's /certs/download.zip endpoint
#    - Imports each CA as trusted authority in Firefox on console VM
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
#    - Enable SSH via API on NSX Managers (direct API call)
#    - Enable SSH via NSX Manager transport node API on NSX Edges
#    - Configure SSH authorized_keys for passwordless access
#    - Set 729-day password expiration for admin, root, audit users
#    - Auto-recover SDDC Manager rotated root passwords
#
# 4. SDDC Manager Configuration:
#    - Configure SSH authorized_keys
#    - Set non-expiring passwords for vcf, backup, root accounts
#
# 5. Operations VMs:
#    - Enable SSH via Guest Operations (if not already running)
#    - Set non-expiring passwords
#    - Configure SSH authorized_keys
#
# 6. SDDC Manager Auto-Rotate Disable:
#    - Queries SDDC Manager API for credentials with auto-rotate policies
#    - Disables auto-rotation for all service credentials
#    - Prevents failed password rotation tasks after template deployment
#
# 7. Final Steps:
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
# NOTE: Some operations (vCenter shell) may require manual confirmation.
#       NSX Edge SSH is now enabled automatically via Guest Operations.
#       See HOLIFICATION.md for details.

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
import zipfile
import io
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

SCRIPT_VERSION = '2.4'
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

# NSX password expiration (729 days = ~2 years, matches Fleet MaxExpiration policy)
NSX_PASSWORD_EXPIRY_DAYS = 729

# Password expiration setting for vCenter (9999 days ~ 27 years)
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
    # NOTE: ESXi does not support the 'chage' command (it uses BusyBox).
    # Password expiration on ESXi is handled via advanced settings or host profile,
    # but not via standard Linux user management commands.
    # Disabling this step as requested.
    # if not dry_run:
    #     lsf.write_output(f'{hostname}: Setting non-expiring password for root')
    #     result = lsf.ssh(f'chage -M {PASSWORD_MAX_DAYS} root', f'{ESX_USERNAME}@{hostname}', password)
    #     if result.returncode != 0:
    #         lsf.write_output(f'{hostname}: WARNING - Failed to set password expiration')
    # else:
    #     lsf.write_output(f'{hostname}: Would set password expiration to {PASSWORD_MAX_DAYS} days')
    
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
        # Create a fresh pyVmomi connection to ensure we're authenticated
        lsf.write_output(f'{hostname}: Connecting to vSphere API for cluster configuration...')
        
        si = None
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            si = connect.SmartConnect(
                host=hostname,
                user=user,
                pwd=password,
                sslContext=context
            )
            lsf.write_output(f'{hostname}: SUCCESS - Connected to vSphere API')
        except Exception as conn_err:
            lsf.write_output(f'{hostname}: FAILED - Could not connect to vSphere API: {conn_err}')
            si = None
        
        if si:
            try:
                content = si.RetrieveContent()
                
                # Get all clusters
                container = content.viewManager.CreateContainerView(
                    content.rootFolder, [vim.ClusterComputeResource], True
                )
                
                for cluster in container.view:
                    lsf.write_output(f'{hostname}: Configuring cluster {cluster.name}...')
                    
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
                        
                        lsf.write_output(f'{hostname}: SUCCESS - Cluster {cluster.name} configured')
                        
                    except Exception as e:
                        lsf.write_output(f'{hostname}: FAILED - Could not configure cluster {cluster.name}: {e}')
                
                container.Destroy()
                
                # Disconnect from vSphere
                connect.Disconnect(si)
                
            except Exception as e:
                lsf.write_output(f'{hostname}: ERROR - vSphere API error: {e}')
                if si:
                    try:
                        connect.Disconnect(si)
                    except Exception:
                        pass
        else:
            lsf.write_output(f'{hostname}: WARNING - Skipping cluster configuration (no vSphere connection)')
        
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

def get_nsx_root_password_from_sddc(nsx_fqdn: str, password: str) -> Optional[str]:
    """
    Retrieve the actual NSX Manager root SSH password from SDDC Manager.
    
    SDDC Manager may have rotated the root password away from the standard
    lab password. This queries the SDDC Manager credentials API to get the
    current password.
    
    :param nsx_fqdn: NSX Manager FQDN (individual node, e.g. nsx-wld01-01a)
    :param password: Standard lab password (used to auth to SDDC Manager)
    :return: The actual root password, or None if lookup fails
    """
    sddc_host = 'sddcmanager-a.site-a.vcf.lab'
    
    try:
        # Map individual node name to VIP/cluster name for SDDC Manager lookup
        # e.g. nsx-wld01-01a -> nsx-wld01-a, nsx-mgmt-01a -> nsx-mgmt-a
        import re
        # Remove node number suffix (e.g. -01a -> -a) to get cluster VIP name
        cluster_name = re.sub(r'-\d+a\.', '-a.', nsx_fqdn)
        if cluster_name == nsx_fqdn:
            cluster_name = re.sub(r'-\d+b\.', '-b.', nsx_fqdn)
        
        # Get SDDC Manager token
        get_token_cmd = (
            f'curl -sk -X POST https://localhost/v1/tokens '
            f'-H "Content-Type: application/json" '
            f'-d \'{{"username":"admin@local","password":"{password}"}}\''
        )
        token_result = lsf.ssh(
            f'{get_token_cmd} | python3 -c "import json,sys; print(json.load(sys.stdin)[\'accessToken\'])"',
            f'vcf@{sddc_host}', password)
        
        if token_result.returncode != 0:
            return None
        
        token = str(token_result.stdout).strip() if hasattr(token_result, 'stdout') and token_result.stdout else str(token_result).strip()
        # Extract just the token from output
        for line in token.split('\n'):
            line = line.strip()
            if line and not line.startswith('Warning') and not line.startswith('Welcome') and len(line) > 50:
                token = line
                break
        
        # Query credentials for NSX managers
        cred_cmd = (
            f'curl -sk -X GET "https://localhost/v1/credentials?resourceType=NSXT_MANAGER" '
            f'-H "Authorization: Bearer {token}" '
            f'-H "Content-Type: application/json"'
        )
        cred_result = lsf.ssh(cred_cmd, f'vcf@{sddc_host}', password)
        
        if cred_result.returncode != 0:
            return None
        
        import json
        output = str(cred_result.stdout) if hasattr(cred_result, 'stdout') and cred_result.stdout else str(cred_result)
        # Find the JSON portion
        json_start = output.find('{"elements"')
        if json_start < 0:
            return None
        
        data = json.loads(output[json_start:])
        
        for elem in data.get('elements', []):
            resource = elem.get('resource', {})
            rname = resource.get('resourceName', '')
            cred_type = elem.get('credentialType', '')
            username = elem.get('username', '')
            
            if cred_type == 'SSH' and username == 'root':
                if cluster_name in rname or nsx_fqdn in rname:
                    return elem.get('password', None)
        
        return None
    except Exception as e:
        lsf.write_output(f'WARNING: Could not retrieve NSX root password from SDDC Manager: {e}')
        return None


def reset_nsx_root_password(hostname: str, admin_password: str,
                             old_root_password: str, new_root_password: str) -> bool:
    """
    Reset NSX Manager root password via the NSX API.
    
    Uses the admin user to authenticate and change root's password.
    
    :param hostname: NSX Manager hostname
    :param admin_password: Admin user password
    :param old_root_password: Current root password
    :param new_root_password: Desired new root password
    :return: True if successful
    """
    try:
        resp = requests.put(
            f'https://{hostname}/api/v1/node/users/0',
            auth=('admin', admin_password),
            json={'password': new_root_password, 'old_password': old_root_password},
            verify=False, timeout=30
        )
        if resp.status_code == 200:
            lsf.write_output(f'{hostname}: SUCCESS - Root password reset to standard')
            return True
        else:
            error_msg = resp.json().get('error_message', resp.text[:100]) if resp.text else 'Unknown error'
            lsf.write_output(f'{hostname}: FAILED - Password reset: {error_msg}')
            return False
    except Exception as e:
        lsf.write_output(f'{hostname}: FAILED - Password reset error: {e}')
        return False


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


def nsx_cli_ssh(command: str, hostname: str, password: str) -> 'subprocess.CompletedProcess':
    """
    Execute an NSX CLI command via SSH with the -T flag.
    
    NSX appliances (especially Edges) close the connection when a PTY is
    allocated for non-interactive CLI commands. The -T flag disables PTY
    allocation and is required for reliable automated CLI access.
    
    :param command: NSX CLI command (e.g. 'set service ssh start-on-boot')
    :param hostname: NSX hostname
    :param password: Admin password
    :return: subprocess.CompletedProcess
    """
    options = 'StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -T'
    return lsf.ssh(command, f'admin@{hostname}', password, options=options)


def configure_nsx_ssh_start_on_boot(hostname: str, password: str,
                                     dry_run: bool = False) -> bool:
    """
    Configure NSX SSH service to start on boot via CLI command.
    
    The NSX API does not support setting start-on-boot directly, so we
    must use SSH to run the CLI command:
    - set service ssh start-on-boot
    
    Uses -T flag to disable PTY allocation (required for NSX Edge CLI).
    
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
    
    result = nsx_cli_ssh('set service ssh start-on-boot', hostname, password)
    
    if result.returncode == 0:
        lsf.write_output(f'{hostname}: SSH start-on-boot configured')
        return True
    else:
        lsf.write_output(f'{hostname}: WARNING - Failed to configure start-on-boot')
        return False


def configure_nsx_manager(hostname: str, auth_keys_file: str, password: str,
                          dry_run: bool = False) -> bool:
    """
    Configure an NSX Manager node for HOLification.
    
    This function:
    1. Attempts to enable SSH via API (NSX Managers support this)
    2. Copies authorized_keys for passwordless SSH access
    3. Configures SSH to start on boot
    4. Removes password expiration for admin, root, audit users
    
    :param hostname: NSX Manager hostname
    :param auth_keys_file: Path to authorized_keys file
    :param password: Admin/root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    lsf.write_output(f'{hostname}: Configuring NSX Manager...')
    
    success = True
    
    # Step 1: Try to enable SSH via API (only NSX Managers support this)
    if not enable_nsx_ssh_via_api(hostname, 'admin', password, dry_run):
        lsf.write_output(f'{hostname}: WARNING - API SSH enablement failed')
        if not dry_run:
            lsf.write_output(f'{hostname}: Please enable SSH manually via vSphere Remote Console:')
            lsf.write_output(f'{hostname}:   1. Login as admin')
            lsf.write_output(f'{hostname}:   2. Run: start service ssh')
            lsf.write_output(f'{hostname}:   3. Run: set service ssh start-on-boot')
            answer = input(f'{hostname}: Is SSH enabled now? (y/n): ')
            if not answer.lower().startswith('y'):
                lsf.write_output(f'{hostname}: Skipping configuration - SSH not enabled')
                return False

    if not dry_run:
        # Give SSH service time to start
        time.sleep(3)
        
        # Determine root password (may have been rotated by SDDC Manager)
        root_password = password
        lsf.write_output(f'{hostname}: Copying authorized_keys...')
        result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', password)
        if result.returncode != 0:
            lsf.write_output(f'{hostname}: Standard password failed for root SSH - checking SDDC Manager...')
            sddc_root_pw = get_nsx_root_password_from_sddc(hostname, password)
            if sddc_root_pw and sddc_root_pw != password:
                lsf.write_output(f'{hostname}: Found rotated root password in SDDC Manager')
                # Reset to standard password via NSX API
                if reset_nsx_root_password(hostname, password, sddc_root_pw, password):
                    root_password = password
                    time.sleep(3)
                    result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', root_password)
                else:
                    root_password = sddc_root_pw
                    result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', root_password)
        
        if result.returncode == 0:
            lsf.write_output(f'{hostname}: SUCCESS - authorized_keys copied')
            lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', root_password)
        else:
            lsf.write_output(f'{hostname}: FAILED - Could not copy authorized_keys')
            success = False
        
        # Step 3: Configure SSH to start on boot
        configure_nsx_ssh_start_on_boot(hostname, password, dry_run)
        
        # Step 4: Set password expiration for NSX users (729 days)
        for user in NSX_USERS:
            lsf.write_output(f'{hostname}: Setting {NSX_PASSWORD_EXPIRY_DAYS}-day password expiration for {user}...')
            result = nsx_cli_ssh(f'set user {user} password-expiration {NSX_PASSWORD_EXPIRY_DAYS}', hostname, password)
            if result.returncode == 0:
                lsf.write_output(f'{hostname}: SUCCESS - {user} password expiration set to {NSX_PASSWORD_EXPIRY_DAYS} days')
            else:
                lsf.write_output(f'{hostname}: WARNING - Failed to set password expiration for {user}')
    else:
        lsf.write_output(f'{hostname}: Would enable SSH via API')
        lsf.write_output(f'{hostname}: Would copy authorized_keys (with SDDC Manager password fallback)')
        lsf.write_output(f'{hostname}: Would configure SSH start-on-boot')
        for user in NSX_USERS:
            lsf.write_output(f'{hostname}: Would set {NSX_PASSWORD_EXPIRY_DAYS}-day password expiration for {user}')
    
    return success


def _get_nsx_manager_for_edge(edge_hostname: str) -> Optional[str]:
    """
    Determine which NSX Manager manages a given edge node by name convention.
    
    Edge names follow the pattern edge-{domain}-{num}{site} where domain
    matches the NSX Manager pattern nsx-{domain}-{num}{site}.
    For example: edge-wld01-01a -> nsx-wld01-01a (or any nsx-wld01-* manager)
    
    Falls back to trying all configured NSX Managers via API.
    
    :param edge_hostname: NSX Edge hostname (e.g. edge-wld01-01a)
    :return: NSX Manager FQDN, or None if not found
    """
    import re
    
    if 'VCF' not in lsf.config or 'vcfnsxmgr' not in lsf.config['VCF']:
        return None
    
    vcfnsxmgrs = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
    nsx_managers = []
    for entry in vcfnsxmgrs:
        if not entry or entry.strip().startswith('#'):
            continue
        parts = entry.split(':')
        nsx_managers.append(parts[0].strip())
    
    # Try name-based matching: edge-wld01-01a -> nsx-wld01-*
    edge_match = re.match(r'edge-(\w+)-\d+', edge_hostname)
    if edge_match:
        edge_domain = edge_match.group(1)
        for mgr in nsx_managers:
            if edge_domain in mgr:
                return mgr
    
    return nsx_managers[0] if nsx_managers else None


def enable_nsx_edge_ssh_via_api(edge_hostname: str, nsx_manager: str,
                                 password: str,
                                 dry_run: bool = False) -> bool:
    """
    Enable SSH on an NSX Edge node via the central NSX Manager API.
    
    NSX Edges are managed as transport nodes by their NSX Manager. The SSH
    service on edges must be controlled through the NSX Manager's transport
    node API, NOT via systemctl (which returns exit code 5 on NSX Edges
    because sshd is managed by the NSX control plane).
    
    API endpoint:
    POST /api/v1/transport-nodes/{node-id}/node/services/ssh?action=start
    
    :param edge_hostname: NSX Edge hostname (e.g. edge-wld01-01a)
    :param nsx_manager: NSX Manager FQDN that manages this edge
    :param password: Admin password for NSX Manager API
    :param dry_run: If True, preview only
    :return: True if SSH is now enabled
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    if dry_run:
        lsf.write_output(f'{edge_hostname}: Would enable SSH via NSX Manager API ({nsx_manager})')
        return True
    
    lsf.write_output(f'{edge_hostname}: Enabling SSH via NSX Manager API ({nsx_manager})...')
    
    try:
        # Find the edge's transport node ID
        tn_url = f'https://{nsx_manager}/api/v1/transport-nodes'
        resp = requests.get(tn_url, auth=('admin', password), verify=False, timeout=30)
        if resp.status_code != 200:
            lsf.write_output(f'{edge_hostname}: Failed to query transport nodes: HTTP {resp.status_code}')
            return False
        
        node_id = None
        for node in resp.json().get('results', []):
            if node.get('display_name', '') == edge_hostname:
                node_id = node.get('node_id', node.get('id'))
                break
        
        if not node_id:
            lsf.write_output(f'{edge_hostname}: Edge not found as transport node in {nsx_manager}')
            return False
        
        lsf.write_output(f'{edge_hostname}: Found transport node ID: {node_id}')
        
        # Start SSH service via the transport node API
        ssh_url = f'https://{nsx_manager}/api/v1/transport-nodes/{node_id}/node/services/ssh?action=start'
        resp = requests.post(ssh_url, auth=('admin', password), verify=False, timeout=30)
        
        if resp.status_code == 200:
            result = resp.json()
            runtime_state = result.get('runtime_state', 'unknown')
            lsf.write_output(f'{edge_hostname}: SSH service state: {runtime_state}')
            return runtime_state == 'running'
        else:
            lsf.write_output(f'{edge_hostname}: Failed to start SSH: HTTP {resp.status_code}')
            return False
    
    except Exception as e:
        lsf.write_output(f'{edge_hostname}: Error enabling SSH via API: {e}')
        return False


def configure_nsx_edge(hostname: str, auth_keys_file: str, password: str,
                       esx_host: str = '', dry_run: bool = False) -> bool:
    """
    Configure an NSX Edge node for HOLification.
    
    SSH is enabled via the central NSX Manager API (transport node endpoint).
    NSX Edges use a managed sshd that cannot be controlled via systemctl;
    the NSX Manager API is the only reliable remote method.
    
    This function:
    1. Enables SSH via NSX Manager transport node API
    2. Copies authorized_keys for root user
    3. Configures SSH to start on boot (via NSX CLI over SSH)
    4. Sets 729-day password expiration for admin, root, audit users
    
    :param hostname: NSX Edge hostname
    :param auth_keys_file: Path to authorized_keys file
    :param password: Admin/root password
    :param esx_host: ESXi host the Edge runs on (unused, kept for compat)
    :param dry_run: If True, preview only
    :return: True if successful
    """
    lsf.write_output(f'{hostname}: Configuring NSX Edge...')
    
    success = True
    
    if not dry_run:
        # Step 1: Enable SSH via NSX Manager API if not already running
        if not lsf.test_tcp_port(hostname, 22):
            lsf.write_output(f'{hostname}: SSH not running - enabling via NSX Manager API...')
            nsx_mgr = _get_nsx_manager_for_edge(hostname)
            if not nsx_mgr:
                lsf.write_output(f'{hostname}: FAILED - Could not determine NSX Manager for this edge')
                return False
            
            if not enable_nsx_edge_ssh_via_api(hostname, nsx_mgr, password, dry_run):
                lsf.write_output(f'{hostname}: FAILED - Could not enable SSH via NSX Manager API')
                lsf.write_output(f'{hostname}:         Enable SSH manually via NSX Manager UI or console:')
                lsf.write_output(f'{hostname}:           Login as admin, run: start service ssh')
                lsf.write_output(f'{hostname}:           Then run: set service ssh start-on-boot')
                return False
            time.sleep(5)
            if not lsf.test_tcp_port(hostname, 22):
                lsf.write_output(f'{hostname}: FAILED - SSH still not reachable after API enable')
                return False
        else:
            lsf.write_output(f'{hostname}: SSH already running')
        
        # Step 2: Copy authorized_keys for root user
        lsf.write_output(f'{hostname}: Copying authorized_keys for root...')
        result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', password)
        if result.returncode == 0:
            lsf.write_output(f'{hostname}: SUCCESS - authorized_keys copied')
            chmod_result = lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', password)
            if chmod_result.returncode == 0:
                lsf.write_output(f'{hostname}: SUCCESS - Permissions set on authorized_keys')
            else:
                lsf.write_output(f'{hostname}: WARNING - Failed to set permissions')
        else:
            lsf.write_output(f'{hostname}: FAILED - Could not copy authorized_keys')
            if result.returncode == 255:
                lsf.write_output(f'{hostname}:         SSH connection failed despite enable attempt')
            success = False
        
        # Step 3: Configure SSH to start on boot via NSX CLI
        configure_nsx_ssh_start_on_boot(hostname, password, dry_run)
        
        # Step 4: Set password expiration for NSX users (729 days)
        for user in NSX_USERS:
            lsf.write_output(f'{hostname}: Setting {NSX_PASSWORD_EXPIRY_DAYS}-day password expiration for {user}...')
            result = nsx_cli_ssh(f'set user {user} password-expiration {NSX_PASSWORD_EXPIRY_DAYS}', hostname, password)
            if result.returncode == 0:
                lsf.write_output(f'{hostname}: SUCCESS - {user} password expiration set to {NSX_PASSWORD_EXPIRY_DAYS} days')
            else:
                lsf.write_output(f'{hostname}: WARNING - Failed to set password expiration for {user}')
    else:
        lsf.write_output(f'{hostname}: Would enable SSH via NSX Manager API (if not running)')
        lsf.write_output(f'{hostname}: Would copy authorized_keys for root')
        lsf.write_output(f'{hostname}: Would configure SSH start-on-boot')
        for user in NSX_USERS:
            lsf.write_output(f'{hostname}: Would set {NSX_PASSWORD_EXPIRY_DAYS}-day password expiration for {user}')
    
    return success


def configure_aria_automation_vms(auth_keys_file: str, password: str,
                                   dry_run: bool = False) -> bool:
    """
    Configure all VCF Automation VMs from config.ini.
    
    Processes VMs defined in the [VCFFINAL] vravms section.
    These are VCF Automation appliances that use 'vmware-system-user' for SSH.
    
    :param auth_keys_file: Path to authorized_keys file
    :param password: vmware-system-user password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if 'VCFFINAL' not in lsf.config.sections():
        lsf.write_output('No VCFFINAL section in config.ini, skipping VCF Automation')
        return True
    
    if 'vravms' not in lsf.config['VCFFINAL']:
        lsf.write_output('No vravms defined in VCFFINAL section')
        return True
    
    vravms_raw = lsf.config.get('VCFFINAL', 'vravms').strip()
    if not vravms_raw:
        lsf.write_output('No VCF Automation VMs defined')
        return True
    
    vravms = [vm.strip() for vm in vravms_raw.split('\n') if vm.strip() and not vm.strip().startswith('#')]
    
    if not vravms:
        lsf.write_output('No VCF Automation VMs found in config')
        return True
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('VCF Automation VMs Configuration')
    lsf.write_output('=' * 60)
    lsf.write_output('NOTE: These VMs use vmware-system-user for SSH access')
    lsf.write_output('      SSH is always available on VCF Automation appliances')
    
    success = True
    
    import re
    for vravm in vravms:
        # VMs may have format: vmname:vcenter
        parts = vravm.split(':')
        hostname = parts[0].strip()
        
        # Strip wildcard/regex patterns (e.g., "auto-platform-a.*" -> "auto-platform-a")
        # These patterns are used for VM name matching in vSphere but are not valid hostnames
        hostname = re.sub(r'\.\*$', '', hostname)   # Remove trailing .*
        hostname = re.sub(r'\*$', '', hostname)      # Remove trailing *
        hostname = hostname.rstrip('.')              # Remove trailing dots
        
        # Only process VMs starting with 'auto-' (VCF Automation)
        if not hostname.lower().startswith('auto-'):
            lsf.write_output(f'{hostname}: Skipping - Name does not start with "auto-"')
            continue
        
        if not configure_aria_automation(hostname, auth_keys_file, password, dry_run):
            success = False
    
    return success


def configure_aria_automation(hostname: str, auth_keys_file: str, password: str,
                               dry_run: bool = False) -> bool:
    """
    Configure VCF Automation (VCF Automation) appliance for HOLification.
    
    The VCF Automation appliance uses 'vmware-system-user' for SSH access
    with sudo NOPASSWD privileges. SSH is always available on this appliance.
    
    :param hostname: VCF Automation hostname (e.g., auto-a.site-a.vcf.lab)
    :param auth_keys_file: Path to authorized_keys file
    :param password: vmware-system-user password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    ssh_user = 'vmware-system-user'
    
    lsf.write_output(f'{hostname}: Configuring VCF Automation appliance...')
    lsf.write_output(f'{hostname}: Using SSH user: {ssh_user}')
    
    success = True
    
    if not dry_run:
        # Step 1: Copy authorized_keys for vmware-system-user
        lsf.write_output(f'{hostname}: Copying authorized_keys for {ssh_user}...')
        
        # vmware-system-user home directory
        user_auth_file = f'/home/{ssh_user}/.ssh/authorized_keys'
        
        result = lsf.scp(auth_keys_file, f'{ssh_user}@{hostname}:{user_auth_file}', password)
        if result.returncode == 0:
            lsf.write_output(f'{hostname}: SUCCESS - authorized_keys copied for {ssh_user}')
            # Set proper permissions
            chmod_result = lsf.ssh(f'chmod 600 {user_auth_file}', f'{ssh_user}@{hostname}', password)
            if chmod_result.returncode == 0:
                lsf.write_output(f'{hostname}: SUCCESS - Permissions set on authorized_keys')
            else:
                lsf.write_output(f'{hostname}: WARNING - Failed to set permissions')
        else:
            lsf.write_output(f'{hostname}: FAILED - Could not copy authorized_keys')
            if result.returncode == 255:
                lsf.write_output(f'{hostname}:         SSH connection failed')
                lsf.write_output(f'{hostname}:         User: {ssh_user}, Password provided: {"yes" if password else "no"}')
            success = False
        
        # Step 2: Copy authorized_keys for root using sudo
        lsf.write_output(f'{hostname}: Copying authorized_keys for root via sudo...')
        
        # Use sudo -S to pipe the password via stdin (vmware-system-user
        # requires password for sudo on VCF Automation appliances)
        sudo_cmd = (
            f'echo \'{password}\' | sudo -S mkdir -p /root/.ssh && '
            f'echo \'{password}\' | sudo -S cp {user_auth_file} /root/.ssh/authorized_keys && '
            f'echo \'{password}\' | sudo -S chmod 600 /root/.ssh/authorized_keys'
        )
        result = lsf.ssh(sudo_cmd, f'{ssh_user}@{hostname}', password)
        if result.returncode == 0:
            lsf.write_output(f'{hostname}: SUCCESS - authorized_keys copied for root')
        else:
            lsf.write_output(f'{hostname}: WARNING - Failed to copy authorized_keys for root')
    else:
        lsf.write_output(f'{hostname}: Would copy authorized_keys for {ssh_user}')
        lsf.write_output(f'{hostname}: Would copy authorized_keys for root via sudo')
    
    return success


def configure_nsx_components(auth_keys_file: str, password: str,
                              skip_nsx: bool = False, dry_run: bool = False) -> bool:
    """
    Configure all NSX components from config.ini.
    
    Processes NSX Managers (vcfnsxmgr) and NSX Edges (vcfnsxedges)
    defined in the [VCF] section of config.ini.
    
    NSX Managers: SSH enabled via REST API on the manager itself.
    NSX Edges: SSH enabled via NSX Manager transport node API.
    
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
        lsf.write_output('')
        lsf.write_output('Processing NSX Managers...')
        vcfnsxmgrs = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
        
        for entry in vcfnsxmgrs:
            if not entry or entry.strip().startswith('#'):
                continue
            
            # Format: nsxmgr_hostname:esxhost
            parts = entry.split(':')
            nsxmgr = parts[0].strip()
            
            if not dry_run:
                # Interactive prompt - SSH can be enabled via API for NSX Managers
                answer = input(f'Configure NSX Manager {nsxmgr}? (y/n): ')
                if not answer.lower().startswith('y'):
                    lsf.write_output(f'{nsxmgr}: Skipping')
                    continue
            
            if not configure_nsx_manager(nsxmgr, auth_keys_file, password, dry_run):
                success = False
    
    # Process NSX Edges
    if 'vcfnsxedges' in lsf.config['VCF']:
        lsf.write_output('')
        lsf.write_output('Processing NSX Edges...')
        lsf.write_output('SSH will be enabled automatically via NSX Manager API if needed.')
        vcfnsxedges = lsf.config.get('VCF', 'vcfnsxedges').split('\n')
        
        for entry in vcfnsxedges:
            if not entry or entry.strip().startswith('#'):
                continue
            
            # Format: nsxedge_hostname:esxhost
            parts = entry.split(':')
            nsxedge = parts[0].strip()
            esx_host = parts[1].strip() if len(parts) > 1 else ''
            
            if not configure_nsx_edge(nsxedge, auth_keys_file, password,
                                       esx_host=esx_host, dry_run=dry_run):
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
    1. Copies authorized_keys for the vcf user using ssh-copy-id
    2. Sets non-expiring passwords for vcf, root, backup accounts
    
    The expect script sddcmgr.exp is used to handle the interactive su command
    required to modify root account settings.
    
    :param auth_keys_file: Path to authorized_keys file
    :param password: VCF password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    sddcmgr = 'sddcmanager-a.site-a.vcf.lab'
    vcf_user = 'vcf'
    
    lsf.write_output('')
    lsf.write_output(f'Configuring SDDC Manager: {sddcmgr}')
    lsf.write_output('-' * 50)
    lsf.write_output(f'{sddcmgr}: Using SSH user: {vcf_user}')
    
    if dry_run:
        lsf.write_output(f'{sddcmgr}: Would copy authorized_keys using ssh-copy-id')
        lsf.write_output(f'{sddcmgr}: Would set non-expiring passwords')
        return True
    
    success = True
    
    # First check if the host is reachable
    if not lsf.test_ping(sddcmgr):
        lsf.write_output(f'{sddcmgr}: FAILED - Host is not reachable (ping failed)')
        return False
    
    # Check if SSH port is open
    if not lsf.test_tcp_port(sddcmgr, 22):
        lsf.write_output(f'{sddcmgr}: FAILED - SSH port 22 is not open')
        return False
    
    # Copy authorized_keys for vcf user using ssh-copy-id
    # This is the preferred method as it handles key format and permissions
    lsf.write_output(f'{sddcmgr}: Copying SSH keys for {vcf_user} user using ssh-copy-id...')
    
    # Try Manager key first
    manager_key = PUBLIC_KEY_FILE
    if os.path.isfile(manager_key):
        # Use sshpass with ssh-copy-id
        cmd = f'sshpass -p "{password}" ssh-copy-id -o StrictHostKeyChecking=no -i {manager_key} {vcf_user}@{sddcmgr}'
        result = lsf.run_command(cmd)
        if result.returncode == 0:
            lsf.write_output(f'{sddcmgr}: SUCCESS - Manager SSH key copied for {vcf_user}')
        else:
            lsf.write_output(f'{sddcmgr}: FAILED - Could not copy Manager SSH key')
            lsf.write_output(f'{sddcmgr}:         User: {vcf_user}, Password provided: {"yes" if password else "no"}')
            if result.stderr:
                lsf.write_output(f'{sddcmgr}:         Error: {result.stderr.strip()[:100]}')
            success = False
    
    # Also copy LMC key if available
    lmc_key_file = '/lmchol/home/holuser/.ssh/id_rsa.pub'
    if os.path.isfile(lmc_key_file):
        lsf.write_output(f'{sddcmgr}: Copying LMC SSH key for {vcf_user} user...')
        cmd = f'sshpass -p "{password}" ssh-copy-id -o StrictHostKeyChecking=no -i {lmc_key_file} {vcf_user}@{sddcmgr}'
        result = lsf.run_command(cmd)
        if result.returncode == 0:
            lsf.write_output(f'{sddcmgr}: SUCCESS - LMC SSH key copied for {vcf_user}')
        else:
            lsf.write_output(f'{sddcmgr}: WARNING - Could not copy LMC SSH key')
    
    # Run expect script to configure password expiration
    # This handles the interactive su command needed to modify root settings
    expect_script = os.path.expanduser('~/hol/Tools/sddcmgr.exp')
    if os.path.isfile(expect_script):
        lsf.write_output(f'{sddcmgr}: Configuring non-expiring passwords...')
        result = lsf.run_command(f'/usr/bin/expect {expect_script} {sddcmgr} {password}')
        if result.returncode == 0:
            lsf.write_output(f'{sddcmgr}: SUCCESS - Password expiration configured')
        else:
            lsf.write_output(f'{sddcmgr}: WARNING - Password config may have failed')
            if result.stderr:
                lsf.write_output(f'{sddcmgr}:         Error: {result.stderr.strip()[:100]}')
    else:
        lsf.write_output(f'{sddcmgr}: WARNING - sddcmgr.exp not found at {expect_script}')
    
    if success:
        lsf.write_output(f'{sddcmgr}: SDDC Manager configuration completed')
    else:
        lsf.write_output(f'{sddcmgr}: SDDC Manager configuration completed with errors')
    
    return success


#==============================================================================
# OPERATIONS VMS CONFIGURATION
#==============================================================================

def enable_ops_vm_ssh_via_guest_ops(vm_name: str, vcenter_fqdn: str,
                                     vcenter_user: str, password: str,
                                     dry_run: bool = False) -> bool:
    """
    Enable SSH on a VCF Operations VM via vSphere Guest Operations Manager.
    
    VCF Operations appliances (Photon OS) have sshd installed but disabled
    by default. This uses VMware Tools guest operations to run systemctl
    inside the VM without needing SSH access first.
    
    :param vm_name: VM name as it appears in vCenter inventory
    :param vcenter_fqdn: vCenter managing this VM
    :param vcenter_user: vCenter SSO user
    :param password: Root password for the guest OS
    :param dry_run: If True, preview only
    :return: True if SSH is now enabled
    """
    if dry_run:
        lsf.write_output(f'{vm_name}: Would enable SSH via Guest Operations API')
        return True
    
    import ssl as ssl_module
    
    try:
        context = ssl_module._create_unverified_context()
        si = connect.SmartConnect(host=vcenter_fqdn, user=vcenter_user,
                                  pwd=password, sslContext=context)
    except Exception as e:
        lsf.write_output(f'{vm_name}: Could not connect to vCenter {vcenter_fqdn}: {e}')
        return False
    
    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True)
        target_vm = None
        for vm in container.view:
            if vm.name == vm_name:
                target_vm = vm
                break
        container.Destroy()
        
        if not target_vm:
            lsf.write_output(f'{vm_name}: VM not found in vCenter {vcenter_fqdn}')
            return False
        
        if target_vm.runtime.powerState != 'poweredOn':
            lsf.write_output(f'{vm_name}: VM is not powered on ({target_vm.runtime.powerState})')
            return False
        
        if target_vm.guest.toolsStatus not in ('toolsOk', 'toolsOld'):
            lsf.write_output(f'{vm_name}: VMware Tools not available ({target_vm.guest.toolsStatus})')
            return False
        
        gom = content.guestOperationsManager
        creds = vim.vm.guest.NamePasswordAuthentication(
            username='root', password=password)
        
        for action in ['enable', 'start']:
            spec = vim.vm.guest.ProcessManager.ProgramSpec(
                programPath='/usr/bin/systemctl',
                arguments=f'{action} sshd'
            )
            try:
                pid = gom.processManager.StartProgramInGuest(target_vm, creds, spec)
                time.sleep(2)
                processes = gom.processManager.ListProcessesInGuest(target_vm, creds, [pid])
                for p in processes:
                    if p.exitCode == 0:
                        lsf.write_output(f'{vm_name}: SUCCESS - systemctl {action} sshd')
                    else:
                        lsf.write_output(f'{vm_name}: WARNING - systemctl {action} sshd exited with code {p.exitCode}')
            except vim.fault.InvalidGuestLogin:
                lsf.write_output(f'{vm_name}: FAILED - Invalid guest credentials for root')
                return False
            except Exception as e:
                lsf.write_output(f'{vm_name}: FAILED - Guest operations error: {e}')
                return False
        
        return True
    finally:
        connect.Disconnect(si)


def configure_operations_vms(auth_keys_file: str, password: str,
                              dry_run: bool = False) -> bool:
    """
    Configure Operations VMs (VCF Operations) for HOLification.
    
    Finds VMs with "ops" in the name from the config.ini [RESOURCES] VMs
    section and configures:
    1. Enables SSH via Guest Operations API (if not already running)
    2. Non-expiring password for root
    3. SSH authorized_keys for passwordless access
    
    :param auth_keys_file: Path to authorized_keys file
    :param password: Root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if 'VMs' not in lsf.config['RESOURCES']:
        lsf.write_output('No Operations VMs defined in config')
        return True
    
    vms_raw = lsf.config.get('RESOURCES', 'VMs').split('\n')
    ops_vms = []
    
    for vm in vms_raw:
        if not vm or vm.strip().startswith('#'):
            continue
        if 'ops' in vm.lower():
            parts = vm.split(':')
            vm_name = parts[0].strip()
            vcenter = parts[1].strip() if len(parts) > 1 else ''
            ops_vms.append((vm_name, vcenter))
    
    if not ops_vms:
        lsf.write_output('No Operations VMs found in config')
        return True
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('Operations VMs Configuration')
    lsf.write_output('=' * 60)
    
    # Determine default vCenter user for Guest Operations
    default_vcenter_user = 'administrator@vsphere.local'
    if lsf.config.has_option('RESOURCES', 'vCenters'):
        vc_lines = lsf.config.get('RESOURCES', 'vCenters').split('\n')
        for vc_line in vc_lines:
            if vc_line and not vc_line.strip().startswith('#'):
                vc_parts = vc_line.split(':')
                if len(vc_parts) > 2:
                    default_vcenter_user = vc_parts[2].strip()
                break
    
    overall_success = True
    
    for opsvm, vcenter in ops_vms:
        lsf.write_output('')
        lsf.write_output(f'{opsvm}: Starting configuration...')
        vm_success = True
        
        if not dry_run:
            # First, check if the host is reachable
            lsf.write_output(f'{opsvm}: Checking connectivity...')
            if not lsf.test_ping(opsvm):
                lsf.write_output(f'{opsvm}: SKIPPING - Host is not reachable (ping failed)')
                lsf.write_output(f'{opsvm}:           VM may not be deployed in this environment')
                continue
            lsf.write_output(f'{opsvm}: SUCCESS - Host is reachable')
            
            # Check if SSH port is open; if not, enable via Guest Operations
            if not lsf.test_tcp_port(opsvm, 22):
                lsf.write_output(f'{opsvm}: SSH port 22 is not open - enabling via Guest Operations...')
                if vcenter:
                    # Determine the vCenter user for this vCenter
                    vc_user = default_vcenter_user
                    if lsf.config.has_option('RESOURCES', 'vCenters'):
                        for vc_line in lsf.config.get('RESOURCES', 'vCenters').split('\n'):
                            if vc_line and not vc_line.strip().startswith('#') and vcenter in vc_line:
                                vc_parts = vc_line.split(':')
                                if len(vc_parts) > 2:
                                    vc_user = vc_parts[2].strip()
                                break
                    
                    enable_ops_vm_ssh_via_guest_ops(opsvm, vcenter, vc_user, password, dry_run)
                    time.sleep(3)
                    
                    if not lsf.test_tcp_port(opsvm, 22):
                        lsf.write_output(f'{opsvm}: FAILED - SSH still not available after Guest Operations enable')
                        overall_success = False
                        continue
                    lsf.write_output(f'{opsvm}: SUCCESS - SSH enabled via Guest Operations')
                else:
                    lsf.write_output(f'{opsvm}: FAILED - SSH port 22 not open and no vCenter specified for Guest Operations')
                    overall_success = False
                    continue
            else:
                lsf.write_output(f'{opsvm}: SUCCESS - SSH port 22 is open')
            
            # Set non-expiring password for root
            lsf.write_output(f'{opsvm}: Setting non-expiring password for root...')
            result = lsf.ssh('chage -M -1 root', f'root@{opsvm}', password)
            if result.returncode == 0:
                lsf.write_output(f'{opsvm}: SUCCESS - Non-expiring password set for root')
            elif result.returncode == 255:
                lsf.write_output(f'{opsvm}: FAILED - SSH connection failed')
                lsf.write_output(f'{opsvm}:         User: root, Password provided: {"yes" if password else "no"}')
                lsf.write_output(f'{opsvm}:         This may indicate invalid credentials')
                vm_success = False
            elif 'permission denied' in str(result.stderr).lower():
                lsf.write_output(f'{opsvm}: FAILED - Permission denied (invalid credentials)')
                lsf.write_output(f'{opsvm}:         User: root')
                vm_success = False
            else:
                lsf.write_output(f'{opsvm}: FAILED - chage command failed (exit code: {result.returncode})')
                if result.stderr:
                    lsf.write_output(f'{opsvm}:         Error: {str(result.stderr).strip()[:100]}')
                vm_success = False
            
            # Copy authorized_keys
            lsf.write_output(f'{opsvm}: Copying authorized_keys...')
            result = lsf.scp(auth_keys_file, f'root@{opsvm}:{LINUX_AUTH_FILE}', password)
            if result.returncode == 0:
                lsf.write_output(f'{opsvm}: SUCCESS - authorized_keys copied')
                # Set proper permissions
                chmod_result = lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{opsvm}', password)
                if chmod_result.returncode == 0:
                    lsf.write_output(f'{opsvm}: SUCCESS - authorized_keys permissions set (chmod 600)')
                else:
                    lsf.write_output(f'{opsvm}: WARNING - Failed to set permissions on authorized_keys')
            elif result.returncode == 255 or 'permission denied' in str(result.stderr).lower():
                lsf.write_output(f'{opsvm}: FAILED - SCP failed (authentication error)')
                lsf.write_output(f'{opsvm}:         User: root, Password provided: {"yes" if password else "no"}')
                vm_success = False
            else:
                lsf.write_output(f'{opsvm}: FAILED - SCP failed (exit code: {result.returncode})')
                if result.stderr:
                    lsf.write_output(f'{opsvm}:         Error: {str(result.stderr).strip()[:100]}')
                vm_success = False
            
            # Summary for this VM
            if vm_success:
                lsf.write_output(f'{opsvm}: Configuration completed successfully')
            else:
                lsf.write_output(f'{opsvm}: Configuration completed with errors')
                overall_success = False
        else:
            lsf.write_output(f'{opsvm}: Would check connectivity (ping, SSH port)')
            lsf.write_output(f'{opsvm}: Would enable SSH via Guest Operations if not running')
            lsf.write_output(f'{opsvm}: Would set non-expiring password for root')
            lsf.write_output(f'{opsvm}: Would copy authorized_keys to {LINUX_AUTH_FILE}')
    
    return overall_success


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
        sudo apt install -y libnss3-tools
    
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
        result = lsf.run_command('sudo apt update && sudo apt install -y libnss3-tools')
        if result.returncode == 0:
            lsf.write_output('libnss3-tools installed successfully')
            return True
        else:
            lsf.write_output('ERROR: Failed to install libnss3-tools via apt')
            lsf.write_output('')
            lsf.write_output('To install manually, run:')
            lsf.write_output('  sudo apt install libnss3-tools')
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
# VCENTER CA CERTIFICATE IMPORT
#==============================================================================

VCENTER_CERTS_ENDPOINT = '/certs/download.zip'


def get_vcenters_from_config() -> list:
    """
    Get list of vCenter hostnames from the config.ini file.
    
    Parses the [RESOURCES] vCenters section to extract vCenter FQDNs.
    Format in config.ini: hostname:type:user
    
    :return: List of vCenter hostnames (FQDNs)
    """
    vcenters = []
    
    if 'RESOURCES' not in lsf.config:
        lsf.write_output('WARNING: No RESOURCES section in config.ini')
        return vcenters
    
    if 'vCenters' not in lsf.config['RESOURCES']:
        lsf.write_output('WARNING: No vCenters defined in config.ini')
        return vcenters
    
    vcenter_entries = lsf.config.get('RESOURCES', 'vCenters').split('\n')
    
    for entry in vcenter_entries:
        entry = entry.strip()
        # Skip empty lines and comments
        if not entry or entry.startswith('#'):
            continue
        
        # Parse format: hostname:type:user
        parts = entry.split(':')
        if parts:
            hostname = parts[0].strip()
            if hostname:
                vcenters.append(hostname)
    
    return vcenters


def check_vcenter_accessible(vcenter_hostname: str, timeout: int = 5) -> Tuple[bool, str]:
    """
    Check if a vCenter's certificate endpoint is accessible.
    
    :param vcenter_hostname: vCenter FQDN
    :param timeout: Connection timeout in seconds
    :return: Tuple of (accessible: bool, message: str)
    """
    url = f"https://{vcenter_hostname}{VCENTER_CERTS_ENDPOINT}"
    
    try:
        response = requests.get(url, timeout=timeout, verify=False)
        
        if response.status_code == 200:
            # Check if we got a valid zip file (starts with PK)
            if response.content[:2] == b'PK':
                return True, f"vCenter {vcenter_hostname} certificate endpoint is accessible"
            else:
                return False, f"vCenter {vcenter_hostname} responded but did not return a valid zip file"
        else:
            return False, f"vCenter {vcenter_hostname} responded with HTTP {response.status_code}"
            
    except requests.exceptions.ConnectTimeout:
        return False, f"Connection timeout - vCenter {vcenter_hostname} not responding"
    except requests.exceptions.ConnectionError:
        return False, f"Connection error - Cannot reach vCenter {vcenter_hostname}"
    except Exception as e:
        return False, f"Error checking vCenter {vcenter_hostname}: {e}"


def download_vcenter_ca_certificates(vcenter_hostname: str) -> Optional[list]:
    """
    Download CA certificates from a vCenter server.
    
    vCenter exposes its CA certificates at /certs/download.zip which contains
    certificates in different formats for Linux, Mac, and Windows.
    
    :param vcenter_hostname: vCenter FQDN
    :return: List of tuples (cert_name, cert_pem) or None on failure
    """
    import zipfile
    import io
    
    url = f"https://{vcenter_hostname}{VCENTER_CERTS_ENDPOINT}"
    lsf.write_output(f'Downloading CA certificates from: {url}')
    
    try:
        response = requests.get(url, timeout=30, verify=False)
        
        if response.status_code != 200:
            lsf.write_output(f'ERROR: Failed to download certificates: HTTP {response.status_code}')
            return None
        
        # Extract certificates from zip
        certificates = []
        
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            # Look for .crt files in the win folder (or .0 files in lin folder)
            for filename in zf.namelist():
                # Prefer Windows format (.crt) or Linux format (.0)
                if filename.endswith('.crt') or (filename.endswith('.0') and '/lin/' in filename):
                    # Skip CRL files
                    if '.r0' in filename or '.crl' in filename:
                        continue
                    
                    cert_data = zf.read(filename)
                    cert_pem = cert_data.decode('utf-8')
                    
                    # Verify it's a valid certificate
                    if '-----BEGIN CERTIFICATE-----' in cert_pem:
                        # Extract a friendly name from the certificate
                        try:
                            result = subprocess.run(
                                ['openssl', 'x509', '-noout', '-subject'],
                                input=cert_pem,
                                capture_output=True,
                                text=True
                            )
                            if result.returncode == 0:
                                subject = result.stdout.strip()
                                # Extract CN or O from subject
                                cert_name = f"{vcenter_hostname} CA"
                                if 'O = ' in subject:
                                    # Extract organization
                                    org = subject.split('O = ')[1].split(',')[0].strip()
                                    cert_name = f"{org} CA"
                            else:
                                cert_name = f"{vcenter_hostname} CA"
                        except:
                            cert_name = f"{vcenter_hostname} CA"
                        
                        certificates.append((cert_name, cert_pem))
                        lsf.write_output(f'  Found certificate: {cert_name}')
                        # Only take the first valid certificate per format
                        break
        
        if not certificates:
            lsf.write_output('ERROR: No valid CA certificates found in download')
            return None
        
        lsf.write_output(f'Successfully extracted {len(certificates)} CA certificate(s)')
        return certificates
        
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to download/extract certificates: {e}')
        return None


def prompt_vcenter_unavailable(vcenter_hostname: str, message: str) -> str:
    """
    Prompt user for action when a vCenter CA is not accessible.
    
    :param vcenter_hostname: The vCenter that is not accessible
    :param message: Error message describing the issue
    :return: User's choice: 'skip', 'retry', or 'fail'
    """
    print('')
    print('!' * 60)
    print(f'  WARNING: vCenter CA Certificate Not Accessible')
    print('!' * 60)
    print('')
    print(f'  vCenter: {vcenter_hostname}')
    print(f'  {message}')
    print('')
    print('  Options:')
    print('    [S]kip  - Skip this vCenter and continue')
    print('    [R]etry - Check this vCenter again')
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


def configure_vcenter_ca_for_firefox(dry_run: bool = False) -> bool:
    """
    Download CA certificates from all vCenters and import into Firefox.
    
    This function:
    1. Reads vCenter list from /tmp/config.ini
    2. For each vCenter, checks accessibility (with SKIP/RETRY/FAIL options)
    3. Downloads CA certificates from the vCenter's /certs/download.zip endpoint
    4. Imports each CA as a trusted authority in Firefox on the console VM
    
    After running this function, Firefox on the console VM will trust
    certificates from all vCenters without showing security warnings.
    
    PREREQUISITES:
    - libnss3-tools package must be installed (provides certutil)
    - vCenter servers must be accessible
    - Firefox profile must exist on the console VM
    
    :param dry_run: If True, preview what would be done
    :return: True if successful (or all failures were skipped)
    """
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('vCenter CA Certificate Import for Firefox')
    lsf.write_output('=' * 60)
    
    # Step 1: Get vCenters from config
    vcenters = get_vcenters_from_config()
    
    if not vcenters:
        lsf.write_output('No vCenters found in config.ini - skipping vCenter CA import')
        return True
    
    lsf.write_output(f'Found {len(vcenters)} vCenter(s) in config.ini: {", ".join(vcenters)}')
    
    # Step 2: Ensure certutil is installed
    if not dry_run:
        if not check_certutil_installed():
            if not install_certutil(dry_run):
                lsf.write_output('ERROR: Cannot proceed without certutil')
                return False
    
    # Step 3: Find Firefox profiles
    profiles = find_firefox_profiles()
    
    if not profiles:
        lsf.write_output('WARNING: No Firefox profiles found on console VM')
        return False
    
    lsf.write_output(f'Found {len(profiles)} Firefox profile(s)')
    
    # Step 4: Process each vCenter
    overall_success = True
    imported_count = 0
    
    for vcenter in vcenters:
        lsf.write_output('')
        lsf.write_output(f'Processing vCenter: {vcenter}')
        lsf.write_output('-' * 40)
        
        if dry_run:
            lsf.write_output(f'  Would check accessibility of {vcenter}')
            lsf.write_output(f'  Would download CA from https://{vcenter}{VCENTER_CERTS_ENDPOINT}')
            lsf.write_output(f'  Would import CA to {len(profiles)} Firefox profile(s)')
            imported_count += 1
            continue
        
        # Check accessibility with retry loop
        while True:
            lsf.write_output(f'Checking vCenter accessibility...')
            accessible, message = check_vcenter_accessible(vcenter)
            
            if accessible:
                lsf.write_output(f'✓ {message}')
                break
            else:
                choice = prompt_vcenter_unavailable(vcenter, message)
                
                if choice == 'skip':
                    lsf.write_output(f'Skipping vCenter {vcenter} (user choice)')
                    break
                elif choice == 'retry':
                    lsf.write_output('Retrying...')
                    continue
                elif choice == 'fail':
                    lsf.write_output(f'Exiting due to vCenter unavailability (user choice)')
                    return False
        
        if not accessible:
            continue  # Skip this vCenter
        
        # Download CA certificates
        certificates = download_vcenter_ca_certificates(vcenter)
        
        if not certificates:
            lsf.write_output(f'WARNING: Could not get CA certificates from {vcenter}')
            continue
        
        # Import each certificate to Firefox profiles
        for cert_name, cert_pem in certificates:
            for profile_path in profiles:
                if import_ca_to_firefox_profile(cert_pem, profile_path, cert_name, dry_run):
                    imported_count += 1
    
    # Summary
    lsf.write_output('')
    if imported_count > 0:
        lsf.write_output(f'Successfully imported CA certificates from {len(vcenters)} vCenter(s)')
        lsf.write_output('Firefox will now trust vCenter-signed certificates')
        return True
    else:
        lsf.write_output('WARNING: No vCenter CA certificates were imported')
        return overall_success


#==============================================================================
# SDDC MANAGER AUTO-ROTATE POLICY DISABLE
#==============================================================================

def disable_sddc_auto_rotate(dry_run: bool = False) -> bool:
    """
    Disable automatic password rotation for all SDDC Manager service credentials.

    SDDC Manager configures automatic password rotation (typically every 30 days)
    for service accounts used to communicate between VCF components (e.g., vCenter,
    NSX Manager). In a lab/template environment, this auto-rotation causes failures
    because the lab is frequently powered off, rebuilt, or cloned from a template.
    When the rotation fires during a powered-off period or immediately after
    deployment, it fails and creates unresolvable error notifications in the
    SDDC Manager UI.

    This function:
    1. Authenticates to the SDDC Manager API
    2. Retrieves all credentials with an autoRotatePolicy
    3. Disables auto-rotation for each credential using the API
    4. Waits for each disable task to complete

    API Reference:
        PATCH /v1/credentials with operationType: UPDATE_AUTO_ROTATE_POLICY
        and autoRotatePolicy: { enableAutoRotatePolicy: false }

    :param dry_run: If True, preview what would be done
    :return: True if successful (or no auto-rotate policies found)
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Determine if SDDC Manager exists in this environment
    sddc_host = None
    if 'VCF' in lsf.config:
        sddc_host = lsf.config.get('VCF', 'sddcmgr', fallback=None)
    if not sddc_host:
        sddc_host = 'sddcmanager-a.site-a.vcf.lab'

    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('SDDC Manager Auto-Rotate Policy Disable')
    lsf.write_output('=' * 60)
    lsf.write_output(f'SDDC Manager: {sddc_host}')

    if dry_run:
        lsf.write_output('Would check for credentials with auto-rotate policies')
        lsf.write_output('Would disable auto-rotation for all service credentials')
        return True

    # Check connectivity
    if not lsf.test_ping(sddc_host):
        lsf.write_output(f'{sddc_host}: Not reachable - skipping auto-rotate disable')
        return True  # Don't fail the overall process

    password = lsf.get_password()

    # Step 1: Get API token
    lsf.write_output(f'{sddc_host}: Authenticating to SDDC Manager API...')
    try:
        token_url = f'https://{sddc_host}/v1/tokens'
        token_body = {
            'username': 'administrator@vsphere.local',
            'password': password
        }
        response = requests.post(token_url, json=token_body, verify=False, timeout=30)
        if response.status_code != 200:
            lsf.write_output(f'{sddc_host}: Failed to authenticate (HTTP {response.status_code})')
            return False
        token = response.json().get('accessToken', '')
        if not token:
            lsf.write_output(f'{sddc_host}: No access token received')
            return False
        lsf.write_output(f'{sddc_host}: Authentication successful')
    except Exception as e:
        lsf.write_output(f'{sddc_host}: API authentication error: {e}')
        return False

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    # Step 2: Get all credentials and find those with auto-rotate policies
    lsf.write_output(f'{sddc_host}: Checking for credentials with auto-rotate policies...')
    try:
        creds_url = f'https://{sddc_host}/v1/credentials'
        response = requests.get(creds_url, headers=headers, verify=False, timeout=30)
        if response.status_code != 200:
            lsf.write_output(f'{sddc_host}: Failed to get credentials (HTTP {response.status_code})')
            return False

        all_creds = response.json().get('elements', [])
        auto_rotate_creds = [c for c in all_creds if c.get('autoRotatePolicy')]

        if not auto_rotate_creds:
            lsf.write_output(f'{sddc_host}: No credentials with auto-rotate policies found')
            lsf.write_output(f'{sddc_host}: Nothing to disable')
            return True

        lsf.write_output(f'{sddc_host}: Found {len(auto_rotate_creds)} credential(s) '
                         f'with auto-rotate enabled:')
        for cred in auto_rotate_creds:
            policy = cred['autoRotatePolicy']
            lsf.write_output(f'  - {cred["username"]} on '
                             f'{cred.get("resource", {}).get("resourceName", "?")} '
                             f'({cred.get("credentialType")}) - '
                             f'every {policy.get("frequencyInDays")} days')

    except Exception as e:
        lsf.write_output(f'{sddc_host}: Error retrieving credentials: {e}')
        return False

    # Step 3: Disable auto-rotate for each credential
    # Group by resource to batch the API calls efficiently
    # The API accepts multiple credentials per element in a single PATCH call
    resource_groups = {}
    for cred in auto_rotate_creds:
        resource = cred.get('resource', {})
        resource_name = resource.get('resourceName', '')
        resource_type = resource.get('resourceType', '')
        key = (resource_name, resource_type)

        if key not in resource_groups:
            resource_groups[key] = []
        resource_groups[key].append({
            'credentialType': cred.get('credentialType'),
            'username': cred.get('username')
        })

    success = True
    tasks = []

    for (resource_name, resource_type), credentials in resource_groups.items():
        lsf.write_output(f'{sddc_host}: Disabling auto-rotate for {resource_name} '
                         f'({len(credentials)} credential(s))...')

        patch_body = {
            'operationType': 'UPDATE_AUTO_ROTATE_POLICY',
            'elements': [{
                'resourceName': resource_name,
                'resourceType': resource_type,
                'credentials': credentials
            }],
            'autoRotatePolicy': {
                'enableAutoRotatePolicy': False
            }
        }

        try:
            response = requests.patch(
                creds_url, headers=headers, json=patch_body,
                verify=False, timeout=30
            )

            if response.status_code == 202:
                task_data = response.json()
                task_id = task_data.get('id', '')
                lsf.write_output(f'{sddc_host}: Disable task submitted '
                                 f'(task: {task_id[:8]}...)')
                tasks.append(task_id)
            else:
                error_msg = ''
                try:
                    error_data = response.json()
                    error_msg = error_data.get('message', '')
                except Exception:
                    error_msg = response.text[:200]
                lsf.write_output(f'{sddc_host}: Failed to disable auto-rotate for '
                                 f'{resource_name}: HTTP {response.status_code} - {error_msg}')
                success = False

        except Exception as e:
            lsf.write_output(f'{sddc_host}: Error disabling auto-rotate for '
                             f'{resource_name}: {e}')
            success = False

    # Step 4: Wait for tasks to complete
    failed_tasks = []
    if tasks:
        lsf.write_output(f'{sddc_host}: Waiting for {len(tasks)} disable task(s) '
                         f'to complete...')
        time.sleep(10)

        for task_id in tasks:
            # Poll task status (up to 90 seconds)
            task_success = False
            for attempt in range(18):
                try:
                    task_url = f'https://{sddc_host}/v1/tasks/{task_id}'
                    response = requests.get(
                        task_url, headers=headers, verify=False, timeout=15
                    )
                    if response.status_code == 200:
                        task_data = response.json()
                        status = task_data.get('status', '')
                        if status == 'SUCCESSFUL':
                            lsf.write_output(f'{sddc_host}: Task {task_id[:8]}... '
                                             f'completed successfully')
                            task_success = True
                            break
                        elif status == 'FAILED':
                            # Extract error details for better logging
                            error_msg = 'Unknown error'
                            for subtask in task_data.get('subTasks', []):
                                for error in subtask.get('errors', []):
                                    error_msg = error.get('message', error_msg)
                            lsf.write_output(f'{sddc_host}: Task {task_id[:8]}... '
                                             f'FAILED - {error_msg}')
                            failed_tasks.append(task_id)
                            break
                        # Still in progress or pending, wait and retry
                        time.sleep(5)
                    else:
                        time.sleep(5)
                except Exception:
                    time.sleep(5)

            if not task_success and task_id not in failed_tasks:
                lsf.write_output(f'{sddc_host}: Task {task_id[:8]}... did not '
                                 f'complete within timeout')

    # Step 5: Cancel any failed tasks to prevent UI notifications
    if failed_tasks:
        lsf.write_output(f'{sddc_host}: Cancelling {len(failed_tasks)} failed task(s) '
                         f'to prevent UI notifications...')
        for task_id in failed_tasks:
            try:
                cancel_url = f'https://{sddc_host}/v1/credentials/tasks/{task_id}'
                response = requests.delete(
                    cancel_url, headers=headers, verify=False, timeout=15
                )
                if response.status_code in [200, 202]:
                    lsf.write_output(f'{sddc_host}: Task {task_id[:8]}... cancelled')
                else:
                    lsf.write_output(f'{sddc_host}: Could not cancel task '
                                     f'{task_id[:8]}... (HTTP {response.status_code})')
            except Exception as e:
                lsf.write_output(f'{sddc_host}: Error cancelling task '
                                 f'{task_id[:8]}...: {e}')

    # Step 6: Verify
    lsf.write_output(f'{sddc_host}: Verifying auto-rotate policies are disabled...')
    try:
        response = requests.get(creds_url, headers=headers, verify=False, timeout=30)
        if response.status_code == 200:
            remaining = [c for c in response.json().get('elements', [])
                         if c.get('autoRotatePolicy')]
            if not remaining:
                lsf.write_output(f'{sddc_host}: SUCCESS - All auto-rotate policies '
                                 f'have been disabled')
            else:
                lsf.write_output(f'{sddc_host}: WARNING - {len(remaining)} credential(s) '
                                 f'still have auto-rotate enabled '
                                 f'(resource may not be in ACTIVE state):')
                for cred in remaining:
                    res_name = cred.get('resource', {}).get('resourceName', '?')
                    lsf.write_output(f'  - {cred["username"]} on {res_name}')
                lsf.write_output(f'{sddc_host}: NOTE - These can be manually disabled '
                                 f'once the resources are in ACTIVE state')
    except Exception as e:
        lsf.write_output(f'{sddc_host}: Could not verify: {e}')

    if failed_tasks:
        lsf.write_output(f'{sddc_host}: Completed with {len(failed_tasks)} resource(s) '
                         f'unavailable - auto-rotate could not be disabled for '
                         f'resources not in ACTIVE state')
    else:
        lsf.write_output(f'{sddc_host}: Auto-rotate disable completed successfully')

    return True  # Don't fail HOLification for unavailable resources


#==============================================================================
# VCF OPERATIONS FLEET PASSWORD POLICY
#==============================================================================

def configure_vcf_fleet_password_policy(dry_run: bool = False) -> bool:
    """
    Create and assign the MaxExpiration password policy in VCF Operations Manager
    Fleet Settings, then remediate all inventory items.
    
    This function:
    1. Authenticates to VCF Operations Manager suite-api
    2. Checks if "MaxExpiration" policy already exists
    3. If not, creates it with expiration = 729 days from today
    4. Queries all policies to find inventory assigned to other policies
    5. Reassigns MANAGEMENT to MaxExpiration (auto-unassigns from old policy)
    6. Reassigns each INSTANCE to MaxExpiration using resourceId (auto-unassigns)
    7. Attempts remediation of all inventory items
    
    If "MaxExpiration" already exists, skips creation and reassigns all inventory.
    
    :param dry_run: If True, preview only
    :return: True if successful
    """
    import requests
    import urllib3
    from datetime import datetime, timedelta
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('VCF Operations Fleet Password Policy')
    lsf.write_output('=' * 60)
    
    # Determine VCF Operations Manager FQDN from config
    ops_fqdn = None
    try:
        if lsf.config.has_option('RESOURCES', 'URLs'):
            urls_raw = lsf.config.get('RESOURCES', 'URLs').split('\n')
            for entry in urls_raw:
                url = entry.split(',')[0].strip()
                if 'ops-' in url and '.vcf.lab' in url:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    ops_fqdn = parsed.hostname
                    break
    except Exception:
        pass
    
    if not ops_fqdn:
        try:
            if lsf.config.has_section('VCF'):
                if lsf.config.has_option('VCF', 'urls'):
                    vcf_urls = lsf.config.get('VCF', 'urls').split('\n')
                    for entry in vcf_urls:
                        url = entry.split(',')[0].strip()
                        if 'ops-' in url:
                            from urllib.parse import urlparse
                            parsed = urlparse(url)
                            ops_fqdn = parsed.hostname
                            break
        except Exception:
            pass
    
    if not ops_fqdn:
        lsf.write_output('WARNING: VCF Operations Manager FQDN not found in config - skipping')
        return False
    
    password = lsf.get_password()
    base_url = f"https://{ops_fqdn}"
    api_base = f"{base_url}/suite-api"
    
    lsf.write_output(f'VCF Operations Manager: {ops_fqdn}')
    
    if dry_run:
        lsf.write_output('Would create MaxExpiration policy, assign all inventory, and remediate')
        return True
    
    # Step 1: Authenticate
    lsf.write_output('Authenticating to VCF Operations Manager...')
    try:
        token_resp = requests.post(
            f"{api_base}/api/auth/token/acquire",
            json={"username": "admin", "password": password, "authSource": "local"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            verify=False, timeout=30
        )
        token_resp.raise_for_status()
        token = token_resp.json()["token"]
        lsf.write_output('SUCCESS - Authenticated to VCF Operations Manager')
    except Exception as e:
        lsf.write_output(f'FAILED - Could not authenticate: {e}')
        return False
    
    headers = {
        "Authorization": f"OpsToken {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-vRealizeOps-API-use-unsupported": "true"
    }
    
    # Step 2: Query all existing policies and their assignments
    lsf.write_output('Querying existing password policies...')
    existing_policy_id = None
    all_policies = []
    try:
        query_resp = requests.post(
            f"{api_base}/internal/passwordmanagement/policies/query",
            headers=headers, json={}, verify=False, timeout=30
        )
        query_resp.raise_for_status()
        all_policies = query_resp.json().get("vcfPolicies", [])
        
        for policy in all_policies:
            pname = policy.get("policyInfo", {}).get("policyName", "")
            pid = policy.get("policyId", "")
            assigned = policy.get("vcfPolicyAssignedResourceList", [])
            lsf.write_output(f'  Policy: {pname} (ID: {pid}), Assigned: {len(assigned)} resource(s)')
            for res in assigned:
                lsf.write_output(f'    - {res.get("resourceName", "?")} ({res.get("resourceType", "?")})')
            if pname == "MaxExpiration":
                existing_policy_id = pid
    except Exception as e:
        lsf.write_output(f'WARNING - Could not query existing policies: {e}')
    
    # Step 3: Create MaxExpiration policy if it doesn't exist
    expiration_days = 729
    expiration_date = (datetime.now() + timedelta(days=expiration_days)).strftime("%Y-%m-%d")
    policy_description = f"Passwords will expire {expiration_date} ({expiration_days} days from creation)"
    
    if not existing_policy_id:
        lsf.write_output(f'Creating MaxExpiration policy (expires {expiration_date}, {expiration_days} days)...')
        try:
            create_resp = requests.post(
                f"{api_base}/internal/passwordmanagement/policies",
                headers=headers,
                json={
                    "policyInfo": {
                        "policyName": "MaxExpiration",
                        "description": policy_description,
                        "isFleetPolicy": False
                    },
                    "complexityConstraint": {
                        "minLength": 8,
                        "minLowercase": 0,
                        "minUppercase": 0,
                        "minNumeric": 0,
                        "minSpecial": 0,
                        "passwordHistory": 1
                    },
                    "expirationConstraint": {
                        "passwordExpirationDays": expiration_days
                    },
                    "lockoutConstraint": {
                        "lockoutMaxAuthFailures": 5,
                        "lockoutEvaluationPeriod": 300,
                        "lockoutPeriod": 600
                    }
                },
                verify=False, timeout=30
            )
            create_resp.raise_for_status()
            policy_data = create_resp.json()
            existing_policy_id = policy_data.get("policyId")
            lsf.write_output(f'SUCCESS - Created MaxExpiration policy: {existing_policy_id}')
        except Exception as e:
            lsf.write_output(f'FAILED - Could not create policy: {e}')
            return False
    else:
        lsf.write_output('MaxExpiration policy already exists - skipping creation')
    
    # Step 4: Assign ALL inventory to MaxExpiration
    # Assigning a resource to MaxExpiration automatically unassigns it from any
    # other policy. We assign MANAGEMENT first, then each INSTANCE by resourceId.
    
    # Step 4a: Assign MANAGEMENT to MaxExpiration
    lsf.write_output('Assigning MANAGEMENT to MaxExpiration...')
    try:
        assign_resp = requests.post(
            f"{api_base}/internal/passwordmanagement/policies/{existing_policy_id}/assign",
            headers=headers,
            json={"assignmentGroup": ["MANAGEMENT"]},
            verify=False, timeout=30
        )
        if assign_resp.status_code == 204:
            lsf.write_output('SUCCESS - MANAGEMENT assigned to MaxExpiration')
        else:
            lsf.write_output(f'WARNING - MANAGEMENT assign returned {assign_resp.status_code}: {assign_resp.text[:100]}')
    except Exception as e:
        lsf.write_output(f'WARNING - Could not assign MANAGEMENT: {e}')
    
    # Step 4b: Collect all INSTANCE resources from other policies and assign to MaxExpiration
    # The assign endpoint for INSTANCE requires: {"assignmentGroup": ["INSTANCE"], "resourceId": [<uuid>, ...]}
    instance_ids = []
    for policy in all_policies:
        if policy.get("policyId") == existing_policy_id:
            continue
        for res in policy.get("vcfPolicyAssignedResourceList", []):
            if res.get("resourceType") == "INSTANCE":
                instance_ids.append(res["resourceId"])
                lsf.write_output(f'  Will reassign: {res.get("resourceName", "?")} from {policy["policyInfo"]["policyName"]}')
    
    # Also check if MaxExpiration itself is missing any INSTANCE assignments
    # by re-querying after MANAGEMENT assignment
    try:
        query_resp = requests.post(
            f"{api_base}/internal/passwordmanagement/policies/query",
            headers=headers, json={}, verify=False, timeout=30
        )
        query_resp.raise_for_status()
        refreshed_policies = query_resp.json().get("vcfPolicies", [])
        for policy in refreshed_policies:
            if policy.get("policyId") == existing_policy_id:
                continue
            for res in policy.get("vcfPolicyAssignedResourceList", []):
                if res.get("resourceType") == "INSTANCE" and res["resourceId"] not in instance_ids:
                    instance_ids.append(res["resourceId"])
                    lsf.write_output(f'  Will reassign: {res.get("resourceName", "?")} from {policy["policyInfo"]["policyName"]}')
    except Exception:
        pass
    
    if instance_ids:
        lsf.write_output(f'Assigning {len(instance_ids)} INSTANCE resource(s) to MaxExpiration...')
        try:
            assign_resp = requests.post(
                f"{api_base}/internal/passwordmanagement/policies/{existing_policy_id}/assign",
                headers=headers,
                json={"assignmentGroup": ["INSTANCE"], "resourceId": instance_ids},
                verify=False, timeout=30
            )
            if assign_resp.status_code == 204:
                lsf.write_output(f'SUCCESS - {len(instance_ids)} INSTANCE resource(s) assigned to MaxExpiration')
            else:
                msg = assign_resp.text[:150] if assign_resp.text else ""
                lsf.write_output(f'WARNING - INSTANCE assign returned {assign_resp.status_code}: {msg}')
        except Exception as e:
            lsf.write_output(f'WARNING - Could not assign INSTANCE resources: {e}')
    else:
        lsf.write_output('No INSTANCE resources to reassign from other policies')
    
    # Step 5: Verify final assignment state
    lsf.write_output('Verifying final policy assignments...')
    try:
        verify_resp = requests.get(
            f"{api_base}/internal/passwordmanagement/policies/{existing_policy_id}",
            headers=headers, verify=False, timeout=30
        )
        verify_resp.raise_for_status()
        policy_state = verify_resp.json()
        assigned = policy_state.get("vcfPolicyAssignedResourceList", [])
        lsf.write_output(f'MaxExpiration is assigned to {len(assigned)} resource(s):')
        for res in assigned:
            lsf.write_output(f'  - {res.get("resourceName", "?")} ({res.get("resourceType", "?")})')
        
        # Also check if the policy's isFleetPolicy flag is correct
        is_fleet = policy_state.get("policyInfo", {}).get("isFleetPolicy", False)
        if is_fleet:
            lsf.write_output('INFO - Resetting isFleetPolicy to False (was set by previous FLEET assignment)')
            policy_state["policyInfo"]["isFleetPolicy"] = False
            requests.put(
                f"{api_base}/internal/passwordmanagement/policies/{existing_policy_id}",
                headers=headers, json=policy_state, verify=False, timeout=30
            )
    except Exception as e:
        lsf.write_output(f'WARNING - Could not verify policy state: {e}')
    
    # Step 6: Delete other policies that are now unassigned
    lsf.write_output('Cleaning up old policies with no remaining assignments...')
    try:
        query_resp = requests.post(
            f"{api_base}/internal/passwordmanagement/policies/query",
            headers=headers, json={}, verify=False, timeout=30
        )
        query_resp.raise_for_status()
        for policy in query_resp.json().get("vcfPolicies", []):
            pid = policy.get("policyId")
            pname = policy.get("policyInfo", {}).get("policyName", "")
            if pid == existing_policy_id:
                continue
            assigned = policy.get("vcfPolicyAssignedResourceList", [])
            if len(assigned) == 0:
                lsf.write_output(f'Deleting unassigned policy: {pname} ({pid})')
                del_resp = requests.delete(
                    f"{api_base}/internal/passwordmanagement/policies/{pid}",
                    headers=headers, verify=False, timeout=30
                )
                if del_resp.status_code == 204:
                    lsf.write_output(f'SUCCESS - Deleted policy: {pname}')
                else:
                    msg = del_resp.text[:100] if del_resp.text else ""
                    lsf.write_output(f'WARNING - Could not delete {pname}: {del_resp.status_code} {msg}')
            else:
                lsf.write_output(f'Keeping policy {pname} - still has {len(assigned)} assignment(s)')
    except Exception as e:
        lsf.write_output(f'WARNING - Could not clean up old policies: {e}')
    
    # Step 7: Attempt remediation
    lsf.write_output('Attempting to remediate all inventory items...')
    remediation_attempted = False
    try:
        remediate_resp = requests.post(
            f"{api_base}/internal/passwordmanagement/policies/{existing_policy_id}/remediate",
            headers=headers, json={}, verify=False, timeout=60
        )
        if remediate_resp.status_code in (200, 202, 204):
            lsf.write_output('SUCCESS - Remediation triggered successfully')
            remediation_attempted = True
        else:
            lsf.write_output(f'INFO - Remediate endpoint returned {remediate_resp.status_code}')
    except Exception:
        pass
    
    if not remediation_attempted:
        lsf.write_output('INFO - Automatic remediation via API not available through suite-api proxy.')
        lsf.write_output('INFO - Please remediate manually via VCF Operations Manager UI:')
        lsf.write_output(f'INFO -   {base_url}/vcf-operations/ui/manage/fleet/fleet-settings')
        lsf.write_output('INFO -   Navigate to Fleet Settings > select MaxExpiration > Remediate All')
    
    lsf.write_output('VCF Operations Fleet Password Policy configuration complete')
    return True


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
    0a. Vault root CA import to Firefox on console VM (with SKIP/RETRY/FAIL options)
    0b. vCenter CA certificates import to Firefox on console VM (with SKIP/RETRY/FAIL options)
    1. Pre-checks and environment setup
    2. ESXi host configuration
    3. vCenter configuration
    4. NSX configuration (Managers and Edges)
    5. SDDC Manager configuration
    6. VCF Automation VMs configuration (uses vmware-system-user)
    7. Operations VMs configuration
    8. Disable SDDC Manager auto-rotate policies (prevents post-deployment failures)
    9. Final cleanup
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

NOTE: NSX Edge SSH is enabled automatically via Guest Operations.
      See HOLIFICATION.md for any remaining manual steps.
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
    
    # Step 0a: Import Vault root CA to Firefox on console VM (at the beginning)
    # This allows the user to skip/retry/fail early if Vault is not accessible
    if not configure_vault_ca_for_firefox(args.dry_run):
        lsf.write_output('ERROR: Failed to configure Vault CA for Firefox')
        sys.exit(1)
    
    # Step 0b: Import vCenter CA certificates to Firefox on console VM
    # This reads vCenters from config.ini and imports their CA certificates
    if not configure_vcenter_ca_for_firefox(args.dry_run):
        lsf.write_output('ERROR: Failed to configure vCenter CA certificates for Firefox')
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
    
    # Step 5: Configure VCF Automation VMs
    configure_aria_automation_vms(auth_keys_file, password, args.dry_run)
    
    # Step 6: Configure Operations VMs
    configure_operations_vms(auth_keys_file, password, args.dry_run)
    
    # Step 7: Disable SDDC Manager auto-rotate policies
    # This prevents credential rotation failures when the lab template is deployed
    if 'VCF' in lsf.config or not args.esx_only:
        disable_sddc_auto_rotate(args.dry_run)
    
    # Step 8: Configure VCF Operations Fleet Password Policy
    # Creates "MaxExpiration" policy, assigns ALL inventory (MANAGEMENT + INSTANCE), remediates
    configure_vcf_fleet_password_policy(args.dry_run)
    
    # Step 9: Final cleanup
    perform_final_cleanup(args.dry_run)
    
    # Print summary
    print('')
    print('=' * 60)
    print('HOLification Complete')
    print('=' * 60)
    print('')
    print('IMPORTANT: Review HOLIFICATION.md for any remaining manual steps.')
    print('NSX Edge and Operations VM SSH were enabled automatically via')
    print('Guest Operations. Verify SSH connectivity to all components.')
    print('')


if __name__ == '__main__':
    main()
