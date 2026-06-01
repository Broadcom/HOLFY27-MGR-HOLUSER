#!/usr/bin/env python3
# VVF.py - HOLFY27 Core VVF Startup Module
# Version 4.0 - 2026-06-01
# Author - Burke Azbill and HOL Core Team
# VMware Validated Foundation startup sequence
#
# v4.0 Changes (2026-06-01):
# - Task 4b: Added vvfpostedgevms support (license server power-on via ESXi
#   direct before vCenter). License VMs MUST be running before vCenter starts.
# - Adopted _start_vm_on_hosts() helper from VCF.py for host-agnostic VM
#   power-on with stale-registration handling and retry logic.
# - All config reads converted to lsf.get_config_list() for consistent
#   comment-line filtering.
# - Exit maintenance mode promoted to a separate dashboard task (exit_maintenance)
#   matching VCF.py quality and granularity.
# - Host connection failure now calls lsf.labfail() immediately (matches VCF.py
#   v3.4 behavior: missing ESXi hosts = lab fail, not silent continue).
# - Dashboard skip_group improved: VCF and VCFfinal are both skipped for VVF labs.

import os
import sys
import argparse
import logging
import time

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(
    level=logging.WARNING,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VVF'
MODULE_DESCRIPTION = 'VMware Validated Foundation startup'


def _start_vm_on_hosts(lsf, vm_name: str, fail_label: str = 'VM') -> str:
    """
    Find a VM by name across all connected ESXi hosts and ensure it is powered on.

    Identical strategy to VCF.py _start_vm_on_hosts:
    1. Search ALL host connections for the VM (retries for slow registration)
    2. If ANY registration reports poweredOn, the VM is running — done
    3. Sort candidates (connected first), try each until power-on succeeds
    4. FileNotFound/Device-busy = stale registration on wrong host — skip
    5. Re-check all registrations if all attempts exhausted

    :param lsf: lsfunctions module
    :param vm_name: VM name to find and power on
    :param fail_label: Label for logging (e.g. 'License VM', 'vCenter')
    :return: 'already_on' | 'started' | 'failed' | 'not_found'
    """
    from pyVim.task import WaitForTask

    VM_FIND_MAX_RETRIES = 8
    VM_FIND_RETRY_DELAY = 30

    vms = []
    for find_attempt in range(1, VM_FIND_MAX_RETRIES + 1):
        candidates = lsf.get_vm_by_name(vm_name)
        if not candidates:
            prefix_pattern = f'^{vm_name}(-|$)'
            prefix_matches = lsf.get_vm_match(prefix_pattern)
            if prefix_matches:
                actual_name = prefix_matches[0].name
                lsf.write_output(f'{fail_label} exact name "{vm_name}" not found, '
                                 f'prefix match found: "{actual_name}"')
                candidates = prefix_matches

        if candidates:
            vms = candidates
            break

        if find_attempt < VM_FIND_MAX_RETRIES:
            lsf.write_output(f'WARNING: {fail_label} VM "{vm_name}" not found on any host '
                             f'(attempt {find_attempt}/{VM_FIND_MAX_RETRIES}), '
                             f'retrying in {VM_FIND_RETRY_DELAY}s...')
            time.sleep(VM_FIND_RETRY_DELAY)
        else:
            lsf.write_output(f'WARNING: {fail_label} VM not found on any host after '
                             f'{VM_FIND_MAX_RETRIES} attempts: {vm_name}')
            return 'not_found'

    for vm in vms:
        h = vm.runtime.host.name if vm.runtime.host else 'unknown'
        lsf.write_output(f'  {vm_name}: found on {h} '
                         f'(power={vm.runtime.powerState}, conn={vm.runtime.connectionState})')

    for vm in vms:
        if vm.runtime.powerState == 'poweredOn':
            host_name = vm.runtime.host.name if vm.runtime.host else 'unknown'
            lsf.write_output(f'{vm_name} already powered on (host: {host_name})')
            return 'already_on'

    candidates = sorted(vms, key=lambda v: (
        0 if v.runtime.connectionState == 'connected' else 1,
        v.runtime.host.name if v.runtime.host else 'zzz'
    ))

    last_error = None
    for vm in candidates:
        host_name = vm.runtime.host.name if vm.runtime.host else 'unknown'

        max_wait = 30
        waited = 0
        while vm.runtime.connectionState != 'connected' and waited < max_wait:
            lsf.write_output(f'  {vm_name} on {host_name}: '
                             f'state={vm.runtime.connectionState}, waiting...')
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
            is_stale = ('FileNotFound' in error_str or
                        'Device or resource busy' in error_str or
                        'Unable to load configuration file' in error_str)
            if is_stale:
                lsf.write_output(f'  {vm_name} on {host_name}: VMX locked (stale), trying next...')
                continue
            else:
                lsf.write_output(f'FAILED to power on {vm_name} on {host_name}: {e}')
                continue

    lsf.write_output(f'  {vm_name}: all attempts failed, re-checking state...')
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
    Main entry point for VVF module

    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    from pyVim import connect

    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)

    # Verify VVF section exists
    if not lsf.config.has_section('VVF'):
        lsf.write_output('No VVF section in config.ini - skipping VVF startup')
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
        # Skip VCF and VCFfinal groups — not applicable for VVF labs
        dashboard.skip_group('vcf', 'VVF lab - VCF not applicable')
        dashboard.skip_group('vcffinal', 'VVF lab - VCF Final not applicable')
        dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None

    lsf.write_vpodprogress('VVF Start', 'GOOD-3')

    #==========================================================================
    # TASK 1: Connect to VVF Management Cluster Hosts
    #==========================================================================

    vvfmgmtcluster = lsf.get_config_list('VVF', 'vvfmgmtcluster')

    hosts_connected = 0
    hosts_failed = 0
    hosts_exited_mm = 0
    hosts_mm_failed = 0

    if vvfmgmtcluster:
        lsf.write_vpodprogress('VVF Hosts Connect', 'GOOD-3')
        total_hosts = len(vvfmgmtcluster)

        if not dry_run:
            failed_hosts = lsf.connect_vcenters(vvfmgmtcluster)
            hosts_connected = len(lsf.sis)
            hosts_failed = len(failed_hosts) if failed_hosts else 0

            if hosts_failed > 0:
                fail_msg = (f'{hosts_failed} ESXi host(s) unreachable: '
                            f'{", ".join(failed_hosts)}')
                lsf.write_output(f'FATAL: {fail_msg}')

                if dashboard:
                    dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.FAILED,
                                          fail_msg,
                                          total=total_hosts, success=hosts_connected,
                                          failed=hosts_failed)
                    dashboard.generate_html()

                lsf.labfail(fail_msg)
                return

            # Exit maintenance mode (separate dashboard task)
            if dashboard:
                dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.COMPLETE,
                                      total=total_hosts, success=hosts_connected, failed=0)
                dashboard.update_task('vvf', 'exit_maintenance', TaskStatus.RUNNING)
                dashboard.generate_html()

            for entry in vvfmgmtcluster:
                hostname = entry.split(':')[0].strip()
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
                        lsf.write_output(f'Host {hostname} in error state: '
                                         f'{host.runtime.connectionState}')
                        hosts_mm_failed += 1
                    else:
                        hosts_exited_mm += 1
                except Exception as e:
                    lsf.write_output(f'Error processing host {hostname}: {e}')
                    hosts_mm_failed += 1

            if dashboard:
                if hosts_mm_failed > 0:
                    dashboard.update_task('vvf', 'exit_maintenance', TaskStatus.FAILED,
                                          f'{hosts_mm_failed} host(s) failed to exit maintenance',
                                          total=total_hosts, success=hosts_exited_mm,
                                          failed=hosts_mm_failed)
                else:
                    dashboard.update_task('vvf', 'exit_maintenance', TaskStatus.COMPLETE,
                                          total=total_hosts, success=hosts_exited_mm, failed=0)
                dashboard.generate_html()
        else:
            lsf.write_output(f'Would connect to VVF hosts: {vvfmgmtcluster}')
            if dashboard:
                dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.SKIPPED, 'Dry run mode',
                                      total=len(vvfmgmtcluster), success=0, failed=0,
                                      skipped=len(vvfmgmtcluster))
                dashboard.update_task('vvf', 'exit_maintenance', TaskStatus.SKIPPED, 'Dry run mode')
                dashboard.generate_html()
    else:
        lsf.write_output('No VVF management cluster hosts configured')
        if dashboard:
            dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.SKIPPED,
                                  'No hosts configured')
            dashboard.update_task('vvf', 'exit_maintenance', TaskStatus.SKIPPED,
                                  'No hosts configured')
            dashboard.generate_html()

    #==========================================================================
    # TASK 2: Check VVF Management Datastore
    #==========================================================================

    if dashboard:
        dashboard.update_task('vvf', 'datastore', TaskStatus.RUNNING)
        dashboard.generate_html()

    vvfmgmtdatastore = lsf.get_config_list('VVF', 'vvfmgmtdatastore')

    if vvfmgmtdatastore:
        lsf.write_vpodprogress('VVF Datastore check', 'GOOD-3')

        for datastore in vvfmgmtdatastore:
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

                        all_connected = True
                        for vm in vms:
                            if vm.runtime.connectionState != 'connected':
                                all_connected = False
                                lsf.write_output(
                                    f'VM {vm.config.name} not connected - waiting...')
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
                    lsf.write_output(
                        f'Datastore check failed ({dsfailctr}/{dsfailmaxctr}): {e}')

                    if dsfailctr >= dsfailmaxctr:
                        lsf.write_output(f'Datastore {datastore} failed to come online')
                        lsf.labfail(f'{datastore} DOWN')
                        return

                    lsf.labstartup_sleep(30)

    if dashboard:
        if vvfmgmtdatastore:
            dashboard.update_task('vvf', 'datastore', TaskStatus.COMPLETE,
                                  total=len(vvfmgmtdatastore),
                                  success=len(vvfmgmtdatastore), failed=0)
        else:
            dashboard.update_task('vvf', 'datastore', TaskStatus.SKIPPED,
                                  'No datastores configured')
        dashboard.generate_html()

    #==========================================================================
    # TASK 3: NSX Manager (not applicable for VVF — task skipped)
    #==========================================================================

    if dashboard:
        dashboard.update_task('vvf', 'nsx_mgr', TaskStatus.SKIPPED,
                              'VVF lab — NSX Manager not deployed')
        dashboard.generate_html()

    #==========================================================================
    # TASK 4: NSX Edges (not applicable for VVF — task skipped)
    #==========================================================================

    if dashboard:
        dashboard.update_task('vvf', 'nsx_edges', TaskStatus.SKIPPED,
                              'VVF lab — NSX Edges not deployed')
        dashboard.generate_html()

    #==========================================================================
    # TASK 4b: Start Post-Edge VMs (License Servers)
    # License VMs MUST be powered on before vCenter. Power operations use
    # the existing ESXi direct connections from Task 1.
    #==========================================================================

    vvfpostedgevms = lsf.get_config_list('VVF', 'vvfpostedgevms')

    if vvfpostedgevms:
        lsf.write_vpodprogress('VVF License VMs start', 'GOOD-3')
        lsf.write_output('Starting VVF license server VMs (must precede vCenter)...')

        if not dry_run:
            postedge_started = 0
            postedge_need_wait = False

            for entry in vvfpostedgevms:
                vm_name = entry.split(':')[0].strip()

                result = _start_vm_on_hosts(lsf, vm_name, fail_label='License VM')

                if result == 'already_on':
                    postedge_started += 1
                elif result == 'started':
                    postedge_started += 1
                    postedge_need_wait = True
                else:
                    lsf.write_output(
                        f'WARNING: License VM {vm_name} - {result} (non-fatal, continuing)')

            if postedge_need_wait:
                lsf.write_output('License VMs started, waiting 30s before vCenter...')
                lsf.labstartup_sleep(30)
            else:
                lsf.write_output('All license VMs already powered on')
        else:
            lsf.write_output(f'Would start license VMs: {vvfpostedgevms}')

    #==========================================================================
    # TASK 5: Start VVF vCenter
    # Uses host-agnostic approach — searches all connected ESXi hosts.
    #==========================================================================

    if dashboard:
        dashboard.update_task('vvf', 'vcenter', TaskStatus.RUNNING)
        dashboard.generate_html()

    vcenter_count = 0
    vcenter_started = 0
    vcenter_failed = 0

    vvfvCenter = lsf.get_config_list('VVF', 'vvfvCenter')
    vcenter_count = len(vvfvCenter)

    if vvfvCenter:
        lsf.write_vpodprogress('VVF vCenter start', 'GOOD-3')
        lsf.write_output('Starting VVF management vCenter(s)...')

        if not dry_run:
            for entry in vvfvCenter:
                vc_name = entry.split(':')[0].strip()

                result = _start_vm_on_hosts(lsf, vc_name, fail_label='vCenter')

                if result in ('already_on', 'started'):
                    vcenter_started += 1
                else:
                    lsf.write_output(f'WARNING: vCenter {vc_name} failed to start ({result})')
                    vcenter_failed += 1
        else:
            lsf.write_output(f'Would start vCenter: {vvfvCenter}')

    if dashboard:
        if vcenter_count > 0:
            if vcenter_failed > 0:
                dashboard.update_task('vvf', 'vcenter', TaskStatus.FAILED,
                                      f'{vcenter_failed} vCenter(s) failed',
                                      total=vcenter_count, success=vcenter_started,
                                      failed=vcenter_failed)
            else:
                dashboard.update_task('vvf', 'vcenter', TaskStatus.COMPLETE,
                                      total=vcenter_count, success=vcenter_started, failed=0)
        else:
            dashboard.update_task('vvf', 'vcenter', TaskStatus.SKIPPED,
                                  'No vCenter configured')
        dashboard.generate_html()

    #==========================================================================
    # Cleanup
    #==========================================================================

    if not dry_run:
        lsf.write_output('Disconnecting VVF hosts...')
        for si in lsf.sis:
            try:
                connect.Disconnect(si)
            except Exception:
                pass
        # Clear session lists so subsequent modules start fresh
        lsf.sis.clear()
        lsf.sisvc.clear()

    ##=========================================================================
    ## End Core Team code
    ##=========================================================================

    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================

    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================

    lsf.write_vpodprogress('VVF Finished', 'GOOD-3')
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
