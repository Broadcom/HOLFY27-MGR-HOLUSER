#!/usr/bin/env python3
# vSphere.py - HOLFY27 Core vSphere Startup Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# vSphere infrastructure startup sequence

import os
import sys
import argparse
import logging

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(level=logging.WARNING)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'vSphere'
MODULE_DESCRIPTION = 'vSphere infrastructure startup'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for vSphere module
    
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
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('vsphere', 'vcenter_connect', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Connect to vCenters
    #==========================================================================
    
    # Maximum time to wait for vCenter to become available (10 minutes)
    VCENTER_WAIT_TIMEOUT = 600  # seconds
    VCENTER_CHECK_INTERVAL = 20  # seconds between checks
    
    vcenters = []
    if lsf.config.has_option('RESOURCES', 'vCenters'):
        vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
        if '\n' in vcenters_raw:
            vcenters = [v.strip() for v in vcenters_raw.split('\n') if v.strip()]
        else:
            vcenters = [v.strip() for v in vcenters_raw.split(',') if v.strip()]
    
    if not vcenters:
        lsf.write_output('No vCenters configured - skipping vSphere startup')
        if dashboard:
            dashboard.update_task('vsphere', 'vcenter_wait', TaskStatus.SKIPPED, 'No vCenters configured')
            dashboard.generate_html()
        return
    
    # Update dashboard - waiting for vCenter
    if dashboard:
        dashboard.update_task('vsphere', 'vcenter_wait', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    lsf.write_vpodprogress('Connecting vCenters', 'GOOD-3')
    
    if not dry_run:
        import time
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Wait for each vCenter to become available before attempting connection
        for entry in vcenters:
            if not entry or entry.strip().startswith('#'):
                continue
            
            # Extract hostname from entry (format: hostname:type:user)
            vc_hostname = entry.split(':')[0].strip()
            
            lsf.write_output(f'Waiting for vCenter {vc_hostname} to become available (max {VCENTER_WAIT_TIMEOUT // 60} minutes)...')
            
            start_wait = time.time()
            vcenter_available = False
            
            while (time.time() - start_wait) < VCENTER_WAIT_TIMEOUT:
                # Check if vCenter port 443 is responding
                if lsf.test_tcp_port(vc_hostname, 443, timeout=10):
                    # Verify vCenter is responding via multiple endpoints
                    # Some endpoints may respond before others during startup
                    try:
                        session = requests.Session()
                        session.trust_env = False  # Ignore proxy environment vars
                        
                        # Try the API endpoint first
                        api_url = f'https://{vc_hostname}/api'
                        api_response = None
                        try:
                            api_response = session.get(api_url, verify=False, timeout=10, proxies=None)
                        except requests.exceptions.RequestException as e:
                            lsf.write_output(f'  API endpoint check failed: {e}')
                        
                        # Also try the UI endpoint as a fallback
                        ui_url = f'https://{vc_hostname}/ui/'
                        ui_response = None
                        try:
                            ui_response = session.get(ui_url, verify=False, timeout=10, proxies=None)
                        except requests.exceptions.RequestException as e:
                            lsf.write_output(f'  UI endpoint check failed: {e}')
                        
                        # Consider vCenter available if either endpoint responds
                        api_ok = api_response and api_response.status_code in [200, 401, 403]
                        ui_ok = ui_response and ui_response.status_code == 200
                        
                        if api_ok or ui_ok:
                            vcenter_available = True
                            elapsed = int(time.time() - start_wait)
                            which_endpoint = 'API' if api_ok else 'UI'
                            lsf.write_output(f'vCenter {vc_hostname} is available after {elapsed} seconds (detected via {which_endpoint} endpoint)')
                            break
                        else:
                            # Log what we got for debugging
                            api_status = api_response.status_code if api_response else 'no response'
                            ui_status = ui_response.status_code if ui_response else 'no response'
                            lsf.write_output(f'  Endpoint status - API: {api_status}, UI: {ui_status}')
                    except Exception as e:
                        lsf.write_output(f'  vCenter check error: {e}')
                
                elapsed = int(time.time() - start_wait)
                remaining = VCENTER_WAIT_TIMEOUT - elapsed
                lsf.write_output(f'vCenter {vc_hostname} not ready yet, waiting... ({remaining}s remaining)')
                time.sleep(VCENTER_CHECK_INTERVAL)
            
            if not vcenter_available:
                lsf.labfail(f'vCenter {vc_hostname} did not become available within {VCENTER_WAIT_TIMEOUT // 60} minutes')
        
        # Now connect to all vCenters
        lsf.connect_vcenters(vcenters)
    else:
        lsf.write_output(f'Would connect to vCenters: {vcenters}')
    
    if dashboard:
        vc_count = len([v for v in vcenters if v and not v.strip().startswith('#')])
        dashboard.update_task('vsphere', 'vcenter_wait', TaskStatus.COMPLETE,
                              total=vc_count, success=vc_count, failed=0)
        dashboard.update_task('vsphere', 'vcenter_connect', TaskStatus.COMPLETE,
                              total=vc_count, success=len(lsf.sis) if not dry_run else vc_count, failed=0)
        dashboard.update_task('vsphere', 'datastores', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 2: Check Datastores
    #==========================================================================
    
    datastores = []
    if lsf.config.has_option('RESOURCES', 'Datastores'):
        datastores_raw = lsf.config.get('RESOURCES', 'Datastores')
        if '\n' in datastores_raw:
            datastores = [d.strip() for d in datastores_raw.split('\n') if d.strip()]
        else:
            datastores = [d.strip() for d in datastores_raw.split(',') if d.strip()]
    
    if datastores:
        lsf.write_vpodprogress('Checking Datastores', 'GOOD-3')
        lsf.write_output('Checking Datastores')
        
        for entry in datastores:
            if dry_run:
                lsf.write_output(f'Would check datastore: {entry}')
                continue
            
            while True:
                try:
                    if lsf.check_datastore(entry):
                        break
                except Exception as e:
                    lsf.write_output(f'Datastore check error: {e}')
                lsf.labstartup_sleep(lsf.sleep_seconds)
    
    if dashboard:
        if datastores:
            dashboard.update_task('vsphere', 'datastores', TaskStatus.COMPLETE,
                                  total=len(datastores), success=len(datastores), failed=0)
        else:
            dashboard.update_task('vsphere', 'datastores', TaskStatus.SKIPPED,
                                  'No datastores configured',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('vsphere', 'maintenance', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 3: ESXi Hosts Exit Maintenance Mode
    #==========================================================================
    
    # Parse ESXi hosts to determine which should stay in maintenance mode
    esx_hosts = []
    if lsf.config.has_option('RESOURCES', 'ESXiHosts'):
        esx_hosts_raw = lsf.config.get('RESOURCES', 'ESXiHosts')
        if '\n' in esx_hosts_raw:
            esx_hosts = [h.strip() for h in esx_hosts_raw.split('\n') if h.strip()]
        else:
            esx_hosts = [h.strip() for h in esx_hosts_raw.split(',') if h.strip()]
    
    # Build list of hosts to keep in maintenance mode
    for entry in esx_hosts:
        if ':' in entry:
            parts = entry.split(':')
            host = parts[0].strip()
            mm_flag = parts[1].strip().lower() if len(parts) > 1 else 'no'
            if mm_flag == 'yes':
                lsf.mm += f'{host}:'
    
    if not dry_run:
        while not lsf.check_maintenance():
            lsf.write_vpodprogress('Exit Maintenance', 'GOOD-3')
            lsf.write_output('Taking ESXi hosts out of Maintenance Mode...')
            lsf.exit_maintenance()
            lsf.labstartup_sleep(lsf.sleep_seconds)
        
        lsf.write_output('All ESXi hosts are out of Maintenance Mode')
    
    if dashboard:
        host_count = len(esx_hosts) if esx_hosts else len(lsf.get_all_hosts()) if not dry_run else 0
        dashboard.update_task('vsphere', 'maintenance', TaskStatus.COMPLETE,
                              total=host_count, success=host_count, failed=0)
        dashboard.update_task('vsphere', 'drs', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 4: Wait for DRS to Enable
    #==========================================================================
    
    clusters = []
    if lsf.config.has_option('RESOURCES', 'Clusters'):
        clusters_raw = lsf.config.get('RESOURCES', 'Clusters')
        if '\n' in clusters_raw:
            clusters = [c.strip() for c in clusters_raw.split('\n') if c.strip()]
        else:
            clusters = [c.strip() for c in clusters_raw.split(',') if c.strip()]
    
    drs_clusters = []
    for entry in clusters:
        if ':' in entry:
            parts = entry.split(':')
            cluster_name = parts[0].strip()
            drs_flag = parts[1].strip().lower() if len(parts) > 1 else 'off'
            if drs_flag == 'on':
                drs_clusters.append(cluster_name)
    
    if drs_clusters and not dry_run:
        all_clusters = lsf.get_all_clusters()
        for cluster in all_clusters:
            if cluster.name in drs_clusters:
                while not cluster.configuration.drsConfig.enabled:
                    lsf.write_output(f'Waiting for DRS on {cluster.name}...')
                    lsf.labstartup_sleep(lsf.sleep_seconds)
        
        lsf.write_output('DRS is configured on all required clusters')
    
    if dashboard:
        if drs_clusters:
            dashboard.update_task('vsphere', 'drs', TaskStatus.COMPLETE,
                                  total=len(drs_clusters), success=len(drs_clusters), failed=0)
        else:
            dashboard.update_task('vsphere', 'drs', TaskStatus.SKIPPED,
                                  'No DRS clusters configured',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('vsphere', 'shell_warning', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 5: Suppress Shell Warning on ESXi Hosts
    #==========================================================================
    
    shell_warning_count = 0
    shell_warning_failed = 0
    if not dry_run:
        esxhosts = lsf.get_all_hosts()
        for host in esxhosts:
            try:
                option_manager = host.configManager.advancedOption
                option = vim.option.OptionValue(
                    key='UserVars.SuppressShellWarning',
                    value=1
                )
                lsf.write_output(f'Suppressing shell warning on {host.name}')
                option_manager.UpdateOptions(changedValue=[option])
                shell_warning_count += 1
            except Exception as e:
                lsf.write_output(f'Could not suppress shell warning on {host.name}: {e}')
                shell_warning_failed += 1
    
    if dashboard:
        total_shell = shell_warning_count + shell_warning_failed
        if total_shell > 0:
            dashboard.update_task('vsphere', 'shell_warning', TaskStatus.COMPLETE,
                                  total=total_shell, success=shell_warning_count, failed=shell_warning_failed)
        else:
            dashboard.update_task('vsphere', 'shell_warning', TaskStatus.SKIPPED,
                                  'No hosts to configure',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('vsphere', 'vcenter_ready', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 6: Verify vCenter UI Ready
    #==========================================================================
    
    if not dry_run:
        lsf.write_vpodprogress('Checking vCenter', 'GOOD-3')
        lsf.write_output('Checking vCenter readiness...')
        
        vc_urls = []
        for entry in vcenters:
            vc = entry.split(':')[0]
            vc_urls.append(f'https://{vc}/ui/')
        
        for url in vc_urls:
            while not lsf.test_url(url, expected_text='loading-container', timeout=5):
                lsf.write_output(f'Waiting for vCenter UI: {url}')
                lsf.labstartup_sleep(lsf.sleep_seconds)
    
    if dashboard:
        vc_ready_count = len([v for v in vcenters if v and not v.strip().startswith('#')])
        dashboard.update_task('vsphere', 'vcenter_ready', TaskStatus.COMPLETE,
                              total=vc_ready_count, success=vc_ready_count, failed=0)
        dashboard.update_task('vsphere', 'autostart_services', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 7: Verify all Autostart vCenter services are Started
    # Some services configured for AUTOMATIC startup fail to start during
    # vCenter boot (e.g. vapi-endpoint, trustmanagement). Check each vCenter
    # and start any AUTOMATIC services that are not in STARTED state.
    #==========================================================================
    
    autostart_total = 0
    autostart_started = 0
    autostart_fixed = 0
    autostart_failed = 0
    
    if not dry_run:
        import time as _time
        
        lsf.write_output('Verifying vCenter autostart services...')
        
        AUTOSTART_START_TIMEOUT = 60  # seconds to wait for a service to start
        AUTOSTART_CHECK_INTERVAL = 10  # seconds between status checks
        
        for entry in vcenters:
            if not entry or entry.strip().startswith('#'):
                continue
            
            vc_hostname = entry.split(':')[0].strip()
            lsf.write_output(f'Checking autostart services on {vc_hostname}...')
            
            # Query all AUTOMATIC services and their RunState via vmon-cli
            # Use run_command with single-quoted SSH command to avoid
            # double-quote conflicts in the lsf.ssh() wrapper
            ssh_opts = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
            check_cmd = (
                f"{lsf.sshpass} -p {lsf.password} ssh {ssh_opts} root@{vc_hostname} "
                "'for svc in $(vmon-cli --list 2>/dev/null); do "
                'info=$(vmon-cli -s $svc 2>/dev/null); '
                'starttype=$(echo "$info" | grep "Starttype:" | head -1 | sed "s/.*Starttype: //"); '
                'if [ "$starttype" = "AUTOMATIC" ]; then '
                'state=$(echo "$info" | grep "RunState:" | head -1 | sed "s/.*RunState: //"); '
                'echo "$svc:$state"; '
                "fi; done'"
            )
            
            result = lsf.run_command(check_cmd)
            
            if not hasattr(result, 'stdout') or not result.stdout:
                lsf.write_output(f'  WARNING: Could not query services on {vc_hostname}')
                autostart_failed += 1
                continue
            
            # Parse results: each line is "service_name:STATE"
            not_started = []
            vc_service_count = 0
            
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if ':' not in line:
                    continue
                
                svc_name, svc_state = line.split(':', 1)
                svc_name = svc_name.strip()
                svc_state = svc_state.strip()
                vc_service_count += 1
                
                if svc_state == 'STARTED':
                    autostart_started += 1
                else:
                    not_started.append((svc_name, svc_state))
            
            autostart_total += vc_service_count
            
            if not not_started:
                lsf.write_output(f'  All {vc_service_count} autostart services on {vc_hostname} are running')
                continue
            
            # Start services that are not running
            lsf.write_output(f'  Found {len(not_started)} autostart service(s) not started on {vc_hostname}:')
            for svc_name, svc_state in not_started:
                lsf.write_output(f'    {svc_name}: {svc_state} - starting...')
                
                start_result = lsf.ssh(f'vmon-cli --start {svc_name} 2>&1', f'root@{vc_hostname}')
                
                # Wait for service to reach STARTED state
                started = False
                wait_start = _time.time()
                while (_time.time() - wait_start) < AUTOSTART_START_TIMEOUT:
                    verify_result = lsf.ssh(
                        f"vmon-cli -s {svc_name} 2>/dev/null | grep 'RunState:' | head -1 | sed 's/.*RunState: //'",
                        f'root@{vc_hostname}'
                    )
                    if hasattr(verify_result, 'stdout') and verify_result.stdout.strip() == 'STARTED':
                        started = True
                        break
                    _time.sleep(AUTOSTART_CHECK_INTERVAL)
                
                if started:
                    lsf.write_output(f'    {svc_name}: Started successfully')
                    autostart_started += 1
                    autostart_fixed += 1
                else:
                    lsf.write_output(f'    WARNING: {svc_name} did not start within {AUTOSTART_START_TIMEOUT}s')
                    autostart_failed += 1
        
        if autostart_fixed > 0:
            lsf.write_output(f'Autostart services check complete: {autostart_fixed} service(s) were started')
        elif autostart_failed > 0:
            lsf.write_output(f'Autostart services check complete: {autostart_failed} service(s) failed to start')
        else:
            lsf.write_output('All autostart services are running on all vCenters')
    
    if dashboard:
        if autostart_total > 0 or dry_run:
            status = TaskStatus.COMPLETE if autostart_failed == 0 else TaskStatus.FAILED
            msg = ''
            if autostart_fixed > 0:
                msg = f'{autostart_fixed} service(s) required restart'
            if autostart_failed > 0:
                msg = f'{autostart_failed} service(s) failed to start'
            dashboard.update_task('vsphere', 'autostart_services', status,
                                  msg,
                                  total=autostart_total, success=autostart_started,
                                  failed=autostart_failed)
        else:
            dashboard.update_task('vsphere', 'autostart_services', TaskStatus.SKIPPED,
                                  'No vCenters configured')
        dashboard.update_task('vsphere', 'power_on_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 8: Start Nested VMs
    #==========================================================================
    
    vms_to_start = []
    if lsf.config.has_option('RESOURCES', 'VMs'):
        vms_raw = lsf.config.get('RESOURCES', 'VMs')
        if '\n' in vms_raw:
            vms_to_start = [v.strip() for v in vms_raw.split('\n') if v.strip()]
        else:
            vms_to_start = [v.strip() for v in vms_raw.split(',') if v.strip()]
    
    if vms_to_start:
        lsf.write_vpodprogress('Starting VMs', 'GOOD-4')
        lsf.write_output('Starting nested VMs')
        
        if not dry_run:
            while True:
                try:
                    lsf.start_nested(vms_to_start)
                    break
                except Exception as e:
                    lsf.write_output(f'VM startup error: {e}')
                lsf.labstartup_sleep(lsf.sleep_seconds)
        
        if dashboard:
            dashboard.update_task('vsphere', 'power_on_vms', TaskStatus.COMPLETE,
                                  total=len(vms_to_start), success=len(vms_to_start), failed=0)
    else:
        lsf.write_output('No VMs configured to start')
        if dashboard:
            dashboard.update_task('vsphere', 'power_on_vms', TaskStatus.SKIPPED, 
                                  'No VMs defined in config',
                                  total=0, success=0, failed=0, skipped=0)
    
    #==========================================================================
    # TASK 9: Start vApps
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('vsphere', 'power_on_vapps', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    vapps = []
    if lsf.config.has_option('RESOURCES', 'vApps'):
        vapps_raw = lsf.config.get('RESOURCES', 'vApps')
        if '\n' in vapps_raw:
            vapps = [v.strip() for v in vapps_raw.split('\n') if v.strip()]
        else:
            vapps = [v.strip() for v in vapps_raw.split(',') if v.strip()]
    
    if vapps:
        lsf.write_output('Starting vApps')
        
        if not dry_run:
            while True:
                try:
                    lsf.start_nested(vapps)
                    break
                except Exception as e:
                    lsf.write_output(f'vApp startup error: {e}')
                lsf.labstartup_sleep(lsf.sleep_seconds)
        
        if dashboard:
            dashboard.update_task('vsphere', 'power_on_vapps', TaskStatus.COMPLETE,
                                  total=len(vapps), success=len(vapps), failed=0)
    else:
        lsf.write_output('No vApps configured to start')
        if dashboard:
            dashboard.update_task('vsphere', 'power_on_vapps', TaskStatus.SKIPPED, 
                                  'No vApps defined in config',
                                  total=0, success=0, failed=0, skipped=0)
    
    if dashboard:
        total_nested = len(vms_to_start) + len(vapps)
        dashboard.update_task('vsphere', 'nested_vms', TaskStatus.COMPLETE,
                              total=total_nested, success=total_nested, failed=0)
    
    #==========================================================================
    # TASK 10: Clear Host Alarms
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Clearing host alarms')
        try:
            lsf.clear_host_alarms()
        except Exception as e:
            lsf.write_output(f'Could not clear alarms: {e}')
    
    #==========================================================================
    # Cleanup
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Disconnecting vCenters...')
        for si in lsf.sis:
            try:
                connect.Disconnect(si)
            except Exception:
                pass
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom vSphere configuration or checks here
    # See prelim.py for detailed examples of common operations
    
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
