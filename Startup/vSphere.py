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
        return
    
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
                    # Also verify the API endpoint is responding
                    try:
                        # Try to reach the vCenter API - this indicates services are up
                        api_url = f'https://{vc_hostname}/api'
                        response = requests.get(api_url, verify=False, timeout=10)
                        # Any response (even 401) means vCenter is responding
                        if response.status_code in [200, 401, 403]:
                            vcenter_available = True
                            elapsed = int(time.time() - start_wait)
                            lsf.write_output(f'vCenter {vc_hostname} is available after {elapsed} seconds')
                            break
                    except requests.exceptions.RequestException:
                        pass
                
                elapsed = int(time.time() - start_wait)
                remaining = VCENTER_WAIT_TIMEOUT - elapsed
                lsf.write_output(f'vCenter {vc_hostname} not ready yet, waiting... ({remaining}s remaining)')
                time.sleep(VCENTER_CHECK_INTERVAL)
            
            if not vcenter_available:
                lsf.write_output(f'WARNING: vCenter {vc_hostname} did not become available within {VCENTER_WAIT_TIMEOUT // 60} minutes')
                lsf.write_output(f'Continuing with connection attempt anyway...')
        
        # Now connect to all vCenters
        lsf.connect_vcenters(vcenters)
    else:
        lsf.write_output(f'Would connect to vCenters: {vcenters}')
    
    if dashboard:
        dashboard.update_task('vsphere', 'vcenter_connect', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'datastores', TaskStatus.RUNNING)
        dashboard.generate_html()
    
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
        dashboard.update_task('vsphere', 'datastores', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'maintenance', TaskStatus.RUNNING)
        dashboard.generate_html()
    
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
        dashboard.update_task('vsphere', 'maintenance', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'vcls', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Verify vCLS VMs Started
    #==========================================================================
    
    if not dry_run:
        vms = lsf.get_all_vms()
        vcls_count = 0
        
        for vm in vms:
            if 'vCLS' in vm.name:
                vcls_count += 1
                while vm.runtime.powerState != 'poweredOn':
                    lsf.write_output(f'Waiting for {vm.name} to power on...')
                    lsf.labstartup_sleep(lsf.sleep_seconds)
        
        if vcls_count > 0:
            lsf.write_output(f'All {vcls_count} vCLS VMs have started')
    
    if dashboard:
        dashboard.update_task('vsphere', 'vcls', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'drs', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: Wait for DRS to Enable
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
        dashboard.update_task('vsphere', 'drs', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'shell_warning', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 6: Suppress Shell Warning on ESXi Hosts
    #==========================================================================
    
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
            except Exception as e:
                lsf.write_output(f'Could not suppress shell warning on {host.name}: {e}')
    
    if dashboard:
        dashboard.update_task('vsphere', 'shell_warning', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'vcenter_ready', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 7: Verify vCenter UI Ready
    #==========================================================================
    
    if not dry_run:
        lsf.write_vpodprogress('Checking vCenter', 'GOOD-3')
        lsf.write_output('Checking vCenter readiness...')
        
        vc_urls = []
        for entry in vcenters:
            vc = entry.split(':')[0]
            vc_urls.append(f'https://{vc}/ui/')
        
        for url in vc_urls:
            while not lsf.test_url(url, pattern='loading-container', timeout=5):
                lsf.write_output(f'Waiting for vCenter UI: {url}')
                lsf.labstartup_sleep(lsf.sleep_seconds)
    
    if dashboard:
        dashboard.update_task('vsphere', 'vcenter_ready', TaskStatus.COMPLETE)
        dashboard.update_task('vsphere', 'nested_vms', TaskStatus.RUNNING)
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
    
    #==========================================================================
    # TASK 9: Start vApps
    #==========================================================================
    
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
        dashboard.update_task('vsphere', 'nested_vms', TaskStatus.COMPLETE)
        dashboard.generate_html()
    
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
