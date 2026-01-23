#!/usr/bin/env python3
# VVF.py - HOLFY27 Core VVF Startup Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# VMware Validated Foundation startup sequence

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

MODULE_NAME = 'VVF'
MODULE_DESCRIPTION = 'VMware Validated Foundation startup'

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
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    lsf.write_vpodprogress('VVF Start', 'GOOD-3')
    
    #==========================================================================
    # TASK 1: Connect to VVF Management Cluster Hosts
    #==========================================================================
    
    vvfmgmtcluster = []
    if lsf.config.has_option('VVF', 'vvfmgmtcluster'):
        vvfmgmtcluster_raw = lsf.config.get('VVF', 'vvfmgmtcluster')
        vvfmgmtcluster = [h.strip() for h in vvfmgmtcluster_raw.split('\n') if h.strip()]
    
    if vvfmgmtcluster:
        lsf.write_vpodprogress('VVF Hosts Connect', 'GOOD-3')
        
        if not dry_run:
            lsf.connect_vcenters(vvfmgmtcluster)
            
            # Exit maintenance mode for each host
            for entry in vvfmgmtcluster:
                parts = entry.split(':')
                hostname = parts[0].strip()
                
                try:
                    host = lsf.get_host(hostname)
                    if host is None:
                        lsf.write_output(f'Could not find host: {hostname}')
                        continue
                    
                    if host.runtime.inMaintenanceMode:
                        lsf.write_output(f'Removing {hostname} from Maintenance Mode')
                        host.ExitMaintenanceMode_Task(0)
                    elif host.runtime.connectionState != 'connected':
                        lsf.write_output(f'Host {hostname} in error state: {host.runtime.connectionState}')
                    
                    lsf.labstartup_sleep(lsf.sleep_seconds)
                except Exception as e:
                    lsf.write_output(f'Error processing host {hostname}: {e}')
        else:
            lsf.write_output(f'Would connect to VVF hosts: {vvfmgmtcluster}')
    
    if dashboard:
        dashboard.update_task('vvf', 'mgmt_cluster', TaskStatus.COMPLETE)
        dashboard.update_task('vvf', 'datastore', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 2: Check VVF Management Datastore
    #==========================================================================
    
    vvfmgmtdatastore = []
    if lsf.config.has_option('VVF', 'vvfmgmtdatastore'):
        vvfmgmtdatastore_raw = lsf.config.get('VVF', 'vvfmgmtdatastore')
        vvfmgmtdatastore = [d.strip() for d in vvfmgmtdatastore_raw.split('\n') if d.strip()]
    
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
        dashboard.update_task('vvf', 'datastore', TaskStatus.COMPLETE)
        dashboard.update_task('vvf', 'nsx_mgr', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 3: Start NSX Manager (if configured)
    #==========================================================================
    
    if lsf.config.has_option('VVF', 'vvfnsxmgr'):
        vvfnsxmgr_raw = lsf.config.get('VVF', 'vvfnsxmgr')
        vvfnsxmgr = [n.strip() for n in vvfnsxmgr_raw.split('\n') if n.strip()]
        
        if vvfnsxmgr:
            lsf.write_vpodprogress('VVF NSX Mgr start', 'GOOD-3')
            
            if not dry_run:
                lsf.start_nested(vvfnsxmgr)
                lsf.write_output('Waiting 30 seconds for NSX Manager(s) to start...')
                lsf.labstartup_sleep(30)
            else:
                lsf.write_output(f'Would start NSX Manager(s): {vvfnsxmgr}')
    
    if dashboard:
        dashboard.update_task('vvf', 'nsx_mgr', TaskStatus.COMPLETE)
        dashboard.update_task('vvf', 'nsx_edges', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Start NSX Edges (if configured)
    #==========================================================================
    
    if lsf.config.has_option('VVF', 'vvfnsxedges'):
        vvfnsxedges_raw = lsf.config.get('VVF', 'vvfnsxedges')
        vvfnsxedges = [e.strip() for e in vvfnsxedges_raw.split('\n') if e.strip()]
        
        if vvfnsxedges:
            lsf.write_vpodprogress('VVF NSX Edges start', 'GOOD-3')
            lsf.write_output('Starting VVF NSX Edges...')
            
            if not dry_run:
                lsf.start_nested(vvfnsxedges)
                lsf.write_output('Waiting 5 minutes for NSX Edges to start...')
                lsf.labstartup_sleep(300)
            else:
                lsf.write_output(f'Would start NSX Edges: {vvfnsxedges}')
    
    if dashboard:
        dashboard.update_task('vvf', 'nsx_edges', TaskStatus.COMPLETE)
        dashboard.update_task('vvf', 'vcenter', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: Start VVF vCenter (if configured)
    #==========================================================================
    
    if lsf.config.has_option('VVF', 'vvfvCenter'):
        vvfvCenter_raw = lsf.config.get('VVF', 'vvfvCenter')
        vvfvCenter = [v.strip() for v in vvfvCenter_raw.split('\n') if v.strip()]
        
        if vvfvCenter:
            lsf.write_vpodprogress('VVF vCenter start', 'GOOD-3')
            lsf.write_output('Starting VVF management vCenter...')
            
            if not dry_run:
                lsf.start_nested(vvfvCenter)
            else:
                lsf.write_output(f'Would start vCenter: {vvfvCenter}')
    
    if dashboard:
        dashboard.update_task('vvf', 'vcenter', TaskStatus.COMPLETE)
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
