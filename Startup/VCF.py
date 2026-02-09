#!/usr/bin/env python3
# VCF.py - HOLFY27 Core VCF Startup Module
# Version 3.4 - February 2026
# Author - Burke Azbill and HOL Core Team
# VMware Cloud Foundation startup sequence
#
# v3.2 Changes:
# - NSX Edge/NSX Manager/vCenter power-on is now host-agnostic. Since VCF.py
#   is connected directly to all ESXi hosts (not vCenter), it searches across
#   all hosts for each VM by name and powers it on from whichever host it is
#   actually registered on. This eliminates FileNotFound / Device-busy errors
#   caused by vCenter state mismatches after unclean shutdown, and removes the
#   dependency on the config.ini host hint being correct (DRS may have moved VMs).
# - If any edge VM fails to power on, the lab FAILS immediately (lsf.labfail)
#   to prevent downstream cascading failures from missing NSX routing.
# v3.3 Changes:
# - Fix: After DRS/HA moves a VM, it can be registered on multiple ESXi hosts
#   simultaneously (stale registration on old host, active on new host). The
#   stale registration has its VMX file locked by the host actually running it,
#   causing FileNotFound / Device-busy on power-on. _start_vm_on_hosts now:
#   1. Finds ALL registrations across all hosts (not just the first one)
#   2. If any registration is poweredOn, returns immediately (already running)
#   3. Tries to power on each candidate; if FileNotFound/Device-busy (VMX
#      locked = stale registration), skips to the next host automatically
#   4. Re-checks state after all attempts in case the VM was running all along
# - If any edge VM fails to power on, the lab FAILS immediately (lsf.labfail)
#   to prevent downstream cascading failures from missing NSX routing.
# v3.4 Changes:
# - TASK 1 now fails the lab immediately (lsf.labfail) if ANY ESXi host
#   fails to connect after max retries. Previously it logged the failure
#   and continued, wasting 11+ minutes of retry time per unreachable host
#   in subsequent modules and causing cascading failures.
#   connect_vcenters() now returns a list of failed hosts.
#

import os
import sys
import argparse
import logging
import time

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(level=logging.WARNING)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VCF'
MODULE_DESCRIPTION = 'VMware Cloud Foundation startup'

def _start_vm_on_hosts(lsf, vm_name: str, fail_label: str = 'VM') -> str:
    """
    Find a VM by name across all connected ESXi hosts and ensure it is powered on.
    
    Since VCF.py connects directly to ESXi hosts (not vCenter), this function
    searches all host connections for the VM. After an unclean shutdown, a VM
    may be registered on multiple hosts simultaneously - a stale registration
    on its original host plus an active registration on the host DRS/HA moved
    it to. The stale registration will have the VMX file locked by the host
    that is actually running the VM, causing a FileNotFound / Device-busy error
    if we try to power it on from the wrong host.
    
    Strategy:
    1. Search ALL host connections for the VM by name (may return multiple)
    2. If ANY registration reports poweredOn, the VM is running - done
    3. If none are poweredOn, try to power on each one, starting with VMs in
       'connected' state. The first successful power-on wins.
    4. If a power-on fails with FileNotFound / Device-busy (VMX locked), skip
       that stale registration and try the next one - the lock means another
       host owns the VM.
    
    :param lsf: lsfunctions module
    :param vm_name: VM name to find and power on
    :param fail_label: Label for logging (e.g. 'NSX Edge', 'NSX Manager')
    :return: 'already_on' if already powered on, 'started' if successfully
             powered on, 'failed' if power on failed, 'not_found' if VM
             not found on any host
    """
    from pyVim.task import WaitForTask
    
    vms = lsf.get_vm_by_name(vm_name)
    if not vms:
        lsf.write_output(f'WARNING: {fail_label} VM not found on any host: {vm_name}')
        return 'not_found'
    
    # Log all registrations found
    for vm in vms:
        h = vm.runtime.host.name if vm.runtime.host else 'unknown'
        lsf.write_output(f'  {vm_name}: found on {h} (power={vm.runtime.powerState}, conn={vm.runtime.connectionState})')
    
    # Step 1: Check if ANY registration shows poweredOn
    for vm in vms:
        if vm.runtime.powerState == 'poweredOn':
            host_name = vm.runtime.host.name if vm.runtime.host else 'unknown'
            lsf.write_output(f'{vm_name} already powered on (host: {host_name})')
            return 'already_on'
    
    # Step 2: Sort candidates - prefer connected VMs first, then by host name
    # for deterministic ordering
    candidates = sorted(vms, key=lambda v: (
        0 if v.runtime.connectionState == 'connected' else 1,
        v.runtime.host.name if v.runtime.host else 'zzz'
    ))
    
    # Step 3: Try to power on each candidate until one succeeds
    last_error = None
    for vm in candidates:
        host_name = vm.runtime.host.name if vm.runtime.host else 'unknown'
        
        # Wait briefly for VM to reach connected state
        max_wait = 30
        waited = 0
        while vm.runtime.connectionState != 'connected' and waited < max_wait:
            lsf.write_output(f'  {vm_name} on {host_name}: connection state {vm.runtime.connectionState}, waiting...')
            time.sleep(5)
            waited += 5
        
        if vm.runtime.connectionState != 'connected':
            lsf.write_output(f'  {vm_name} on {host_name}: not connected after {max_wait}s, skipping')
            continue
        
        lsf.write_output(f'Powering on {vm_name} (host: {host_name})...')
        
        try:
            task = vm.PowerOnVM_Task()
            WaitForTask(task)
            lsf.write_output(f'Powered on {vm_name} (host: {host_name})')
            return 'started'
        except Exception as e:
            error_str = str(e)
            last_error = error_str
            
            # FileNotFound / Device busy = VMX locked by another host
            # This means the VM is actually running on a different host.
            # Skip this stale registration and try the next one.
            is_stale = ('FileNotFound' in error_str or
                        'Device or resource busy' in error_str or
                        'Unable to load configuration file' in error_str)
            
            if is_stale:
                lsf.write_output(f'  {vm_name} on {host_name}: VMX locked (stale registration), trying next host...')
                continue
            else:
                lsf.write_output(f'FAILED to power on {vm_name} on {host_name}: {e}')
                # Non-lock error is a real failure - still try remaining candidates
                continue
    
    # If we exhausted all candidates with FileNotFound/busy errors, the VM is
    # likely running on a host we lost the stale registration from (it was
    # cleaned up). Re-check all registrations for poweredOn - the reload from
    # the failed PowerOn may have refreshed state.
    lsf.write_output(f'  {vm_name}: all power-on attempts failed, re-checking state...')
    vms_recheck = lsf.get_vm_by_name(vm_name)
    for vm in vms_recheck:
        if vm.runtime.powerState == 'poweredOn':
            host_name = vm.runtime.host.name if vm.runtime.host else 'unknown'
            lsf.write_output(f'{vm_name} is now reporting poweredOn (host: {host_name})')
            return 'already_on'
    
    lsf.write_output(f'FAILED: {fail_label} {vm_name} could not be powered on from any host')
    if last_error:
        lsf.write_output(f'  Last error: {last_error[:200]}')
    return 'failed'


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for VCF module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    from pyVim import connect
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    # Verify VCF section exists
    if not lsf.config.has_section('VCF'):
        lsf.write_output('No VCF section in config.ini - skipping VCF startup')
        return
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        # Skip VVF group since we're running VCF
        dashboard.skip_group('vvf', 'VCF lab - VVF not applicable')
        dashboard.update_task('vcf', 'mgmt_cluster', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    lsf.write_vpodprogress('VCF Start', 'GOOD-3')
    
    #==========================================================================
    # TASK 1: Connect to VCF Management Cluster Hosts
    #==========================================================================
    
    vcfmgmtcluster = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        vcfmgmtcluster_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        vcfmgmtcluster = [h.strip() for h in vcfmgmtcluster_raw.split('\n') if h.strip()]
    
    hosts_connected = 0
    hosts_failed = 0
    hosts_exited_mm = 0
    hosts_mm_failed = 0
    
    if vcfmgmtcluster:
        lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3')
        total_hosts = len(vcfmgmtcluster)
        
        if not dry_run:
            failed_hosts = lsf.connect_vcenters(vcfmgmtcluster)
            hosts_connected = len(lsf.sis)  # Number of successful connections
            hosts_failed = len(failed_hosts) if failed_hosts else 0
            
            # If ANY host failed to connect, the lab must fail immediately.
            # All ESXi hosts are required for VCF - missing hosts means VMs
            # registered on that host cannot be managed, datastores may be
            # degraded, and vSAN quorum may be at risk.
            if hosts_failed > 0:
                fail_msg = f'{hosts_failed} ESXi host(s) unreachable: {", ".join(failed_hosts)}'
                lsf.write_output(f'FATAL: {fail_msg}')
                
                if dashboard:
                    dashboard.update_task('vcf', 'mgmt_cluster', TaskStatus.FAILED,
                                          fail_msg,
                                          total=total_hosts, success=hosts_connected,
                                          failed=hosts_failed)
                    dashboard.generate_html()
                
                lsf.labfail(fail_msg)
                return
            
            # Exit maintenance mode for each host
            if dashboard:
                dashboard.update_task('vcf', 'exit_maintenance', TaskStatus.RUNNING)
            
            for entry in vcfmgmtcluster:
                parts = entry.split(':')
                hostname = parts[0].strip()
                
                lsf.write_output(f'Checking host status: {hostname}')
                try:
                    host = lsf.get_host(hostname)
                    if host is None:
                        lsf.write_output(f'Could not find host: {hostname}')
                        hosts_mm_failed += 1
                        continue
                    
                    if host.runtime.inMaintenanceMode:
                        lsf.write_output(f'Removing {hostname} from Maintenance Mode')
                        host.ExitMaintenanceMode_Task(0)
                        hosts_exited_mm += 1
                        lsf.labstartup_sleep(lsf.sleep_seconds)
                    elif host.runtime.connectionState != 'connected':
                        lsf.write_output(f'Host {hostname} in error state: {host.runtime.connectionState}')
                        hosts_mm_failed += 1
                    else:
                        hosts_exited_mm += 1  # Already out of maintenance
                except Exception as e:
                    lsf.write_output(f'Error processing host {hostname}: {e}')
                    hosts_mm_failed += 1
            
            if dashboard:
                if hosts_mm_failed > 0:
                    dashboard.update_task('vcf', 'exit_maintenance', TaskStatus.FAILED,
                                          f'{hosts_mm_failed} host(s) failed to exit maintenance',
                                          total=total_hosts, success=hosts_exited_mm, failed=hosts_mm_failed)
                else:
                    dashboard.update_task('vcf', 'exit_maintenance', TaskStatus.COMPLETE,
                                          total=total_hosts, success=hosts_exited_mm, failed=0)
        else:
            lsf.write_output(f'Would connect to VCF hosts: {vcfmgmtcluster}')
            if dashboard:
                dashboard.update_task('vcf', 'exit_maintenance', TaskStatus.SKIPPED, 'Dry run mode',
                                      total=total_hosts, success=0, failed=0, skipped=total_hosts)
    else:
        if dashboard:
            dashboard.update_task('vcf', 'exit_maintenance', TaskStatus.SKIPPED, 
                                  'No VCF management cluster hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
    
    if dashboard:
        if vcfmgmtcluster:
            total_hosts = len(vcfmgmtcluster)
            if hosts_failed > 0:
                dashboard.update_task('vcf', 'mgmt_cluster', TaskStatus.FAILED,
                                      f'{hosts_failed} host(s) failed to connect',
                                      total=total_hosts, success=hosts_connected, failed=hosts_failed)
            else:
                dashboard.update_task('vcf', 'mgmt_cluster', TaskStatus.COMPLETE,
                                      total=total_hosts, success=hosts_connected, failed=0)
        else:
            dashboard.update_task('vcf', 'mgmt_cluster', TaskStatus.SKIPPED,
                                  'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('vcf', 'datastore', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 2: Check VCF Management Datastore
    #==========================================================================
    
    vcfmgmtdatastore = []
    if lsf.config.has_option('VCF', 'vcfmgmtdatastore'):
        vcfmgmtdatastore_raw = lsf.config.get('VCF', 'vcfmgmtdatastore')
        vcfmgmtdatastore = [d.strip() for d in vcfmgmtdatastore_raw.split('\n') if d.strip()]
    
    if vcfmgmtdatastore:
        lsf.write_vpodprogress('VCF Datastore check', 'GOOD-3')
        
        for datastore in vcfmgmtdatastore:
            if dry_run:
                lsf.write_output(f'Would check datastore: {datastore}')
                continue
            
            dsfailctr = 0
            dsfailmaxctr = 10
            
            while True:
                try:
                    lsf.write_output(f'Checking datastore: {datastore}')
                    ds = lsf.get_datastore(datastore)
                    
                    if ds is None:
                        lsf.write_output(f'Datastore not found: {datastore} - skipping')
                        break
                    
                    if ds.summary.accessible:
                        vms = ds.vm
                        if len(vms) == 0:
                            raise Exception(f'No VMs on datastore: {datastore}')
                        
                        # Check if VMs are connected
                        all_connected = True
                        for vm in vms:
                            if vm.runtime.connectionState != 'connected':
                                all_connected = False
                                lsf.write_output(f'VM {vm.config.name} not connected - waiting...')
                                lsf.labstartup_sleep(30)
                                break
                        
                        if all_connected:
                            lsf.write_output(f'Datastore {datastore} is available')
                            break
                    else:
                        lsf.write_output(f'Datastore {datastore} not accessible')
                        lsf.labstartup_sleep(30)
                
                except Exception as e:
                    dsfailctr += 1
                    lsf.write_output(f'Datastore check failed ({dsfailctr}/{dsfailmaxctr}): {e}')
                    
                    if dsfailctr >= dsfailmaxctr:
                        lsf.write_output(f'Datastore {datastore} failed to come online')
                        lsf.labfail(f'{datastore} DOWN')
                        return
                    
                    lsf.labstartup_sleep(30)
    
    if dashboard:
        if vcfmgmtdatastore:
            dashboard.update_task('vcf', 'datastore', TaskStatus.COMPLETE,
                                  total=len(vcfmgmtdatastore), success=len(vcfmgmtdatastore), failed=0)
        else:
            dashboard.update_task('vcf', 'datastore', TaskStatus.SKIPPED,
                                  'No datastores configured',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('vcf', 'nsx_mgr', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 3: Start NSX Manager
    # Uses host-agnostic approach - searches all connected ESXi hosts.
    #==========================================================================
    
    nsx_mgr_count = 0
    nsx_mgr_started = 0
    nsx_mgr_failed = 0
    
    if lsf.config.has_option('VCF', 'vcfnsxmgr'):
        vcfnsxmgr_raw = lsf.config.get('VCF', 'vcfnsxmgr')
        vcfnsxmgr = [n.strip() for n in vcfnsxmgr_raw.split('\n') if n.strip() and not n.strip().startswith('#')]
        nsx_mgr_count = len(vcfnsxmgr)
        
        if vcfnsxmgr:
            lsf.write_vpodprogress('VCF NSX Mgr start', 'GOOD-3')
            lsf.write_output('Starting VCF NSX Manager(s)...')
            
            if not dry_run:
                mgr_need_wait = False
                
                for entry in vcfnsxmgr:
                    mgr_name = entry.split(':')[0].strip()
                    
                    result = _start_vm_on_hosts(lsf, mgr_name, fail_label='NSX Manager')
                    
                    if result == 'already_on':
                        nsx_mgr_started += 1
                    elif result == 'started':
                        nsx_mgr_started += 1
                        mgr_need_wait = True
                    else:
                        lsf.write_output(f'WARNING: NSX Manager {mgr_name} failed to start ({result})')
                        nsx_mgr_failed += 1
                
                if mgr_need_wait:
                    lsf.write_output('Waiting 30 seconds for NSX Manager(s) to start...')
                    lsf.labstartup_sleep(30)
                else:
                    lsf.write_output('All NSX Manager VMs already powered on, skipping wait')
            else:
                lsf.write_output(f'Would start NSX Manager(s): {vcfnsxmgr}')
    else:
        lsf.write_output('No NSX Manager configured')
    
    if dashboard:
        if nsx_mgr_count > 0:
            if nsx_mgr_failed > 0:
                dashboard.update_task('vcf', 'nsx_mgr', TaskStatus.FAILED,
                                      f'{nsx_mgr_failed} manager(s) failed',
                                      total=nsx_mgr_count, success=nsx_mgr_started,
                                      failed=nsx_mgr_failed)
            else:
                dashboard.update_task('vcf', 'nsx_mgr', TaskStatus.COMPLETE,
                                      total=nsx_mgr_count, success=nsx_mgr_started, failed=0)
        else:
            dashboard.update_task('vcf', 'nsx_mgr', TaskStatus.SKIPPED,
                                  'No NSX Manager configured',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('vcf', 'nsx_edges', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 4: Start NSX Edges
    # Uses host-agnostic approach: since we're connected directly to all ESXi
    # hosts, _start_vm_on_hosts searches across all hosts for each VM by name
    # and powers it on from whichever host it is actually registered on.
    # This avoids FileNotFound / Device-busy errors from vCenter state
    # mismatches and does not depend on the config.ini host hint being correct.
    # If any edge VM fails to start, the lab FAILS immediately.
    #==========================================================================
    
    nsx_edges_count = 0
    nsx_edges_started = 0
    nsx_edges_failed = 0
    
    if lsf.config.has_option('VCF', 'vcfnsxedges'):
        vcfnsxedges_raw = lsf.config.get('VCF', 'vcfnsxedges')
        vcfnsxedges = [e.strip() for e in vcfnsxedges_raw.split('\n') if e.strip() and not e.strip().startswith('#')]
        nsx_edges_count = len(vcfnsxedges)
        
        if vcfnsxedges:
            lsf.write_vpodprogress('VCF NSX Edges start', 'GOOD-3')
            lsf.write_output('Starting VCF NSX Edges...')
            
            if not dry_run:
                edges_need_wait = False
                
                for entry in vcfnsxedges:
                    edge_name = entry.split(':')[0].strip()
                    
                    result = _start_vm_on_hosts(lsf, edge_name, fail_label='NSX Edge')
                    
                    if result == 'already_on':
                        nsx_edges_started += 1
                    elif result == 'started':
                        nsx_edges_started += 1
                        edges_need_wait = True
                    else:
                        # 'failed' or 'not_found' - NSX Edge is critical
                        nsx_edges_failed += 1
                        
                        if dashboard:
                            dashboard.update_task('vcf', 'nsx_edges', TaskStatus.FAILED,
                                                  f'FATAL: {edge_name} failed to start',
                                                  total=nsx_edges_count,
                                                  success=nsx_edges_started,
                                                  failed=nsx_edges_failed)
                            dashboard.generate_html()
                        
                        lsf.labfail(f'NSX Edge {edge_name} failed to start ({result})')
                        return  # labfail calls sys.exit, but just in case
                
                if edges_need_wait:
                    lsf.write_output('Waiting 5 minutes for NSX Edges to start...')
                    lsf.labstartup_sleep(300)
                else:
                    lsf.write_output('All NSX Edge VMs already powered on, skipping wait')
            else:
                lsf.write_output(f'Would start NSX Edges: {vcfnsxedges}')
    
    if dashboard:
        if nsx_edges_count > 0:
            if nsx_edges_failed > 0:
                dashboard.update_task('vcf', 'nsx_edges', TaskStatus.FAILED,
                                      f'{nsx_edges_failed} edge(s) failed',
                                      total=nsx_edges_count,
                                      success=nsx_edges_started,
                                      failed=nsx_edges_failed)
            else:
                dashboard.update_task('vcf', 'nsx_edges', TaskStatus.COMPLETE,
                                      total=nsx_edges_count,
                                      success=nsx_edges_started,
                                      failed=0)
        else:
            dashboard.update_task('vcf', 'nsx_edges', TaskStatus.SKIPPED,
                                  'No NSX Edges configured',
                                  total=0, success=0, failed=0, skipped=0)
    
    #==========================================================================
    # TASK 4b: Start Post-Edge VMs (e.g., VCF Automation appliances)
    #==========================================================================
    # These VMs need to boot after NSX Edges are up but before vCenter
    # to allow maximum boot time. VCF Automation (auto-a) is a typical
    # example that benefits from early boot.
    
    if lsf.config.has_option('VCF', 'vcfpostedgevms'):
        vcfpostedgevms_raw = lsf.config.get('VCF', 'vcfpostedgevms')
        vcfpostedgevms = [v.strip() for v in vcfpostedgevms_raw.split('\n') 
                         if v.strip() and not v.strip().startswith('#')]
        
        if vcfpostedgevms:
            lsf.write_vpodprogress('VCF Post-Edge VMs start', 'GOOD-3')
            lsf.write_output('Starting post-edge VMs (VCF Automation, etc.)...')
            
            if not dry_run:
                postedge_need_wait = False
                
                for entry in vcfpostedgevms:
                    vm_name = entry.split(':')[0].strip()
                    
                    result = _start_vm_on_hosts(lsf, vm_name, fail_label='Post-Edge VM')
                    
                    if result == 'started':
                        postedge_need_wait = True
                    elif result in ('failed', 'not_found'):
                        lsf.write_output(f'WARNING: Post-edge VM {vm_name} - {result} (non-fatal, continuing)')
                
                if postedge_need_wait:
                    # Short wait - these VMs will continue booting in parallel
                    # with subsequent startup tasks
                    lsf.write_output('Post-edge VMs started, continuing with startup...')
                    lsf.labstartup_sleep(30)
                else:
                    lsf.write_output('All post-edge VMs already powered on')
            else:
                lsf.write_output(f'Would start post-edge VMs: {vcfpostedgevms}')
    
    if dashboard:
        dashboard.update_task('vcf', 'vcenter', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: Start VCF vCenter
    # Uses host-agnostic approach - searches all connected ESXi hosts.
    #==========================================================================
    
    vcenter_count = 0
    vcenter_started = 0
    vcenter_failed = 0
    
    if lsf.config.has_option('VCF', 'vcfvCenter'):
        vcfvCenter_raw = lsf.config.get('VCF', 'vcfvCenter')
        vcfvCenter = [v.strip() for v in vcfvCenter_raw.split('\n') if v.strip() and not v.strip().startswith('#')]
        vcenter_count = len(vcfvCenter)
        
        if vcfvCenter:
            lsf.write_vpodprogress('VCF vCenter start', 'GOOD-3')
            lsf.write_output('Starting VCF vCenter(s)...')
            
            if not dry_run:
                for entry in vcfvCenter:
                    vc_name = entry.split(':')[0].strip()
                    
                    result = _start_vm_on_hosts(lsf, vc_name, fail_label='vCenter')
                    
                    if result in ('already_on', 'started'):
                        vcenter_started += 1
                    else:
                        lsf.write_output(f'WARNING: vCenter {vc_name} failed to start ({result})')
                        vcenter_failed += 1
            else:
                lsf.write_output(f'Would start vCenter: {vcfvCenter}')
    
    if dashboard:
        if vcenter_count > 0:
            if vcenter_failed > 0:
                dashboard.update_task('vcf', 'vcenter', TaskStatus.FAILED,
                                      f'{vcenter_failed} vCenter(s) failed',
                                      total=vcenter_count, success=vcenter_started,
                                      failed=vcenter_failed)
            else:
                dashboard.update_task('vcf', 'vcenter', TaskStatus.COMPLETE,
                                      total=vcenter_count, success=vcenter_started, failed=0)
        else:
            dashboard.update_task('vcf', 'vcenter', TaskStatus.SKIPPED,
                                  'No vCenter configured',
                                  total=0, success=0, failed=0, skipped=0)
    
    #==========================================================================
    # Cleanup
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Disconnecting VCF hosts...')
        for si in lsf.sis:
            try:
                connect.Disconnect(si)
            except Exception:
                pass
        # Clear the session lists so subsequent modules start fresh
        lsf.sis.clear()
        lsf.sisvc.clear()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom VCF configuration or checks here
    # See prelim.py for detailed examples of common operations
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
    lsf.write_vpodprogress('VCF Finished', 'GOOD-3')
    lsf.write_output(f'{MODULE_NAME} completed')


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
    parser.add_argument('run_seconds', nargs='?', type=int, default=0,
                        help='Seconds already elapsed (for labstartup integration)')
    parser.add_argument('labcheck', nargs='?', default='False',
                        help='Whether this is a labcheck run')
    
    args = parser.parse_args()
    
    import lsfunctions as lsf
    
    if not args.skip_init:
        lsf.init(router=False)
    
    # Handle legacy arguments
    if args.run_seconds > 0:
        import datetime
        lsf.start_time = datetime.datetime.now() - datetime.timedelta(seconds=args.run_seconds)
    
    if args.labcheck == 'True':
        lsf.labcheck = True
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'Dry run: {args.dry_run}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run)
