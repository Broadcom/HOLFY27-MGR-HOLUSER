#!/usr/bin/env python3
# confighol-9.1.py - HOLFY27 vApp HOLification Tool
# Version 2.15 - 2026-04-02
# Author - Burke Azbill and HOL Core Team
#
# Script Naming Convention:
# This script is named according to the VCF version it was developed and
# tested against: confighol-9.1.py for VCF 9.1.x. Future VCF versions may
# require a new script version (e.g., confighol-9.5.py for VCF 9.5.x).
#
# CHANGELOG:
# v2.15 - 2026-04-02:
#   - Fixed for dual site
# v2.14 - 2026-04-01:
#   - Fixed: updated to have vCenters trust the Vault CA.
# v2.13 - 2026-03-19:
#   - Fixed: NSX Edge entries with vna- prefix (VCF 9.1 naming) were skipped
#     due to an earlier build filter in configure_nsx_components. Removed vna- exclusion.
#   - Fixed: _get_nsx_manager_for_edge now matches both edge- and vna- prefixed
#     edge hostnames (e.g. vna-wld01-01a -> nsx-wld01-01a).
#   - Fixed: VCF Automation VM auto-a was not configured because it was not in
#     the vravms config. Now also discovers VCF Automation VMs from vraurls
#     (e.g. auto-a.site-a.vcf.lab) in the [VCFFINAL] section.
#   - Fixed: Operations VMs now discovered from vcfcomponenturls in addition to
#     [RESOURCES] VMs section, picking up opslogs-a and other Ops VMs.
#   - Fixed: Operations VMs with vmware-system-user SSH (opslogs-a) now handled
#     correctly with sudo-based chage, authorized_keys copy via sudo, and
#     password expiration for both vmware-system-user and root accounts.
#   - Fixed: opsnet VMs (no SSH access) now skipped gracefully instead of
#     reporting authentication failures.
#   - Fixed: NSX Edge configure_nsx_edge now handles unreachable STANDBY edges
#     gracefully — skips SSH-based operations but still sets password expiration
#     via NSX Manager REST API.
#   - Fixed: SDDC Manager credentials API now uses Bearer token auth (required
#     for VCF 9.1 C4 — Basic Auth returns empty results). Affects NSX root
#     password recovery via get_nsx_root_password_from_sddc.
#   - Fixed: NSX Edge SSH authorized_keys copy now recovers rotated root
#     passwords from SDDC Manager (resource_type=NSXT_EDGE) when standard
#     lab password fails.
#   - Added: auto-platform-a host isolation on vc-mgmt-a / cluster-mgmt-01a.
#     Creates DRS VM-Host groups and mandatory affinity/anti-affinity rules
#     to ensure auto-platform-a-* is the only VM on its dedicated host.
#     Migrates any co-located VMs to another host.
# v2.12 - 2026-03-13:
#   - Fixed: vCenter CA import now handles CN= format in certificate subject
#     (e.g. "CN = vc-mgmt-a.site-a.vcf.lab, O = VMware, Inc.")
# v2.11 - 2026-03-11:
#   - Fixed: vCenter Vault CA trust import now checks vmdir for existing cert
#     before calling dir-cli trustedcert publish. The publish command silently
#     appends a duplicate PEM into the same vmdir entry when called twice,
#     creating a multi-cert PEM that breaks NSX compute-manager re-registration
#     (NSX TrustStoreServiceImpl error MP2179).
#   - Added: NSX compute-manager re-registration step after Vault CA trust
#     distribution. PUTs each compute manager with the vCenter's current
#     SHA-256 thumbprint so NSX re-validates the connection with the updated
#     trust chain. Skips compute managers already in UP/REGISTERED state.
# v2.10 - 2026-03-10:
#   - Vault CA trust distribution: imports the Vault PKI root CA certificate
#     as a trusted authority across the entire VCF suite — vCenter Servers
#     (dir-cli trustedcert publish → VECS TRUSTED_ROOTS), ESXi hosts
#     (appended to /etc/vmware/ssl/castore.pem + auto-backup.sh), NSX
#     Managers (trust-management API), SDDC Manager (trusted-certificates
#     API), VCF Automation appliances, and VCF Operations VMs (OS trust
#     stores). Idempotent: checks for existing CA before importing.
# v2.9 - 2026-03-06:
#   - VSP & Supervisor proxy: configures HTTP/HTTPS proxy on all VSP cluster
#     nodes (Photon OS) and the Supervisor via vCenter API. Enables outbound
#     internet access through holorouter Squid proxy (10.1.1.1:3128).
#     Dynamically discovers VSP node IPs via kubectl, configures
#     /etc/sysconfig/proxy, /etc/environment, containerd and kubelet
#     systemd drop-in files with appropriate NO_PROXY list.
# v2.8 - 2026-03-03:
#   - Fully non-interactive: removed all input() prompts for vCenter shell/
#     browser configuration, NSX Manager configuration, NSX SSH enablement
#     fallback, Vault CA unavailable, and vCenter CA unavailable — all
#     operations now proceed automatically (auto-skip on unavailability)
#   - NSX root password recovery: rewrote get_nsx_root_password_from_sddc
#     to use direct HTTPS to SDDC Manager API (Basic Auth) instead of
#     fragile SSH-based token flow. Added diagnostic logging throughout.
#   - NSX authorized_keys: now resolves root password BEFORE attempting
#     mkdir/scp (previously mkdir with wrong password would fail silently,
#     then scp would also fail, and SDDC Manager lookup was unreliable)
#   - Removed browser warning fix (vcbrowser.sh integration) — approach does
#     not reliably work on VCF 9.1 due to JSP compilation and Tomcat caching
# v2.7 - 2026-03-03:
#   - NSX SSH start-on-boot: now checks current state before setting, and
#     verifies state after set — no longer reports failure when already enabled
#   - NSX Manager/Edge authorized_keys: creates /root/.ssh/ directory before
#     SCP copy (fixes "Could not copy authorized_keys" on appliances without
#     /root/.ssh/ directory)
#   - vCenter browser warning fix: removed — approach does not reliably work
#     on VCF 9.1 (JSP compilation and Tomcat caching prevent the fix from
#     taking effect)
#   - MOB enablement: added idempotency check for enableDebugBrowse element
#     value (not just existence) — safe to re-run without duplicating elements
#   - enable_vcenter_shell: now checks if bash is already the login shell
#     via SSH before running the expect script — safe to re-run
# v2.6 - 2026-02-26:
#   - vCenter CA import: fixed bug where only the first certificate from each
#     download.zip was imported (premature break); now imports ALL CA certs
#   - vCenter CA import: deduplicate certs by SHA-256 fingerprint across vCenters
#   - vCenter CA import: fixed cert nickname quoting (strip stray quotes from
#     openssl subject O= field, e.g. "Broadcom, Inc" -> Broadcom, Inc)
# v2.5 - 2026-02-26:
#   - VCF Automation: set vmware-system-user and root passwords to never expire
#     (chage -M -1 via sudo -S)
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
# 0b. Vault CA Trust Distribution (runs after Firefox import):
#    - Distributes the Vault root CA certificate to all VCF components:
#      * vCenter Servers: dir-cli trustedcert publish → VECS TRUSTED_ROOTS
#      * ESXi Hosts: appended to /etc/vmware/ssl/castore.pem
#      * NSX Managers: POST /api/v1/trust-management/certificates?action=import
#      * SDDC Manager: POST /v1/sddc-manager/trusted-certificates
#      * VCF Automation: OS trust store (/etc/pki/tls/certs/)
#      * VCF Operations VMs: OS trust store
#    - Re-registers NSX compute managers with updated vCenter thumbprints
#    - Idempotent: checks for existing CA before importing; skips UP compute managers
#
# 0c. vCenter CA Import (runs after Vault CA trust, with SKIP/RETRY/FAIL options):
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
#    - Set 9999-day password expiration for admin, root, audit users
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
# 7. VSP & Supervisor Proxy Configuration:
#    - Configures Supervisor HTTP/HTTPS proxy via vCenter API
#      (CLUSTER_CONFIGURED mode on WLD vCenter namespace-management API)
#    - Discovers VSP cluster node IPs via kubectl on the control plane VIP
#    - Configures OS-level proxy on each VSP node (Photon OS):
#      /etc/sysconfig/proxy, /etc/environment, containerd and kubelet
#      systemd drop-in files for HTTP_PROXY/HTTPS_PROXY/NO_PROXY
#    - Proxy: holorouter Squid at http://10.1.1.1:3128
#    - NO_PROXY includes internal subnets, service CIDRs, internal registry
#
# 8. Final Steps:
#    - Clear ARP cache on console and router
#    - Run vpodchecker.py to update L2 VMs (uuid, typematicdelay)
#
# USAGE:
#    python3 confighol.py                    # Full non-interactive HOLification
#    python3 confighol.py --dry-run          # Preview what would be done
#    python3 confighol.py --skip-vcshell     # Skip vCenter shell configuration
#    python3 confighol.py --skip-nsx         # Skip NSX configuration
#    python3 confighol.py --esx-only         # Only configure ESXi hosts
#
# NOTE: All operations run non-interactively. Unavailable components are
#       auto-skipped with a warning. NSX Edge SSH is enabled automatically
#       via Guest Operations. See HOLIFICATION.md for details.

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
from typing import Optional, Tuple, List

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

SCRIPT_VERSION = '2.13'
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

# NSX password expiration (9999 days)
NSX_PASSWORD_EXPIRY_DAYS = 9999

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
    checks whether bash is already the login shell by running a test command
    via SSH. If bash is already active, the function returns True without
    making changes. Otherwise it uses an expect script to change root's
    shell to /bin/bash.
    
    :param hostname: vCenter hostname
    :param password: Root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would enable bash shell for root')
        return True
    
    # Check if bash shell is already configured by testing SSH command execution
    check_result = lsf.ssh('echo SHELL_OK', f'root@{hostname}', password)
    if hasattr(check_result, 'stdout') and 'SHELL_OK' in (check_result.stdout or ''):
        lsf.write_output(f'{hostname}: Bash shell already enabled')
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


def configure_vcenter_mob(hostname: str, password: str,
                          dry_run: bool = False) -> bool:
    """
    Enable the Managed Object Browser (MOB) on vCenter by editing vpxd.cfg.
    
    Idempotent — checks current state before making changes and skips
    if already configured.
    
    :param hostname: vCenter hostname
    :param password: Root password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would configure MOB')
        return True

    # Enable the Managed Object Browser (MOB)
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
        
        # Check if MOB is already enabled (idempotent)
        mob_element = vpxd_element.find('enableDebugBrowse')
        if mob_element is not None and mob_element.text == 'true':
            lsf.write_output(f'{hostname}: MOB already enabled')
        elif mob_element is not None:
            mob_element.text = 'true'
            tree.write(LOCAL_VPXD_CONFIG)
            lsf.scp(LOCAL_VPXD_CONFIG, f'root@{hostname}:{VPXD_CONFIG}', password)
            lsf.write_output(f'{hostname}: Restarting vpxd service...')
            lsf.ssh('service-control --restart vmware-vpxd', f'root@{hostname}', password)
            lsf.write_output(f'{hostname}: MOB enabled successfully')
        else:
            mob_element = ET.Element('enableDebugBrowse')
            mob_element.text = 'true'
            vpxd_element.append(mob_element)
            tree.write(LOCAL_VPXD_CONFIG)
            lsf.scp(LOCAL_VPXD_CONFIG, f'root@{hostname}:{VPXD_CONFIG}', password)
            lsf.write_output(f'{hostname}: Restarting vpxd service...')
            lsf.ssh('service-control --restart vmware-vpxd', f'root@{hostname}', password)
            lsf.write_output(f'{hostname}: MOB enabled successfully')
        
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


def configure_auto_platform_isolation(hostname: str, user: str, password: str,
                                       dry_run: bool = False) -> bool:
    """
    Ensure auto-platform-a-* VM runs alone on its host via DRS rules.

    Creates VM/Host groups and a VM-Host affinity rule so that
    auto-platform-a-* is pinned to a dedicated host, then vMotions any
    other VMs off that host. A second anti-affinity rule prevents all
    other cluster VMs from landing on the same host.

    Only applies to vc-mgmt-a / cluster-mgmt-01a.

    :param hostname: vCenter hostname (only vc-mgmt-a is processed)
    :param user: vCenter SSO user
    :param password: vCenter password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if 'vc-mgmt-a' not in hostname:
        return True

    lsf.write_output(f'{hostname}: Configuring auto-platform-a host isolation...')

    if dry_run:
        lsf.write_output(f'{hostname}: Would create DRS groups/rules for auto-platform-a isolation')
        return True

    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        si = connect.SmartConnect(host=hostname, user=user, pwd=password,
                                  sslContext=context)
        content = si.RetrieveContent()

        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.ClusterComputeResource], True)
        cluster = None
        for c in container.view:
            if c.name == 'cluster-mgmt-01a':
                cluster = c
                break
        container.Destroy()

        if not cluster:
            lsf.write_output(f'{hostname}: WARNING - cluster-mgmt-01a not found')
            connect.Disconnect(si)
            return False

        # Find auto-platform-a VM
        auto_vm = None
        for vm in cluster.resourcePool.vm if hasattr(cluster, 'resourcePool') else []:
            if vm.name.startswith('auto-platform-a'):
                auto_vm = vm
                break
        if not auto_vm:
            all_vms_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.VirtualMachine], True)
            for vm in all_vms_view.view:
                if vm.name.startswith('auto-platform-a'):
                    auto_vm = vm
                    break
            all_vms_view.Destroy()

        if not auto_vm:
            lsf.write_output(f'{hostname}: WARNING - auto-platform-a VM not found')
            connect.Disconnect(si)
            return False

        auto_host = auto_vm.runtime.host
        lsf.write_output(f'{hostname}: Found {auto_vm.name} on {auto_host.name}')

        # -- Build existing group/rule index --
        existing_groups = {g.name: g for g in cluster.configurationEx.group}
        existing_rules = {r.name: r for r in cluster.configurationEx.rule}

        VM_GROUP = 'auto-platform-VMs'
        HOST_GROUP = 'auto-platform-Hosts'
        AFFINITY_RULE = 'auto-platform-must-run-on-host'
        ANTIAFFINITY_RULE = 'other-VMs-must-not-run-on-auto-platform-host'
        OTHER_VM_GROUP = 'non-auto-platform-VMs'

        # Collect VMs that should NOT be on auto_host
        other_vms_on_host = [v for v in auto_host.vm
                             if v.name != auto_vm.name
                             and v.runtime.powerState == 'poweredOn']

        # -- Step 1: Create / update groups --
        group_specs = []

        # VM group: auto-platform-a VM
        vm_group_spec = vim.cluster.GroupSpec()
        if VM_GROUP in existing_groups:
            vm_group_spec.operation = 'edit'
        else:
            vm_group_spec.operation = 'add'
        vm_group = vim.cluster.VmGroup()
        vm_group.name = VM_GROUP
        vm_group.vm = [auto_vm]
        vm_group_spec.info = vm_group
        group_specs.append(vm_group_spec)

        # Host group: the single dedicated host
        host_group_spec = vim.cluster.GroupSpec()
        if HOST_GROUP in existing_groups:
            host_group_spec.operation = 'edit'
        else:
            host_group_spec.operation = 'add'
        host_group = vim.cluster.HostGroup()
        host_group.name = HOST_GROUP
        host_group.host = [auto_host]
        host_group_spec.info = host_group
        group_specs.append(host_group_spec)

        # VM group: every other powered-on VM in the cluster
        all_other_vms = []
        for h in cluster.host:
            for v in h.vm:
                if not v.name.startswith('auto-platform-a'):
                    all_other_vms.append(v)
        other_vm_group_spec = vim.cluster.GroupSpec()
        if OTHER_VM_GROUP in existing_groups:
            other_vm_group_spec.operation = 'edit'
        else:
            other_vm_group_spec.operation = 'add'
        other_vm_group = vim.cluster.VmGroup()
        other_vm_group.name = OTHER_VM_GROUP
        other_vm_group.vm = all_other_vms
        other_vm_group_spec.info = other_vm_group
        group_specs.append(other_vm_group_spec)

        # Apply group changes
        spec = vim.cluster.ConfigSpecEx()
        spec.groupSpec = group_specs
        task = cluster.ReconfigureComputeResource_Task(spec, True)
        WaitForTask(task)
        lsf.write_output(f'{hostname}: SUCCESS - DRS groups created/updated')

        # -- Step 2: Create / update rules --
        # Re-read cluster config to get current rule keys after group changes
        current_rules = {r.name: r for r in cluster.configurationEx.rule}
        rule_specs = []

        # Must-run-on rule: auto-platform-a -> dedicated host
        affinity_spec = vim.cluster.RuleSpec()
        affinity_rule = vim.cluster.VmHostRuleInfo()
        affinity_rule.name = AFFINITY_RULE
        affinity_rule.enabled = True
        affinity_rule.mandatory = True
        affinity_rule.vmGroupName = VM_GROUP
        affinity_rule.affineHostGroupName = HOST_GROUP
        if AFFINITY_RULE in current_rules:
            affinity_spec.operation = 'edit'
            affinity_rule.key = current_rules[AFFINITY_RULE].key
        else:
            affinity_spec.operation = 'add'
        affinity_spec.info = affinity_rule
        rule_specs.append(affinity_spec)

        # Must-not-run-on rule: all other VMs cannot be on that host
        anti_spec = vim.cluster.RuleSpec()
        anti_rule = vim.cluster.VmHostRuleInfo()
        anti_rule.name = ANTIAFFINITY_RULE
        anti_rule.enabled = True
        anti_rule.mandatory = True
        anti_rule.vmGroupName = OTHER_VM_GROUP
        anti_rule.antiAffineHostGroupName = HOST_GROUP
        if ANTIAFFINITY_RULE in current_rules:
            anti_spec.operation = 'edit'
            anti_rule.key = current_rules[ANTIAFFINITY_RULE].key
        else:
            anti_spec.operation = 'add'
        anti_spec.info = anti_rule
        rule_specs.append(anti_spec)

        spec2 = vim.cluster.ConfigSpecEx()
        spec2.rulesSpec = rule_specs
        task2 = cluster.ReconfigureComputeResource_Task(spec2, True)
        WaitForTask(task2)
        lsf.write_output(f'{hostname}: SUCCESS - DRS rules created/updated')

        # -- Step 3: Migrate other VMs off the dedicated host --
        if other_vms_on_host:
            # Pick a destination host (any other host in the cluster)
            dest_hosts = [h for h in cluster.host if h != auto_host]
            if dest_hosts:
                dest_host = dest_hosts[0]
                for vm in other_vms_on_host:
                    lsf.write_output(
                        f'{hostname}: Migrating {vm.name} from '
                        f'{auto_host.name} to {dest_host.name}...')
                    try:
                        relocate_spec = vim.vm.RelocateSpec()
                        relocate_spec.host = dest_host
                        migrate_task = vm.RelocateVM_Task(relocate_spec)
                        WaitForTask(migrate_task)
                        lsf.write_output(
                            f'{hostname}: SUCCESS - {vm.name} migrated to '
                            f'{dest_host.name}')
                    except Exception as mig_err:
                        lsf.write_output(
                            f'{hostname}: WARNING - Could not migrate '
                            f'{vm.name}: {mig_err}')
            else:
                lsf.write_output(
                    f'{hostname}: WARNING - No alternative host for migration')
        else:
            lsf.write_output(
                f'{hostname}: {auto_vm.name} already sole VM on {auto_host.name}')

        connect.Disconnect(si)
        lsf.write_output(f'{hostname}: auto-platform-a host isolation configured')
        return True

    except Exception as e:
        lsf.write_output(
            f'{hostname}: ERROR configuring auto-platform-a isolation: {e}')
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
    
    # Step 1: Enable shell, browser support, and authorized_keys
    if not skip_shell:
        if not dry_run:
            # Enable bash shell
            enable_vcenter_shell(hostname, password, dry_run)
            
            # Configure SSH authorized_keys
            lsf.write_output(f'{hostname}: Copying authorized_keys')
            lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', password)
            lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', password)
            
            # Enable the Managed Object Browser (MOB)
            configure_vcenter_mob(hostname, password, dry_run)
        else:
            lsf.write_output(f'{hostname}: Would configure shell and MOB')
    
    # Step 2: Set password expiration for root
    if not dry_run:
        lsf.write_output(f'{hostname}: Setting non-expiring password for root')
        lsf.ssh('chage -M -1 root', f'root@{hostname}', password)
    else:
        lsf.write_output(f'{hostname}: Would set non-expiring password for root')
    
    # Step 3: Configure password policies and cluster settings
    configure_vcenter_password_policies(hostname, user, password, dry_run)
    
    # Step 3b: Isolate auto-platform-a to a dedicated host (vc-mgmt-a only)
    configure_auto_platform_isolation(hostname, user, password, dry_run)
    
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

def _get_sddc_managers() -> List[str]:
    """
    Discover all SDDC Manager FQDNs from config.ini.
    """
    sddc_managers = []
    if lsf and hasattr(lsf, 'config'):
        if lsf.config.has_section('VCF') and lsf.config.has_option('VCF', 'sddcmanager'):
            sddc_raw = lsf.config.get('VCF', 'sddcmanager').split('\n')
            for entry in sddc_raw:
                if not entry or entry.strip().startswith('#'):
                    continue
                hostname = entry.split(':')[0].strip()
                if hostname and hostname not in sddc_managers:
                    sddc_managers.append(hostname)
        
        if lsf.config.has_section('RESOURCES') and lsf.config.has_option('RESOURCES', 'urls'):
            urls_raw = lsf.config.get('RESOURCES', 'urls').split('\n')
            for entry in urls_raw:
                if 'sddcmanager' in entry.lower():
                    url = entry.split(',')[0].strip()
                    if '://' in url:
                        hostname = url.split('://')[1].split('/')[0].split(':')[0]
                        if hostname and hostname not in sddc_managers:
                            sddc_managers.append(hostname)
                            
    if not sddc_managers:
        sddc_managers = ['sddcmanager-a.site-a.vcf.lab']
    return sddc_managers


def _get_sddc_bearer_token(sddc_fqdn: str, password: str) -> Optional[str]:
    """
    Acquire a Bearer token from SDDC Manager.
    
    VCF 9.1 C4 requires Bearer token auth for the credentials API
    (Basic Auth returns empty results).
    
    :param sddc_fqdn: SDDC Manager FQDN
    :param password: Standard lab password
    :return: Bearer token string, or None if auth fails
    """
    sddc_url = f'https://{sddc_fqdn}'
    try:
        resp = requests.post(
            f'{sddc_url}/v1/tokens',
            json={'username': 'admin@local', 'password': password},
            headers={'Content-Type': 'application/json'},
            verify=False, timeout=30
        )
        if resp.status_code in (200, 201):
            return resp.json().get('accessToken')
    except Exception:
        pass
    return None


def get_nsx_root_password_from_sddc(nsx_fqdn: str, password: str,
                                     resource_type: str = 'NSXT_MANAGER') -> Optional[str]:
    """
    Retrieve the actual NSX root SSH password from SDDC Manager.
    
    SDDC Manager may have rotated the root password away from the standard
    lab password. Uses Bearer token auth (required for VCF 9.1 C4).
    
    Matches credentials by checking if the node hostname (with or without
    domain suffix) appears in the resource name. Also checks the cluster
    VIP name (e.g. nsx-wld01-01a -> nsx-wld01-a).
    
    :param nsx_fqdn: NSX hostname (e.g. nsx-wld01-01a or vna-wld01-01a.site-a.vcf.lab)
    :param password: Standard lab password (used to auth to SDDC Manager)
    :param resource_type: SDDC Manager resource type (NSXT_MANAGER or NSXT_EDGE)
    :return: The actual root password, or None if lookup fails
    """
    import re
    
    # Try to find the matching SDDC Manager based on the domain
    sddc_managers = _get_sddc_managers()
    sddc_fqdn = sddc_managers[0]
    
    if '.' in nsx_fqdn:
        nsx_domain = nsx_fqdn.split('.', 1)[1]
        for sm in sddc_managers:
            if nsx_domain in sm:
                sddc_fqdn = sm
                break
    else:
        # Try to match site-a, site-b from the short name
        site_match = re.search(r'-(site-[a-z])', nsx_fqdn)
        if not site_match:
            site_match = re.search(r'-(a|b)$', nsx_fqdn)
            
        if site_match:
            site_str = site_match.group(1)
            for sm in sddc_managers:
                if site_str in sm:
                    sddc_fqdn = sm
                    break

    sddc_url = f'https://{sddc_fqdn}'
    
    short_name = nsx_fqdn.split('.')[0]
    cluster_name = re.sub(r'-\d+([a-z])$', r'-\1', short_name)
    match_patterns = [short_name, cluster_name]
    if '.' in nsx_fqdn:
        match_patterns.append(nsx_fqdn)
    
    try:
        # Try Bearer token auth first (required for VCF 9.1 C4)
        token = _get_sddc_bearer_token(sddc_fqdn, password)
        if token:
            resp = requests.get(
                f'{sddc_url}/v1/credentials',
                params={'resourceType': resource_type},
                headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
                verify=False, timeout=30
            )
        else:
            # Fallback to Basic Auth for older VCF versions
            resp = requests.get(
                f'{sddc_url}/v1/credentials',
                params={'resourceType': resource_type},
                auth=('vcf', password),
                verify=False, timeout=30
            )
        
        if resp.status_code != 200:
            lsf.write_output(f'{nsx_fqdn}: SDDC Manager credentials API returned {resp.status_code}')
            return None
        
        data = resp.json()
        elements = data.get('elements', [])
        lsf.write_output(f'{nsx_fqdn}: SDDC Manager returned {len(elements)} {resource_type} credentials')
        
        for elem in elements:
            resource = elem.get('resource', {})
            rname = resource.get('resourceName', '')
            cred_type = elem.get('credentialType', '')
            username = elem.get('username', '')
            
            if cred_type == 'SSH' and username == 'root':
                if any(pattern in rname for pattern in match_patterns):
                    lsf.write_output(f'{nsx_fqdn}: Found root credential in SDDC Manager (resource: {rname})')
                    return elem.get('password', None)
        
        lsf.write_output(f'{nsx_fqdn}: No matching root SSH credential found in SDDC Manager '
                          f'(searched for: {match_patterns})')
        
        root_ssh_resources = [
            elem.get('resource', {}).get('resourceName', 'unknown')
            for elem in elements
            if elem.get('credentialType') == 'SSH' and elem.get('username') == 'root'
        ]
        if root_ssh_resources:
            lsf.write_output(f'{nsx_fqdn}: Available root SSH resources: {root_ssh_resources}')
        
        return None
    except Exception as e:
        lsf.write_output(f'{nsx_fqdn}: Could not retrieve root password from SDDC Manager: {e}')
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


def set_nsx_password_expiration_via_api(hostname: str, password: str,
                                        user_id: int, username: str,
                                        days: int,
                                        dry_run: bool = False) -> bool:
    """
    Set password expiration for an NSX user via the REST API.
    
    The NSX CLI command 'set user <user> password-expiration <days>' does not
    work reliably for admin/audit users in NSX 9.1 (sets frequency to 0
    instead of the requested value). The REST API is the reliable method.
    
    For NSX Manager nodes, calls /api/v1/node/users/{user_id} directly.
    For NSX Edge nodes, the caller should use the transport node endpoint
    via the managing NSX Manager.
    
    :param hostname: NSX Manager or Edge hostname (must expose API)
    :param password: Admin password
    :param user_id: Numeric user ID (0=root, 10000=admin, 10002=audit)
    :param username: Username (for logging only)
    :param days: Number of days until password expires
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would set {days}-day password expiration for {username}')
        return True
    
    lsf.write_output(f'{hostname}: Setting {days}-day password expiration for {username}...')
    
    try:
        resp = requests.put(
            f'https://{hostname}/api/v1/node/users/{user_id}',
            auth=('admin', password),
            json={'password_change_frequency': days},
            verify=False, timeout=30
        )
        if resp.status_code == 200:
            freq = resp.json().get('password_change_frequency', 'unknown')
            lsf.write_output(f'{hostname}: SUCCESS - {username} password expiration set to {freq} days')
            return True
        else:
            error_msg = resp.text[:100] if resp.text else 'Unknown error'
            lsf.write_output(f'{hostname}: FAILED - HTTP {resp.status_code}: {error_msg}')
            return False
    except Exception as e:
        lsf.write_output(f'{hostname}: FAILED - {e}')
        return False


NSX_USER_ID_MAP = {
    'root': 0,
    'admin': 10000,
    'audit': 10002,
}


def configure_nsx_ssh_start_on_boot(hostname: str, password: str,
                                     dry_run: bool = False) -> bool:
    """
    Configure NSX SSH service to start on boot via CLI command.
    
    The NSX API does not support setting start-on-boot directly, so we
    must use SSH to run the CLI command:
    - set service ssh start-on-boot
    
    Uses -T flag to disable PTY allocation (required for NSX Edge CLI).
    If the setting is already enabled, the command may return a non-zero
    exit code — we verify via 'get service ssh start-on-boot' and treat
    an already-enabled state as success.
    
    PREREQUISITE: SSH must already be enabled on the NSX appliance.
    
    :param hostname: NSX hostname
    :param password: Admin password
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'{hostname}: Would configure SSH start-on-boot')
        return True
    
    # Check current state first
    check_result = nsx_cli_ssh('get service ssh start-on-boot', hostname, password)
    check_output = ''
    if hasattr(check_result, 'stdout') and check_result.stdout:
        check_output = check_result.stdout.strip().lower()
    
    if 'true' in check_output or 'enabled' in check_output:
        lsf.write_output(f'{hostname}: SSH start-on-boot already enabled')
        return True
    
    lsf.write_output(f'{hostname}: Configuring SSH start-on-boot...')
    
    result = nsx_cli_ssh('set service ssh start-on-boot', hostname, password)
    
    if result.returncode == 0:
        lsf.write_output(f'{hostname}: SSH start-on-boot configured')
        return True
    
    # The set command may fail if already set — verify actual state
    verify_result = nsx_cli_ssh('get service ssh start-on-boot', hostname, password)
    verify_output = ''
    if hasattr(verify_result, 'stdout') and verify_result.stdout:
        verify_output = verify_result.stdout.strip().lower()
    
    if 'true' in verify_output or 'enabled' in verify_output:
        lsf.write_output(f'{hostname}: SSH start-on-boot already enabled (set command returned non-zero but state is correct)')
        return True
    
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
        lsf.write_output(f'{hostname}: WARNING - API SSH enablement failed, continuing with remaining steps')
        if not lsf.test_tcp_port(hostname, 22):
            lsf.write_output(f'{hostname}: FAILED - SSH port 22 not reachable, skipping configuration')
            return False

    if not dry_run:
        # Give SSH service time to start
        time.sleep(3)
        
        # Step 2: Resolve the actual root password before any root SSH operations.
        # SDDC Manager may have rotated root's password away from the standard
        # lab password. Test SSH first, then fall back to SDDC Manager lookup.
        root_password = password
        lsf.write_output(f'{hostname}: Testing root SSH access...')
        test_result = lsf.ssh('echo SSH_OK', f'root@{hostname}', password)
        
        if test_result.returncode != 0 or 'SSH_OK' not in str(getattr(test_result, 'stdout', '')):
            lsf.write_output(f'{hostname}: Standard password failed for root SSH - checking SDDC Manager...')
            sddc_root_pw = get_nsx_root_password_from_sddc(hostname, password)
            
            if sddc_root_pw and sddc_root_pw != password:
                lsf.write_output(f'{hostname}: Found rotated root password in SDDC Manager')
                # Try to reset root password back to standard via NSX API (user 0 = root)
                if reset_nsx_root_password(hostname, password, sddc_root_pw, password):
                    root_password = password
                    lsf.write_output(f'{hostname}: Root password reset to standard')
                    time.sleep(3)
                else:
                    # Reset failed — use the SDDC Manager password directly
                    root_password = sddc_root_pw
                    lsf.write_output(f'{hostname}: Using SDDC Manager password for root')
            elif sddc_root_pw and sddc_root_pw == password:
                lsf.write_output(f'{hostname}: SDDC Manager has same password - SSH may have another issue')
            else:
                lsf.write_output(f'{hostname}: Could not resolve root password from SDDC Manager')
        else:
            lsf.write_output(f'{hostname}: Root SSH access confirmed with standard password')
        
        # Step 2b: Copy authorized_keys using the resolved root password
        lsf.write_output(f'{hostname}: Copying authorized_keys...')
        lsf.ssh('mkdir -p /root/.ssh && chmod 700 /root/.ssh', f'root@{hostname}', root_password)
        result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', root_password)
        
        if result.returncode == 0:
            lsf.write_output(f'{hostname}: SUCCESS - authorized_keys copied')
            lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', root_password)
        else:
            lsf.write_output(f'{hostname}: FAILED - Could not copy authorized_keys')
            success = False
        
        # Step 3: Configure SSH to start on boot
        configure_nsx_ssh_start_on_boot(hostname, password, dry_run)
        
        # Step 4: Set password expiration via REST API (CLI is unreliable for admin/audit)
        for user in NSX_USERS:
            user_id = NSX_USER_ID_MAP.get(user)
            if user_id is not None:
                set_nsx_password_expiration_via_api(hostname, password, user_id, user,
                                                    NSX_PASSWORD_EXPIRY_DAYS, dry_run)
            else:
                lsf.write_output(f'{hostname}: WARNING - Unknown NSX user {user}, skipping')
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
    
    # Try name-based matching: edge-wld01-01a or vna-wld01-01a -> nsx-wld01-*
    edge_match = re.match(r'(?:edge|vna)-(\w+)-\d+', edge_hostname)
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


def set_nsx_edge_password_expiration(edge_hostname: str, nsx_manager: str,
                                       password: str, days: int,
                                       dry_run: bool = False) -> bool:
    """
    Set password expiration for all NSX users on an Edge node via the
    central NSX Manager's transport node API.
    
    Edges don't expose /api/v1/node/users directly; the managing NSX
    Manager proxies these calls through the transport node endpoint.
    
    :param edge_hostname: NSX Edge display name
    :param nsx_manager: NSX Manager FQDN
    :param password: Admin password
    :param days: Password expiration days
    :param dry_run: If True, preview only
    :return: True if all users updated successfully
    """
    if dry_run:
        for user in NSX_USERS:
            lsf.write_output(f'{edge_hostname}: Would set {days}-day password expiration for {user}')
        return True
    
    try:
        tn_url = f'https://{nsx_manager}/api/v1/transport-nodes'
        resp = requests.get(tn_url, auth=('admin', password), verify=False, timeout=30)
        if resp.status_code != 200:
            lsf.write_output(f'{edge_hostname}: Failed to query transport nodes')
            return False
        
        node_id = None
        for node in resp.json().get('results', []):
            if node.get('display_name', '') == edge_hostname:
                node_id = node.get('node_id', node.get('id'))
                break
        
        if not node_id:
            lsf.write_output(f'{edge_hostname}: Not found as transport node in {nsx_manager}')
            return False
        
        success = True
        for user, user_id in NSX_USER_ID_MAP.items():
            lsf.write_output(f'{edge_hostname}: Setting {days}-day password expiration for {user}...')
            url = f'https://{nsx_manager}/api/v1/transport-nodes/{node_id}/node/users/{user_id}'
            resp = requests.put(url, auth=('admin', password),
                                json={'password_change_frequency': days},
                                verify=False, timeout=30)
            if resp.status_code == 200:
                freq = resp.json().get('password_change_frequency', 'unknown')
                lsf.write_output(f'{edge_hostname}: SUCCESS - {user} password expiration set to {freq} days')
            else:
                lsf.write_output(f'{edge_hostname}: WARNING - Failed for {user}: HTTP {resp.status_code}')
                success = False
        
        return success
    except Exception as e:
        lsf.write_output(f'{edge_hostname}: Error setting password expiration: {e}')
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
    4. Sets 9999-day password expiration for admin, root, audit users
    
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
        nsx_mgr = _get_nsx_manager_for_edge(hostname)
        ssh_reachable = False
        
        # Step 1: Enable SSH via NSX Manager API if not already running
        if not lsf.test_tcp_port(hostname, 22):
            lsf.write_output(f'{hostname}: SSH not running - enabling via NSX Manager API...')
            if not nsx_mgr:
                lsf.write_output(f'{hostname}: FAILED - Could not determine NSX Manager for this edge')
                return False
            
            if not enable_nsx_edge_ssh_via_api(hostname, nsx_mgr, password, dry_run):
                lsf.write_output(f'{hostname}: WARNING - Could not enable SSH via NSX Manager API')
            else:
                time.sleep(5)
            
            if lsf.test_tcp_port(hostname, 22):
                ssh_reachable = True
            else:
                lsf.write_output(f'{hostname}: WARNING - SSH not reachable (STANDBY edge or mgmt IP unreachable)')
                lsf.write_output(f'{hostname}:         Password expiration will still be set via NSX Manager API')
        else:
            lsf.write_output(f'{hostname}: SSH already running')
            ssh_reachable = True
        
        if ssh_reachable:
            # Step 2: Copy authorized_keys for root user (skip for vna- edges as they use restricted shell)
            if hostname.startswith('vna-'):
                lsf.write_output(f'{hostname}: Skipping authorized_keys copy (vna- edges do not support direct root SSH keys)')
            else:
                # VNA edges may have rotated root passwords — try standard first,
                # then look up rotated password from SDDC Manager
                root_password = password
                lsf.write_output(f'{hostname}: Copying authorized_keys for root...')
                lsf.ssh('mkdir -p /root/.ssh && chmod 700 /root/.ssh', f'root@{hostname}', root_password)
                result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', root_password)
                if result.returncode != 0 and (result.returncode == 255 or 'permission denied' in str(result.stderr).lower()):
                    lsf.write_output(f'{hostname}: Standard password failed - checking SDDC Manager for rotated password...')
                    rotated_pw = get_nsx_root_password_from_sddc(hostname, password, resource_type='NSXT_EDGE')
                    if rotated_pw:
                        root_password = rotated_pw
                        lsf.ssh('mkdir -p /root/.ssh && chmod 700 /root/.ssh', f'root@{hostname}', root_password)
                        result = lsf.scp(auth_keys_file, f'root@{hostname}:{LINUX_AUTH_FILE}', root_password)
                
                if result.returncode == 0:
                    lsf.write_output(f'{hostname}: SUCCESS - authorized_keys copied')
                    chmod_result = lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{hostname}', root_password)
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
        
        # Step 4: Set password expiration via NSX Manager transport node API
        # This works even if SSH is unreachable (uses NSX Manager REST API)
        if nsx_mgr:
            set_nsx_edge_password_expiration(hostname, nsx_mgr, password,
                                              NSX_PASSWORD_EXPIRY_DAYS, dry_run)
        else:
            lsf.write_output(f'{hostname}: WARNING - Cannot set password expiration (no NSX Manager found)')
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
    from urllib.parse import urlparse
    
    # Collect hostnames from vravms config
    hostnames_to_configure = []
    for vravm in vravms:
        parts = vravm.split(':')
        hostname = parts[0].strip()
        hostname = re.sub(r'\.\*$', '', hostname)
        hostname = re.sub(r'\*$', '', hostname)
        hostname = hostname.rstrip('.')
        if hostname.lower().startswith('auto-'):
            hostnames_to_configure.append(hostname)
    
    # Also discover VCF Automation VMs from vraurls (e.g. auto-a.site-a.vcf.lab)
    if lsf.config.has_option('VCFFINAL', 'vraurls'):
        vraurls_raw = lsf.config.get('VCFFINAL', 'vraurls').strip()
        for line in vraurls_raw.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            url = line.split(',')[0].strip()
            try:
                parsed = urlparse(url)
                fqdn = parsed.hostname
                if fqdn and fqdn.startswith('auto-'):
                    short = fqdn.split('.')[0]
                    if short not in [h.split('.')[0] for h in hostnames_to_configure]:
                        hostnames_to_configure.append(fqdn)
            except Exception:
                pass
    
    for hostname in hostnames_to_configure:
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
        
        # Step 3: Set password to never expire for vmware-system-user and root
        for account in [ssh_user, 'root']:
            lsf.write_output(f'{hostname}: Setting non-expiring password for {account}...')
            chage_cmd = f"echo '{password}' | sudo -S chage -M -1 {account}"
            result = lsf.ssh(chage_cmd, f'{ssh_user}@{hostname}', password)
            if result.returncode == 0:
                lsf.write_output(f'{hostname}: SUCCESS - Non-expiring password set for {account}')
            else:
                lsf.write_output(f'{hostname}: WARNING - Failed to set password for {account}')
    else:
        lsf.write_output(f'{hostname}: Would copy authorized_keys for {ssh_user}')
        lsf.write_output(f'{hostname}: Would copy authorized_keys for root via sudo')
        lsf.write_output(f'{hostname}: Would set non-expiring password for {ssh_user} and root')
    
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
            
            parts = entry.split(':')
            nsxmgr = parts[0].strip()
            
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
    vcf_user = 'vcf'
    sddc_managers = _get_sddc_managers()
    success = True
    
    for sddcmgr in sddc_managers:
        lsf.write_output('')
        lsf.write_output(f'Configuring SDDC Manager: {sddcmgr}')
        lsf.write_output('-' * 50)
        lsf.write_output(f'{sddcmgr}: Using SSH user: {vcf_user}')
        
        if dry_run:
            lsf.write_output(f'{sddcmgr}: Would copy authorized_keys using ssh-copy-id')
            lsf.write_output(f'{sddcmgr}: Would set non-expiring passwords')
            continue
        
        # First check if the host is reachable
        if not lsf.test_ping(sddcmgr):
            lsf.write_output(f'{sddcmgr}: FAILED - Host is not reachable (ping failed)')
            success = False
            continue
        
        # Check if SSH port is open
        if not lsf.test_tcp_port(sddcmgr, 22):
            lsf.write_output(f'{sddcmgr}: FAILED - SSH port 22 is not open')
            success = False
            continue
        
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
            lsf.write_output(f'{sddcmgr}: Running expect script to set non-expiring passwords...')
            cmd = f'expect {expect_script} {sddcmgr} {password}'
            result = lsf.run_command(cmd)
            if result.returncode == 0:
                lsf.write_output(f'{sddcmgr}: SUCCESS - Passwords set to non-expiring')
            else:
                lsf.write_output(f'{sddcmgr}: FAILED - Expect script returned {result.returncode}')
                if result.stdout:
                    # Print last few lines of output
                    out_lines = result.stdout.strip().split('\n')
                    for line in out_lines[-5:]:
                        lsf.write_output(f'{sddcmgr}:         {line}')
                success = False
        else:
            lsf.write_output(f'{sddcmgr}: WARNING - Expect script not found at {expect_script}')
            success = False
            
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
        lsf.write_output('No VMs section in RESOURCES config')
    
    vms_raw = lsf.config.get('RESOURCES', 'VMs', fallback='').split('\n')
    ops_vms = []
    seen_short = set()
    
    for vm in vms_raw:
        if not vm or vm.strip().startswith('#'):
            continue
        if 'ops' in vm.lower():
            parts = vm.split(':')
            vm_name = parts[0].strip()
            vcenter = parts[1].strip() if len(parts) > 1 else ''
            
            if '.*' in vm_name or vm_name.endswith('*'):
                resolved = lsf.get_vm_match(vm_name)
                if resolved:
                    for rvm in resolved:
                        if 'ops' in rvm.name.lower():
                            short = rvm.name.split('.')[0]
                            if short not in seen_short:
                                actual_vc = vcenter
                                if hasattr(rvm, '_stub') and hasattr(rvm._stub, 'host'):
                                    actual_vc = rvm._stub.host.split(':')[0]
                                ops_vms.append((rvm.name, actual_vc))
                                seen_short.add(short)
                else:
                    lsf.write_output(f'Pattern "{vm_name}" matched no VMs in vCenter')
            else:
                short = vm_name.split('.')[0]
                if short not in seen_short:
                    actual_vc = vcenter
                    vm_to_use = vm_name
                    resolved = lsf.get_vm_match(f'^{short}$')
                    if resolved:
                        vm_to_use = resolved[0].name
                        if hasattr(resolved[0], '_stub') and hasattr(resolved[0]._stub, 'host'):
                            actual_vc = resolved[0]._stub.host.split(':')[0]
                    ops_vms.append((vm_to_use, actual_vc))
                    seen_short.add(short)
    
    # Also discover Ops VMs from vcfcomponenturls (e.g. opslogs-a.site-a.vcf.lab)
    if lsf.config.has_section('VCFFINAL') and lsf.config.has_option('VCFFINAL', 'vcfcomponenturls'):
        from urllib.parse import urlparse
        comp_urls = lsf.config.get('VCFFINAL', 'vcfcomponenturls').split('\n')
        default_vc = ''
        if lsf.config.has_option('RESOURCES', 'vCenters'):
            for vc_line in lsf.config.get('RESOURCES', 'vCenters').split('\n'):
                if vc_line and not vc_line.strip().startswith('#'):
                    default_vc = vc_line.split(':')[0].strip()
                    break
        for line in comp_urls:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            url = line.split(',')[0].strip()
            try:
                parsed = urlparse(url)
                fqdn = parsed.hostname
                if fqdn and 'ops' in fqdn.lower():
                    short = fqdn.split('.')[0]
                    if short not in seen_short:
                        actual_vc = default_vc
                        vm_to_use = fqdn
                        resolved = lsf.get_vm_match(f'^{short}$')
                        if resolved:
                            vm_to_use = resolved[0].name
                            if hasattr(resolved[0], '_stub') and hasattr(resolved[0]._stub, 'host'):
                                actual_vc = resolved[0]._stub.host.split(':')[0]
                        ops_vms.append((vm_to_use, actual_vc))
                        seen_short.add(short)
            except Exception:
                pass
    
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
        
        # opsnet VMs have no SSH access — skip entirely
        if 'opsnet' in opsvm.lower():
            lsf.write_output(f'{opsvm}: SKIPPING - opsnet VMs do not support SSH access')
            continue
        
        # opslogs VMs use vmware-system-user; others use root
        if 'opslogs' in opsvm.lower():
            ssh_user = 'vmware-system-user'
        else:
            ssh_user = 'root'
        
        if not dry_run:
            lsf.write_output(f'{opsvm}: Checking connectivity...')
            if not lsf.test_ping(opsvm):
                lsf.write_output(f'{opsvm}: SKIPPING - Host is not reachable (ping failed)')
                lsf.write_output(f'{opsvm}:           VM may not be deployed in this environment')
                continue
            lsf.write_output(f'{opsvm}: SUCCESS - Host is reachable')
            
            if not lsf.test_tcp_port(opsvm, 22):
                lsf.write_output(f'{opsvm}: SSH port 22 is not open - enabling via Guest Operations...')
                if vcenter:
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
            
            if ssh_user == 'root':
                # Direct root SSH
                lsf.write_output(f'{opsvm}: Setting non-expiring password for root...')
                result = lsf.ssh('chage -M -1 root', f'root@{opsvm}', password)
                if result.returncode == 0:
                    lsf.write_output(f'{opsvm}: SUCCESS - Non-expiring password set for root')
                elif result.returncode == 255:
                    lsf.write_output(f'{opsvm}: FAILED - SSH connection failed')
                    lsf.write_output(f'{opsvm}:         User: root, Password provided: {"yes" if password else "no"}')
                    vm_success = False
                elif 'permission denied' in str(result.stderr).lower():
                    lsf.write_output(f'{opsvm}: FAILED - Permission denied (invalid credentials)')
                    vm_success = False
                else:
                    lsf.write_output(f'{opsvm}: FAILED - chage command failed (exit code: {result.returncode})')
                    vm_success = False
                
                lsf.write_output(f'{opsvm}: Copying authorized_keys...')
                result = lsf.scp(auth_keys_file, f'root@{opsvm}:{LINUX_AUTH_FILE}', password)
                if result.returncode == 0:
                    lsf.write_output(f'{opsvm}: SUCCESS - authorized_keys copied')
                    chmod_result = lsf.ssh(f'chmod 600 {LINUX_AUTH_FILE}', f'root@{opsvm}', password)
                    if chmod_result.returncode == 0:
                        lsf.write_output(f'{opsvm}: SUCCESS - authorized_keys permissions set (chmod 600)')
                    else:
                        lsf.write_output(f'{opsvm}: WARNING - Failed to set permissions on authorized_keys')
                elif result.returncode == 255 or 'permission denied' in str(result.stderr).lower():
                    lsf.write_output(f'{opsvm}: FAILED - SCP failed (authentication error)')
                    vm_success = False
                else:
                    lsf.write_output(f'{opsvm}: FAILED - SCP failed (exit code: {result.returncode})')
                    vm_success = False
            else:
                # vmware-system-user SSH with sudo
                user_auth_file = f'/home/{ssh_user}/.ssh/authorized_keys'
                
                for account in [ssh_user, 'root']:
                    lsf.write_output(f'{opsvm}: Setting non-expiring password for {account}...')
                    chage_cmd = f"echo '{password}' | sudo -S chage -M -1 {account}"
                    result = lsf.ssh(chage_cmd, f'{ssh_user}@{opsvm}', password)
                    if result.returncode == 0:
                        lsf.write_output(f'{opsvm}: SUCCESS - Non-expiring password set for {account}')
                    else:
                        lsf.write_output(f'{opsvm}: WARNING - Failed to set password for {account}')
                        vm_success = False
                
                lsf.write_output(f'{opsvm}: Copying authorized_keys for {ssh_user}...')
                result = lsf.scp(auth_keys_file, f'{ssh_user}@{opsvm}:{user_auth_file}', password)
                if result.returncode == 0:
                    lsf.write_output(f'{opsvm}: SUCCESS - authorized_keys copied for {ssh_user}')
                    lsf.ssh(f'chmod 600 {user_auth_file}', f'{ssh_user}@{opsvm}', password)
                else:
                    lsf.write_output(f'{opsvm}: FAILED - Could not copy authorized_keys for {ssh_user}')
                    vm_success = False
                
                lsf.write_output(f'{opsvm}: Copying authorized_keys for root via sudo...')
                sudo_cmd = (
                    f"echo '{password}' | sudo -S mkdir -p /root/.ssh && "
                    f"echo '{password}' | sudo -S cp {user_auth_file} /root/.ssh/authorized_keys && "
                    f"echo '{password}' | sudo -S chmod 600 /root/.ssh/authorized_keys"
                )
                result = lsf.ssh(sudo_cmd, f'{ssh_user}@{opsvm}', password)
                if result.returncode == 0:
                    lsf.write_output(f'{opsvm}: SUCCESS - authorized_keys copied for root')
                else:
                    lsf.write_output(f'{opsvm}: WARNING - Failed to copy authorized_keys for root')
            
            if vm_success:
                lsf.write_output(f'{opsvm}: Configuration completed successfully')
            else:
                lsf.write_output(f'{opsvm}: Configuration completed with errors')
                overall_success = False
        else:
            lsf.write_output(f'{opsvm}: Would check connectivity (ping, SSH port)')
            lsf.write_output(f'{opsvm}: Would enable SSH via Guest Operations if not running')
            lsf.write_output(f'{opsvm}: Would set non-expiring password (SSH user: {ssh_user})')
            lsf.write_output(f'{opsvm}: Would copy authorized_keys')
    
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
    Handle Vault CA not accessible — auto-skip for non-interactive execution.
    
    :param message: Error message describing why Vault is not accessible
    :return: Always 'skip' for non-interactive mode
    """
    lsf.write_output(f'WARNING: Vault PKI CA Certificate Not Accessible - {message}')
    lsf.write_output('Auto-skipping Vault CA import (non-interactive mode)')
    return 'skip'


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
# VAULT CA TRUST DISTRIBUTION ACROSS VCF SUITE
#==============================================================================


def _trust_vault_ca_on_vcenter(hostname: str, user: str, password: str,
                                ca_pem: str, dry_run: bool = False) -> bool:
    """
    Import the Vault root CA into a vCenter's TRUSTED_ROOTS store.

    Uses dir-cli trustedcert publish to add the CA to vmdir, then
    vecs-cli to refresh the TRUSTED_ROOTS VECS store.

    :param hostname: vCenter FQDN
    :param user: SSO admin user (e.g. administrator@vsphere.local)
    :param password: Root/admin password
    :param ca_pem: PEM-encoded CA certificate
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'  {hostname}: Would import Vault CA via dir-cli trustedcert publish')
        return True

    if not lsf.test_tcp_port(hostname, 22):
        lsf.write_output(f'  {hostname}: SKIP - SSH port 22 not open')
        return False

    # Determine SSO domain from the user parameter
    sso_domain = 'vsphere.local'
    if '@' in user:
        sso_domain = user.split('@')[1]
    admin_user = f'administrator@{sso_domain}'

    # Check if the Vault CA is already published in vmdir (prevents double-cert bug).
    # dir-cli trustedcert publish silently appends a duplicate PEM into the same
    # vmdir entry when called twice, creating a multi-cert PEM that breaks NSX
    # compute-manager re-registration (NSX TrustStoreServiceImpl error MP2179).
    check_cmd = (
        f"/usr/lib/vmware-vmafd/bin/dir-cli trustedcert list "
        f"--login {admin_user} --password '{password}' 2>/dev/null "
        f"| grep -c 'vcf.lab Root Authority'"
    )
    result = lsf.ssh(check_cmd, f'root@{hostname}', password)
    stdout = getattr(result, 'stdout', '') or ''
    # The SSH banner may prepend text. grep -c prints the count on the last line.
    lines = [line.strip() for line in stdout.strip().split('\n') if line.strip()]
    count = lines[-1] if lines else '0'
    if count != '0':
        lsf.write_output(f'  {hostname}: Vault CA already trusted in vmdir')
        lsf.ssh('/usr/lib/vmware-vmafd/bin/vecs-cli force-refresh', f'root@{hostname}', password)
        return True

    # Write CA PEM to a temp file on vCenter
    escaped_pem = ca_pem.replace("'", "'\\''")
    write_cmd = f"echo '{escaped_pem}' > /tmp/vault-ca.pem"
    lsf.ssh(write_cmd, f'root@{hostname}', password)

    # Publish to vmdir via dir-cli (makes it available cluster-wide)
    publish_cmd = (
        f'/usr/lib/vmware-vmafd/bin/dir-cli trustedcert publish '
        f'--cert /tmp/vault-ca.pem '
        f'--login {admin_user} --password \'{password}\''
    )
    result = lsf.ssh(publish_cmd, f'root@{hostname}', password)
    if hasattr(result, 'returncode') and result.returncode != 0:
        lsf.write_output(f'  {hostname}: WARNING - dir-cli publish returned non-zero')

    # Force VECS refresh so the cert appears in TRUSTED_ROOTS immediately
    lsf.ssh('/usr/lib/vmware-vmafd/bin/vecs-cli force-refresh', f'root@{hostname}', password)

    lsf.write_output(f'  {hostname}: SUCCESS - Vault CA published to TRUSTED_ROOTS')

    # Clean up
    lsf.ssh('rm -f /tmp/vault-ca.pem', f'root@{hostname}', password)

    return True


def _trust_vault_ca_on_esxi(hostname: str, password: str,
                             ca_pem: str, dry_run: bool = False) -> bool:
    """
    Import the Vault root CA into an ESXi host's castore.pem.

    Appends the CA cert to /etc/vmware/ssl/castore.pem and persists
    with /sbin/auto-backup.sh.

    :param hostname: ESXi host FQDN
    :param password: Root password
    :param ca_pem: PEM-encoded CA certificate
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'  {hostname}: Would append Vault CA to /etc/vmware/ssl/castore.pem')
        return True

    if not lsf.test_tcp_port(hostname, 22):
        lsf.write_output(f'  {hostname}: SKIP - SSH port 22 not open')
        return False

    # Check if already present
    check_cmd = 'grep -c "vcf.lab Root Authority" /etc/vmware/ssl/castore.pem 2>/dev/null || echo 0'
    result = lsf.ssh(check_cmd, f'root@{hostname}', password)
    stdout = getattr(result, 'stdout', '') or ''
    lines = [line.strip() for line in stdout.strip().split('\n') if line.strip()]
    count = lines[-1] if lines else '0'
    if count != '0':
        lsf.write_output(f'  {hostname}: Vault CA already in castore.pem')
        return True

    # Write the CA PEM to a temp file and append to castore
    escaped_pem = ca_pem.replace("'", "'\\''")
    append_cmd = (
        f"echo '{escaped_pem}' >> /etc/vmware/ssl/castore.pem && "
        f"/sbin/auto-backup.sh > /dev/null 2>&1"
    )
    result = lsf.ssh(append_cmd, f'root@{hostname}', password)

    if hasattr(result, 'returncode') and result.returncode != 0:
        lsf.write_output(f'  {hostname}: WARNING - Failed to append CA to castore.pem')
        return False

    lsf.write_output(f'  {hostname}: SUCCESS - Vault CA appended to castore.pem')
    return True


def _trust_vault_ca_on_nsx(hostname: str, password: str,
                            ca_pem: str, dry_run: bool = False) -> bool:
    """
    Import the Vault root CA into an NSX Manager's trust store via API.

    Uses POST /api/v1/trust-management/certificates?action=import.

    :param hostname: NSX Manager FQDN
    :param password: Admin password
    :param ca_pem: PEM-encoded CA certificate
    :param dry_run: If True, preview only
    :return: True if successful
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if dry_run:
        lsf.write_output(f'  {hostname}: Would import Vault CA via NSX trust-management API')
        return True

    # Check if already imported by listing existing certs
    try:
        list_url = f'https://{hostname}/api/v1/trust-management/certificates'
        resp = requests.get(list_url, auth=('admin', password),
                            verify=False, timeout=30)
        if resp.status_code == 200:
            certs_data = resp.json()
            for cert_entry in certs_data.get('results', []):
                pem = cert_entry.get('pem_encoded', '')
                display = cert_entry.get('display_name', '')
                if 'vcf.lab' in pem or 'vcf.lab Root Authority' in display:
                    lsf.write_output(f'  {hostname}: Vault CA already in NSX trust store')
                    return True
    except Exception:
        pass

    # Import the CA
    try:
        import_url = f'https://{hostname}/api/v1/trust-management/certificates?action=import'
        pem_with_newlines = ca_pem.replace('\n', '\\n')
        payload = {
            'display_name': VAULT_CA_NAME,
            'pem_encoded': ca_pem
        }
        resp = requests.post(import_url, auth=('admin', password),
                             json=payload, verify=False, timeout=30)

        if resp.status_code in (200, 201):
            lsf.write_output(f'  {hostname}: SUCCESS - Vault CA imported to NSX trust store')
            return True
        elif resp.status_code == 409:
            lsf.write_output(f'  {hostname}: Vault CA already exists in NSX trust store')
            return True
        else:
            lsf.write_output(f'  {hostname}: WARNING - NSX import returned HTTP {resp.status_code}')
            body = resp.text[:200] if resp.text else ''
            if body:
                lsf.write_output(f'  {hostname}:   Response: {body}')
            return False
    except Exception as e:
        lsf.write_output(f'  {hostname}: ERROR - {e}')
        return False


def _nsx_reregister_compute_managers(hostname: str, password: str,
                                      lab_password: str,
                                      dry_run: bool = False) -> bool:
    """
    Re-register all compute managers in an NSX Manager to pick up new
    vCenter certificate trust chains.

    After a vCenter SSL cert is replaced, NSX compute managers go DOWN
    because the trusted root cert no longer matches. This PUTs each
    compute manager with the new vCenter SHA-256 thumbprint, forcing NSX
    to re-validate the connection.

    :param hostname: NSX Manager FQDN
    :param password: NSX admin password
    :param lab_password: vCenter admin password (used in the credential payload)
    :param dry_run: If True, preview only
    :return: True if all compute managers were re-registered
    """
    import urllib3, ssl, socket, hashlib
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if dry_run:
        lsf.write_output(f'  {hostname}: Would re-register compute managers')
        return True

    try:
        base = f'https://{hostname}/api/v1/fabric/compute-managers'
        auth = ('admin', password)
        r = requests.get(base, auth=auth, verify=False, timeout=30)
        if r.status_code != 200:
            lsf.write_output(f'  {hostname}: WARNING - failed to list compute managers (HTTP {r.status_code})')
            return False

        cms = r.json().get('results', [])
        if not cms:
            lsf.write_output(f'  {hostname}: No compute managers found')
            return True

        all_ok = True
        for cm in cms:
            cm_id = cm['id']
            vc_server = cm.get('server', 'unknown')

            # Check if this CM is already UP
            status_r = requests.get(f'{base}/{cm_id}/status', auth=auth, verify=False, timeout=30)
            if status_r.status_code == 200:
                status = status_r.json()
                if status.get('connection_status') == 'UP' and status.get('registration_status') == 'REGISTERED':
                    if not status.get('registration_errors') and not status.get('connection_errors'):
                        lsf.write_output(f'  {hostname}: {vc_server} already UP and REGISTERED')
                        continue

            # Get the current SHA-256 thumbprint of the vCenter
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with ctx.wrap_socket(socket.socket(), server_hostname=vc_server) as sock:
                    sock.settimeout(10)
                    sock.connect((vc_server, 443))
                    der = sock.getpeercert(binary_form=True)
                digest = hashlib.sha256(der).hexdigest().upper()
                thumb = ':'.join(digest[i:i+2] for i in range(0, len(digest), 2))
            except Exception as e:
                lsf.write_output(f'  {hostname}: WARNING - cannot get thumbprint for {vc_server}: {e}')
                all_ok = False
                continue

            # Determine the SSO admin user from the existing credential
            sso_user = 'administrator@vsphere.local'
            existing_cred = cm.get('credential', {})
            if existing_cred.get('username'):
                sso_user = existing_cred['username']

            # Build the PUT payload
            for k in ['_create_time', '_create_user', '_last_modified_time',
                       '_last_modified_user', '_protection', '_system_owned',
                       'origin_properties', 'certificate']:
                cm.pop(k, None)

            cm['credential'] = {
                'credential_type': 'UsernamePasswordLoginCredential',
                'username': sso_user,
                'password': lab_password,
                'thumbprint': thumb
            }

            r2 = requests.put(f'{base}/{cm_id}', auth=auth, json=cm,
                              verify=False, timeout=60)
            if r2.status_code in (200, 201):
                lsf.write_output(f'  {hostname}: {vc_server} re-registered (rev={r2.json().get("_revision")})')
            else:
                body = r2.text[:200] if r2.text else ''
                lsf.write_output(f'  {hostname}: WARNING - {vc_server} re-register returned HTTP {r2.status_code}: {body}')
                all_ok = False

        return all_ok
    except Exception as e:
        lsf.write_output(f'  {hostname}: ERROR - {e}')
        return False


def _trust_vault_ca_on_sddc_manager(hostname: str, password: str,
                                     ca_pem: str, dry_run: bool = False) -> bool:
    """
    Import the Vault root CA into SDDC Manager's trusted certificates.

    Uses POST /v1/sddc-manager/trusted-certificates.

    :param hostname: SDDC Manager FQDN
    :param password: VCF admin password
    :param ca_pem: PEM-encoded CA certificate
    :param dry_run: If True, preview only
    :return: True if successful
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if dry_run:
        lsf.write_output(f'  {hostname}: Would import Vault CA via SDDC Manager trusted-certificates API')
        return True

    # Get Bearer token
    try:
        token_url = f'https://{hostname}/v1/tokens'
        token_resp = requests.post(token_url, json={
            'username': 'admin@local', 'password': password
        }, verify=False, timeout=30)

        if token_resp.status_code not in (200, 201):
            lsf.write_output(f'  {hostname}: WARNING - Failed to get Bearer token: HTTP {token_resp.status_code}')
            return False

        token = token_resp.json().get('accessToken', '')
        if not token:
            lsf.write_output(f'  {hostname}: WARNING - Empty Bearer token')
            return False
    except Exception as e:
        lsf.write_output(f'  {hostname}: ERROR getting token - {e}')
        return False

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

    # Check if already imported
    try:
        list_url = f'https://{hostname}/v1/sddc-manager/trusted-certificates'
        list_resp = requests.get(list_url, headers=headers, verify=False, timeout=30)
        if list_resp.status_code == 200:
            existing = list_resp.json()
            elements = existing if isinstance(existing, list) else existing.get('elements', [])
            for entry in elements:
                alias = entry.get('alias', '')
                if 'vcf.lab' in alias.lower() or 'vault' in alias.lower():
                    lsf.write_output(f'  {hostname}: Vault CA already trusted (alias: {alias})')
                    return True
    except Exception:
        pass

    # Import
    try:
        import_url = f'https://{hostname}/v1/sddc-manager/trusted-certificates'
        payload = {
            'certificate': ca_pem,
            'certificateUsageType': 'TRUSTED_FOR_OUTBOUND'
        }
        resp = requests.post(import_url, headers=headers, json=payload,
                             verify=False, timeout=30)

        if resp.status_code in (200, 201, 202):
            lsf.write_output(f'  {hostname}: SUCCESS - Vault CA imported to SDDC Manager trust store')
            return True
        elif resp.status_code == 409:
            lsf.write_output(f'  {hostname}: Vault CA already in SDDC Manager trust store')
            return True
        else:
            lsf.write_output(f'  {hostname}: WARNING - SDDC Manager import returned HTTP {resp.status_code}')
            body = resp.text[:200] if resp.text else ''
            if body:
                lsf.write_output(f'  {hostname}:   Response: {body}')
            return False
    except Exception as e:
        lsf.write_output(f'  {hostname}: ERROR - {e}')
        return False


def _trust_vault_ca_on_linux_appliance(hostname: str, user: str,
                                        password: str, ca_pem: str,
                                        dry_run: bool = False) -> bool:
    """
    Import the Vault root CA into a Linux appliance's OS trust store.

    Works for Photon OS (VCF Automation, VSP nodes, etc.) and
    other Linux appliances by writing to the system CA trust bundle
    and running update-ca-certificates or rehash.

    :param hostname: Appliance FQDN or IP
    :param user: SSH user
    :param password: SSH password
    :param ca_pem: PEM-encoded CA certificate
    :param dry_run: If True, preview only
    :return: True if successful
    """
    if dry_run:
        lsf.write_output(f'  {hostname}: Would add Vault CA to OS trust store')
        return True

    if not lsf.test_tcp_port(hostname, 22):
        lsf.write_output(f'  {hostname}: SKIP - SSH port 22 not open')
        return False

    target = f'{user}@{hostname}'
    escaped_pem = ca_pem.replace("'", "'\\''")

    # Determine if we need sudo
    needs_sudo = user != 'root'
    sudo_prefix = f"echo '{password}' | sudo -S " if needs_sudo else ''

    # Check if already present
    check_cmd = f'{sudo_prefix}grep -c "vcf.lab Root Authority" /etc/ssl/certs/ca-certificates.crt 2>/dev/null || echo 0'
    result = lsf.ssh(check_cmd, target, password)
    stdout = getattr(result, 'stdout', '') or ''
    lines = [line.strip() for line in stdout.strip().split('\n') if line.strip()]
    count = lines[-1] if lines else '0'
    if count != '0':
        lsf.write_output(f'  {hostname}: Vault CA already in OS trust store')
        return True

    # Write the cert to the trusted anchors directory (works on both Photon and Ubuntu)
    # Photon: /etc/pki/tls/certs/  or /etc/ssl/certs/
    # Ubuntu: /usr/local/share/ca-certificates/
    write_cmd = f"echo '{escaped_pem}' | {sudo_prefix}tee /etc/pki/tls/certs/vault-ca.pem > /dev/null 2>/dev/null"
    lsf.ssh(write_cmd, target, password)

    write_cmd2 = f"echo '{escaped_pem}' | {sudo_prefix}tee /usr/local/share/ca-certificates/vault-ca.crt > /dev/null 2>/dev/null"
    lsf.ssh(write_cmd2, target, password)

    # Run update-ca-certificates (Ubuntu/Debian) or rehash (Photon/RHEL)
    update_cmd = (
        f'{sudo_prefix}update-ca-certificates 2>/dev/null || '
        f'{sudo_prefix}update-ca-trust 2>/dev/null || '
        f'{sudo_prefix}c_rehash /etc/ssl/certs 2>/dev/null || true'
    )
    lsf.ssh(update_cmd, target, password)

    lsf.write_output(f'  {hostname}: SUCCESS - Vault CA added to OS trust store')
    return True


def distribute_vault_ca_trust(ca_pem: str, password: str,
                               dry_run: bool = False) -> bool:
    """
    Distribute the Vault root CA certificate to all VCF components.

    Imports the CA into:
    - vCenter TRUSTED_ROOTS (via dir-cli trustedcert publish)
    - ESXi hosts (appended to /etc/vmware/ssl/castore.pem)
    - NSX Managers (via trust-management API)
    - SDDC Manager (via trusted-certificates API)
    - VCF Automation appliances (OS trust store)
    - VCF Operations VMs (OS trust store)

    :param ca_pem: PEM-encoded Vault root CA certificate
    :param password: Lab password
    :param dry_run: If True, preview only
    :return: True if at least some components succeeded
    """
    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('Vault CA Trust Distribution Across VCF Suite')
    lsf.write_output('=' * 60)

    success_count = 0
    total_count = 0

    # --- vCenters ---
    lsf.write_output('')
    lsf.write_output('--- vCenter Servers ---')
    if 'RESOURCES' in lsf.config and 'vCenters' in lsf.config['RESOURCES']:
        vcenter_entries = lsf.config.get('RESOURCES', 'vCenters').split('\n')
        for entry in vcenter_entries:
            entry = entry.strip()
            if not entry or entry.startswith('#'):
                continue
            parts = entry.split(':')
            hostname = parts[0].strip()
            user = parts[2].strip() if len(parts) > 2 else 'administrator@vsphere.local'
            total_count += 1
            if _trust_vault_ca_on_vcenter(hostname, user, password, ca_pem, dry_run):
                success_count += 1
    else:
        lsf.write_output('  No vCenters found in config.ini')

    # --- ESXi Hosts ---
    lsf.write_output('')
    lsf.write_output('--- ESXi Hosts ---')
    if 'RESOURCES' in lsf.config and 'ESXiHosts' in lsf.config['RESOURCES']:
        esx_entries = lsf.config.get('RESOURCES', 'ESXiHosts').split('\n')
        for entry in esx_entries:
            entry = entry.strip()
            if not entry or entry.startswith('#'):
                continue
            parts = entry.split(':')
            hostname = parts[0].strip()
            total_count += 1
            if _trust_vault_ca_on_esxi(hostname, password, ca_pem, dry_run):
                success_count += 1
    else:
        lsf.write_output('  No ESXi hosts found in config.ini')

    # --- NSX Managers ---
    lsf.write_output('')
    lsf.write_output('--- NSX Managers ---')
    nsx_hostnames = []
    if 'VCF' in lsf.config and 'vcfnsxmgr' in lsf.config['VCF']:
        nsx_entries = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
        for entry in nsx_entries:
            entry = entry.strip()
            if not entry or entry.startswith('#'):
                continue
            parts = entry.split(':')
            hostname = parts[0].strip()
            nsx_hostnames.append(hostname)
            total_count += 1
            if _trust_vault_ca_on_nsx(hostname, password, ca_pem, dry_run):
                success_count += 1
    else:
        lsf.write_output('  No NSX Managers found in config.ini')

    # --- NSX Compute Manager Re-registration ---
    # After importing the Vault CA into NSX and vCenter trust stores, NSX
    # compute managers need to be re-registered (PUT) so NSX re-validates
    # the vCenter connection with the updated trust chain.
    lsf.write_output('')
    lsf.write_output('--- NSX Compute Manager Re-registration ---')
    for hostname in nsx_hostnames:
        _nsx_reregister_compute_managers(hostname, password, password, dry_run)

    # --- SDDC Manager ---
    lsf.write_output('')
    lsf.write_output('--- SDDC Manager ---')
    sddc_managers = _get_sddc_managers()
    for sddcmgr in sddc_managers:
        if lsf.test_ping(sddcmgr):
            total_count += 1
            if _trust_vault_ca_on_sddc_manager(sddcmgr, password, ca_pem, dry_run):
                success_count += 1
        else:
            lsf.write_output(f'  {sddcmgr}: SKIP - not reachable')

    # --- VCF Automation Appliances ---
    lsf.write_output('')
    lsf.write_output('--- VCF Automation Appliances ---')
    vcfa_hosts = []
    
    # Dynamically find VCF Automation VMs from config.ini
    if lsf and hasattr(lsf, 'config'):
        for section in ['VCFFINAL', 'VCF', 'RESOURCES']:
            if lsf.config.has_section(section):
                # Check vravms
                if lsf.config.has_option(section, 'vravms'):
                    for entry in lsf.config.get(section, 'vravms').split('\n'):
                        if entry.strip() and not entry.strip().startswith('#'):
                            hostname = entry.split(':')[0].strip()
                            if hostname and hostname.replace('.*', '') not in [h for h, _ in vcfa_hosts]:
                                # If it's a regex like auto-platform-a.*, just use the base name
                                clean_host = hostname.replace('.*', '')
                                vcfa_hosts.append((clean_host, 'vmware-system-user'))
                
                # Check vraurls
                if lsf.config.has_option(section, 'vraurls'):
                    for entry in lsf.config.get(section, 'vraurls').split('\n'):
                        if entry.strip() and not entry.strip().startswith('#'):
                            url = entry.split(',')[0].strip()
                            if '://' in url:
                                hostname = url.split('://')[1].split('/')[0].split(':')[0]
                                if hostname and hostname not in [h for h, _ in vcfa_hosts]:
                                    vcfa_hosts.append((hostname, 'vmware-system-user'))
                                    
    # Fallback to defaults if none found
    if not vcfa_hosts:
        vcfa_hosts = [
            ('auto-a.site-a.vcf.lab', 'vmware-system-user'),
            ('auto-platform-a.site-a.vcf.lab', 'vmware-system-user'),
        ]

    for vcfa_host, vcfa_user in vcfa_hosts:
        if lsf.test_ping(vcfa_host):
            total_count += 1
            if _trust_vault_ca_on_linux_appliance(vcfa_host, vcfa_user, password, ca_pem, dry_run):
                success_count += 1
        else:
            lsf.write_output(f'  {vcfa_host}: SKIP - not reachable')

    # --- VCF Operations VMs ---
    lsf.write_output('')
    lsf.write_output('--- VCF Operations VMs ---')
    ops_vms = []
    if 'VCF' in lsf.config and 'vcfopsvms' in lsf.config['VCF']:
        ops_entries = lsf.config.get('VCF', 'vcfopsvms').split('\n')
        for entry in ops_entries:
            entry = entry.strip()
            if not entry or entry.startswith('#'):
                continue
            parts = entry.split(':')
            hostname = parts[0].strip()
            ops_vms.append(hostname)
    # # Also try well-known ops hostnames if not in config
    # for ops_host in ['ops-a.site-a.vcf.lab', 'opslogs-a.site-a.vcf.lab', 'ops-b.site-b.vcf.lab', 'opslogs-b.site-b.vcf.lab']:
    #     if ops_host not in ops_vms:
    #         ops_vms.append(ops_host)

    for ops_host in ops_vms:
        # Determine SSH user based on hostname
        if 'opslogs' in ops_host:
            ssh_user = 'vmware-system-user'
        else:
            ssh_user = 'root'

        if lsf.test_ping(ops_host):
            total_count += 1
            if _trust_vault_ca_on_linux_appliance(ops_host, ssh_user, password, ca_pem, dry_run):
                success_count += 1
        else:
            lsf.write_output(f'  {ops_host}: SKIP - not reachable')

    # --- Summary ---
    lsf.write_output('')
    lsf.write_output(f'Vault CA Trust Distribution: {success_count}/{total_count} components succeeded')

    return success_count > 0


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
    Each zip may contain multiple CA certificates (VMCA root, Broadcom VCF
    root, etc.) that all need to be imported.
    
    We use only the Linux (.0) files to avoid importing the same cert twice
    from both lin/ and win/ directories, and deduplicate by SHA-256 fingerprint.
    
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
        
        certificates = []
        seen_fingerprints = set()
        
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for filename in zf.namelist():
                # Use only Linux format (.0) to avoid duplicates across lin/win/mac
                if not (filename.endswith('.0') and '/lin/' in filename):
                    continue
                
                cert_data = zf.read(filename)
                cert_pem = cert_data.decode('utf-8')
                
                if '-----BEGIN CERTIFICATE-----' not in cert_pem:
                    continue
                
                # Deduplicate by SHA-256 fingerprint
                try:
                    fp_result = subprocess.run(
                        ['openssl', 'x509', '-noout', '-fingerprint', '-sha256'],
                        input=cert_pem, capture_output=True, text=True
                    )
                    fingerprint = fp_result.stdout.strip() if fp_result.returncode == 0 else None
                except Exception:
                    fingerprint = None
                
                if fingerprint and fingerprint in seen_fingerprints:
                    continue
                if fingerprint:
                    seen_fingerprints.add(fingerprint)
                
                # Build a friendly, unique nickname from the certificate subject
                cert_name = f"{vcenter_hostname} CA"
                try:
                    result = subprocess.run(
                        ['openssl', 'x509', '-noout', '-subject'],
                        input=cert_pem, capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        subject = result.stdout.strip()
                        if 'O = ' in subject:
                            org = subject.split('O = ')[1].split(',')[0].strip()
                            # Strip surrounding quotes that openssl may include
                            org = org.strip('"')
                            cert_name = f"{org} CA"
                        elif 'CN = ' in subject:
                            cn = subject.split('CN = ')[1].split(',')[0].strip()
                            cn = cn.strip('"')
                            cert_name = cn
                except Exception:
                    pass
                
                certificates.append((cert_name, cert_pem))
                lsf.write_output(f'  Found certificate: {cert_name} ({os.path.basename(filename)})')
        
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
    Handle vCenter CA not accessible — auto-skip for non-interactive execution.
    
    :param vcenter_hostname: The vCenter that is not accessible
    :param message: Error message describing the issue
    :return: Always 'skip' for non-interactive mode
    """
    lsf.write_output(f'WARNING: vCenter {vcenter_hostname} CA not accessible - {message}')
    lsf.write_output(f'Auto-skipping vCenter {vcenter_hostname} (non-interactive mode)')
    return 'skip'


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

    sddc_managers = _get_sddc_managers()
    overall_success = True

    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('SDDC Manager Auto-Rotate Policy Disable')
    lsf.write_output('=' * 60)

    for sddc_host in sddc_managers:
        lsf.write_output(f'\nProcessing SDDC Manager: {sddc_host}')

        if dry_run:
            lsf.write_output('Would check for credentials with auto-rotate policies')
            lsf.write_output('Would disable auto-rotation for all service credentials')
            continue

            # Check connectivity
            if not lsf.test_ping(sddc_host):
                lsf.write_output(f'{sddc_host}: Not reachable - skipping auto-rotate disable')
                continue  # Don't fail the overall process

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
            overall_success = False
        else:
            lsf.write_output(f'{sddc_host}: Auto-rotate disable completed successfully')

    return overall_success  # Don't fail HOLification for unavailable resources


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
    3. If not, creates it with expiration = 9999 days from today
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
    ops_fqdns = []
    try:
        if lsf.config.has_option('RESOURCES', 'URLs'):
            urls_raw = lsf.config.get('RESOURCES', 'URLs').split('\n')
            for entry in urls_raw:
                url = entry.split(',')[0].strip()
                if 'ops-' in url and '.vcf.lab' in url:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    ops_fqdns.append(parsed.hostname)
    except Exception:
        pass
    
    if not ops_fqdns:
        try:
            if lsf.config.has_section('VCF'):
                if lsf.config.has_option('VCF', 'urls'):
                    vcf_urls = lsf.config.get('VCF', 'urls').split('\n')
                    for entry in vcf_urls:
                        url = entry.split(',')[0].strip()
                        if 'ops-' in url:
                            from urllib.parse import urlparse
                            parsed = urlparse(url)
                            ops_fqdns.append(parsed.hostname)
        except Exception:
            pass
    
    if not ops_fqdns:
        lsf.write_output('WARNING: VCF Operations Manager FQDN not found in config - skipping')
        return False
        
    overall_success = True
    for ops_fqdn in ops_fqdns:
        lsf.write_output(f'\nProcessing VCF Operations Manager: {ops_fqdn}')
        
        password = lsf.get_password()
        base_url = f"https://{ops_fqdn}"
        api_base = f"{base_url}/suite-api"
        
        if dry_run:
            lsf.write_output('Would create MaxExpiration policy, assign all inventory, and remediate')
            continue
        
        # Step 1: Authenticate
        lsf.write_output(f'{ops_fqdn}: Authenticating to VCF Operations Manager...')
        try:
            token_resp = requests.post(
                f"{api_base}/api/auth/token/acquire",
                json={"username": "admin", "password": password, "authSource": "local"},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                verify=False, timeout=30
            )
            token_resp.raise_for_status()
            token = token_resp.json()["token"]
            lsf.write_output(f'{ops_fqdn}: SUCCESS - Authenticated to VCF Operations Manager')
        except Exception as e:
            lsf.write_output(f'{ops_fqdn}: FAILED - Could not authenticate: {e}')
            overall_success = False
            continue
        
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
            lsf.write_output(f'{ops_fqdn}: INFO - Automatic remediation via API not available through suite-api proxy.')
            lsf.write_output(f'{ops_fqdn}: INFO - Please remediate manually via VCF Operations Manager UI:')
            lsf.write_output(f'{ops_fqdn}: INFO -   {base_url}/vcf-operations/ui/manage/fleet/fleet-settings')
            lsf.write_output(f'{ops_fqdn}: INFO -   Navigate to Fleet Settings > select MaxExpiration > Remediate All')
    
    lsf.write_output('VCF Operations Fleet Password Policy configuration complete')
    return overall_success


#==============================================================================
# FINAL CLEANUP
#==============================================================================

def configure_vsp_proxy(dry_run: bool = False) -> bool:
    """
    Configure HTTP/HTTPS proxy on VSP cluster nodes and Supervisor.

    The VSP (VCF Services Runtime) cluster nodes are Photon OS VMs that
    run VCF component services (Salt, Fleet LCM, Depot, etc.) as K8s
    workloads. The Supervisor runs on a separate set of control plane VMs
    managed by vCenter WCP.

    In the Holodeck lab environment, outbound internet access requires the
    Squid proxy on holorouter (10.1.1.1:3128). This function configures:

    1. Supervisor proxy via the vCenter namespace-management API
       (CLUSTER_CONFIGURED mode with http and https proxy)
    2. VSP node OS-level proxy (/etc/sysconfig/proxy, /etc/environment)
    3. VSP node containerd systemd drop-in for image pulls
    4. VSP node kubelet systemd drop-in for API server communication

    The no_proxy list includes all internal subnets, service CIDRs, the
    internal container registry, and the .site-a.vcf.lab domain.
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    PROXY_URL = 'http://10.1.1.1:3128'
    NO_PROXY = (
        'localhost,127.0.0.1,10.0.0.0/8,10.96.0.0/12,172.16.0.0/16,192.168.100.0/24,'
        '198.18.0.0/16,'
        '.site-a.vcf.lab,.svc,.cluster.local,.svc.cluster.local,'
        '10.1.0.0/24,registry.vmsp-platform.svc.cluster.local'
    )
    VSP_SSH_USER = 'vmware-system-user'

    lsf.write_output('')
    lsf.write_output('=' * 60)
    lsf.write_output('VSP & Supervisor Proxy Configuration')
    lsf.write_output('=' * 60)

    password = lsf.get_password()
    success = True

    # --- Part 1: Configure Supervisor proxy via vCenter API ---
    wld_vcenter = None
    supervisor_cluster_id = None
    wld_sso_user = 'administrator@wld.sso'

    if 'VCF' in lsf.config:
        vc_entries = lsf.config.get('VCF', 'vcfvCenter', fallback='').strip().split('\n')
        for entry in vc_entries:
            entry = entry.strip()
            if not entry or entry.startswith('#'):
                continue
            vc_name = entry.split(':')[0].strip()
            if 'wld' in vc_name.lower():
                wld_vcenter = f'{vc_name}.site-a.vcf.lab'
                break

    if not wld_vcenter:
        wld_vcenter = 'vc-wld01-a.site-a.vcf.lab'

    lsf.write_output(f'Supervisor proxy: Configuring via {wld_vcenter}')

    if dry_run:
        lsf.write_output(f'Would configure Supervisor proxy on {wld_vcenter}')
        lsf.write_output(f'  HTTP proxy:  {PROXY_URL}')
        lsf.write_output(f'  HTTPS proxy: {PROXY_URL}')
    else:
        if not lsf.test_ping(wld_vcenter):
            lsf.write_output(f'{wld_vcenter}: Not reachable - skipping Supervisor proxy')
        else:
            try:
                session_resp = requests.post(
                    f'https://{wld_vcenter}/api/session',
                    auth=(wld_sso_user, password),
                    verify=False, timeout=15
                )
                if session_resp.status_code not in (200, 201):
                    lsf.write_output(f'{wld_vcenter}: Failed to create session (HTTP {session_resp.status_code})')
                    success = False
                else:
                    session_id = session_resp.text.strip('"')
                    headers = {
                        'vmware-api-session-id': session_id,
                        'Content-Type': 'application/json'
                    }

                    clusters_resp = requests.get(
                        f'https://{wld_vcenter}/api/vcenter/namespace-management/clusters',
                        headers=headers, verify=False, timeout=15
                    )
                    if clusters_resp.status_code == 200:
                        clusters = clusters_resp.json()
                        if clusters:
                            supervisor_cluster_id = clusters[0].get('cluster')
                            lsf.write_output(f'Found Supervisor cluster: {supervisor_cluster_id}')
                        else:
                            lsf.write_output('No Supervisor clusters found')
                    else:
                        lsf.write_output(f'Failed to list Supervisor clusters (HTTP {clusters_resp.status_code})')

                    if supervisor_cluster_id:
                        proxy_body = {
                            'cluster_proxy_config': {
                                'proxy_settings_source': 'CLUSTER_CONFIGURED',
                                'http_proxy_config': PROXY_URL,
                                'https_proxy_config': PROXY_URL,
                            }
                        }
                        patch_resp = requests.patch(
                            f'https://{wld_vcenter}/api/vcenter/namespace-management/clusters/{supervisor_cluster_id}',
                            headers=headers, json=proxy_body, verify=False, timeout=30
                        )
                        if patch_resp.status_code == 204:
                            lsf.write_output(f'SUCCESS - Supervisor proxy configured (HTTP+HTTPS: {PROXY_URL})')
                        else:
                            lsf.write_output(f'WARNING - Supervisor proxy PATCH returned HTTP {patch_resp.status_code}')
                            lsf.write_output(f'  Response: {patch_resp.text[:200]}')
                            success = False
            except Exception as e:
                lsf.write_output(f'{wld_vcenter}: Error configuring Supervisor proxy: {e}')
                success = False

    # --- Part 2: Configure VSP cluster nodes ---
    lsf.write_output('')
    lsf.write_output('VSP nodes: Discovering cluster nodes...')

    vsp_cp_vip = '10.1.1.142'
    vsp_node_ips = []

    if dry_run:
        lsf.write_output(f'Would SSH to {vsp_cp_vip} to discover VSP node IPs')
        lsf.write_output(f'Would configure proxy on each VSP node:')
        lsf.write_output(f'  /etc/sysconfig/proxy (PROXY_ENABLED=yes)')
        lsf.write_output(f'  /etc/environment (http_proxy, https_proxy, no_proxy)')
        lsf.write_output(f'  /etc/systemd/system/containerd.service.d/http-proxy.conf')
        lsf.write_output(f'  /etc/systemd/system/kubelet.service.d/http-proxy.conf')
        return True

    if not lsf.test_ping(vsp_cp_vip):
        lsf.write_output(f'{vsp_cp_vip}: VSP control plane VIP not reachable - skipping')
        return True

    discover_cmd = (
        f"echo '{password}' | sudo -S -i "
        f"kubectl get nodes -o jsonpath='{{range .items[*]}}{{.status.addresses[?(@.type==\"InternalIP\")].address}}{{\" \"}}{{end}}'"
    )
    result = lsf.ssh(discover_cmd, f'{VSP_SSH_USER}@{vsp_cp_vip}', password)
    if result.returncode == 0:
        raw_output = result.stdout.strip() if hasattr(result, 'stdout') else ''
        if not raw_output and hasattr(result, 'output'):
            raw_output = result.output.strip()
        for line in raw_output.split('\n'):
            for token in line.split():
                token = token.strip()
                if token and token[0].isdigit() and '.' in token:
                    vsp_node_ips.append(token)

    if not vsp_node_ips:
        lsf.write_output('WARNING: Could not discover VSP node IPs, falling back to SSH probing')
        for candidate_ip in ['10.1.1.143', '10.1.1.141', '10.1.1.144',
                             '10.1.1.145', '10.1.1.146', '10.1.1.147']:
            if lsf.test_ping(candidate_ip):
                test = lsf.ssh('hostname', f'{VSP_SSH_USER}@{candidate_ip}', password)
                if test.returncode == 0:
                    vsp_node_ips.append(candidate_ip)

    if not vsp_node_ips:
        lsf.write_output('WARNING: No VSP nodes found - skipping proxy configuration')
        return True

    lsf.write_output(f'Found {len(vsp_node_ips)} VSP nodes: {", ".join(vsp_node_ips)}')

    proxy_script = f'''#!/bin/bash
PROXY_URL="{PROXY_URL}"
NO_PROXY="{NO_PROXY}"

cat > /etc/sysconfig/proxy << 'PROXYEOF'
PROXY_ENABLED="yes"
HTTP_PROXY="{PROXY_URL}"
HTTPS_PROXY="{PROXY_URL}"
FTP_PROXY=""
GOPHER_PROXY=""
SOCKS_PROXY=""
SOCKS5_SERVER=""
NO_PROXY="{NO_PROXY}"
PROXYEOF

if ! grep -q 'http_proxy=' /etc/environment 2>/dev/null; then
    cat >> /etc/environment << 'ENVEOF'
http_proxy={PROXY_URL}
https_proxy={PROXY_URL}
no_proxy={NO_PROXY}
HTTP_PROXY={PROXY_URL}
HTTPS_PROXY={PROXY_URL}
NO_PROXY={NO_PROXY}
ENVEOF
fi

mkdir -p /etc/systemd/system/containerd.service.d
cat > /etc/systemd/system/containerd.service.d/http-proxy.conf << 'CTDEOF'
[Service]
Environment="HTTP_PROXY={PROXY_URL}"
Environment="HTTPS_PROXY={PROXY_URL}"
Environment="NO_PROXY={NO_PROXY}"
CTDEOF

mkdir -p /etc/systemd/system/kubelet.service.d
cat > /etc/systemd/system/kubelet.service.d/http-proxy.conf << 'KUBEOF'
[Service]
Environment="HTTP_PROXY={PROXY_URL}"
Environment="HTTPS_PROXY={PROXY_URL}"
Environment="NO_PROXY={NO_PROXY}"
KUBEOF

systemctl daemon-reload
echo "PROXY_CONFIGURED"
'''

    script_path = '/tmp/confighol_vsp_proxy.sh'
    try:
        with open(script_path, 'w') as f:
            f.write(proxy_script)
        os.chmod(script_path, 0o755)
    except Exception as e:
        lsf.write_output(f'ERROR: Failed to write proxy script: {e}')
        return False

    for node_ip in vsp_node_ips:
        lsf.write_output(f'{node_ip}: Configuring proxy...')

        scp_result = lsf.scp(script_path, f'{VSP_SSH_USER}@{node_ip}:/tmp/confighol_vsp_proxy.sh', password)
        if scp_result.returncode != 0:
            lsf.write_output(f'{node_ip}: FAILED - Could not copy proxy script')
            success = False
            continue

        run_cmd = f"echo '{password}' | sudo -S bash /tmp/confighol_vsp_proxy.sh"
        run_result = lsf.ssh(run_cmd, f'{VSP_SSH_USER}@{node_ip}', password)

        output = ''
        if hasattr(run_result, 'stdout'):
            output = run_result.stdout
        elif hasattr(run_result, 'output'):
            output = run_result.output

        if run_result.returncode == 0 and 'PROXY_CONFIGURED' in str(output):
            lsf.write_output(f'{node_ip}: SUCCESS - Proxy configured')
        elif run_result.returncode == 0:
            lsf.write_output(f'{node_ip}: Proxy script executed (exit 0)')
        else:
            lsf.write_output(f'{node_ip}: WARNING - Proxy script returned exit code {run_result.returncode}')
            success = False

        lsf.ssh(f"echo '{password}' | sudo -S rm -f /tmp/confighol_vsp_proxy.sh",
                f'{VSP_SSH_USER}@{node_ip}', password)

    try:
        os.remove(script_path)
    except OSError:
        pass

    if success:
        lsf.write_output('VSP & Supervisor proxy configuration complete')
    else:
        lsf.write_output('VSP & Supervisor proxy configuration completed with warnings')

    return success


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
    0b. Vault CA trust distribution across VCF suite (vCenters, ESXi, NSX, SDDC Mgr, VCFA, Ops)
    0c. vCenter CA certificates import to Firefox on console VM (with SKIP/RETRY/FAIL options)
    1. Pre-checks and environment setup
    2. ESXi host configuration
    3. vCenter configuration
    4. NSX configuration (Managers and Edges)
    5. SDDC Manager configuration
    6. VCF Automation VMs configuration (uses vmware-system-user)
    7. Operations VMs configuration
    8. Disable SDDC Manager auto-rotate policies (prevents post-deployment failures)
    9. Configure VSP & Supervisor proxy (enables outbound internet via holorouter proxy)
    10. Final cleanup
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
    
    # Step 0b: Distribute Vault CA trust across VCF suite
    # Import the Vault root CA into vCenters, ESXi, NSX, SDDC Manager, VCFA, Ops VMs
    vault_ca_pem = download_vault_ca_certificate() if not args.dry_run else None
    if vault_ca_pem or args.dry_run:
        distribute_vault_ca_trust(vault_ca_pem or '', password, args.dry_run)
    else:
        lsf.write_output('WARNING: Could not download Vault CA - skipping VCF trust distribution')
    
    # Step 0c: Import vCenter CA certificates to Firefox on console VM
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
    
    # Step 9: Configure VSP & Supervisor proxy
    # Enables proxy on VSP cluster nodes and Supervisor for outbound internet access
    configure_vsp_proxy(args.dry_run)
    
    # Step 10: Final cleanup
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
