#!/usr/bin/env python3
# VCFshutdown.py - HOLFY27 Core VCF Shutdown Module
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# VMware Cloud Foundation graceful shutdown sequence

"""
VCF Shutdown Module

This module handles the graceful shutdown of VMware Cloud Foundation environments.
The shutdown order is the REVERSE of the startup order to ensure dependencies are
respected:

Startup Order (VCF.py):
1. Management Cluster Hosts (exit maintenance)
2. Datastore check
3. NSX Manager
4. NSX Edges
5. vCenter

Shutdown Order (this module):
1. vCenter services (stop WCP on workload vCenters)
2. Workload VMs (Tanzu, K8s nodes, user VMs)
3. Management VMs (vCenter, SDDC Manager)
4. NSX Edges
5. NSX Manager
6. vSAN preparation
7. ESXi Hosts

Additional operations handled:
- Fleet Operations (SDDC Manager) for Aria suite shutdown
- WCP (Workload Control Plane) shutdown
- vSAN elevator operations for clean shutdown
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
MODULE_DESCRIPTION = 'VMware Cloud Foundation graceful shutdown'

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


def set_vsan_elevator(lsf, host: str, username: str, password: str, 
                      enable: bool = True) -> bool:
    """
    Set the vSAN elevator mode on an ESXi host for graceful shutdown.
    
    Before vSAN hosts can be shut down, the plogRunElevator setting must be
    enabled to flush all pending I/O, then disabled after the wait period.
    
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
            lsf.write_output(f'Connecting to {len(mgmt_hosts)} management host(s)')
            lsf.connect_vcenters(mgmt_hosts)
    
    #==========================================================================
    # TASK 3: Stop WCP on vCenters
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 3: Stop Workload Control Plane (WCP)')
    lsf.write_output('='*60)
    
    wcp_vcenters = []
    if lsf.config.has_option('SHUTDOWN', 'wcp_vcenters'):
        wcp_raw = lsf.config.get('SHUTDOWN', 'wcp_vcenters')
        wcp_vcenters = [v.strip() for v in wcp_raw.split('\n') 
                       if v.strip() and not v.strip().startswith('#')]
    elif lsf.config.has_option('VCF', 'vcfvCenter'):
        # Use vCenter list from VCF config
        vc_raw = lsf.config.get('VCF', 'vcfvCenter')
        for entry in vc_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                wcp_vcenters.append(parts[0].strip())
    
    if wcp_vcenters and not dry_run:
        for vc in wcp_vcenters:
            if lsf.test_tcp_port(vc, 443, timeout=10):
                shutdown_wcp_service(lsf, vc, password)
            else:
                lsf.write_output(f'{vc} not reachable, skipping WCP stop')
    elif dry_run:
        lsf.write_output(f'Would stop WCP on: {wcp_vcenters}')
    
    #==========================================================================
    # TASK 4: Shutdown Workload VMs
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 4: Shutdown Workload VMs')
    lsf.write_output('='*60)
    
    # VM regex patterns to find and shutdown (Tanzu, K8s, etc.)
    vm_patterns = [
        r'^kubernetes-cluster-.*$',  # TKGs clusters
        r'^dev-project-.*$',  # vSphere with Tanzu projects
        r'^cci-service-.*$',  # CCI services
        r'^SupervisorControlPlaneVM.*$',  # Supervisor VMs
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
        # Find VMs by pattern
        for pattern in vm_patterns:
            lsf.write_output(f'Finding VMs matching: {pattern}')
            vms = get_vms_by_regex(lsf, pattern)
            for vm in vms:
                shutdown_vm_gracefully(lsf, vm)
                time.sleep(2)  # Brief pause between shutdowns
        
        # Shutdown static VM list
        for vm_name in workload_vms:
            vms = lsf.get_vm_by_name(vm_name)
            for vm in vms:
                shutdown_vm_gracefully(lsf, vm)
                time.sleep(2)
    else:
        lsf.write_output(f'Would shutdown VMs matching patterns: {vm_patterns}')
        lsf.write_output(f'Would shutdown VMs: {workload_vms}')
    
    #==========================================================================
    # TASK 5: Shutdown Management VMs
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 5: Shutdown Management VMs')
    lsf.write_output('='*60)
    
    # Management VMs (in shutdown order - reverse of startup)
    mgmt_vms = [
        # Aria Orchestrator
        'o11n-02a', 'o11n-01a',
        # Aria Operations for Logs
        'opslogs-01a', 'ops-01a', 'ops-a',
        # Aria Operations collectors/proxy
        'opscollector-01a', 'opsproxy-01a',
        # Aria Suite Lifecycle
        'opslcm-01a', 'opslcm-a',
        # Aria Operations for Networks
        'opsnet-a', 'opsnet-01a', 'opsnetcollector-01a',
        # SDDC Manager
        'sddcmanager-a',
        # Workload vCenters (before management vCenter)
        'vc-wld02-a', 'vc-wld01-a',
        # Management vCenter (last vCenter)
        'vc-mgmt-a',
    ]
    
    if lsf.config.has_option('SHUTDOWN', 'mgmt_vms'):
        mgmt_vms_raw = lsf.config.get('SHUTDOWN', 'mgmt_vms')
        mgmt_vms = [v.strip() for v in mgmt_vms_raw.split('\n') 
                   if v.strip() and not v.strip().startswith('#')]
    
    if not dry_run:
        for vm_name in mgmt_vms:
            vms = lsf.get_vm_by_name(vm_name)
            for vm in vms:
                shutdown_vm_gracefully(lsf, vm)
                time.sleep(5)  # Longer pause for management VMs
    else:
        lsf.write_output(f'Would shutdown management VMs: {mgmt_vms}')
    
    #==========================================================================
    # TASK 6: Shutdown NSX Edges
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 6: Shutdown NSX Edges')
    lsf.write_output('='*60)
    
    nsx_edges = []
    if lsf.config.has_option('SHUTDOWN', 'nsx_edges'):
        edges_raw = lsf.config.get('SHUTDOWN', 'nsx_edges')
        nsx_edges = [e.strip() for e in edges_raw.split('\n') 
                    if e.strip() and not e.strip().startswith('#')]
    elif lsf.config.has_option('VCF', 'vcfnsxedges'):
        edges_raw = lsf.config.get('VCF', 'vcfnsxedges')
        for entry in edges_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                nsx_edges.append(parts[0].strip())
    
    if not dry_run:
        for edge_name in nsx_edges:
            vms = lsf.get_vm_by_name(edge_name)
            for vm in vms:
                shutdown_vm_gracefully(lsf, vm)
                time.sleep(2)
    else:
        lsf.write_output(f'Would shutdown NSX Edges: {nsx_edges}')
    
    #==========================================================================
    # TASK 7: Shutdown NSX Manager
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 7: Shutdown NSX Manager')
    lsf.write_output('='*60)
    
    nsx_mgr = []
    if lsf.config.has_option('SHUTDOWN', 'nsx_mgr'):
        mgr_raw = lsf.config.get('SHUTDOWN', 'nsx_mgr')
        nsx_mgr = [m.strip() for m in mgr_raw.split('\n') 
                  if m.strip() and not m.strip().startswith('#')]
    elif lsf.config.has_option('VCF', 'vcfnsxmgr'):
        mgr_raw = lsf.config.get('VCF', 'vcfnsxmgr')
        for entry in mgr_raw.split('\n'):
            if entry.strip() and not entry.strip().startswith('#'):
                parts = entry.split(':')
                nsx_mgr.append(parts[0].strip())
    
    if not dry_run:
        for mgr_name in nsx_mgr:
            vms = lsf.get_vm_by_name(mgr_name)
            for vm in vms:
                shutdown_vm_gracefully(lsf, vm)
                time.sleep(5)
    else:
        lsf.write_output(f'Would shutdown NSX Manager: {nsx_mgr}')
    
    #==========================================================================
    # TASK 8: Set Host Advanced Settings
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 8: Set Host Advanced Settings')
    lsf.write_output('='*60)
    
    # Get list of hosts for vSAN operations
    esx_hosts = []
    if lsf.config.has_option('SHUTDOWN', 'esx_hosts'):
        hosts_raw = lsf.config.get('SHUTDOWN', 'esx_hosts')
        esx_hosts = [h.strip() for h in hosts_raw.split('\n') 
                    if h.strip() and not h.strip().startswith('#')]
    elif mgmt_hosts:
        # Extract hostnames from mgmt_hosts entries
        for entry in mgmt_hosts:
            parts = entry.split(':')
            host = parts[0].strip()
            if lsf.test_tcp_port(host, 22, timeout=5):
                esx_hosts.append(host)
    
    esx_username = 'root'
    if lsf.config.has_option('SHUTDOWN', 'esx_username'):
        esx_username = lsf.config.get('SHUTDOWN', 'esx_username')
    
    if not dry_run:
        for host in esx_hosts:
            lsf.write_output(f'Setting advanced settings on {host}')
            try:
                cmd = 'esxcli system settings advanced set -o /Mem/AllocGuestLargePage -i 1'
                lsf.ssh(cmd, f'{esx_username}@{host}', password)
            except Exception as e:
                lsf.write_output(f'Warning: Failed to set advanced settings on {host}: {e}')
    else:
        lsf.write_output(f'Would set advanced settings on: {esx_hosts}')
    
    #==========================================================================
    # TASK 9: vSAN Elevator Operations
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 9: vSAN Elevator Operations')
    lsf.write_output('='*60)
    
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
    
    if vsan_enabled and esx_hosts and not dry_run:
        # Enable vSAN elevator on all hosts
        lsf.write_output('Enabling vSAN elevator on all hosts')
        for host in esx_hosts:
            set_vsan_elevator(lsf, host, esx_username, password, enable=True)
        
        # Wait for vSAN I/O to complete
        lsf.write_output(f'Waiting {vsan_timeout/60:.0f} minutes for vSAN I/O to complete')
        
        elapsed = 0
        while elapsed < vsan_timeout:
            remaining = (vsan_timeout - elapsed) / 60
            lsf.write_output(f'vSAN wait: {remaining:.1f} minutes remaining')
            time.sleep(VSAN_ELEVATOR_CHECK_INTERVAL)
            elapsed += VSAN_ELEVATOR_CHECK_INTERVAL
        
        # Disable vSAN elevator on all hosts
        lsf.write_output('Disabling vSAN elevator on all hosts')
        for host in esx_hosts:
            set_vsan_elevator(lsf, host, esx_username, password, enable=False)
    elif dry_run:
        lsf.write_output(f'Would run vSAN elevator on: {esx_hosts}')
        lsf.write_output(f'vSAN timeout: {vsan_timeout} seconds')
    else:
        lsf.write_output('vSAN elevator skipped (disabled or no hosts)')
    
    #==========================================================================
    # TASK 10: Shutdown ESXi Hosts
    #==========================================================================
    
    lsf.write_output('='*60)
    lsf.write_output('PHASE 10: Shutdown ESXi Hosts')
    lsf.write_output('='*60)
    
    shutdown_hosts = True
    if lsf.config.has_option('SHUTDOWN', 'shutdown_hosts'):
        shutdown_hosts = lsf.config.getboolean('SHUTDOWN', 'shutdown_hosts')
    
    if shutdown_hosts and esx_hosts and not dry_run:
        for host in esx_hosts:
            shutdown_host(lsf, host, esx_username, password)
            time.sleep(5)  # Stagger host shutdowns
    elif dry_run:
        lsf.write_output(f'Would shutdown hosts: {esx_hosts}')
    else:
        lsf.write_output('Host shutdown skipped (disabled or no hosts)')
    
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
    return True


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
