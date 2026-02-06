#!/usr/bin/env python3
# VCFshutdown.py - HOLFY27 Core VCF Shutdown Module
# Version 1.6 - February 2026
# Author - Burke Azbill and HOL Core Team
# VMware Cloud Foundation graceful shutdown sequence
# v 1.6 Changes:
# - Fixed ESA vs OSA detection: replaced unreliable vsish plogRunElevator
#   path check with authoritative 'esxcli vsan cluster get' command.
#   The vsish plog paths still exist on ESA hosts, causing false OSA
#   detection and an unnecessary 45-minute elevator wait.
#
# v 1.5 Changes:
# - WCP vCenters now determined from [VCFFINAL] tanzucontrol config
#
# v 1.4 Changes:
# - Added support to check ESA vs OSA vSAN architecture to determine if the vSAN elevator is needed
#
# v1.3 Changes:
# - Removed dependency on [SHUTDOWN] section for NSX/vCenter/ESXi components
# - Now reads from [VCF] section to eliminate duplicate configuration
# - NSX Edges: Read from [VCF] vcfnsxedges, filtered by "wld" or "mgmt"
# - NSX Managers: Read from [VCF] vcfnsxmgr, filtered by "wld" or "mgmt"
# - vCenters: Read from [VCF] vcfvCenter, filtered by "wld" or "mgmt"
# - ESXi Hosts: Read from [VCF] vcfmgmtcluster

"""
VCF Shutdown Module

This module handles the graceful shutdown of VMware Cloud Foundation environments.
The shutdown order follows the official Broadcom VCF 9.0 documentation:
https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/fleet-management/vcf-shutdown-and-startup/vcf-shutdown.html

Key principles from VCF 9.0 documentation:
- Workload domains must be shut down BEFORE the management domain
- If multiple VCF instances: shut down instances without VCF Operations/Automation first
- VCF instance running VCF Operations must be last to shut down
- If NSX Manager/Edge clusters are shared, shut them down with the first workload domain

VCF 9.0 WORKLOAD DOMAIN Shutdown Order:
  1. Virtualized customer workloads
  2. VMware Live Recovery (if applicable)
  4. NSX Edge nodes
  5. NSX Manager nodes  
  7. ESX hosts
  8. vCenter Server (LAST for workload domain)

VCF 9.0 MANAGEMENT DOMAIN Shutdown Order:
  1. VCF Automation (Aria Automation / vra)
  2. VCF Operations for Networks (vrni)
  3. VCF Operations collector
  4. VCF Operations for logs (vrli)
  5. VCF Identity Broker
  6. VCF Operations fleet management (Aria Suite Lifecycle)
  7. VCF Operations (vrops)
  8. VMware Live Site Recovery (if applicable)
  9. NSX Edge nodes
  10. NSX Manager
  11. SDDC Manager
  12. vSAN and ESX Hosts (includes vCenter shutdown)

Shutdown Order (this module) - aligned with VCF 9.0 docs:

PHASE 1: Fleet Operations (VCF Automation via API)
PHASE 2: Connect to Management Infrastructure
PHASE 3: Stop WCP (Workload Control Plane) services
PHASE 4: Shutdown Workload VMs (Tanzu, K8s, vCLS)
PHASE 5: Shutdown Workload Domain NSX Edges (if separate from mgmt)
PHASE 6: Shutdown Workload Domain NSX Manager (if separate from mgmt)
PHASE 7: Shutdown Workload vCenters (LAST per VCF 9.0 workload domain order)
PHASE 8: Shutdown VCF Operations for Networks (vrni)
PHASE 9: Shutdown VCF Operations Collector
PHASE 10: Shutdown VCF Operations for Logs (vrli)
PHASE 11: Shutdown VCF Identity Broker
PHASE 12: Shutdown VCF Operations Fleet Management (Aria Suite Lifecycle)
PHASE 13: Shutdown VCF Operations (vrops, orchestrator)
PHASE 14: Shutdown Management Domain NSX Edges
PHASE 15: Shutdown Management Domain NSX Manager
PHASE 16: Shutdown SDDC Manager
PHASE 17: Shutdown Management vCenter
PHASE 18: Set Host Advanced Settings
PHASE 19: vSAN Elevator Operations
PHASE 20: Shutdown ESXi Hosts

Additional operations handled:
- Fleet Operations (SDDC Manager) for VCF Automation shutdown
- WCP (Workload Control Plane) shutdown
- vSAN elevator operations for clean shutdown (OSA only - ESA auto-detected and skipped)

NSX VM Domain Detection:
- VMs with "wld" in name are treated as Workload Domain (Phase 5-6)
- VMs with "mgmt" in name are treated as Management Domain (Phase 14-15)

vSAN Architecture Detection:
- OSA (Original Storage Architecture): Requires plogRunElevator and 45-minute wait
- ESA (Express Storage Architecture): Does NOT use plog, elevator wait is skipped
- Detection is automatic via vsish path check on first ESXi host
"""

import os
import sys
import argparse
import logging
import ssl
import re
import json
import time

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')
sys.path.insert(0, '/home/holuser/hol/Shutdown')

# Default logging level
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VCFshutdown'
MODULE_VERSION = '1.6'
MODULE_DESCRIPTION = 'VMware Cloud Foundation graceful shutdown (VCF 9.x compliant)'

# Status file for console display
STATUS_FILE = '/lmchol/hol/startup_status.txt'

# vSAN elevator timeout (45 minutes recommended by VMware)
VSAN_ELEVATOR_TIMEOUT = 2700  # 45 minutes in seconds
VSAN_ELEVATOR_CHECK_INTERVAL = 60  # Check every minute

# VM shutdown timeout
VM_SHUTDOWN_TIMEOUT = 300  # 5 minutes per VM
VM_SHUTDOWN_POLL_INTERVAL = 5  # seconds

# Host shutdown timeout
HOST_SHUTDOWN_TIMEOUT = 600  # 10 minutes per host

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def update_shutdown_status(phase_num: int, phase_name: str, dry_run: bool = False):
    """
    Update the startup_status.txt file with current shutdown phase.
    
    :param phase_num: Phase number
    :param phase_name: Phase description
    :param dry_run: If True, skip status update
    """
    if dry_run:
        return
    
    try:
        status_dir = os.path.dirname(STATUS_FILE)
        if status_dir and not os.path.exists(status_dir):
            os.makedirs(status_dir, exist_ok=True)
        
        with open(STATUS_FILE, 'w') as f:
            f.write(f'Shutdown Phase {phase_num}: {phase_name}')
    except Exception:
        pass  # Don't fail shutdown if status file can't be written

def get_vms_by_regex(lsf, pattern: str) -> list:
    """
    Get VMs matching a regex pattern from all connected vCenters.
    
    :param lsf: lsfunctions module reference
    :param pattern: Regex pattern to match VM names
    :return: List of matching VM names
    """
    return lsf.get_vm_match(pattern)


def is_vm_powered_on(vm) -> bool:
    """
    Check if a VM is powered on.
    
    :param vm: VM object
    :return: True if powered on
    """
    from pyVmomi import vim
    try:
        return vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn
    except Exception:
        return False


def shutdown_vm_gracefully(lsf, vm, timeout: int = VM_SHUTDOWN_TIMEOUT) -> bool:
    """
    Gracefully shutdown a VM using VMware Tools if available.
    
    :param lsf: lsfunctions module reference
    :param vm: VM object
    :param timeout: Maximum time to wait for shutdown
    :return: True if shutdown succeeded or VM was already off
    """
    from pyVmomi import vim
    
    vm_name = vm.name
    
    # Check current power state
    try:
        power_state = vm.runtime.powerState
    except Exception as e:
        lsf.write_output(f'{vm_name}: Unable to check power state: {e}')
        return False
    
    # Already powered off?
    if power_state == vim.VirtualMachinePowerState.poweredOff:
        lsf.write_output(f'{vm_name}: Already powered off - skipping')
        return True
    
    # Suspended?
    if power_state == vim.VirtualMachinePowerState.suspended:
        lsf.write_output(f'{vm_name}: VM is suspended, powering off')
        try:
            task = vm.PowerOffVM_Task()
            from pyVim.task import WaitForTask
            WaitForTask(task)
            return True
        except Exception as e:
            lsf.write_output(f'{vm_name}: Failed to power off suspended VM: {e}')
            return False
    
    # VM is powered on - proceed with shutdown
    lsf.write_output(f'{vm_name}: Currently powered on, initiating shutdown')
    
    # Check VMware Tools status
    try:
        tools_status = vm.guest.toolsRunningStatus
        lsf.write_output(f'{vm_name}: VMware Tools status: {tools_status}')
    except Exception:
        tools_status = 'guestToolsNotRunning'
        lsf.write_output(f'{vm_name}: Unable to check Tools status, assuming not running')
    
    try:
        if tools_status == 'guestToolsRunning':
            # Graceful shutdown via guest
            lsf.write_output(f'{vm_name}: Initiating graceful guest shutdown')
            vm.ShutdownGuest()
        else:
            # Force power off if no tools
            lsf.write_output(f'{vm_name}: No VMware Tools available, forcing power off')
            task = vm.PowerOffVM_Task()
            from pyVim.task import WaitForTask
            WaitForTask(task)
            lsf.write_output(f'{vm_name}: Powered off successfully')
            return True
        
        # Wait for graceful shutdown
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            try:
                if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
                    lsf.write_output(f'{vm_name}: Powered off successfully')
                    return True
            except Exception:
                pass
            time.sleep(VM_SHUTDOWN_POLL_INTERVAL)
        
        # Timeout - force power off
        lsf.write_output(f'{vm_name}: Graceful shutdown timeout, forcing power off')
        task = vm.PowerOffVM_Task()
        from pyVim.task import WaitForTask
        WaitForTask(task)
        lsf.write_output(f'{vm_name}: Powered off successfully (forced)')
        return True
        
    except Exception as e:
        lsf.write_output(f'{vm_name}: Error during shutdown: {e}')
        try:
            # Last resort - force power off
            task = vm.PowerOffVM_Task()
            from pyVim.task import WaitForTask
            WaitForTask(task)
            lsf.write_output(f'{vm_name}: Powered off successfully (forced after error)')
            return True
        except Exception as e2:
            lsf.write_output(f'{vm_name}: Force power off failed: {e2}')
            return False


def shutdown_wcp_service(lsf, vc_fqdn: str, password: str) -> bool:
    """
    Stop the WCP (Workload Control Plane) service on a vCenter.
    
    :param lsf: lsfunctions module reference
    :param vc_fqdn: vCenter FQDN
    :param password: root password for vCenter
    :return: True if WCP stopped successfully
    """
    lsf.write_output(f'Stopping WCP service on {vc_fqdn}')
    
    if not lsf.test_tcp_port(vc_fqdn, 22, timeout=5):
        lsf.write_output(f'{vc_fqdn} SSH port not reachable')
        return False
    
    try:
        result = lsf.ssh('vmon-cli -k wcp', f'root@{vc_fqdn}', password)
        if result.returncode == 0:
            lsf.write_output(f'WCP service stopped on {vc_fqdn}')
            return True
        else:
            lsf.write_output(f'WCP stop returned: {result.stderr}')
            return False
    except Exception as e:
        lsf.write_output(f'Error stopping WCP on {vc_fqdn}: {e}')
        return False


def check_vsan_esa(lsf, host: str, username: str, password: str) -> bool:
    """
    Check if vSAN ESA (Express Storage Architecture) is in use on an ESXi host.
    
    vSAN ESA does NOT use the plog mechanism, so the elevator wait is not needed.
    vSAN OSA (Original Storage Architecture) DOES use plog and requires the wait.
    
    Detection method: Use 'esxcli vsan cluster get' and check for
    'vSAN ESA Enabled: true'. This is the authoritative detection method.
    
    NOTE: The previous vsish-based check (looking for plogRunElevator) was
    unreliable because the plog vsish paths still exist on ESA hosts even
    though ESA does not use the plog mechanism.
    
    :param lsf: lsfunctions module reference
    :param host: ESXi hostname
    :param username: ESXi username
    :param password: ESXi password
    :return: True if ESA is detected, False if OSA (or unable to determine)
    """
    if not lsf.test_tcp_port(host, 22, timeout=5):
        lsf.write_output(f'{host} SSH port not reachable for ESA check')
        return False
    
    try:
        # Use esxcli to definitively determine ESA vs OSA
        cmd = 'esxcli vsan cluster get 2>/dev/null'
        result = lsf.ssh(cmd, f'{username}@{host}', password)
        
        if result.returncode == 0:
            output = result.stdout if hasattr(result, 'stdout') and result.stdout else ''
            # Handle cases where output might be bytes
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            
            # Look for "vSAN ESA Enabled: true" in the output
            for line in output.splitlines():
                if 'vSAN ESA Enabled' in line:
                    if 'true' in line.lower():
                        lsf.write_output(f'{host}: vSAN ESA Enabled = true')
                        return True
                    else:
                        lsf.write_output(f'{host}: vSAN ESA Enabled = false (OSA)')
                        return False
            
            # If "vSAN ESA Enabled" line not found, likely older ESXi (pre-ESA)
            lsf.write_output(f'{host}: vSAN ESA field not found in cluster info (assuming OSA)')
            return False
        else:
            lsf.write_output(f'{host}: esxcli vsan cluster get failed (vSAN may not be configured)')
            return False
    except Exception as e:
        lsf.write_output(f'Error checking vSAN architecture on {host}: {e}')
        # Default to OSA behavior (safer - assumes elevator is needed)
        return False


def set_vsan_elevator(lsf, host: str, username: str, password: str, 
                      enable: bool = True) -> bool:
    """
    Set the vSAN elevator mode on an ESXi host for graceful shutdown.
    
    Before vSAN OSA hosts can be shut down, the plogRunElevator setting must be
    enabled to flush all pending I/O, then disabled after the wait period.
    
    NOTE: This is only applicable to vSAN OSA. vSAN ESA does not use plog.
    
    :param lsf: lsfunctions module reference
    :param host: ESXi hostname
    :param username: ESXi username
    :param password: ESXi password
    :param enable: True to enable elevator (start), False to disable (end)
    :return: True if command succeeded
    """
    value = "1" if enable else "0"
    action = "Enabling" if enable else "Disabling"
    
    lsf.write_output(f'{action} vSAN elevator on {host}')
    
    if not lsf.test_tcp_port(host, 22, timeout=5):
        lsf.write_output(f'{host} SSH port not reachable')
        return False
    
    try:
        cmd = f'yes | vsish -e set /config/LSOM/intOpts/plogRunElevator {value}'
        result = lsf.ssh(cmd, f'{username}@{host}', password)
        return result.returncode == 0
    except Exception as e:
        lsf.write_output(f'Error setting vSAN elevator on {host}: {e}')
        return False


def shutdown_host(lsf, host_fqdn: str, username: str, password: str) -> bool:
    """
    Shutdown an ESXi host.
    
    :param lsf: lsfunctions module reference
    :param host_fqdn: ESXi hostname
    :param username: ESXi username
    :param password: ESXi password
    :return: True if shutdown initiated
    """
    from pyVmomi import vim
    from pyVim import connect
    
    lsf.write_output(f'Shutting down ESXi host: {host_fqdn}')
    
    if not lsf.test_tcp_port(host_fqdn, 443, timeout=5):
        lsf.write_output(f'{host_fqdn} is not reachable')
        return False
    
    try:
        # Connect directly to ESXi host
        context = ssl._create_unverified_context()
        try:
            si = connect.SmartConnect(
                host=host_fqdn,
                user=username,
                pwd=password,
                port=443,
                sslContext=context
            )
        except vim.fault.InvalidLogin:
            # Try without password (some hosts have blank password)
            si = connect.SmartConnect(
                host=host_fqdn,
                user=username,
                pwd='',
                port=443,
                sslContext=context
            )
        
        content = si.RetrieveContent()
        host = content.rootFolder.childEntity[0].hostFolder.childEntity[0].host[0]
        
        task = host.ShutdownHost_Task(force=True)
        lsf.write_output(f'Shutdown task initiated for {host_fqdn}')
        
        connect.Disconnect(si)
        return True
        
    except Exception as e:
        lsf.write_output(f'Error shutting down {host_fqdn}: {e}')
        return False


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for VCFshutdown module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    from pyVim import connect
    from pyVmomi import vim
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    import fleet  # Import fleet operations module
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Get password
    password = lsf.get_password()
    
    #==========================================================================
    # TASK 1: Shutdown Fleet Operations Products (Aria Suite)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 1: Fleet Operations (Aria Suite) Shutdown')
    lsf.write_output('='*60)
    update_shutdown_status(1, 'Fleet Operations (Aria Suite)', dry_run)
    
    fleet_fqdn = None
    fleet_username = 'admin@local'
    
    # Check for fleet configuration
    if lsf.config.has_option('SHUTDOWN', 'fleet_fqdn'):
        fleet_fqdn = lsf.config.get('SHUTDOWN', 'fleet_fqdn')
    elif lsf.config.has_option('VCF', 'fleet_fqdn'):
        fleet_fqdn = lsf.config.get('VCF', 'fleet_fqdn')
    else:
        # Default VCF fleet management FQDN
        fleet_fqdn = 'opslcm-a.site-a.vcf.lab'
    
    if lsf.config.has_option('SHUTDOWN', 'fleet_username'):
        fleet_username = lsf.config.get('SHUTDOWN', 'fleet_username')
    
    # Products to shutdown via Fleet Operations (reverse order from startup)
    # NOTE: Only vra and vrni support power-off via Fleet Operations API
    # vrops and vrli return "Shut Down Operation is not supported" - their VMs
    # are handled directly in PHASE 5 (Management VMs) instead
    fleet_products = ['vra', 'vrni']
    if lsf.config.has_option('SHUTDOWN', 'fleet_products'):
        fleet_products_raw = lsf.config.get('SHUTDOWN', 'fleet_products')
        fleet_products = [p.strip() for p in fleet_products_raw.split(',')]
    
    if lsf.test_tcp_port(fleet_fqdn, 443, timeout=10):
        lsf.write_output(f'Fleet Management available at {fleet_fqdn}')
        
        if not dry_run:
            try:
                token = fleet.get_encoded_token(fleet_username, password)
                # Skip inventory sync during shutdown - it often fails when vCenter
                # is slow or already being shut down, and isn't required for power-off
                success = fleet.shutdown_products(fleet_fqdn, token, fleet_products,
                                                  write_output=lsf.write_output,
                                                  skip_inventory_sync=True)
                if success:
                    lsf.write_output('Fleet Operations products shutdown complete')
                else:
                    lsf.write_output('WARNING: Some Fleet Operations products may not have shutdown cleanly')
                    lsf.write_output('(Products will be shut down via VM power-off in later phases)')
            except Exception as e:
                lsf.write_output(f'Fleet Operations shutdown error: {e}')
        else:
            lsf.write_output(f'Would shutdown Fleet products: {fleet_products}')
    else:
        lsf.write_output(f'Fleet Management not reachable at {fleet_fqdn}, skipping')
    
    #==========================================================================
    # TASK 2: Connect to vCenters and Management Hosts
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 2: Connect to Management Infrastructure')
    lsf.write_output('='*60)
    update_shutdown_status(2, 'Connect to Infrastructure', dry_run)
    
    # Get list of vCenters
    vcenters = []
    if lsf.config.has_option('RESOURCES', 'vCenters'):
        vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
        vcenters = [v.strip() for v in vcenters_raw.split('\n') 
                   if v.strip() and not v.strip().startswith('#')]
    
    # Get list of ESXi hosts
    mgmt_hosts = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        mgmt_hosts_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        mgmt_hosts = [h.strip() for h in mgmt_hosts_raw.split('\n') 
                     if h.strip() and not h.strip().startswith('#')]
    
    if not dry_run:
        # Connect to management cluster hosts first (for VM operations)
        if mgmt_hosts:
            lsf.write_output(f'Connecting to {len(mgmt_hosts)} management host(s):')
            for host in mgmt_hosts:
                lsf.write_output(f'  - {host}')
            lsf.connect_vcenters(mgmt_hosts)
            lsf.write_output(f'Connected to {len(lsf.sis)} vSphere endpoint(s)')
        else:
            lsf.write_output('No management hosts configured in VCF section')
    
    #==========================================================================
    # TASK 3: Stop WCP on vCenters
    # Determined from [VCFFINAL] tanzucontrol (same config used by VCFfinal.py)
    # This eliminates redundant config entries and reduces errors
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 3: Stop Workload Control Plane (WCP)')
    lsf.write_output('='*60)
    update_shutdown_status(3, 'Stop WCP Services', dry_run)
    
    # Determine WCP vCenters from [VCFFINAL] tanzucontrol config
    # Format: vmname:vcenter or vmname (regex patterns supported)
    # Extract unique vCenter names from the tanzucontrol entries
    wcp_vcenters = []
    
    if lsf.config.has_option('VCFFINAL', 'tanzucontrol'):
        # Parse tanzucontrol to extract vCenter names
        tanzu_raw = lsf.config.get('VCFFINAL', 'tanzucontrol')
        seen_vcenters = set()
        for entry in tanzu_raw.split('\n'):
            entry = entry.strip()
            if entry and not entry.startswith('#'):
                # Format: vmname:vcenter or just vmname
                if ':' in entry:
                    parts = entry.split(':')
                    if len(parts) >= 2:
                        vc = parts[1].strip()
                        if vc and vc not in seen_vcenters:
                            wcp_vcenters.append(vc)
                            seen_vcenters.add(vc)
        
        if wcp_vcenters:
            lsf.write_output(f'WCP vCenters from tanzucontrol config: {wcp_vcenters}')
        else:
            lsf.write_output('tanzucontrol configured but no vCenters specified')
    else:
        lsf.write_output('No tanzucontrol in [VCFFINAL] - WCP not configured')
    
    if wcp_vcenters and not dry_run:
        lsf.write_output(f'Found {len(wcp_vcenters)} vCenter(s) with WCP to stop')
        for vc in wcp_vcenters:
            lsf.write_output(f'Checking WCP on {vc}...')
            if lsf.test_tcp_port(vc, 443, timeout=10):
                shutdown_wcp_service(lsf, vc, password)
            else:
                lsf.write_output(f'{vc} not reachable, skipping WCP stop')
    elif dry_run:
        lsf.write_output(f'Would stop WCP on: {wcp_vcenters}')
    elif not wcp_vcenters:
        lsf.write_output('No WCP vCenters to stop - skipping')
    
    #==========================================================================
    # TASK 4: Shutdown Workload VMs
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 4: Shutdown Workload VMs')
    lsf.write_output('='*60)
    update_shutdown_status(4, 'Shutdown Workload VMs', dry_run)
    
    # VM regex patterns to find and shutdown (Tanzu, K8s, vCLS, etc.)
    # Order follows VCF docs: containerized workloads → Supervisor → TKG → vCLS
    vm_patterns = [
        r'^kubernetes-cluster-.*$',  # TKGs clusters (worker nodes)
        r'^dev-project-.*$',  # vSphere with Tanzu projects
        r'^cci-service-.*$',  # CCI services
        r'^SupervisorControlPlaneVM.*$',  # Supervisor Control Plane VMs
        r'^vCLS-.*$',  # vSphere Cluster Services VMs (per VCF docs)
    ]
    
    if lsf.config.has_option('SHUTDOWN', 'vm_patterns'):
        patterns_raw = lsf.config.get('SHUTDOWN', 'vm_patterns')
        vm_patterns = [p.strip() for p in patterns_raw.split('\n') 
                      if p.strip() and not p.strip().startswith('#')]
    
    # Static VM list (specific VMs to shutdown)
    workload_vms = []
    if lsf.config.has_option('SHUTDOWN', 'workload_vms'):
        vms_raw = lsf.config.get('SHUTDOWN', 'workload_vms')
        workload_vms = [v.strip() for v in vms_raw.split('\n') 
                       if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        total_workload_vms = 0
        
        # Find VMs by pattern
        lsf.write_output(f'Searching for VMs matching {len(vm_patterns)} pattern(s)...')
        for pattern in vm_patterns:
            lsf.write_output(f'  Pattern: {pattern}')
            vms = get_vms_by_regex(lsf, pattern)
            if vms:
                lsf.write_output(f'  Found {len(vms)} VM(s) matching pattern')
                for vm in vms:
                    total_workload_vms += 1
                    lsf.write_output(f'  [{total_workload_vms}] Shutting down: {vm.name}')
                    shutdown_vm_gracefully(lsf, vm)
                    time.sleep(2)  # Brief pause between shutdowns
            else:
                lsf.write_output(f'  No VMs found matching this pattern')
        
        # Shutdown static VM list
        if workload_vms:
            lsf.write_output(f'Processing {len(workload_vms)} static workload VM(s)...')
            for vm_name in workload_vms:
                lsf.write_output(f'  Looking for VM: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        total_workload_vms += 1
                        lsf.write_output(f'  [{total_workload_vms}] Shutting down: {vm.name}')
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(2)
                else:
                    lsf.write_output(f'  VM not found: {vm_name}')
        else:
            lsf.write_output('No static workload VMs configured')
        
        lsf.write_output(f'Workload VM shutdown complete: {total_workload_vms} VM(s) processed')
    else:
        lsf.write_output(f'Would shutdown VMs matching patterns: {vm_patterns}')
        lsf.write_output(f'Would shutdown VMs: {workload_vms}')
    
    #==========================================================================
    # VCF 9.0 WORKLOAD DOMAIN SHUTDOWN
    # Per VCF 9.0 docs, workload domain order is:
    # 1. Customer workloads (done above)
    # 2. VMware Live Recovery (if applicable)
    # 4. NSX Edge nodes
    # 5. NSX Manager nodes
    # 7. ESX hosts  
    # 8. vCenter Server (LAST for workload domain)
    #==========================================================================
    
    #==========================================================================
    # TASK 5: Shutdown Workload Domain NSX Edges
    # Per VCF 9.0: NSX Edges shut down before NSX Manager in workload domain
    # Filter: Only edges with "wld" in their name (workload domain)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 5: Shutdown Workload Domain NSX Edges')
    lsf.write_output('='*60)
    update_shutdown_status(5, 'Shutdown Workload NSX Edges', dry_run)
    
    # Get all NSX edges from [VCF] vcfnsxedges (primary source)
    # Format: edge-name:esxhost (we only need the edge name)
    all_nsx_edges = []
    if lsf.config.has_option('VCF', 'vcfnsxedges'):
        edges_raw = lsf.config.get('VCF', 'vcfnsxedges')
        for entry in edges_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                all_nsx_edges.append(parts[0].strip())
        lsf.write_output(f'Found {len(all_nsx_edges)} NSX Edge(s) in [VCF] vcfnsxedges')
    else:
        lsf.write_output('No NSX Edges configured in [VCF] vcfnsxedges')
    
    # Filter for workload domain edges (contain "wld" in name)
    workload_nsx_edges = [e for e in all_nsx_edges if 'wld' in e.lower()]
    
    if not dry_run:
        if workload_nsx_edges:
            lsf.write_output(f'Processing {len(workload_nsx_edges)} workload NSX Edge VM(s)...')
            for edge_name in workload_nsx_edges:
                lsf.write_output(f'  Looking for: {edge_name}')
                vms = lsf.get_vm_by_name(edge_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(2)
                else:
                    lsf.write_output(f'    NSX Edge VM not found')
            lsf.write_output('Workload NSX Edge shutdown complete')
        else:
            lsf.write_output('No workload NSX Edges found (no edges with "wld" in name)')
    else:
        lsf.write_output(f'Would shutdown workload NSX Edges: {workload_nsx_edges}')
    
    #==========================================================================
    # TASK 6: Shutdown Workload Domain NSX Manager
    # Per VCF 9.0: NSX Manager shuts down after NSX Edges in workload domain
    # Filter: Only managers with "wld" in their name (workload domain)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 6: Shutdown Workload Domain NSX Manager')
    lsf.write_output('='*60)
    update_shutdown_status(6, 'Shutdown Workload NSX Manager', dry_run)
    
    # Get all NSX managers from [VCF] vcfnsxmgr (primary source)
    # Format: nsx-name:esxhost (we only need the NSX manager name)
    all_nsx_mgr = []
    if lsf.config.has_option('VCF', 'vcfnsxmgr'):
        mgr_raw = lsf.config.get('VCF', 'vcfnsxmgr')
        for entry in mgr_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                all_nsx_mgr.append(parts[0].strip())
        lsf.write_output(f'Found {len(all_nsx_mgr)} NSX Manager(s) in [VCF] vcfnsxmgr')
    else:
        lsf.write_output('No NSX Managers configured in [VCF] vcfnsxmgr')
    
    # Filter for workload domain managers (contain "wld" in name)
    workload_nsx_mgr = [m for m in all_nsx_mgr if 'wld' in m.lower()]
    
    if not dry_run:
        if workload_nsx_mgr:
            lsf.write_output(f'Processing {len(workload_nsx_mgr)} workload NSX Manager VM(s)...')
            for mgr_name in workload_nsx_mgr:
                lsf.write_output(f'  Looking for: {mgr_name}')
                vms = lsf.get_vm_by_name(mgr_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    NSX Manager VM not found')
            lsf.write_output('Workload NSX Manager shutdown complete')
        else:
            lsf.write_output('No workload NSX Managers found (no managers with "wld" in name)')
    else:
        lsf.write_output(f'Would shutdown workload NSX Manager: {workload_nsx_mgr}')
    
    #==========================================================================
    # TASK 7: Shutdown Workload vCenters
    # Per VCF 9.0: vCenter is LAST in workload domain shutdown order (#8)
    # Note: In VCF 9.0, ESX hosts shutdown before vCenter for workload domains
    # For HOL, we keep vCenter up to manage the ESX shutdown, then shut it down
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 7: Shutdown Workload vCenters')
    lsf.write_output('='*60)
    update_shutdown_status(7, 'Shutdown Workload vCenters', dry_run)
    
    # Get all vCenters from [VCF] vcfvCenter (primary source)
    # Format: vcenter-name:esxhost (we only need the vCenter name)
    # Filter for workload vCenters (contain "wld" in name)
    all_vcenters = []
    if lsf.config.has_option('VCF', 'vcfvCenter'):
        vc_raw = lsf.config.get('VCF', 'vcfvCenter')
        for entry in vc_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                all_vcenters.append(parts[0].strip())
        lsf.write_output(f'Found {len(all_vcenters)} vCenter(s) in [VCF] vcfvCenter')
    else:
        lsf.write_output('No vCenters configured in [VCF] vcfvCenter')
    
    # Filter for workload vCenters (contain "wld" in name)
    workload_vcenters = [v for v in all_vcenters if 'wld' in v.lower()]
    
    if not dry_run:
        if workload_vcenters:
            lsf.write_output(f'Processing {len(workload_vcenters)} workload vCenter(s)...')
            lsf.write_output('  (Per VCF 9.0: vCenter shuts down LAST in workload domain)')
            vc_count = 0
            for vc_name in workload_vcenters:
                vc_count += 1
                lsf.write_output(f'  [{vc_count}/{len(workload_vcenters)}] Looking for: {vc_name}')
                vms = lsf.get_vm_by_name(vc_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(10)  # Longer pause for vCenter
                else:
                    lsf.write_output(f'    vCenter VM not found (may not exist in this lab)')
            lsf.write_output('Workload vCenter shutdown complete')
        else:
            lsf.write_output('No workload vCenters configured - skipping')
    else:
        lsf.write_output(f'Would shutdown workload vCenters: {workload_vcenters}')
    
    #==========================================================================
    # VCF 9.0 MANAGEMENT DOMAIN SHUTDOWN
    # Per VCF 9.0 docs, management domain order is:
    # 1. VCF Automation (vra)
    # 2. VCF Operations for Networks (vrni)
    # 3. VCF Operations collector  
    # 4. VCF Operations for logs (vrli)
    # 5. VCF Identity Broker
    # 6. VCF Operations fleet management (Aria Suite Lifecycle)
    # 7. VCF Operations (vrops)
    # 8. VMware Live Site Recovery (if applicable)
    # 9. NSX Edge nodes
    # 10. NSX Manager
    # 11. SDDC Manager
    # 12. vSAN and ESX Hosts (includes vCenter)
    #==========================================================================
    
    #==========================================================================
    # TASK 8: Shutdown VCF Operations for Networks (vrni)
    # Per VCF 9.0 Management Domain order #2
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 8: Shutdown VCF Operations for Networks')
    lsf.write_output('='*60)
    update_shutdown_status(8, 'Shutdown VCF Ops for Networks', dry_run)
    
    vcf_ops_networks_vms = ['opsnet-a', 'opsnet-01a', 'opsnetcollector-01a']
    
    if lsf.config.has_option('SHUTDOWN', 'vcf_ops_networks_vms'):
        net_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_networks_vms')
        vcf_ops_networks_vms = [v.strip() for v in net_raw.split('\n') 
                               if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if vcf_ops_networks_vms:
            lsf.write_output(f'Processing {len(vcf_ops_networks_vms)} VCF Ops for Networks VM(s)...')
            for vm_name in vcf_ops_networks_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    VM not found')
            lsf.write_output('VCF Operations for Networks shutdown complete')
        else:
            lsf.write_output('No VCF Ops for Networks VMs configured')
    else:
        lsf.write_output(f'Would shutdown VCF Ops for Networks: {vcf_ops_networks_vms}')
    
    #==========================================================================
    # TASK 9: Shutdown VCF Operations Collector
    # Per VCF 9.0 Management Domain order #3
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 9: Shutdown VCF Operations Collector')
    lsf.write_output('='*60)
    update_shutdown_status(9, 'Shutdown VCF Ops Collector', dry_run)
    
    vcf_ops_collector_vms = ['opscollector-01a', 'opsproxy-01a']
    
    if lsf.config.has_option('SHUTDOWN', 'vcf_ops_collector_vms'):
        coll_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_collector_vms')
        vcf_ops_collector_vms = [v.strip() for v in coll_raw.split('\n') 
                                if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if vcf_ops_collector_vms:
            lsf.write_output(f'Processing {len(vcf_ops_collector_vms)} VCF Ops Collector VM(s)...')
            for vm_name in vcf_ops_collector_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    VM not found')
            lsf.write_output('VCF Operations Collector shutdown complete')
        else:
            lsf.write_output('No VCF Ops Collector VMs configured')
    else:
        lsf.write_output(f'Would shutdown VCF Ops Collector: {vcf_ops_collector_vms}')
    
    #==========================================================================
    # TASK 10: Shutdown VCF Operations for Logs (vrli)
    # Per VCF 9.0 Management Domain order #4
    # Note: In VCF 9.0, this is NOT late - it shuts down before Identity Broker
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 10: Shutdown VCF Operations for Logs')
    lsf.write_output('='*60)
    update_shutdown_status(10, 'Shutdown VCF Ops for Logs', dry_run)
    
    vcf_ops_logs_vms = ['opslogs-01a', 'ops-01a', 'ops-a']
    
    if lsf.config.has_option('SHUTDOWN', 'vcf_ops_logs_vms'):
        logs_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_logs_vms')
        vcf_ops_logs_vms = [v.strip() for v in logs_raw.split('\n') 
                          if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if vcf_ops_logs_vms:
            lsf.write_output(f'Processing {len(vcf_ops_logs_vms)} VCF Ops for Logs VM(s)...')
            for vm_name in vcf_ops_logs_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    VM not found')
            lsf.write_output('VCF Operations for Logs shutdown complete')
        else:
            lsf.write_output('No VCF Ops for Logs VMs configured')
    else:
        lsf.write_output(f'Would shutdown VCF Ops for Logs: {vcf_ops_logs_vms}')
    
    #==========================================================================
    # TASK 11: Shutdown VCF Identity Broker
    # Per VCF 9.0 Management Domain order #5
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 11: Shutdown VCF Identity Broker')
    lsf.write_output('='*60)
    update_shutdown_status(11, 'Shutdown VCF Identity Broker', dry_run)
    
    vcf_identity_broker_vms = []  # Not present in all deployments
    
    if lsf.config.has_option('SHUTDOWN', 'vcf_identity_broker_vms'):
        ib_raw = lsf.config.get('SHUTDOWN', 'vcf_identity_broker_vms')
        vcf_identity_broker_vms = [v.strip() for v in ib_raw.split('\n') 
                                  if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if vcf_identity_broker_vms:
            lsf.write_output(f'Processing {len(vcf_identity_broker_vms)} VCF Identity Broker VM(s)...')
            for vm_name in vcf_identity_broker_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    VM not found')
            lsf.write_output('VCF Identity Broker shutdown complete')
        else:
            lsf.write_output('No VCF Identity Broker VMs configured (may not be deployed)')
    else:
        lsf.write_output(f'Would shutdown VCF Identity Broker: {vcf_identity_broker_vms}')
    
    #==========================================================================
    # TASK 12: Shutdown VCF Operations Fleet Management (Aria Suite Lifecycle)
    # Per VCF 9.0 Management Domain order #6
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 12: Shutdown VCF Operations Fleet Management')
    lsf.write_output('='*60)
    update_shutdown_status(12, 'Shutdown VCF Ops Fleet Mgmt', dry_run)
    
    vcf_ops_fleet_vms = ['opslcm-01a', 'opslcm-a']
    
    if lsf.config.has_option('SHUTDOWN', 'vcf_ops_fleet_vms'):
        fleet_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_fleet_vms')
        vcf_ops_fleet_vms = [v.strip() for v in fleet_raw.split('\n') 
                            if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if vcf_ops_fleet_vms:
            lsf.write_output(f'Processing {len(vcf_ops_fleet_vms)} VCF Ops Fleet Management VM(s)...')
            for vm_name in vcf_ops_fleet_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    VM not found')
            lsf.write_output('VCF Operations Fleet Management shutdown complete')
        else:
            lsf.write_output('No VCF Ops Fleet Management VMs configured')
    else:
        lsf.write_output(f'Would shutdown VCF Ops Fleet Management: {vcf_ops_fleet_vms}')
    
    #==========================================================================
    # TASK 13: Shutdown VCF Operations (vrops)
    # Per VCF 9.0 Management Domain order #7
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 13: Shutdown VCF Operations')
    lsf.write_output('='*60)
    update_shutdown_status(13, 'Shutdown VCF Operations', dry_run)
    
    # Note: VCF Operations (vrops) may have been partially shut down via Fleet API in Phase 1
    # This phase ensures any remaining VMs are shut down
    vcf_ops_vms = ['o11n-02a', 'o11n-01a']  # Aria Orchestrator VMs
    
    if lsf.config.has_option('SHUTDOWN', 'vcf_ops_vms'):
        ops_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_vms')
        vcf_ops_vms = [v.strip() for v in ops_raw.split('\n') 
                      if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if vcf_ops_vms:
            lsf.write_output(f'Processing {len(vcf_ops_vms)} VCF Operations VM(s)...')
            for vm_name in vcf_ops_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    VM not found')
            lsf.write_output('VCF Operations shutdown complete')
        else:
            lsf.write_output('No VCF Operations VMs configured')
    else:
        lsf.write_output(f'Would shutdown VCF Operations: {vcf_ops_vms}')
    
    #==========================================================================
    # TASK 14: Shutdown Management Domain NSX Edges
    # Per VCF 9.0 Management Domain order #9
    # Filter: Only edges with "mgmt" in their name (management domain)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 14: Shutdown Management NSX Edges')
    lsf.write_output('='*60)
    update_shutdown_status(14, 'Shutdown Mgmt NSX Edges', dry_run)
    
    # Get all NSX edges from [VCF] vcfnsxedges (primary source)
    # Format: edge-name:esxhost (we only need the edge name)
    # Note: all_nsx_edges was already populated in Phase 5, but we rebuild
    # here to ensure we have the full list in case Phase 5 was skipped
    all_nsx_edges = []
    if lsf.config.has_option('VCF', 'vcfnsxedges'):
        edges_raw = lsf.config.get('VCF', 'vcfnsxedges')
        for entry in edges_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                all_nsx_edges.append(parts[0].strip())
        lsf.write_output(f'Found {len(all_nsx_edges)} NSX Edge(s) in [VCF] vcfnsxedges')
    else:
        lsf.write_output('No NSX Edges configured in [VCF] vcfnsxedges')
    
    # Filter for management domain edges (contain "mgmt" in name)
    mgmt_nsx_edges = [e for e in all_nsx_edges if 'mgmt' in e.lower()]
    
    if not dry_run:
        if mgmt_nsx_edges:
            lsf.write_output(f'Processing {len(mgmt_nsx_edges)} Management NSX Edge VM(s)...')
            edge_count = 0
            for edge_name in mgmt_nsx_edges:
                edge_count += 1
                lsf.write_output(f'  [{edge_count}/{len(mgmt_nsx_edges)}] Looking for: {edge_name}')
                vms = lsf.get_vm_by_name(edge_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(2)
                else:
                    lsf.write_output(f'    Management NSX Edge VM not found')
            lsf.write_output('Management NSX Edge shutdown complete')
        else:
            lsf.write_output('No Management NSX Edges found (no edges with "mgmt" in name)')
    else:
        lsf.write_output(f'Would shutdown Management NSX Edges: {mgmt_nsx_edges}')
    
    #==========================================================================
    # TASK 15: Shutdown Management Domain NSX Manager
    # Per VCF 9.0 Management Domain order #10
    # Filter: Only managers with "mgmt" in their name (management domain)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 15: Shutdown Management NSX Manager')
    lsf.write_output('='*60)
    update_shutdown_status(15, 'Shutdown Mgmt NSX Manager', dry_run)
    
    # Get all NSX managers from [VCF] vcfnsxmgr (primary source)
    # Format: nsx-name:esxhost (we only need the NSX manager name)
    # Note: all_nsx_mgr was already populated in Phase 6, but we rebuild
    # here to ensure we have the full list in case Phase 6 was skipped
    all_nsx_mgr = []
    if lsf.config.has_option('VCF', 'vcfnsxmgr'):
        mgr_raw = lsf.config.get('VCF', 'vcfnsxmgr')
        for entry in mgr_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                all_nsx_mgr.append(parts[0].strip())
        lsf.write_output(f'Found {len(all_nsx_mgr)} NSX Manager(s) in [VCF] vcfnsxmgr')
    else:
        lsf.write_output('No NSX Managers configured in [VCF] vcfnsxmgr')
    
    # Filter for management domain managers (contain "mgmt" in name)
    mgmt_nsx_mgr = [m for m in all_nsx_mgr if 'mgmt' in m.lower()]
    
    if not dry_run:
        if mgmt_nsx_mgr:
            lsf.write_output(f'Processing {len(mgmt_nsx_mgr)} Management NSX Manager VM(s)...')
            mgr_count = 0
            for mgr_name in mgmt_nsx_mgr:
                mgr_count += 1
                lsf.write_output(f'  [{mgr_count}/{len(mgmt_nsx_mgr)}] Looking for: {mgr_name}')
                vms = lsf.get_vm_by_name(mgr_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(5)
                else:
                    lsf.write_output(f'    Management NSX Manager VM not found')
            lsf.write_output('Management NSX Manager shutdown complete')
        else:
            lsf.write_output('No Management NSX Managers found (no managers with "mgmt" in name)')
    else:
        lsf.write_output(f'Would shutdown Management NSX Manager: {mgmt_nsx_mgr}')
    
    #==========================================================================
    # TASK 16: Shutdown SDDC Manager
    # Per VCF 9.0 Management Domain order #11
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 16: Shutdown SDDC Manager')
    lsf.write_output('='*60)
    update_shutdown_status(16, 'Shutdown SDDC Manager', dry_run)
    
    sddc_manager_vms = ['sddcmanager-a']
    
    if lsf.config.has_option('SHUTDOWN', 'sddc_manager_vms'):
        sddc_raw = lsf.config.get('SHUTDOWN', 'sddc_manager_vms')
        sddc_manager_vms = [v.strip() for v in sddc_raw.split('\n') 
                          if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        if sddc_manager_vms:
            lsf.write_output(f'Processing {len(sddc_manager_vms)} SDDC Manager VM(s)...')
            for vm_name in sddc_manager_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(10)  # Longer pause for SDDC Manager
                else:
                    lsf.write_output(f'    SDDC Manager VM not found')
            lsf.write_output('SDDC Manager shutdown complete')
        else:
            lsf.write_output('No SDDC Manager VMs configured')
    else:
        lsf.write_output(f'Would shutdown SDDC Manager: {sddc_manager_vms}')
    
    #==========================================================================
    # TASK 17: Shutdown Management vCenter
    # Per VCF 9.0 Management Domain order #12 (with vSAN and ESX hosts)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 17: Shutdown Management vCenter')
    lsf.write_output('='*60)
    update_shutdown_status(17, 'Shutdown Management vCenter', dry_run)
    
    # Get all vCenters from [VCF] vcfvCenter (primary source)
    # Format: vcenter-name:esxhost (we only need the vCenter name)
    # Filter for management vCenters (contain "mgmt" in name)
    all_vcenters_mgmt = []
    if lsf.config.has_option('VCF', 'vcfvCenter'):
        vc_raw = lsf.config.get('VCF', 'vcfvCenter')
        for entry in vc_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                all_vcenters_mgmt.append(parts[0].strip())
        lsf.write_output(f'Found {len(all_vcenters_mgmt)} vCenter(s) in [VCF] vcfvCenter')
    else:
        lsf.write_output('No vCenters configured in [VCF] vcfvCenter')
    
    # Filter for management vCenters (contain "mgmt" in name)
    mgmt_vcenter_vms = [v for v in all_vcenters_mgmt if 'mgmt' in v.lower()]
    
    if not dry_run:
        if mgmt_vcenter_vms:
            lsf.write_output(f'Processing {len(mgmt_vcenter_vms)} Management vCenter VM(s)...')
            for vm_name in mgmt_vcenter_vms:
                lsf.write_output(f'  Looking for: {vm_name}')
                vms = lsf.get_vm_by_name(vm_name)
                if vms:
                    for vm in vms:
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(10)  # Longer pause for vCenter
                else:
                    lsf.write_output(f'    Management vCenter VM not found')
            lsf.write_output('Management vCenter shutdown complete')
        else:
            lsf.write_output('No Management vCenter VMs configured')
    else:
        lsf.write_output(f'Would shutdown Management vCenter: {mgmt_vcenter_vms}')
    
    #==========================================================================
    # TASK 18: Set Host Advanced Settings
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 18: Set Host Advanced Settings')
    lsf.write_output('='*60)
    update_shutdown_status(18, 'Host Advanced Settings', dry_run)
    
    # Get list of hosts for vSAN operations from [VCF] vcfmgmtcluster (primary source)
    # Format: hostname:esx (we only need the hostname)
    esx_hosts = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        hosts_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        for entry in hosts_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                host = parts[0].strip()
                esx_hosts.append(host)
        lsf.write_output(f'Found {len(esx_hosts)} ESXi host(s) in [VCF] vcfmgmtcluster')
    else:
        lsf.write_output('No ESXi hosts configured in [VCF] vcfmgmtcluster')
    
    esx_username = 'root'
    if lsf.config.has_option('SHUTDOWN', 'esx_username'):
        esx_username = lsf.config.get('SHUTDOWN', 'esx_username')
    
    if not dry_run:
        if esx_hosts:
            lsf.write_output(f'Configuring {len(esx_hosts)} ESXi host(s)...')
            host_count = 0
            for host in esx_hosts:
                host_count += 1
                lsf.write_output(f'  [{host_count}/{len(esx_hosts)}] Setting advanced settings on {host}')
                try:
                    cmd = 'esxcli system settings advanced set -o /Mem/AllocGuestLargePage -i 1'
                    lsf.ssh(cmd, f'{esx_username}@{host}', password)
                    lsf.write_output(f'    Settings applied successfully')
                except Exception as e:
                    lsf.write_output(f'    Warning: Failed to set advanced settings: {e}')
            lsf.write_output('Host advanced settings complete')
        else:
            lsf.write_output('No ESXi hosts configured - skipping')
    else:
        lsf.write_output(f'Would set advanced settings on: {esx_hosts}')
    
    #==========================================================================
    # TASK 19: vSAN Elevator Operations (OSA only - ESA does not use plog)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 19: vSAN Elevator Operations')
    lsf.write_output('='*60)
    update_shutdown_status(19, 'vSAN Elevator Operations', dry_run)
    
    vsan_enabled = True
    if lsf.config.has_option('SHUTDOWN', 'vsan_enabled'):
        vsan_enabled = lsf.config.getboolean('SHUTDOWN', 'vsan_enabled')
    
    vsan_timeout = VSAN_ELEVATOR_TIMEOUT
    if lsf.config.has_option('SHUTDOWN', 'vsan_timeout'):
        vsan_timeout_raw = lsf.config.get('SHUTDOWN', 'vsan_timeout')
        # Strip any inline comments (e.g., "2700  # 45 minutes")
        vsan_timeout_str = vsan_timeout_raw.split('#')[0].strip()
        try:
            vsan_timeout = int(vsan_timeout_str)
        except ValueError:
            lsf.write_output(f'Invalid vsan_timeout value: {vsan_timeout_raw}, using default {VSAN_ELEVATOR_TIMEOUT}')
            vsan_timeout = VSAN_ELEVATOR_TIMEOUT
    
    lsf.write_output(f'vSAN enabled: {vsan_enabled}, ESXi hosts: {len(esx_hosts)}, Timeout: {vsan_timeout}s ({vsan_timeout/60:.0f}min)')
    
    if vsan_enabled and esx_hosts and not dry_run:
        # Check if this is vSAN ESA (Express Storage Architecture)
        # ESA does NOT use the plog mechanism, so elevator wait is not needed
        lsf.write_output('Checking vSAN architecture (OSA vs ESA)...')
        is_esa = False
        if esx_hosts:
            # Check the first host to determine architecture
            test_host = esx_hosts[0]
            is_esa = check_vsan_esa(lsf, test_host, esx_username, password)
            if is_esa:
                lsf.write_output(f'  vSAN ESA detected on {test_host}')
                lsf.write_output('  ESA does not use plog - skipping 45-minute elevator wait')
            else:
                lsf.write_output(f'  vSAN OSA detected on {test_host}')
                lsf.write_output('  OSA uses plog - elevator wait is required')
        
        if is_esa:
            # vSAN ESA - skip the elevator process entirely
            lsf.write_output('vSAN ESA: Elevator operations not required (no plog)')
        else:
            # vSAN OSA - run the full elevator process
            # Enable vSAN elevator on all hosts
            lsf.write_output(f'Enabling vSAN elevator on {len(esx_hosts)} host(s)...')
            host_count = 0
            for host in esx_hosts:
                host_count += 1
                lsf.write_output(f'  [{host_count}/{len(esx_hosts)}] Enabling elevator on {host}')
                set_vsan_elevator(lsf, host, esx_username, password, enable=True)
            
            # Wait for vSAN I/O to complete
            lsf.write_output(f'Starting vSAN I/O flush wait ({vsan_timeout/60:.0f} minutes)...')
            lsf.write_output('  This ensures all pending writes are committed to disk (OSA plog)')
            
            elapsed = 0
            while elapsed < vsan_timeout:
                remaining = (vsan_timeout - elapsed) / 60
                elapsed_min = elapsed / 60
                lsf.write_output(f'  vSAN wait: {elapsed_min:.0f}m elapsed, {remaining:.1f}m remaining')
                time.sleep(VSAN_ELEVATOR_CHECK_INTERVAL)
                elapsed += VSAN_ELEVATOR_CHECK_INTERVAL
            
            lsf.write_output('vSAN I/O flush wait complete')
            
            # Disable vSAN elevator on all hosts
            lsf.write_output(f'Disabling vSAN elevator on {len(esx_hosts)} host(s)...')
            host_count = 0
            for host in esx_hosts:
                host_count += 1
                lsf.write_output(f'  [{host_count}/{len(esx_hosts)}] Disabling elevator on {host}')
                set_vsan_elevator(lsf, host, esx_username, password, enable=False)
            lsf.write_output('vSAN elevator operations complete')
    elif dry_run:
        lsf.write_output(f'Would run vSAN elevator on: {esx_hosts}')
        lsf.write_output(f'vSAN timeout: {vsan_timeout} seconds')
        lsf.write_output('Note: Actual run will check for ESA and skip if detected')
    elif not vsan_enabled:
        lsf.write_output('vSAN elevator skipped (vsan_enabled=false in config)')
    else:
        lsf.write_output('vSAN elevator skipped (no ESXi hosts configured)')
    
    #==========================================================================
    # TASK 20: Shutdown ESXi Hosts
    # Per VCF 9.0 Management Domain order #12 (with vSAN)
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 20: Shutdown ESXi Hosts')
    lsf.write_output('='*60)
    update_shutdown_status(20, 'Shutdown ESXi Hosts', dry_run)
    
    shutdown_hosts = True
    if lsf.config.has_option('SHUTDOWN', 'shutdown_hosts'):
        shutdown_hosts = lsf.config.getboolean('SHUTDOWN', 'shutdown_hosts')
    
    lsf.write_output(f'Host shutdown enabled: {shutdown_hosts}, ESXi hosts: {len(esx_hosts)}')
    
    if shutdown_hosts and esx_hosts and not dry_run:
        lsf.write_output(f'Shutting down {len(esx_hosts)} ESXi host(s)...')
        host_count = 0
        for host in esx_hosts:
            host_count += 1
            lsf.write_output(f'  [{host_count}/{len(esx_hosts)}] Initiating shutdown: {host}')
            shutdown_host(lsf, host, esx_username, password)
            time.sleep(5)  # Stagger host shutdowns
        lsf.write_output('ESXi host shutdown commands sent')
        lsf.write_output('  Note: Hosts may take several minutes to fully power off')
    elif dry_run:
        lsf.write_output(f'Would shutdown hosts: {esx_hosts}')
    elif not shutdown_hosts:
        lsf.write_output('Host shutdown skipped (shutdown_hosts=false in config)')
    else:
        lsf.write_output('Host shutdown skipped (no ESXi hosts configured)')
    
    #==========================================================================
    # Cleanup
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('SHUTDOWN COMPLETE')
    lsf.write_output('='*60)
    
    if not dry_run:
        lsf.write_output('Disconnecting sessions...')
        for si in lsf.sis:
            try:
                connect.Disconnect(si)
            except Exception:
                pass
        lsf.sis.clear()
        lsf.sisvc.clear()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom shutdown tasks here
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'{MODULE_NAME} completed')
    
    # Return success status and list of ESXi hosts that were shut down
    # This allows the caller to wait for hosts to fully power off
    # esx_hosts is defined in Phase 8 so should always exist at this point
    hosts_to_return = esx_hosts if 'esx_hosts' in locals() else []
    return {'success': True, 'esx_hosts': hosts_to_return}


#==============================================================================
# STANDALONE EXECUTION
#==============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=MODULE_DESCRIPTION)
    parser.add_argument('--standalone', action='store_true',
                        help='Run in standalone test mode')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--skip-init', action='store_true',
                        help='Skip lsf.init() call')
    
    args = parser.parse_args()
    
    import lsfunctions as lsf
    
    if not args.skip_init:
        lsf.init(router=False)
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'Dry run: {args.dry_run}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run)
