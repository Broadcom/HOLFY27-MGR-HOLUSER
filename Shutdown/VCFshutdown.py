#!/usr/bin/env python3
# VCFshutdown.py - HOLFY27 Core VCF Shutdown Module
# Version 2.1 - February 2026
# Author - Burke Azbill and HOL Core Team
# VMware Cloud Foundation graceful shutdown sequence
#
# v 2.1 Changes:
# - Fixed --phase parameter: previously validated and logged the phase but
#   never gated execution, causing all phases to run regardless of selection
# - Added should_run() helper function to gate each phase block; every phase
#   (1, 1b, 2, 2b, 3-20) is now wrapped in if should_run() guards
# - Fixed cross-phase variable scoping: esx_hosts, mgmt_hosts, and
#   esx_username are now initialized unconditionally before phase guards
#   so --phase 17b, 19, 20, etc. can access them without NameError
# - Single-phase runs now print "Phase N complete." instead of the full
#   shutdown completion instructions
# - Cleaner Phase 1 failure messages: "Fleet LCM API shutdown not available
#   through suite-api proxy" instead of generic warnings
#
# v 2.0 Changes:
# - Phase 1 now supports both VCF 9.0 (opslcm-a, Basic auth) and VCF 9.1
#   (ops-a Fleet LCM plugin, JWT Bearer auth) API paths
# - Version detection: reads [VCF] vcf_version from config.ini; if not set,
#   auto-probes ops-a to detect VCF 9.1 Fleet LCM plugin at runtime
# - VCF 9.1 path: obtains JWT via suite-api, lists components, triggers
#   SHUTDOWN_COMPONENT_WORKFLOW, polls task status
# - Falls back to VCF 9.0 path if 9.1 probe fails or API errors occur
#   during auto-detect mode (explicit vcf_version=9.1 does not fall back)
#
# v 1.9 Changes:
# - Fixed Fleet API false-positive: shutdown_products() returning True when
#   no environments found caused Phase 1b (VCF Automation fallback) to be
#   skipped, leaving auto-platform-a-* running until ESXi host power-off
# - Added Phase 13b: Shutdown VCF Automation VMs (always runs, verifies
#   auto-platform-a-* is powered off regardless of Fleet API result)
# - Added Phase 19b: Shutdown VSP Platform VMs (vsp-01a-*) gracefully
#   before ESXi host shutdown
# - Added Phase 19c: Pre-ESXi Audit - enumerates all VMs still powered on
#   across all ESXi hosts and attempts graceful shutdown of any stragglers
#
# v 1.8 Changes:
# - ALL phase output now written to shutdown.log via vcf_write() so the
#   shutdown log shows complete progress (previously only labstartup.log)
# - Added progress heartbeat during VM graceful shutdown waits so logs
#   never sit idle >60 seconds
# - Added VCF Component Services (K8s workloads on VSP) scale-down phase
#   (reverse of VCFfinal.py Task 2e startup)
# - Bumped version to 1.8
#
# v 1.7 Changes:
# - Added VCF Automation (auto-platform-a) fallback shutdown when Fleet
#   Operations API is unreachable: reads [VCFFINAL] vravms for VM names
# - Fixed VCF Operations for Networks default VM names to include VCF 9.1
#   naming convention (ops_networks-*) alongside legacy names (opsnet-*)
# - Fixed ops-a miscategorized as "Logs" VM: moved to Phase 13 (VCF Ops)
# - Added regex-based VM discovery for management VMs when exact name
#   lookups fail, improving compatibility across VCF naming conventions
#
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
  1. VCF Automation (vra)
  2. VCF Operations for Networks (vrni)
  3. VCF Operations collector
  4. VCF Operations for logs (vrli)
  5. VCF Identity Broker
  6. VCF Operations fleet management (VCF Operations Manager)
  7. VCF Operations (vrops)
  8. VMware Live Site Recovery (if applicable)
  9. NSX Edge nodes
  10. NSX Manager
  11. SDDC Manager
  12. vSAN and ESX Hosts (includes vCenter shutdown)

Shutdown Order (this module) - aligned with VCF 9.0/9.1 docs:

PHASE 1:   Fleet Operations (VCF Operations Suite shutdown via API)
PHASE 1b:  VCF Automation VM fallback (if Fleet API failed)
PHASE 2:   Connect to vCenters (while still available)
PHASE 2b:  Scale Down VCF Component Services (K8s on VSP)
PHASE 3:   Stop WCP (Workload Control Plane) services
PHASE 4:   Shutdown Workload VMs (Tanzu, K8s)
PHASE 5:   Shutdown Workload Domain NSX Edges
PHASE 6:   Shutdown Workload Domain NSX Manager
PHASE 7:   Shutdown Workload vCenters (LAST per workload domain order)
PHASE 8:   Shutdown VCF Operations for Networks (vrni) VMs
PHASE 9:   Shutdown VCF Operations Collector VMs
PHASE 10:  Shutdown VCF Operations for Logs (vrli) VMs
PHASE 11:  Shutdown VCF Identity Broker VMs
PHASE 12:  Shutdown VCF Operations Fleet Management VMs
PHASE 13:  Shutdown VCF Operations (vrops) VMs
PHASE 14:  Shutdown Management Domain NSX Edges
PHASE 15:  Shutdown Management Domain NSX Manager
PHASE 16:  Shutdown SDDC Manager
PHASE 17:  Shutdown Management vCenter
PHASE 17b: Connect to ESXi Hosts directly (vCenters now down)
PHASE 18:  Set Host Advanced Settings
PHASE 19:  vSAN Elevator Operations
PHASE 19b: Shutdown VSP Platform VMs
PHASE 19c: Pre-ESXi Shutdown Audit
PHASE 20:  Shutdown ESXi Hosts

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
logging.basicConfig(
    level=logging.WARNING,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VCFshutdown'
MODULE_VERSION = '2.1'
MODULE_DESCRIPTION = 'VMware Cloud Foundation graceful shutdown (VCF 9.x compliant)'

# Status file for console display
STATUS_FILE = '/lmchol/hol/startup_status.txt'

# Shutdown log file (mirrors all VCF phase output to shutdown.log)
SHUTDOWN_LOG = '/home/holuser/hol/shutdown.log'

# vSAN elevator timeout (45 minutes recommended by VMware)
VSAN_ELEVATOR_TIMEOUT = 2700  # 45 minutes in seconds
VSAN_ELEVATOR_CHECK_INTERVAL = 60  # Check every minute

# VM shutdown timeout
VM_SHUTDOWN_TIMEOUT = 300  # 5 minutes per VM
VM_SHUTDOWN_POLL_INTERVAL = 5  # seconds

# Progress heartbeat: log a waiting message if no output for this many seconds
HEARTBEAT_INTERVAL = 60  # seconds

# Host shutdown timeout
HOST_SHUTDOWN_TIMEOUT = 600  # 10 minutes per host

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def write_to_shutdown_log(msg: str):
    """
    Append a timestamped message to shutdown.log.
    
    This ensures shutdown.log receives ALL phase output from VCFshutdown.py,
    not just the outer Shutdown.py orchestrator phases.
    """
    import datetime as _dt
    timestamp = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted = f'[{timestamp}] {msg}'
    try:
        with open(SHUTDOWN_LOG, 'a') as f:
            f.write(formatted + '\n')
    except Exception:
        pass


def vcf_write(lsf, msg: str):
    """
    Write a message to BOTH lsf.write_output() (labstartup.log + console)
    AND shutdown.log. Every VCF shutdown phase message should use this
    function so that shutdown.log has a complete record.
    """
    lsf.write_output(msg)
    write_to_shutdown_log(msg)


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


def shutdown_vms_by_names(lsf, vm_names: list, dry_run: bool = False,
                         phase_label: str = '', use_regex: bool = False) -> int:
    """
    Look up VMs by name (exact or regex) and shut them down gracefully.
    
    :param lsf: lsfunctions module reference
    :param vm_names: List of VM name strings (exact names or regex patterns)
    :param dry_run: Preview mode
    :param phase_label: Label for log messages
    :param use_regex: If True, treat names as regex patterns
    :return: Number of VMs successfully processed
    """
    if dry_run:
        mode = 'regex' if use_regex else 'exact'
        vcf_write(lsf, f'Would shutdown ({mode}): {vm_names}')
        return 0
    
    processed = 0
    for vm_name in vm_names:
        vcf_write(lsf, f'  Looking for: {vm_name}')
        if use_regex:
            vms = lsf.get_vm_match(vm_name)
        else:
            vms = lsf.get_vm_by_name(vm_name)
        if vms:
            for vm in vms:
                processed += 1
                shutdown_vm_gracefully(lsf, vm)
                time.sleep(5)
        else:
            vcf_write(lsf, f'    VM not found')
    return processed


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
    Logs progress heartbeats so shutdown.log never sits idle >60s.
    
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
        vcf_write(lsf, f'{vm_name}: Unable to check power state: {e}')
        return False
    
    # Already powered off?
    if power_state == vim.VirtualMachinePowerState.poweredOff:
        vcf_write(lsf, f'{vm_name}: Already powered off - skipping')
        return True
    
    # Suspended?
    if power_state == vim.VirtualMachinePowerState.suspended:
        vcf_write(lsf, f'{vm_name}: VM is suspended, powering off')
        try:
            task = vm.PowerOffVM_Task()
            from pyVim.task import WaitForTask
            WaitForTask(task)
            return True
        except Exception as e:
            vcf_write(lsf, f'{vm_name}: Failed to power off suspended VM: {e}')
            return False
    
    # VM is powered on - proceed with shutdown
    vcf_write(lsf, f'{vm_name}: Currently powered on, initiating shutdown')
    
    # Check VMware Tools status
    try:
        tools_status = vm.guest.toolsRunningStatus
        vcf_write(lsf, f'{vm_name}: VMware Tools status: {tools_status}')
    except Exception:
        tools_status = 'guestToolsNotRunning'
        vcf_write(lsf, f'{vm_name}: Unable to check Tools status, assuming not running')
    
    try:
        if tools_status == 'guestToolsRunning':
            vcf_write(lsf, f'{vm_name}: Initiating graceful guest shutdown')
            try:
                vm.ShutdownGuest()
            except vim.fault.ToolsUnavailable:
                # Race condition: toolsRunningStatus was stale (common with
                # K8s pod VMs). Fall back to PowerOffVM_Task immediately.
                vcf_write(lsf, f'{vm_name}: Tools reported running but unavailable '
                          f'(stale status), forcing power off')
                task = vm.PowerOffVM_Task()
                from pyVim.task import WaitForTask
                WaitForTask(task)
                vcf_write(lsf, f'{vm_name}: Powered off successfully')
                return True
        else:
            vcf_write(lsf, f'{vm_name}: No VMware Tools available, forcing power off')
            task = vm.PowerOffVM_Task()
            from pyVim.task import WaitForTask
            WaitForTask(task)
            vcf_write(lsf, f'{vm_name}: Powered off successfully')
            return True
        
        # Wait for graceful shutdown with heartbeat progress
        start_time = time.time()
        last_heartbeat = start_time
        while (time.time() - start_time) < timeout:
            try:
                if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
                    elapsed = int(time.time() - start_time)
                    vcf_write(lsf, f'{vm_name}: Powered off successfully ({elapsed}s)')
                    return True
            except Exception:
                pass
            
            # Heartbeat: log progress so shutdown.log doesn't appear idle
            now = time.time()
            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                elapsed = int(now - start_time)
                remaining = timeout - elapsed
                vcf_write(lsf, f'{vm_name}: Waiting for guest shutdown... '
                          f'({elapsed}s elapsed, {remaining}s remaining)')
                last_heartbeat = now
            
            time.sleep(VM_SHUTDOWN_POLL_INTERVAL)
        
        # Timeout - force power off
        vcf_write(lsf, f'{vm_name}: Graceful shutdown timeout ({timeout}s), forcing power off')
        task = vm.PowerOffVM_Task()
        from pyVim.task import WaitForTask
        WaitForTask(task)
        vcf_write(lsf, f'{vm_name}: Powered off successfully (forced)')
        return True
        
    except Exception as e:
        vcf_write(lsf, f'{vm_name}: Error during shutdown: {e}')
        try:
            task = vm.PowerOffVM_Task()
            from pyVim.task import WaitForTask
            WaitForTask(task)
            vcf_write(lsf, f'{vm_name}: Powered off successfully (forced after error)')
            return True
        except Exception as e2:
            vcf_write(lsf, f'{vm_name}: Force power off failed: {e2}')
            return False


def shutdown_wcp_service(lsf, vc_fqdn: str, password: str) -> bool:
    """
    Stop the WCP (Workload Control Plane) service on a vCenter.
    
    :param lsf: lsfunctions module reference
    :param vc_fqdn: vCenter FQDN
    :param password: root password for vCenter
    :return: True if WCP stopped successfully
    """
    vcf_write(lsf, f'Stopping WCP service on {vc_fqdn}')
    
    if not lsf.test_tcp_port(vc_fqdn, 22, timeout=5):
        vcf_write(lsf, f'{vc_fqdn} SSH port not reachable')
        return False
    
    try:
        result = lsf.ssh('vmon-cli -k wcp', f'root@{vc_fqdn}', password)
        if result.returncode == 0:
            vcf_write(lsf, f'WCP service stopped on {vc_fqdn}')
            return True
        else:
            vcf_write(lsf, f'WCP stop returned: {result.stderr}')
            return False
    except Exception as e:
        vcf_write(lsf, f'Error stopping WCP on {vc_fqdn}: {e}')
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
        vcf_write(lsf, f'{host} SSH port not reachable for ESA check')
        return False
    
    try:
        cmd = 'esxcli vsan cluster get 2>/dev/null'
        result = lsf.ssh(cmd, f'{username}@{host}', password)
        
        if result.returncode == 0:
            output = result.stdout if hasattr(result, 'stdout') and result.stdout else ''
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            
            for line in output.splitlines():
                if 'vSAN ESA Enabled' in line:
                    if 'true' in line.lower():
                        vcf_write(lsf, f'{host}: vSAN ESA Enabled = true')
                        return True
                    else:
                        vcf_write(lsf, f'{host}: vSAN ESA Enabled = false (OSA)')
                        return False
            
            vcf_write(lsf, f'{host}: vSAN ESA field not found in cluster info (assuming OSA)')
            return False
        else:
            vcf_write(lsf, f'{host}: esxcli vsan cluster get failed (vSAN may not be configured)')
            return False
    except Exception as e:
        vcf_write(lsf, f'Error checking vSAN architecture on {host}: {e}')
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
    
    vcf_write(lsf, f'{action} vSAN elevator on {host}')
    
    if not lsf.test_tcp_port(host, 22, timeout=5):
        vcf_write(lsf, f'{host} SSH port not reachable')
        return False
    
    try:
        cmd = f'yes | vsish -e set /config/LSOM/intOpts/plogRunElevator {value}'
        result = lsf.ssh(cmd, f'{username}@{host}', password)
        return result.returncode == 0
    except Exception as e:
        vcf_write(lsf, f'Error setting vSAN elevator on {host}: {e}')
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
    
    vcf_write(lsf, f'Shutting down ESXi host: {host_fqdn}')
    
    if not lsf.test_tcp_port(host_fqdn, 443, timeout=5):
        vcf_write(lsf, f'{host_fqdn} is not reachable')
        return False
    
    try:
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
        vcf_write(lsf, f'Shutdown task initiated for {host_fqdn}')
        
        connect.Disconnect(si)
        return True
        
    except Exception as e:
        vcf_write(lsf, f'Error shutting down {host_fqdn}: {e}')
        return False


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False, phase=None):
    """
    Main entry point for VCFshutdown module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    :param phase: If set, run only this specific phase (e.g., '1', '1b', '2b', '17b')
                  If None, run all phases in order.
    """
    from pyVim import connect
    from pyVmomi import vim
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    import fleet  # Import fleet operations module
    
    # Valid phase IDs for --phase parameter validation
    ALL_PHASES = [
        '1', '1b', '2', '2b', '3', '4', '5', '6', '7',
        '8', '9', '10', '11', '12', '13',
        '14', '15', '16', '17', '17b',
        '18', '19', '19b', '19c', '20'
    ]
    
    if phase is not None and str(phase).lower() not in ALL_PHASES:
        vcf_write(lsf, f'ERROR: Invalid phase "{phase}". Valid phases: {", ".join(ALL_PHASES)}')
        return {'success': False, 'esx_hosts': []}
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    if phase is None:
        vcf_write(lsf, f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    password = lsf.get_password()
    
    #==========================================================================
    # TASK 1: Shutdown Fleet Operations Products (VCF Operations Suite)
    # Supports two API paths:
    #   VCF 9.1: ops-a Fleet LCM plugin (JWT Bearer, component-based)
    #   VCF 9.0: opslcm-a legacy API (Basic auth, environment/product-based)
    # Version is read from [VCF] vcf_version or auto-probed at runtime.
    #==========================================================================
    
    def should_run(phase_id: str) -> bool:
        """Return True if this phase should execute (no filter, or matches)."""
        if phase is None:
            return True
        return str(phase).lower() == str(phase_id).lower()
    
    # All config reading happens unconditionally so later phases have
    # the data they need even when running a single phase.
    
    # --- Read configuration for both API versions (always, needed by later phases) ---
    
    # VCF 9.0 legacy settings
    fleet_fqdn = None
    fleet_username = 'admin@local'
    if lsf.config.has_option('SHUTDOWN', 'fleet_fqdn'):
        fleet_fqdn = lsf.config.get('SHUTDOWN', 'fleet_fqdn')
    elif lsf.config.has_option('VCF', 'fleet_fqdn'):
        fleet_fqdn = lsf.config.get('VCF', 'fleet_fqdn')
    else:
        fleet_fqdn = 'opslcm-a.site-a.vcf.lab'
    if lsf.config.has_option('SHUTDOWN', 'fleet_username'):
        fleet_username = lsf.config.get('SHUTDOWN', 'fleet_username')
    
    # VCF 9.1 settings
    ops_fqdn = 'ops-a.site-a.vcf.lab'
    ops_username = 'admin'
    if lsf.config.has_option('SHUTDOWN', 'ops_fqdn'):
        ops_fqdn = lsf.config.get('SHUTDOWN', 'ops_fqdn')
    if lsf.config.has_option('SHUTDOWN', 'ops_username'):
        ops_username = lsf.config.get('SHUTDOWN', 'ops_username')
    
    # Products to shutdown via Fleet Operations (reverse order from startup)
    fleet_products = ['vra', 'vrni']
    if lsf.config.has_option('SHUTDOWN', 'fleet_products'):
        fleet_products_raw = lsf.config.get('SHUTDOWN', 'fleet_products')
        fleet_products = [p.strip() for p in fleet_products_raw.split(',')]
    
    # --- Version detection ---
    vcf_version = fleet.detect_vcf_version(lsf.config)
    version_explicit = vcf_version is not None
    
    if vcf_version:
        vcf_write(lsf, f'VCF version from config: {vcf_version}')
    else:
        vcf_write(lsf, 'VCF version not set in config, will auto-detect')
    
    fleet_api_succeeded = False
    
    # --- Management host lists (needed across multiple phases) ---
    # mgmt_hosts: full config entries for connect_vcenters (Phase 2, 17b)
    # esx_hosts: hostnames only for SSH operations (Phase 18, 19, 20)
    esx_hosts = []
    mgmt_hosts = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        hosts_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        mgmt_hosts = [h.strip() for h in hosts_raw.split('\n')
                      if h.strip() and not h.strip().startswith('#')]
        for entry in mgmt_hosts:
            parts = entry.split(':')
            esx_hosts.append(parts[0].strip())
    
    esx_username = 'root'
    if lsf.config.has_option('SHUTDOWN', 'esx_username'):
        esx_username = lsf.config.get('SHUTDOWN', 'esx_username')
    
    if should_run('1'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 1: Fleet Operations (VCF Operations Suite) Shutdown')
        vcf_write(lsf, '='*60)
        update_shutdown_status(1, 'Fleet Operations (VCF Operations Suite)', dry_run)
        
        # --- VCF 9.1 API path ---
        use_v91 = (vcf_version == '9.1')
        
        if not version_explicit:
            if lsf.test_tcp_port(ops_fqdn, 443, timeout=10):
                vcf_write(lsf, f'Probing {ops_fqdn} for VCF 9.1 internal components API...')
                if fleet.probe_vcf_91(ops_fqdn, password=password):
                    vcf_write(lsf, 'VCF 9.1 internal components API detected (auto-probe)')
                    use_v91 = True
                else:
                    vcf_write(lsf, 'VCF 9.1 internal components API not detected, using VCF 9.0 API')
            else:
                vcf_write(lsf, f'{ops_fqdn} not reachable, using VCF 9.0 API')
        
        if use_v91:
            vcf_write(lsf, f'Using VCF 9.1 internal components API via {ops_fqdn}')
            
            if not dry_run:
                try:
                    vcf_write(lsf, f'Acquiring OpsToken from {ops_fqdn} suite-api...')
                    jwt_token = fleet.get_ops_jwt_token(ops_fqdn, ops_username, password)
                    vcf_write(lsf, 'OpsToken acquired successfully')
                    
                    def _fleet_log(msg):
                        vcf_write(lsf, msg)
                    
                    success = fleet.shutdown_products_v91(ops_fqdn, jwt_token,
                                                          fleet_products,
                                                          write_output=_fleet_log)
                    if success:
                        vcf_write(lsf, 'Fleet LCM (VCF 9.1) API shutdown complete')
                        fleet_api_succeeded = True
                    else:
                        vcf_write(lsf, 'Fleet LCM API shutdown not available through suite-api proxy')
                        vcf_write(lsf, 'VCF Automation VMs will be shut down directly in Phase 1b')
                except Exception as e:
                    vcf_write(lsf, f'Fleet LCM (VCF 9.1) shutdown error: {e}')
                    if not version_explicit:
                        vcf_write(lsf, 'Falling back to VCF 9.0 legacy API...')
                        use_v91 = False
                    else:
                        vcf_write(lsf, 'VCF version explicitly set to 9.1, not falling back to 9.0')
            else:
                vcf_write(lsf, f'Would shutdown Fleet products via VCF 9.1 API: {fleet_products}')
        
        # --- VCF 9.0 API path (used when 9.1 is not available or as fallback) ---
        if not use_v91 and not fleet_api_succeeded:
            vcf_write(lsf, f'Using VCF 9.0 legacy Fleet API via {fleet_fqdn}')
            
            if lsf.test_tcp_port(fleet_fqdn, 443, timeout=10):
                vcf_write(lsf, f'Fleet Management available at {fleet_fqdn}')
                
                if not dry_run:
                    try:
                        token = fleet.get_encoded_token(fleet_username, password)
                        success = fleet.shutdown_products(fleet_fqdn, token, fleet_products,
                                                          write_output=lsf.write_output,
                                                          skip_inventory_sync=True)
                        if success:
                            vcf_write(lsf, 'Fleet Operations (VCF 9.0) products shutdown complete')
                            fleet_api_succeeded = True
                        else:
                            vcf_write(lsf, 'WARNING: Some Fleet Operations products may not have shutdown cleanly')
                            vcf_write(lsf, '(Products will be shut down via VM power-off in later phases)')
                    except Exception as e:
                        vcf_write(lsf, f'Fleet Operations (VCF 9.0) shutdown error: {e}')
                else:
                    vcf_write(lsf, f'Would shutdown Fleet products via VCF 9.0 API: {fleet_products}')
            else:
                vcf_write(lsf, f'Fleet Management not reachable at {fleet_fqdn}, skipping')
                vcf_write(lsf, 'VCF Automation VMs will be shut down directly in Phase 1b')
    
    #==========================================================================
    # TASK 1b: VCF Automation VM fallback shutdown
    # When Fleet Operations API fails to shut down VCF Automation (Phase 1),
    # the VMs must be shut down directly. VM names come from [VCFFINAL] vravms.
    # This runs BEFORE connecting to infrastructure (Phase 2) so that it
    # follows Phase 1 in the logical shutdown sequence.
    #==========================================================================
    
    if should_run('1b'):
        if not fleet_api_succeeded:
            vcf_write(lsf, '='*60)
            vcf_write(lsf, 'PHASE 1b: Shutdown VCF Automation VMs (Fleet API fallback)')
            vcf_write(lsf, '='*60)
            update_shutdown_status(1, 'Shutdown VCF Automation VMs', dry_run)
            
            vcf_write(lsf, 'Fleet API did not fully succeed - connecting to vCenters for VM shutdown...')
            
            vcenters_for_1b = []
            if lsf.config.has_option('RESOURCES', 'vCenters'):
                vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
                vcenters_for_1b = [v.strip() for v in vcenters_raw.split('\n')
                                  if v.strip() and not v.strip().startswith('#')]
            
            if vcenters_for_1b and not dry_run and not lsf.sis:
                vcf_write(lsf, f'Connecting to {len(vcenters_for_1b)} vCenter(s) for Phase 1b:')
                for vc in vcenters_for_1b:
                    vcf_write(lsf, f'  - {vc}')
                lsf.connect_vcenters(vcenters_for_1b)
                vcf_write(lsf, f'Connected to {len(lsf.sis)} vSphere endpoint(s)')
            
            vra_vm_patterns = []
            if lsf.config.has_option('VCFFINAL', 'vravms'):
                vra_raw = lsf.config.get('VCFFINAL', 'vravms')
                for entry in vra_raw.split('\n'):
                    entry = entry.strip()
                    if entry and not entry.startswith('#'):
                        vm_pattern = entry.split(':')[0].strip()
                        if vm_pattern:
                            vra_vm_patterns.append(vm_pattern)
            
            if vra_vm_patterns:
                vcf_write(lsf, f'Found {len(vra_vm_patterns)} VCF Automation VM pattern(s) from [VCFFINAL] vravms')
                if not dry_run:
                    for pattern in vra_vm_patterns:
                        vcf_write(lsf, f'  Searching for VMs matching: {pattern}')
                        vms = lsf.get_vm_match(pattern)
                        if vms:
                            vcf_write(lsf, f'  Found {len(vms)} VM(s)')
                            for vm in vms:
                                if 'sddcmanager' in vm.name.lower():
                                    vcf_write(lsf, f'  {vm.name}: Skipping (handled in Phase 16)')
                                    continue
                                shutdown_vm_gracefully(lsf, vm)
                                time.sleep(5)
                        else:
                            vcf_write(lsf, f'  No VMs found matching pattern')
                    vcf_write(lsf, 'VCF Automation VM fallback shutdown complete')
                else:
                    vcf_write(lsf, f'Would shutdown VCF Automation VMs: {vra_vm_patterns}')
            else:
                vcf_write(lsf, 'No VCF Automation VMs configured in [VCFFINAL] vravms')
        else:
            vcf_write(lsf, '')
            vcf_write(lsf, 'Phase 1b: Skipped (Fleet API handled VCF Automation shutdown)')
    
    #==========================================================================
    # TASK 2: Connect to vCenters and Management Infrastructure
    # Connect to vCenters first (while they are still available) for all
    # subsequent VM shutdown operations. ESXi host direct connections are
    # only needed after vCenters are shut down (Phase 17+).
    #==========================================================================
    
    if should_run('2'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 2: Connect to Management Infrastructure')
        vcf_write(lsf, '='*60)
        update_shutdown_status(2, 'Connect to Infrastructure', dry_run)
        
        vcenters = []
        if lsf.config.has_option('RESOURCES', 'vCenters'):
            vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
            vcenters = [v.strip() for v in vcenters_raw.split('\n') 
                       if v.strip() and not v.strip().startswith('#')]
        
        if not dry_run:
            if vcenters and not lsf.sis:
                vcf_write(lsf, f'Connecting to {len(vcenters)} vCenter(s):')
                for vc in vcenters:
                    vcf_write(lsf, f'  - {vc}')
                lsf.connect_vcenters(vcenters)
                vcf_write(lsf, f'Connected to {len(lsf.sis)} vSphere endpoint(s)')
            elif lsf.sis:
                vcf_write(lsf, f'Already connected to {len(lsf.sis)} vSphere endpoint(s) (from Phase 1b)')
            elif mgmt_hosts:
                vcf_write(lsf, 'No vCenters configured, falling back to ESXi host connections')
                vcf_write(lsf, f'Connecting to {len(mgmt_hosts)} management host(s):')
                for host in mgmt_hosts:
                    vcf_write(lsf, f'  - {host}')
                lsf.connect_vcenters(mgmt_hosts)
                vcf_write(lsf, f'Connected to {len(lsf.sis)} vSphere endpoint(s)')
            else:
                vcf_write(lsf, 'No vCenters or management hosts configured')
    
    #==========================================================================
    # TASK 2b: Scale down VCF Component Services on VSP management cluster
    # Reverse of VCFfinal.py Task 2e (vcfcomponents startup).
    # These are K8s workloads (Salt, Telemetry, Fleet Depot, VIDB, etc.)
    # that must be gracefully scaled to 0 replicas before the VSP VMs or
    # ESXi hosts are shut down. Also annotates Component CRDs as NotRunning.
    #==========================================================================
    
    if should_run('2b'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 2b: Scale Down VCF Component Services (K8s on VSP)')
        vcf_write(lsf, '='*60)
        update_shutdown_status(2, 'Scale Down VCF Component Services', dry_run)

        vcfcomponents = []
        if lsf.config.has_option('VCFFINAL', 'vcfcomponents'):
            comp_raw = lsf.config.get('VCFFINAL', 'vcfcomponents')
            vcfcomponents = [c.strip() for c in comp_raw.split('\n')
                             if c.strip() and not c.strip().startswith('#')]

        if vcfcomponents:
            vcf_write(lsf, f'Found {len(vcfcomponents)} VCF Component(s) to scale down')

            if not dry_run:
                import socket

                vsp_control_plane_ip = None
                vsp_user = 'vmware-system-user'

                # Check for static override first
                if lsf.config.has_option('VCFFINAL', 'vspcontrolplaneip'):
                    vsp_control_plane_ip = lsf.config.get('VCFFINAL', 'vspcontrolplaneip').strip()
                    if vsp_control_plane_ip:
                        vcf_write(lsf, f'  VSP control plane IP (from config): {vsp_control_plane_ip}')

                # Auto-discover VSP control plane via DNS + SSH
                if not vsp_control_plane_ip:
                    vsp_candidates = ['vsp-01a.site-a.vcf.lab']
                    for candidate in vsp_candidates:
                        try:
                            worker_ip = socket.gethostbyname(candidate)
                            vcf_write(lsf, f'  VSP worker: {candidate} -> {worker_ip}')
                            result = lsf.ssh(
                                f"echo '{password}' | sudo -S grep server: /etc/kubernetes/node-agent.conf",
                                f'{vsp_user}@{worker_ip}'
                            )
                            if hasattr(result, 'stdout') and result.stdout:
                                import re as _re
                                for line in result.stdout.strip().split('\n'):
                                    if 'server:' in line:
                                        match = _re.search(r'https?://([0-9.]+):', line)
                                        if match:
                                            vsp_control_plane_ip = match.group(1)
                                            vcf_write(lsf, f'  VSP control plane IP: {vsp_control_plane_ip}')
                                            break
                            if vsp_control_plane_ip:
                                break
                        except Exception as e:
                            vcf_write(lsf, f'  VSP discovery failed for {candidate}: {e}')

                if vsp_control_plane_ip:
                    # Detect sudo mode
                    sudo_needs_password = True
                    sudo_check = lsf.ssh('sudo -n true', f'{vsp_user}@{vsp_control_plane_ip}')
                    if hasattr(sudo_check, 'returncode') and sudo_check.returncode == 0:
                        sudo_needs_password = False

                    def vsp_kubectl(kubectl_cmd):
                        if sudo_needs_password:
                            ssh_cmd = f"echo '{password}' | sudo -S -i bash -c '{kubectl_cmd}'"
                        else:
                            ssh_cmd = f"sudo -i bash -c '{kubectl_cmd}'"
                        return lsf.ssh(ssh_cmd, f'{vsp_user}@{vsp_control_plane_ip}')

                    # Scale down each component to 0 replicas (reverse order from startup)
                    scaled_down = 0
                    already_stopped = 0
                    errors = 0

                    for entry in reversed(vcfcomponents):
                        parts = entry.split(':', 1)
                        if len(parts) != 2 or '/' not in parts[1]:
                            vcf_write(lsf, f'  WARNING: Invalid vcfcomponents entry: {entry}')
                            errors += 1
                            continue

                        namespace = parts[0].strip()
                        resource = parts[1].strip()

                        # Check current replica count
                        check_cmd = f'kubectl get {resource} -n {namespace} -o jsonpath="{{.spec.replicas}}"'
                        check_result = vsp_kubectl(check_cmd)
                        current_replicas = ''
                        if hasattr(check_result, 'stdout') and check_result.stdout:
                            current_replicas = check_result.stdout.strip().split('\n')[-1].strip()

                        if current_replicas == '0':
                            vcf_write(lsf, f'  {namespace}/{resource}: already stopped (replicas=0)')
                            already_stopped += 1
                            continue

                        vcf_write(lsf, f'  Scaling down: {namespace}/{resource} (was replicas={current_replicas})')
                        scale_cmd = f'kubectl scale {resource} -n {namespace} --replicas=0'
                        scale_result = vsp_kubectl(scale_cmd)

                        if hasattr(scale_result, 'returncode') and scale_result.returncode == 0:
                            scaled_down += 1
                        else:
                            err = ''
                            if hasattr(scale_result, 'stderr') and scale_result.stderr:
                                err = scale_result.stderr.strip()[:200]
                            vcf_write(lsf, f'  WARNING: Failed to scale down {namespace}/{resource}: {err}')
                            errors += 1

                    # Annotate Component CRDs as NotRunning
                    if scaled_down > 0:
                        vcf_write(lsf, '  Updating Component CRD annotations to NotRunning...')
                        comp_list = vsp_kubectl(
                            'kubectl get components.api.vmsp.vmware.com -A '
                            '-o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,'
                            'STATUS:.metadata.annotations.component\\.vmsp\\.vmware\\.com/operational-status '
                            '--no-headers'
                        )
                        if hasattr(comp_list, 'stdout') and comp_list.stdout:
                            for line in comp_list.stdout.strip().split('\n'):
                                cols = line.split()
                                if len(cols) >= 3 and cols[2] == 'Running':
                                    crd_ns, crd_name = cols[0], cols[1]
                                    vcf_write(lsf, f'  Annotating {crd_ns}/{crd_name} -> NotRunning')
                                    vsp_kubectl(
                                        f'kubectl annotate components.api.vmsp.vmware.com '
                                        f'{crd_name} -n {crd_ns} '
                                        f'component.vmsp.vmware.com/operational-status=NotRunning --overwrite'
                                    )

                    # Suspend postgres instances (reverse of startup unsuspend)
                    # Two-step process:
                    #   1. Set suspended label on PostgresInstance CRD
                    #   2. Scale Zalando postgres CRD numberOfInstances to 0
                    # Both are needed for clean shutdown and matching startup unsuspend.
                    vcf_write(lsf, '  Suspending Postgres instances...')
                    pg_check = vsp_kubectl(
                        'kubectl get postgresinstances.database.vmsp.vmware.com -A '
                        '-o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,'
                        'SUSPENDED:.metadata.labels.database\\.vmsp\\.vmware\\.com/suspended '
                        '--no-headers'
                    )
                    if hasattr(pg_check, 'stdout') and pg_check.stdout:
                        for line in pg_check.stdout.strip().split('\n'):
                            cols = line.split()
                            if len(cols) >= 2:
                                pg_ns, pg_name = cols[0], cols[1]
                                suspended = cols[2] if len(cols) >= 3 else '<none>'
                                if suspended != 'true':
                                    vcf_write(lsf, f'  Suspending Postgres: {pg_ns}/{pg_name}')
                                    vsp_kubectl(
                                        f'kubectl label postgresinstances.database.vmsp.vmware.com '
                                        f'{pg_name} -n {pg_ns} database.vmsp.vmware.com/suspended=true --overwrite'
                                    )
                                    patch_json = '{"spec":{"numberOfInstances":0}}'
                                    vsp_kubectl(
                                        f"kubectl patch postgresqls.acid.zalan.do {pg_name} -n {pg_ns} "
                                        f"--type=merge -p '{patch_json}'"
                                    )

                    vcf_write(lsf, f'VCF Component Services: {scaled_down} scaled down, '
                              f'{already_stopped} already stopped, {errors} errors '
                              f'(of {len(vcfcomponents)} total)')
                else:
                    vcf_write(lsf, '  VSP control plane not reachable - skipping component scale-down')
                    vcf_write(lsf, '  (Components will be stopped when VSP VMs are powered off)')
            else:
                vcf_write(lsf, f'Would scale down {len(vcfcomponents)} VCF component(s) (dry run)')
        else:
            vcf_write(lsf, 'No VCF Components configured in [VCFFINAL] vcfcomponents')

        #==========================================================================
        # TASK 3: Stop WCP on vCenters
        # Determined from [VCFFINAL] tanzucontrol (same config used by VCFfinal.py)
        # This eliminates redundant config entries and reduces errors
    #==========================================================================
    
    if should_run('3'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 3: Stop Workload Control Plane (WCP)')
        vcf_write(lsf, '='*60)
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
                vcf_write(lsf, f'WCP vCenters from tanzucontrol config: {wcp_vcenters}')
            else:
                vcf_write(lsf, 'tanzucontrol configured but no vCenters specified')
        else:
            vcf_write(lsf, 'No tanzucontrol in [VCFFINAL] - WCP not configured')

        if wcp_vcenters and not dry_run:
            vcf_write(lsf, f'Found {len(wcp_vcenters)} vCenter(s) with WCP to stop')
            for vc in wcp_vcenters:
                vcf_write(lsf, f'Checking WCP on {vc}...')
                if lsf.test_tcp_port(vc, 443, timeout=10):
                    shutdown_wcp_service(lsf, vc, password)
                else:
                    vcf_write(lsf, f'{vc} not reachable, skipping WCP stop')
        elif dry_run:
            vcf_write(lsf, f'Would stop WCP on: {wcp_vcenters}')
        elif not wcp_vcenters:
            vcf_write(lsf, 'No WCP vCenters to stop - skipping')

        #==========================================================================
        # TASK 4: Shutdown Workload VMs
    #==========================================================================
    
    if should_run('4'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 4: Shutdown Workload VMs')
        vcf_write(lsf, '='*60)
        update_shutdown_status(4, 'Shutdown Workload VMs', dry_run)

        # VM regex patterns to find and shutdown (Tanzu, K8s, etc.)
        # Order follows VCF docs: containerized workloads → Supervisor → TKG
        vm_patterns = [
            r'^kubernetes-cluster-.*$',  # TKGs clusters (worker nodes)
            r'^dev-project-.*$',  # vSphere with Tanzu projects
            r'^cci-service-.*$',  # CCI services
            r'^SupervisorControlPlaneVM.*$',  # Supervisor Control Plane VMs
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
            vcf_write(lsf, f'Searching for VMs matching {len(vm_patterns)} pattern(s)...')
            for pattern in vm_patterns:
                vcf_write(lsf, f'  Pattern: {pattern}')
                vms = get_vms_by_regex(lsf, pattern)
                if vms:
                    vcf_write(lsf, f'  Found {len(vms)} VM(s) matching pattern')
                    for vm in vms:
                        total_workload_vms += 1
                        vcf_write(lsf, f'  [{total_workload_vms}] Shutting down: {vm.name}')
                        shutdown_vm_gracefully(lsf, vm)
                        time.sleep(2)  # Brief pause between shutdowns
                else:
                    vcf_write(lsf, f'  No VMs found matching this pattern')

            # Shutdown static VM list
            if workload_vms:
                vcf_write(lsf, f'Processing {len(workload_vms)} static workload VM(s)...')
                for vm_name in workload_vms:
                    vcf_write(lsf, f'  Looking for VM: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            total_workload_vms += 1
                            vcf_write(lsf, f'  [{total_workload_vms}] Shutting down: {vm.name}')
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(2)
                    else:
                        vcf_write(lsf, f'  VM not found: {vm_name}')
            else:
                vcf_write(lsf, 'No static workload VMs configured')

            vcf_write(lsf, f'Workload VM shutdown complete: {total_workload_vms} VM(s) processed')
        else:
            vcf_write(lsf, f'Would shutdown VMs matching patterns: {vm_patterns}')
            vcf_write(lsf, f'Would shutdown VMs: {workload_vms}')

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
    
    if should_run('5'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 5: Shutdown Workload Domain NSX Edges')
        vcf_write(lsf, '='*60)
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
            vcf_write(lsf, f'Found {len(all_nsx_edges)} NSX Edge(s) in [VCF] vcfnsxedges')
        else:
            vcf_write(lsf, 'No NSX Edges configured in [VCF] vcfnsxedges')

        # Filter for workload domain edges (contain "wld" in name)
        workload_nsx_edges = [e for e in all_nsx_edges if 'wld' in e.lower()]

        if not dry_run:
            if workload_nsx_edges:
                vcf_write(lsf, f'Processing {len(workload_nsx_edges)} workload NSX Edge VM(s)...')
                for edge_name in workload_nsx_edges:
                    vcf_write(lsf, f'  Looking for: {edge_name}')
                    vms = lsf.get_vm_by_name(edge_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(2)
                    else:
                        vcf_write(lsf, f'    NSX Edge VM not found')
                vcf_write(lsf, 'Workload NSX Edge shutdown complete')
            else:
                vcf_write(lsf, 'No workload NSX Edges found (no edges with "wld" in name)')
        else:
            vcf_write(lsf, f'Would shutdown workload NSX Edges: {workload_nsx_edges}')

        #==========================================================================
        # TASK 6: Shutdown Workload Domain NSX Manager
        # Per VCF 9.0: NSX Manager shuts down after NSX Edges in workload domain
        # Filter: Only managers with "wld" in their name (workload domain)
    #==========================================================================
    
    if should_run('6'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 6: Shutdown Workload Domain NSX Manager')
        vcf_write(lsf, '='*60)
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
            vcf_write(lsf, f'Found {len(all_nsx_mgr)} NSX Manager(s) in [VCF] vcfnsxmgr')
        else:
            vcf_write(lsf, 'No NSX Managers configured in [VCF] vcfnsxmgr')

        # Filter for workload domain managers (contain "wld" in name)
        workload_nsx_mgr = [m for m in all_nsx_mgr if 'wld' in m.lower()]

        if not dry_run:
            if workload_nsx_mgr:
                vcf_write(lsf, f'Processing {len(workload_nsx_mgr)} workload NSX Manager VM(s)...')
                for mgr_name in workload_nsx_mgr:
                    vcf_write(lsf, f'  Looking for: {mgr_name}')
                    vms = lsf.get_vm_by_name(mgr_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    NSX Manager VM not found')
                vcf_write(lsf, 'Workload NSX Manager shutdown complete')
            else:
                vcf_write(lsf, 'No workload NSX Managers found (no managers with "wld" in name)')
        else:
            vcf_write(lsf, f'Would shutdown workload NSX Manager: {workload_nsx_mgr}')

        #==========================================================================
        # TASK 7: Shutdown Workload vCenters
        # Per VCF 9.0: vCenter is LAST in workload domain shutdown order (#8)
        # Note: In VCF 9.0, ESX hosts shutdown before vCenter for workload domains
        # For HOL, we keep vCenter up to manage the ESX shutdown, then shut it down
    #==========================================================================
    
    if should_run('7'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 7: Shutdown Workload vCenters')
        vcf_write(lsf, '='*60)
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
            vcf_write(lsf, f'Found {len(all_vcenters)} vCenter(s) in [VCF] vcfvCenter')
        else:
            vcf_write(lsf, 'No vCenters configured in [VCF] vcfvCenter')

        # Filter for workload vCenters (contain "wld" in name)
        workload_vcenters = [v for v in all_vcenters if 'wld' in v.lower()]

        if not dry_run:
            if workload_vcenters:
                vcf_write(lsf, f'Processing {len(workload_vcenters)} workload vCenter(s)...')
                vcf_write(lsf, '  (Per VCF 9.0: vCenter shuts down LAST in workload domain)')
                vc_count = 0
                for vc_name in workload_vcenters:
                    vc_count += 1
                    vcf_write(lsf, f'  [{vc_count}/{len(workload_vcenters)}] Looking for: {vc_name}')
                    vms = lsf.get_vm_by_name(vc_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(10)  # Longer pause for vCenter
                    else:
                        vcf_write(lsf, f'    vCenter VM not found (may not exist in this lab)')
                vcf_write(lsf, 'Workload vCenter shutdown complete')
            else:
                vcf_write(lsf, 'No workload vCenters configured - skipping')
        else:
            vcf_write(lsf, f'Would shutdown workload vCenters: {workload_vcenters}')

        #==========================================================================
        # VCF 9.0 MANAGEMENT DOMAIN SHUTDOWN
        # Per VCF 9.0 docs, management domain order is:
        # 1. VCF Automation (vra)
        # 2. VCF Operations for Networks (vrni)
        # 3. VCF Operations collector  
        # 4. VCF Operations for logs (vrli)
        # 5. VCF Identity Broker
        # 6. VCF Operations fleet management (VCF Operations Manager)
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
    
    if should_run('8'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 8: Shutdown VCF Operations for Networks')
        vcf_write(lsf, '='*60)
        update_shutdown_status(8, 'Shutdown VCF Ops for Networks', dry_run)

        # Default names cover both legacy (opsnet-*) and VCF 9.1 (ops_networks-*) naming
        vcf_ops_networks_vms = ['opsnet-a', 'opsnet-01a', 'opsnetcollector-01a']
        vcf_ops_networks_patterns = [r'^ops_networks-.*$']

        if lsf.config.has_option('SHUTDOWN', 'vcf_ops_networks_vms'):
            net_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_networks_vms')
            vcf_ops_networks_vms = [v.strip() for v in net_raw.split('\n') 
                                   if v.strip() and not v.strip().startswith('#')]
            vcf_ops_networks_patterns = []

        if not dry_run:
            found_count = 0

            # Exact name lookups
            if vcf_ops_networks_vms:
                vcf_write(lsf, f'Searching {len(vcf_ops_networks_vms)} VCF Ops for Networks VM name(s)...')
                found_count += shutdown_vms_by_names(lsf, vcf_ops_networks_vms, dry_run)

            # Regex pattern lookups (VCF 9.1 naming: ops_networks-*)
            if vcf_ops_networks_patterns:
                vcf_write(lsf, f'Searching VCF Ops for Networks patterns: {vcf_ops_networks_patterns}')
                for pattern in vcf_ops_networks_patterns:
                    vcf_write(lsf, f'  Pattern: {pattern}')
                    vms = lsf.get_vm_match(pattern)
                    if vms:
                        vcf_write(lsf, f'  Found {len(vms)} VM(s) matching pattern')
                        for vm in vms:
                            found_count += 1
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'  No VMs found matching pattern')

            vcf_write(lsf, f'VCF Operations for Networks shutdown complete ({found_count} VM(s) processed)')
        else:
            vcf_write(lsf, f'Would shutdown VCF Ops for Networks: {vcf_ops_networks_vms}')
            if vcf_ops_networks_patterns:
                vcf_write(lsf, f'Would also search patterns: {vcf_ops_networks_patterns}')

        #==========================================================================
        # TASK 9: Shutdown VCF Operations Collector
        # Per VCF 9.0 Management Domain order #3
    #==========================================================================
    
    if should_run('9'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 9: Shutdown VCF Operations Collector')
        vcf_write(lsf, '='*60)
        update_shutdown_status(9, 'Shutdown VCF Ops Collector', dry_run)

        vcf_ops_collector_vms = ['opscollector-01a']

        if lsf.config.has_option('SHUTDOWN', 'vcf_ops_collector_vms'):
            coll_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_collector_vms')
            vcf_ops_collector_vms = [v.strip() for v in coll_raw.split('\n') 
                                    if v.strip() and not v.strip().startswith('#')]

        if not dry_run:
            if vcf_ops_collector_vms:
                vcf_write(lsf, f'Processing {len(vcf_ops_collector_vms)} VCF Ops Collector VM(s)...')
                for vm_name in vcf_ops_collector_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    VM not found')
                vcf_write(lsf, 'VCF Operations Collector shutdown complete')
            else:
                vcf_write(lsf, 'No VCF Ops Collector VMs configured')
        else:
            vcf_write(lsf, f'Would shutdown VCF Ops Collector: {vcf_ops_collector_vms}')

        #==========================================================================
        # TASK 10: Shutdown VCF Operations for Logs (vrli)
        # Per VCF 9.0 Management Domain order #4
        # Note: In VCF 9.0, this is NOT late - it shuts down before Identity Broker
    #==========================================================================
    
    if should_run('10'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 10: Shutdown VCF Operations for Logs')
        vcf_write(lsf, '='*60)
        update_shutdown_status(10, 'Shutdown VCF Ops for Logs', dry_run)

        vcf_ops_logs_vms = []

        if lsf.config.has_option('SHUTDOWN', 'vcf_ops_logs_vms'):
            logs_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_logs_vms')
            vcf_ops_logs_vms = [v.strip() for v in logs_raw.split('\n') 
                              if v.strip() and not v.strip().startswith('#')]

        if not dry_run:
            if vcf_ops_logs_vms:
                vcf_write(lsf, f'Processing {len(vcf_ops_logs_vms)} VCF Ops for Logs VM(s)...')
                for vm_name in vcf_ops_logs_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    VM not found')
                vcf_write(lsf, 'VCF Operations for Logs shutdown complete')
            else:
                vcf_write(lsf, 'No VCF Ops for Logs VMs configured')
        else:
            vcf_write(lsf, f'Would shutdown VCF Ops for Logs: {vcf_ops_logs_vms}')

        #==========================================================================
        # TASK 11: Shutdown VCF Identity Broker
        # Per VCF 9.0 Management Domain order #5
    #==========================================================================
    
    if should_run('11'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 11: Shutdown VCF Identity Broker')
        vcf_write(lsf, '='*60)
        update_shutdown_status(11, 'Shutdown VCF Identity Broker', dry_run)

        vcf_identity_broker_vms = []  # Not present in all deployments

        if lsf.config.has_option('SHUTDOWN', 'vcf_identity_broker_vms'):
            ib_raw = lsf.config.get('SHUTDOWN', 'vcf_identity_broker_vms')
            vcf_identity_broker_vms = [v.strip() for v in ib_raw.split('\n') 
                                      if v.strip() and not v.strip().startswith('#')]

        if not dry_run:
            if vcf_identity_broker_vms:
                vcf_write(lsf, f'Processing {len(vcf_identity_broker_vms)} VCF Identity Broker VM(s)...')
                for vm_name in vcf_identity_broker_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    VM not found')
                vcf_write(lsf, 'VCF Identity Broker shutdown complete')
            else:
                vcf_write(lsf, 'No VCF Identity Broker VMs configured (may not be deployed)')
        else:
            vcf_write(lsf, f'Would shutdown VCF Identity Broker: {vcf_identity_broker_vms}')

        #==========================================================================
        # TASK 12: Shutdown VCF Operations Fleet Management (VCF Operations Manager)
        # Per VCF 9.0 Management Domain order #6
    #==========================================================================
    
    if should_run('12'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 12: Shutdown VCF Operations Fleet Management')
        vcf_write(lsf, '='*60)
        update_shutdown_status(12, 'Shutdown VCF Ops Fleet Mgmt', dry_run)

        vcf_ops_fleet_vms = []

        if lsf.config.has_option('SHUTDOWN', 'vcf_ops_fleet_vms'):
            fleet_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_fleet_vms')
            vcf_ops_fleet_vms = [v.strip() for v in fleet_raw.split('\n') 
                                if v.strip() and not v.strip().startswith('#')]

        if not dry_run:
            if vcf_ops_fleet_vms:
                vcf_write(lsf, f'Processing {len(vcf_ops_fleet_vms)} VCF Ops Fleet Management VM(s)...')
                for vm_name in vcf_ops_fleet_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    VM not found')
                vcf_write(lsf, 'VCF Operations Fleet Management shutdown complete')
            else:
                vcf_write(lsf, 'No VCF Ops Fleet Management VMs configured')
        else:
            vcf_write(lsf, f'Would shutdown VCF Ops Fleet Management: {vcf_ops_fleet_vms}')

        #==========================================================================
        # TASK 13: Shutdown VCF Operations (vrops)
        # Per VCF 9.0 Management Domain order #7
    #==========================================================================
    
    if should_run('13'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 13: Shutdown VCF Operations')
        vcf_write(lsf, '='*60)
        update_shutdown_status(13, 'Shutdown VCF Operations', dry_run)

        # Note: VCF Operations (vrops) may have been partially shut down via Fleet API in Phase 1
        # This phase ensures any remaining VMs are shut down
        vcf_ops_vms = ['ops-a']

        if lsf.config.has_option('SHUTDOWN', 'vcf_ops_vms'):
            ops_raw = lsf.config.get('SHUTDOWN', 'vcf_ops_vms')
            vcf_ops_vms = [v.strip() for v in ops_raw.split('\n') 
                          if v.strip() and not v.strip().startswith('#')]

        if not dry_run:
            if vcf_ops_vms:
                vcf_write(lsf, f'Processing {len(vcf_ops_vms)} VCF Operations VM(s)...')
                for vm_name in vcf_ops_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    VM not found')
                vcf_write(lsf, 'VCF Operations shutdown complete')
            else:
                vcf_write(lsf, 'No VCF Operations VMs configured')
        else:
            vcf_write(lsf, f'Would shutdown VCF Operations: {vcf_ops_vms}')

        #==========================================================================
        # TASK 14: Shutdown Management Domain NSX Edges
        # Per VCF 9.0 Management Domain order #9
        # Filter: Only edges with "mgmt" in their name (management domain)
    #==========================================================================
    
    if should_run('14'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 14: Shutdown Management NSX Edges')
        vcf_write(lsf, '='*60)
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
            vcf_write(lsf, f'Found {len(all_nsx_edges)} NSX Edge(s) in [VCF] vcfnsxedges')
        else:
            vcf_write(lsf, 'No NSX Edges configured in [VCF] vcfnsxedges')

        # Filter for management domain edges (contain "mgmt" in name)
        mgmt_nsx_edges = [e for e in all_nsx_edges if 'mgmt' in e.lower()]

        if not dry_run:
            if mgmt_nsx_edges:
                vcf_write(lsf, f'Processing {len(mgmt_nsx_edges)} Management NSX Edge VM(s)...')
                edge_count = 0
                for edge_name in mgmt_nsx_edges:
                    edge_count += 1
                    vcf_write(lsf, f'  [{edge_count}/{len(mgmt_nsx_edges)}] Looking for: {edge_name}')
                    vms = lsf.get_vm_by_name(edge_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(2)
                    else:
                        vcf_write(lsf, f'    Management NSX Edge VM not found')
                vcf_write(lsf, 'Management NSX Edge shutdown complete')
            else:
                vcf_write(lsf, 'No Management NSX Edges found (no edges with "mgmt" in name)')
        else:
            vcf_write(lsf, f'Would shutdown Management NSX Edges: {mgmt_nsx_edges}')

        #==========================================================================
        # TASK 15: Shutdown Management Domain NSX Manager
        # Per VCF 9.0 Management Domain order #10
        # Filter: Only managers with "mgmt" in their name (management domain)
    #==========================================================================
    
    if should_run('15'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 15: Shutdown Management NSX Manager')
        vcf_write(lsf, '='*60)
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
            vcf_write(lsf, f'Found {len(all_nsx_mgr)} NSX Manager(s) in [VCF] vcfnsxmgr')
        else:
            vcf_write(lsf, 'No NSX Managers configured in [VCF] vcfnsxmgr')

        # Filter for management domain managers (contain "mgmt" in name)
        mgmt_nsx_mgr = [m for m in all_nsx_mgr if 'mgmt' in m.lower()]

        if not dry_run:
            if mgmt_nsx_mgr:
                vcf_write(lsf, f'Processing {len(mgmt_nsx_mgr)} Management NSX Manager VM(s)...')
                mgr_count = 0
                for mgr_name in mgmt_nsx_mgr:
                    mgr_count += 1
                    vcf_write(lsf, f'  [{mgr_count}/{len(mgmt_nsx_mgr)}] Looking for: {mgr_name}')
                    vms = lsf.get_vm_by_name(mgr_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(5)
                    else:
                        vcf_write(lsf, f'    Management NSX Manager VM not found')
                vcf_write(lsf, 'Management NSX Manager shutdown complete')
            else:
                vcf_write(lsf, 'No Management NSX Managers found (no managers with "mgmt" in name)')
        else:
            vcf_write(lsf, f'Would shutdown Management NSX Manager: {mgmt_nsx_mgr}')

        #==========================================================================
        # TASK 16: Shutdown SDDC Manager
        # Per VCF 9.0 Management Domain order #11
    #==========================================================================
    
    if should_run('16'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 16: Shutdown SDDC Manager')
        vcf_write(lsf, '='*60)
        update_shutdown_status(16, 'Shutdown SDDC Manager', dry_run)

        sddc_manager_vms = ['sddcmanager-a']

        if lsf.config.has_option('SHUTDOWN', 'sddc_manager_vms'):
            sddc_raw = lsf.config.get('SHUTDOWN', 'sddc_manager_vms')
            sddc_manager_vms = [v.strip() for v in sddc_raw.split('\n') 
                              if v.strip() and not v.strip().startswith('#')]

        if not dry_run:
            if sddc_manager_vms:
                vcf_write(lsf, f'Processing {len(sddc_manager_vms)} SDDC Manager VM(s)...')
                for vm_name in sddc_manager_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(10)  # Longer pause for SDDC Manager
                    else:
                        vcf_write(lsf, f'    SDDC Manager VM not found')
                vcf_write(lsf, 'SDDC Manager shutdown complete')
            else:
                vcf_write(lsf, 'No SDDC Manager VMs configured')
        else:
            vcf_write(lsf, f'Would shutdown SDDC Manager: {sddc_manager_vms}')

        #==========================================================================
        # TASK 17: Shutdown Management vCenter
        # Per VCF 9.0 Management Domain order #12 (with vSAN and ESX hosts)
    #==========================================================================
    
    if should_run('17'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 17: Shutdown Management vCenter')
        vcf_write(lsf, '='*60)
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
            vcf_write(lsf, f'Found {len(all_vcenters_mgmt)} vCenter(s) in [VCF] vcfvCenter')
        else:
            vcf_write(lsf, 'No vCenters configured in [VCF] vcfvCenter')

        # Filter for management vCenters (contain "mgmt" in name)
        mgmt_vcenter_vms = [v for v in all_vcenters_mgmt if 'mgmt' in v.lower()]

        if not dry_run:
            if mgmt_vcenter_vms:
                vcf_write(lsf, f'Processing {len(mgmt_vcenter_vms)} Management vCenter VM(s)...')
                for vm_name in mgmt_vcenter_vms:
                    vcf_write(lsf, f'  Looking for: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if vms:
                        for vm in vms:
                            shutdown_vm_gracefully(lsf, vm)
                            time.sleep(10)  # Longer pause for vCenter
                    else:
                        vcf_write(lsf, f'    Management vCenter VM not found')
                vcf_write(lsf, 'Management vCenter shutdown complete')
            else:
                vcf_write(lsf, 'No Management vCenter VMs configured')
        else:
            vcf_write(lsf, f'Would shutdown Management vCenter: {mgmt_vcenter_vms}')

        #==========================================================================
        # TASK 17b: Reconnect to ESXi hosts directly
        # vCenters are now shut down, so we need direct ESXi connections for
        # the remaining phases (host settings, vSAN elevator, host shutdown).
    #==========================================================================
    
    if should_run('17b'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 17b: Connect to ESXi Hosts Directly')
        vcf_write(lsf, '='*60)
        update_shutdown_status(17, 'Connect to ESXi Hosts', dry_run)

        if not dry_run:
            vcf_write(lsf, 'Disconnecting vCenter sessions...')
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()

            if mgmt_hosts:
                vcf_write(lsf, f'Connecting directly to {len(mgmt_hosts)} ESXi host(s):')
                for host in mgmt_hosts:
                    vcf_write(lsf, f'  - {host}')
                lsf.connect_vcenters(mgmt_hosts)
                vcf_write(lsf, f'Connected to {len(lsf.sis)} ESXi endpoint(s)')
            else:
                vcf_write(lsf, 'No ESXi hosts configured for direct connection')

        #==========================================================================
        # TASK 18: Set Host Advanced Settings
    #==========================================================================
    
    if should_run('18'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 18: Set Host Advanced Settings')
        vcf_write(lsf, '='*60)
        update_shutdown_status(18, 'Host Advanced Settings', dry_run)

        vcf_write(lsf, f'Found {len(esx_hosts)} ESXi host(s) in [VCF] vcfmgmtcluster')

        if not dry_run:
            if esx_hosts:
                vcf_write(lsf, f'Configuring {len(esx_hosts)} ESXi host(s)...')
                host_count = 0
                for host in esx_hosts:
                    host_count += 1
                    vcf_write(lsf, f'  [{host_count}/{len(esx_hosts)}] Setting advanced settings on {host}')
                    try:
                        cmd = 'esxcli system settings advanced set -o /Mem/AllocGuestLargePage -i 1'
                        lsf.ssh(cmd, f'{esx_username}@{host}', password)
                        vcf_write(lsf, f'    Settings applied successfully')
                    except Exception as e:
                        vcf_write(lsf, f'    Warning: Failed to set advanced settings: {e}')
                vcf_write(lsf, 'Host advanced settings complete')
            else:
                vcf_write(lsf, 'No ESXi hosts configured - skipping')
        else:
            vcf_write(lsf, f'Would set advanced settings on: {esx_hosts}')

        #==========================================================================
        # TASK 19: vSAN Elevator Operations (OSA only - ESA does not use plog)
    #==========================================================================
    
    if should_run('19'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 19: vSAN Elevator Operations')
        vcf_write(lsf, '='*60)
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
                vcf_write(lsf, f'Invalid vsan_timeout value: {vsan_timeout_raw}, using default {VSAN_ELEVATOR_TIMEOUT}')
                vsan_timeout = VSAN_ELEVATOR_TIMEOUT

        vcf_write(lsf, f'vSAN enabled: {vsan_enabled}, ESXi hosts: {len(esx_hosts)}, Timeout: {vsan_timeout}s ({vsan_timeout/60:.0f}min)')

        if vsan_enabled and esx_hosts and not dry_run:
            # Check if this is vSAN ESA (Express Storage Architecture)
            # ESA does NOT use the plog mechanism, so elevator wait is not needed
            vcf_write(lsf, 'Checking vSAN architecture (OSA vs ESA)...')
            is_esa = False
            if esx_hosts:
                # Check the first host to determine architecture
                test_host = esx_hosts[0]
                is_esa = check_vsan_esa(lsf, test_host, esx_username, password)
                if is_esa:
                    vcf_write(lsf, f'  vSAN ESA detected on {test_host}')
                    vcf_write(lsf, '  ESA does not use plog - skipping 45-minute elevator wait')
                else:
                    vcf_write(lsf, f'  vSAN OSA detected on {test_host}')
                    vcf_write(lsf, '  OSA uses plog - elevator wait is required')

            if is_esa:
                # vSAN ESA - skip the elevator process entirely
                vcf_write(lsf, 'vSAN ESA: Elevator operations not required (no plog)')
            else:
                # vSAN OSA - run the full elevator process
                # Enable vSAN elevator on all hosts
                vcf_write(lsf, f'Enabling vSAN elevator on {len(esx_hosts)} host(s)...')
                host_count = 0
                for host in esx_hosts:
                    host_count += 1
                    vcf_write(lsf, f'  [{host_count}/{len(esx_hosts)}] Enabling elevator on {host}')
                    set_vsan_elevator(lsf, host, esx_username, password, enable=True)

                # Wait for vSAN I/O to complete
                vcf_write(lsf, f'Starting vSAN I/O flush wait ({vsan_timeout/60:.0f} minutes)...')
                vcf_write(lsf, '  This ensures all pending writes are committed to disk (OSA plog)')

                elapsed = 0
                while elapsed < vsan_timeout:
                    remaining = (vsan_timeout - elapsed) / 60
                    elapsed_min = elapsed / 60
                    vcf_write(lsf, f'  vSAN wait: {elapsed_min:.0f}m elapsed, {remaining:.1f}m remaining')
                    time.sleep(VSAN_ELEVATOR_CHECK_INTERVAL)
                    elapsed += VSAN_ELEVATOR_CHECK_INTERVAL

                vcf_write(lsf, 'vSAN I/O flush wait complete')

                # Disable vSAN elevator on all hosts
                vcf_write(lsf, f'Disabling vSAN elevator on {len(esx_hosts)} host(s)...')
                host_count = 0
                for host in esx_hosts:
                    host_count += 1
                    vcf_write(lsf, f'  [{host_count}/{len(esx_hosts)}] Disabling elevator on {host}')
                    set_vsan_elevator(lsf, host, esx_username, password, enable=False)
                vcf_write(lsf, 'vSAN elevator operations complete')
        elif dry_run:
            vcf_write(lsf, f'Would run vSAN elevator on: {esx_hosts}')
            vcf_write(lsf, f'vSAN timeout: {vsan_timeout} seconds')
            vcf_write(lsf, 'Note: Actual run will check for ESA and skip if detected')
        elif not vsan_enabled:
            vcf_write(lsf, 'vSAN elevator skipped (vsan_enabled=false in config)')
        else:
            vcf_write(lsf, 'vSAN elevator skipped (no ESXi hosts configured)')

        #==========================================================================
        # TASK 19b: Shutdown VSP Platform VMs
        # VSP VMs (vsp-01a-*) run the K8s management cluster. They should be
        # shut down gracefully after all K8s workloads have been scaled down
        # (Phase 2b) and after vCenter is off (Phase 17). We connect to the
        # ESXi hosts directly to find and shut down these VMs.
    #==========================================================================
    
    if should_run('19b'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 19b: Shutdown VSP Platform VMs')
        vcf_write(lsf, '='*60)
        update_shutdown_status(19, 'Shutdown VSP Platform VMs', dry_run)

        vsp_vm_patterns = []
        if lsf.config.has_option('VCF', 'vspvms'):
            vsp_raw = lsf.config.get('VCF', 'vspvms')
            for entry in vsp_raw.split('\n'):
                entry = entry.strip()
                if entry and not entry.startswith('#'):
                    vm_pattern = entry.split(':')[0].strip()
                    if vm_pattern:
                        vsp_vm_patterns.append(vm_pattern)

        if not dry_run:
            if vsp_vm_patterns:
                vcf_write(lsf, f'Searching for VSP VMs matching {len(vsp_vm_patterns)} pattern(s)...')
                vsp_shutdown_count = 0
                vsp_already_off = 0
                for pattern in vsp_vm_patterns:
                    vcf_write(lsf, f'  Pattern: {pattern}')
                    vms = lsf.get_vm_match(pattern)
                    if vms:
                        vcf_write(lsf, f'  Found {len(vms)} VM(s)')
                        for vm in vms:
                            if is_vm_powered_on(vm):
                                shutdown_vm_gracefully(lsf, vm)
                                vsp_shutdown_count += 1
                                time.sleep(5)
                            else:
                                vcf_write(lsf, f'  {vm.name}: Already powered off')
                                vsp_already_off += 1
                    else:
                        vcf_write(lsf, f'  No VMs found matching pattern')
                vcf_write(lsf, f'VSP VMs: {vsp_shutdown_count} shut down, {vsp_already_off} already off')
            else:
                vcf_write(lsf, 'No VSP VMs configured in [VCF] vspvms')
        else:
            vcf_write(lsf, f'Would shutdown VSP VMs: {vsp_vm_patterns}')

        #==========================================================================
        # TASK 19c: Pre-ESXi Shutdown Audit
        # Enumerate all VMs still powered on across all connected ESXi hosts.
        # Any VMs still running at this point were missed by earlier phases.
        # Attempt graceful shutdown of stragglers (except the console/router).
    #==========================================================================
    
    if should_run('19c'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 19c: Pre-ESXi Shutdown Audit')
        vcf_write(lsf, '='*60)
        update_shutdown_status(19, 'Pre-ESXi Shutdown Audit', dry_run)

        skip_vm_patterns = ['holconsole', 'holorouter', 'router', 'console', 'manager']

        if not dry_run:
            from pyVmomi import vim as pyvim
            still_on = []
            for si in lsf.sis:
                try:
                    content = si.RetrieveContent()
                    container = content.viewManager.CreateContainerView(
                        content.rootFolder, [pyvim.VirtualMachine], True)
                    for vm in container.view:
                        try:
                            if vm.runtime.powerState == pyvim.VirtualMachinePowerState.poweredOn:
                                still_on.append(vm)
                        except Exception:
                            pass
                    container.Destroy()
                except Exception as e:
                    vcf_write(lsf, f'  Error enumerating VMs on host: {e}')

            if still_on:
                vcf_write(lsf, f'Found {len(still_on)} VM(s) still powered on:')
                straggler_count = 0
                skipped_count = 0
                for vm in still_on:
                    vm_name_lower = vm.name.lower()
                    should_skip = any(pat in vm_name_lower for pat in skip_vm_patterns)
                    if should_skip:
                        vcf_write(lsf, f'  {vm.name}: Skipping (infrastructure VM)')
                        skipped_count += 1
                    else:
                        vcf_write(lsf, f'  {vm.name}: STRAGGLER - attempting graceful shutdown')
                        shutdown_vm_gracefully(lsf, vm)
                        straggler_count += 1
                        time.sleep(3)
                vcf_write(lsf, f'Audit complete: {straggler_count} stragglers shut down, {skipped_count} infrastructure VMs skipped')
            else:
                vcf_write(lsf, 'All VMs already powered off - clean state for ESXi shutdown')
        else:
            vcf_write(lsf, 'Would enumerate all powered-on VMs and shut down stragglers')

        #==========================================================================
        # TASK 20: Shutdown ESXi Hosts
        # Per VCF 9.0 Management Domain order #12 (with vSAN)
    #==========================================================================
    
    if should_run('20'):
        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'PHASE 20: Shutdown ESXi Hosts')
        vcf_write(lsf, '='*60)
        update_shutdown_status(20, 'Shutdown ESXi Hosts', dry_run)

        shutdown_hosts = True
        if lsf.config.has_option('SHUTDOWN', 'shutdown_hosts'):
            shutdown_hosts = lsf.config.getboolean('SHUTDOWN', 'shutdown_hosts')

        vcf_write(lsf, f'Host shutdown enabled: {shutdown_hosts}, ESXi hosts: {len(esx_hosts)}')

        if shutdown_hosts and esx_hosts and not dry_run:
            vcf_write(lsf, f'Shutting down {len(esx_hosts)} ESXi host(s)...')
            host_count = 0
            for host in esx_hosts:
                host_count += 1
                vcf_write(lsf, f'  [{host_count}/{len(esx_hosts)}] Initiating shutdown: {host}')
                shutdown_host(lsf, host, esx_username, password)
                time.sleep(5)  # Stagger host shutdowns
            vcf_write(lsf, 'ESXi host shutdown commands sent')
            vcf_write(lsf, '  Note: Hosts may take several minutes to fully power off')
        elif dry_run:
            vcf_write(lsf, f'Would shutdown hosts: {esx_hosts}')
        elif not shutdown_hosts:
            vcf_write(lsf, 'Host shutdown skipped (shutdown_hosts=false in config)')
        else:
            vcf_write(lsf, 'Host shutdown skipped (no ESXi hosts configured)')

        #==========================================================================
        # Cleanup
        #==========================================================================

        vcf_write(lsf, '='*60)
        vcf_write(lsf, 'SHUTDOWN COMPLETE')
        vcf_write(lsf, '='*60)

        if not dry_run:
            vcf_write(lsf, 'Disconnecting sessions...')
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

    if phase is None:
        vcf_write(lsf, f'{MODULE_NAME} completed')

    return {'success': True, 'esx_hosts': esx_hosts}


#==============================================================================
# STANDALONE EXECUTION
#==============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=MODULE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phase IDs for --phase:
  1     Fleet Operations (VCF Operations Suite) Shutdown
  1b    VCF Automation VM fallback (if Fleet API failed)
  2     Connect to vCenters
  2b    Scale Down VCF Component Services (K8s on VSP)
  3     Stop Workload Control Plane (WCP)
  4     Shutdown Workload VMs (Tanzu, K8s)
  5     Shutdown Workload Domain NSX Edges
  6     Shutdown Workload Domain NSX Manager
  7     Shutdown Workload vCenters
  8     Shutdown VCF Operations for Networks VMs
  9     Shutdown VCF Operations Collector VMs
  10    Shutdown VCF Operations for Logs VMs
  11    Shutdown VCF Identity Broker VMs
  12    Shutdown VCF Operations Fleet Management VMs
  13    Shutdown VCF Operations (vrops) VMs
  14    Shutdown Management Domain NSX Edges
  15    Shutdown Management Domain NSX Manager
  16    Shutdown SDDC Manager
  17    Shutdown Management vCenter
  17b   Connect to ESXi Hosts directly
  18    Set Host Advanced Settings
  19    vSAN Elevator Operations
  19b   Shutdown VSP Platform VMs
  19c   Pre-ESXi Shutdown Audit
  20    Shutdown ESXi Hosts

Examples:
  python3 VCFshutdown.py --dry-run              # Preview all phases
  python3 VCFshutdown.py --phase 1              # Run only Fleet Operations
  python3 VCFshutdown.py --phase 13 --dry-run   # Preview VCF Operations phase
"""
    )
    parser.add_argument('--standalone', action='store_true',
                        help='Run in standalone test mode')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--skip-init', action='store_true',
                        help='Skip lsf.init() call')
    parser.add_argument('--phase', '-p', type=str, default=None,
                        help='Run only a specific phase (e.g., 1, 1b, 13, 17b)')
    
    args = parser.parse_args()
    
    import lsfunctions as lsf
    
    if not args.skip_init:
        lsf.init(router=False)
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'Dry run: {args.dry_run}')
        if args.phase:
            print(f'Phase: {args.phase}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run,
         phase=args.phase)
