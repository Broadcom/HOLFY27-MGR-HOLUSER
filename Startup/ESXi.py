#!/usr/bin/env python3
# ESXi.py - HOLFY27 Core ESXi Host Verification Module
# Version 3.7 - 2026-08-13
# Author - Burke Azbill and HOL Core Team
# Verifies ESXi hosts are online and responsive
# v3.7: Combine vCPU count reduction and CPU topology changes into a single ConfigSpec reconfig task for auto-platform-a* VMs to prevent InvalidArgument (numCoresPerSocket) faults.
# v3.6: HOL-2701 specific - cap auto-platform-a* (VCF Automation) VM at 24 vCPUs.
# v3.5: Roll back vsp-01.* CPU limit of 8; preserve existing CPU counts and update CPU topology to 8, 4, 2, or 1 cores per socket (whichever divides numCPU evenly).
# v3.4: Added auto-platform-a* CPU topology (8 cores per socket) enforcement. Applied directly on ESXi
#       hosts before power-on.
# v3.3: Removed vsp* VM minimum resource (12 CPU / 24 GB RAM) enforcement.
#       VSP control-plane/worker sizing is now owned exclusively by the
#       BenS vsp-remediate.sh toolkit (VSP CP target: 4 vCPU / 10240 MiB),
#       which conflicted with this module's 12-vCPU floor.
# v3.2: Gate vsp* VM resource enforcement behind vsp-min-core-enforced = true config setting
# v3.1: Enforce minimum resource requirements for vsp* VMs (12 CPU / 24 GB RAM)

import os
import sys
import argparse
import logging

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

MODULE_NAME = 'ESXi'
MODULE_DESCRIPTION = 'ESXi host verification'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for ESXi module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('esxi', 'host_check', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # Get ESXi Hosts from Config
    #==========================================================================
    
    # Use get_config_list to properly filter commented-out values
    esx_hosts = lsf.get_config_list('RESOURCES', 'ESXiHosts')
    
    if not esx_hosts:
        lsf.write_output('No ESXi hosts configured in config.ini')
        if dashboard:
            dashboard.update_task('esxi', 'host_check', TaskStatus.SKIPPED, 'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
            dashboard.update_task('esxi', 'host_ports', TaskStatus.SKIPPED, 'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
        return
    
    lsf.write_output(f'ESXi hosts to check: {len(esx_hosts)}')
    lsf.write_vpodprogress('Checking ESXi hosts', 'GOOD-3')
    
    #==========================================================================
    # Check Each ESXi Host
    #==========================================================================
    
    failed_hosts = []
    successful_hosts = []
    maintenance_mode_hosts = []
    
    for entry in esx_hosts:
        # Parse host:maintenance_mode format
        if ':' in entry:
            parts = entry.split(':')
            host = parts[0].strip()
            mm_flag = parts[1].strip().lower() if len(parts) > 1 else 'no'
        else:
            host = entry.strip()
            mm_flag = 'no'
        
        if mm_flag == 'yes':
            maintenance_mode_hosts.append(host)
            lsf.write_output(f'Host {host} configured to stay in maintenance mode')
        
        if dry_run:
            lsf.write_output(f'Would check ESXi host: {host}')
            continue
        
        lsf.write_output(f'Checking ESXi host: {host}')
        
        # Retry logic with timeout awareness
        max_retries = 10
        retry_delay = 30
        success = False
        
        for attempt in range(max_retries):
            if lsf.test_ping(host):
                lsf.write_output(f'ESXi host responding: {host}')
                success = True
                successful_hosts.append(host)
                break
            else:
                lsf.write_output(f'ESXi host not responding (attempt {attempt + 1}/{max_retries}): {host}')
                lsf.labstartup_sleep(retry_delay)
        
        if not success:
            lsf.write_output(f'FAIL: ESXi host not responding after {max_retries} attempts: {host}')
            failed_hosts.append(host)
            
            # For HOL labs, fail on host timeout
            if lsf.labtype == 'HOL':
                lsf.write_vpodprogress(f'{host} TIMEOUT', 'TIMEOUT')
                if dashboard:
                    dashboard.update_task('esxi', 'host_check', TaskStatus.FAILED, f'{host} not responding')
                    dashboard.generate_html()
                return
    
    #==========================================================================
    # Report Results
    #==========================================================================
    
    if not dry_run:
        lsf.write_output(f'ESXi check results: {len(successful_hosts)} OK, {len(failed_hosts)} failed')
        
        if maintenance_mode_hosts:
            lsf.write_output(f'Hosts staying in maintenance mode: {maintenance_mode_hosts}')
    
    if dashboard:
        total_hosts = len(esx_hosts)
        if failed_hosts:
            dashboard.update_task('esxi', 'host_check', TaskStatus.FAILED, 
                                  f'{len(failed_hosts)} host(s) not responding',
                                  total=total_hosts, success=len(successful_hosts), failed=len(failed_hosts))
        else:
            dashboard.update_task('esxi', 'host_check', TaskStatus.COMPLETE,
                                  total=total_hosts, success=len(successful_hosts), failed=0)
    
    #==========================================================================
    # Check ESXi Management Ports
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('esxi', 'host_ports', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    if not esx_hosts:
        if dashboard:
            dashboard.update_task('esxi', 'host_ports', TaskStatus.SKIPPED, 'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
    elif failed_hosts:
        # Skip port checks if hosts failed connectivity
        if dashboard:
            dashboard.update_task('esxi', 'host_ports', TaskStatus.SKIPPED, 
                                  'Skipped due to host connectivity failures',
                                  total=len(esx_hosts), success=0, failed=0, skipped=len(esx_hosts))
    else:
        lsf.write_output('Checking ESXi management ports (443, 902)...')
        
        port_failed_hosts = []
        port_successful_hosts = []
        port_failure_details = {}  # Track which ports failed for each host
        
        # Port check configuration
        ports_to_check = [443, 902]  # HTTPS/vSphere Client, vSphere Management
        max_port_retries = 10
        port_retry_delay = 30
        
        for entry in esx_hosts:
            # Parse host:maintenance_mode format
            if ':' in entry:
                parts = entry.split(':')
                host = parts[0].strip()
            else:
                host = entry.strip()
            
            if dry_run:
                lsf.write_output(f'Would check ports on: {host}')
                continue
            
            lsf.write_output(f'Checking management ports on {host}...')
            
            # Check each port with retry logic
            failed_ports = []
            for port in ports_to_check:
                port_ok = False
                
                for attempt in range(max_port_retries):
                    if lsf.test_tcp_port(host, port, timeout=10):
                        port_ok = True
                        break
                    else:
                        if attempt < max_port_retries - 1:
                            lsf.write_output(f'  Port {port} not responding (attempt {attempt + 1}/{max_port_retries}), retrying...')
                            lsf.labstartup_sleep(port_retry_delay)
                
                if not port_ok:
                    failed_ports.append(str(port))
                    lsf.write_output(f'  Port {port} FAILED after {max_port_retries} attempts')
                else:
                    lsf.write_output(f'  Port {port} OK')
            
            # Determine overall result for this host
            if not failed_ports:
                lsf.write_output(f'ESXi management ports OK: {host} (all ports responding)')
                port_successful_hosts.append(host)
            else:
                port_failure_details[host] = failed_ports
                lsf.write_output(f'ESXi port check FAILED: {host} (ports {", ".join(failed_ports)} not responding after {max_port_retries} attempts)')
                port_failed_hosts.append(host)
        
        # Report port check results
        if not dry_run:
            lsf.write_output(f'Port check results: {len(port_successful_hosts)} OK, {len(port_failed_hosts)} failed')
            if port_failed_hosts:
                lsf.write_output('Failed hosts and ports:')
                for host in port_failed_hosts:
                    lsf.write_output(f'  {host}: ports {", ".join(port_failure_details[host])}')
        
        if dashboard:
            if port_failed_hosts:
                # Create detailed message showing which hosts and ports failed
                failure_summary = '; '.join([f'{host} (ports {", ".join(port_failure_details[host])})' 
                                             for host in port_failed_hosts])
                dashboard.update_task('esxi', 'host_ports', TaskStatus.FAILED,
                                      f'{len(port_failed_hosts)} host(s) failed: {failure_summary}')
            else:
                dashboard.update_task('esxi', 'host_ports', TaskStatus.COMPLETE,
                                      f'{len(port_successful_hosts)} hosts OK')
            dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================

    ##=========================================================================
    ## CUSTOM - VCF Automation (auto-platform-a*) & VSP Platform (vsp-01.*)
    ##          CPU topology enforcement
    ##
    ## 1. For each ESXi host, find any VM whose name starts with 'auto-platform-a'
    ##    and ensure its CPU topology has 8 cores per socket (numCoresPerSocket = 8).
    ##
    ## 2. For each ESXi host, find any VM whose name starts with 'vsp-01'
    ##    and update its CPU topology to 8, 4, 2, or 1 cores per socket (whichever
    ##    first results in an equal number of cores across sockets for numCPU).
    ##    CPU counts (numCPU) are left unchanged.
    ##
    ## Reconfiguration is only applied to powered-off VMs so running VMs
    ## are never disrupted. Hosts with no matching VMs are logged/skipped.
    ## Host connections are retried up to 5 times with 30s delays.
    ##=========================================================================

    if dry_run:
        lsf.write_output('Dry-run: skipping auto-platform-a* and vsp-01.* CPU topology enforcement')
    elif not esx_hosts:
        lsf.write_output('No ESXi hosts configured - skipping auto-platform-a* and vsp-01.* CPU checks')
    else:
        TARGET_CORES_PER_SOCKET = 8
        MAX_HOST_RETRIES = 5
        HOST_RETRY_DELAY = 30

        lsf.write_output('Checking ESXi host VMs for CPU topology (auto-platform-a* and vsp-01.*)...')
        try:
            import re
            import ssl as _ssl_vcfa
            from pyVmomi import vim
            from pyVim import connect as _vcfa_connect
            from pyVim.task import WaitForTask

            _vcfa_password = lsf.get_password()
            _vcfa_ctx = _ssl_vcfa._create_unverified_context()

            hosts_checked       = 0
            vcfa_vms_found      = 0
            vcfa_vms_updated    = 0
            vcfa_vms_skipped_on = 0

            vsp_vms_found       = 0
            vsp_vms_updated     = 0
            vsp_vms_skipped_on  = 0

            for _entry in esx_hosts:
                _host = _entry.split(':')[0].strip() if ':' in _entry else _entry.strip()

                for _attempt in range(MAX_HOST_RETRIES):
                    try:
                        _si = _vcfa_connect.SmartConnect(
                            host=_host,
                            user='root',
                            pwd=_vcfa_password,
                            sslContext=_vcfa_ctx
                        )
                        _content = _si.RetrieveContent()
                        _container = _content.viewManager.CreateContainerView(
                            _content.rootFolder, [vim.VirtualMachine], True
                        )

                        _all_vms = list(_container.view)
                        _vcfa_vms = [
                            vm for vm in _all_vms
                            if re.match(r'^auto-platform-a', vm.name, re.IGNORECASE)
                        ]
                        _vsp_vms = [
                            vm for vm in _all_vms
                            if re.match(r'^vsp-01', vm.name, re.IGNORECASE)
                        ]
                        _container.Destroy()

                        if not _vcfa_vms and not _vsp_vms:
                            lsf.write_output(f'  {_host}: no auto-platform-a* or vsp-01.* VMs found')
                            _vcfa_connect.Disconnect(_si)
                            hosts_checked += 1
                            break

                        _host_vcfa_found = len(_vcfa_vms)
                        _host_vcfa_updated = 0
                        _host_vcfa_skipped = 0

                        _host_vsp_found = len(_vsp_vms)
                        _host_vsp_updated = 0
                        _host_vsp_skipped = 0

                        # --- Process auto-platform-a* VMs ---
                        if _vcfa_vms:
                            lsf.write_output(f'  {_host}: found {len(_vcfa_vms)} auto-platform-a* VM(s)')
                            for _vm in _vcfa_vms:
                                _hw = _vm.config.hardware if (_vm.config and _vm.config.hardware) else None
                                _cur_cores = _hw.numCoresPerSocket if _hw else None
                                _cur_num_cpu = _hw.numCPU if _hw else None
                                _power = _vm.runtime.powerState

                                lsf.write_output(
                                    f'    {_vm.name}: numCPU={_cur_num_cpu}, '
                                    f'numCoresPerSocket={_cur_cores}, state={_power}'
                                )

                                # Determine target vCPU count
                                _target_num_cpu = _cur_num_cpu
                                if lsf.lab_sku == 'HOL-2701' and _cur_num_cpu and _cur_num_cpu > 24:
                                    _target_num_cpu = 24

                                # Determine target cores per socket in order: 8, 4, 2, 1
                                _target_cores = 8
                                if _target_num_cpu:
                                    for _c in [8, 4, 2, 1]:
                                        if _target_num_cpu >= _c and _target_num_cpu % _c == 0:
                                            _target_cores = _c
                                            break

                                if _cur_num_cpu == _target_num_cpu and _cur_cores == _target_cores:
                                    lsf.write_output(
                                        f'    {_vm.name}: numCPU={_cur_num_cpu}, numCoresPerSocket={_cur_cores} - no changes needed'
                                    )
                                    continue

                                if _power == 'poweredOn':
                                    lsf.write_output(
                                        f'    {_vm.name}: WARNING - VM is powered on; '
                                        f'reconfiguration skipped to avoid disruption'
                                    )
                                    _host_vcfa_skipped += 1
                                    continue

                                # Powered off — safe to reconfigure vCPU count and topology in a single spec
                                _spec = vim.vm.ConfigSpec()
                                _reconfig_msg_parts = []
                                if _cur_num_cpu != _target_num_cpu:
                                    _spec.numCPUs = _target_num_cpu
                                    _reconfig_msg_parts.append(f'numCPU: {_cur_num_cpu} -> {_target_num_cpu}')
                                if _cur_cores != _target_cores:
                                    _spec.numCoresPerSocket = _target_cores
                                    _reconfig_msg_parts.append(f'numCoresPerSocket: {_cur_cores} -> {_target_cores}')

                                _reconfig_msg = ', '.join(_reconfig_msg_parts)
                                lsf.write_output(f'    {_vm.name}: reconfiguring CPU ({_reconfig_msg})')
                                try:
                                    _task = _vm.ReconfigVM_Task(spec=_spec)
                                    WaitForTask(_task)
                                    lsf.write_output(f'    {_vm.name}: CPU reconfiguration complete')
                                    _host_vcfa_updated += 1
                                except Exception as _reconfig_err:
                                    lsf.write_output(
                                        f'    {_vm.name}: CPU reconfiguration FAILED: {_reconfig_err}'
                                    )

                        # --- Process vsp-01.* VMs ---
                        if _vsp_vms:
                            lsf.write_output(f'  {_host}: found {len(_vsp_vms)} vsp-01.* VM(s)')
                            for _vm in _vsp_vms:
                                _hw = _vm.config.hardware if (_vm.config and _vm.config.hardware) else None
                                _cur_cores = _hw.numCoresPerSocket if _hw else None
                                _cur_num_cpu = _hw.numCPU if _hw else None
                                _power = _vm.runtime.powerState

                                lsf.write_output(
                                    f'    {_vm.name}: numCPU={_cur_num_cpu}, '
                                    f'numCoresPerSocket={_cur_cores}, state={_power}'
                                )

                                # Determine target cores per socket in order: 8, 4, 2, 1
                                _target_cores = 1
                                if _cur_num_cpu:
                                    for _c in [8, 4, 2, 1]:
                                        if _cur_num_cpu >= _c and _cur_num_cpu % _c == 0:
                                            _target_cores = _c
                                            break

                                if _cur_cores == _target_cores:
                                    lsf.write_output(
                                        f'    {_vm.name}: CPU topology already set to {_target_cores} cores per socket - no changes needed'
                                    )
                                    continue

                                if _power == 'poweredOn':
                                    lsf.write_output(
                                        f'    {_vm.name}: WARNING - VM is powered on; '
                                        f'CPU topology reconfiguration skipped to avoid disruption'
                                    )
                                    _host_vsp_skipped += 1
                                    continue

                                # Powered off — safe to reconfigure CPU topology
                                _spec = vim.vm.ConfigSpec()
                                _spec.numCoresPerSocket = _target_cores

                                lsf.write_output(
                                    f'    {_vm.name}: reconfiguring CPU topology '
                                    f'(numCoresPerSocket: {_cur_cores} -> {_target_cores})'
                                )
                                try:
                                    _task = _vm.ReconfigVM_Task(spec=_spec)
                                    WaitForTask(_task)
                                    lsf.write_output(f'    {_vm.name}: CPU topology reconfiguration complete')
                                    _host_vsp_updated += 1
                                except Exception as _reconfig_err:
                                    lsf.write_output(
                                        f'    {_vm.name}: CPU topology reconfiguration FAILED: {_reconfig_err}'
                                    )

                        _vcfa_connect.Disconnect(_si)

                        hosts_checked += 1
                        vcfa_vms_found += _host_vcfa_found
                        vcfa_vms_updated += _host_vcfa_updated
                        vcfa_vms_skipped_on += _host_vcfa_skipped

                        vsp_vms_found += _host_vsp_found
                        vsp_vms_updated += _host_vsp_updated
                        vsp_vms_skipped_on += _host_vsp_skipped
                        break

                    except Exception as _host_err:
                        if _attempt < MAX_HOST_RETRIES - 1:
                            lsf.write_output(
                                f'  WARNING: could not connect to {_host} for VM CPU check '
                                f'(attempt {_attempt + 1}/{MAX_HOST_RETRIES}): {_host_err}. Retrying in {HOST_RETRY_DELAY}s...'
                            )
                            lsf.labstartup_sleep(HOST_RETRY_DELAY)
                        else:
                            lsf.write_output(
                                f'  WARNING: could not connect to {_host} for VM CPU check after {MAX_HOST_RETRIES} attempts: {_host_err}'
                            )

            _summary_parts = [
                f'{hosts_checked} host(s) checked',
                f'{vcfa_vms_found} auto-platform-a* VM(s) found ({vcfa_vms_updated} updated)',
                f'{vsp_vms_found} vsp-01.* VM(s) found ({vsp_vms_updated} updated)',
            ]
            if vcfa_vms_skipped_on or vsp_vms_skipped_on:
                _summary_parts.append(
                    f'{vcfa_vms_skipped_on + vsp_vms_skipped_on} skipped (powered on)'
                )
            lsf.write_output('ESXi VM CPU check complete: ' + ', '.join(_summary_parts))

        except Exception as _global_err:
            lsf.write_output(
                f'WARNING: ESXi VM CPU check encountered an error: {_global_err}'
            )

    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================

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
    
    # Handle legacy arguments from labstartup.py
    if args.run_seconds > 0:
        import datetime
        lsf.start_time = datetime.datetime.now() - datetime.timedelta(seconds=args.run_seconds)
    
    if args.labcheck == 'True':
        lsf.labcheck = True
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'LabType: {lsf.labtype}')
        print(f'Dry run: {args.dry_run}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run)
