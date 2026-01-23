#!/usr/bin/env python3
# VCFfinal.py - HOLFY27 Core VCF Final Tasks Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# VCF final startup tasks including Tanzu and Aria

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

MODULE_NAME = 'VCFfinal'
MODULE_DESCRIPTION = 'VCF final tasks (Tanzu, Aria)'

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def verify_nic_connected(lsf, vm_obj, simple=False):
    """
    Verify and reconnect VM NICs if needed
    
    :param lsf: lsfunctions module
    :param vm_obj: The VM object to check
    :param simple: If True, just connect without disconnect first
    """
    try:
        nics = lsf.get_network_adapter(vm_obj)
        for nic in nics:
            if simple:
                lsf.write_output(f'Connecting {nic.deviceInfo.label} on {vm_obj.name}')
                lsf.set_network_adapter_connection(vm_obj, nic, True)
                lsf.labstartup_sleep(lsf.sleep_seconds)
            elif nic.connectable.connected:
                lsf.write_output(f'{vm_obj.name} {nic.deviceInfo.label} is connected')
            else:
                lsf.write_output(f'{vm_obj.name} {nic.deviceInfo.label} is NOT connected')
                lsf.set_network_adapter_connection(vm_obj, nic, False)
                lsf.labstartup_sleep(lsf.sleep_seconds)
                lsf.write_output(f'Reconnecting {nic.deviceInfo.label} on {vm_obj.name}')
                lsf.set_network_adapter_connection(vm_obj, nic, True)
    except Exception as e:
        lsf.write_output(f'Error checking NICs on {vm_obj.name}: {e}')


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for VCFfinal module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    from pyVim import connect
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    # Verify VCFFINAL section exists
    if not lsf.config.has_section('VCFFINAL'):
        lsf.write_output('No VCFFINAL section in config.ini - skipping')
        return
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    lsf.write_vpodprogress('Tanzu Start', 'GOOD-8')
    
    #==========================================================================
    # TASK 1: Connect to VCF Management Cluster
    #==========================================================================
    
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        vcfmgmtcluster_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        vcfmgmtcluster = [h.strip() for h in vcfmgmtcluster_raw.split('\n') if h.strip()]
        
        if vcfmgmtcluster and not dry_run:
            lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3')
            lsf.connect_vcenters(vcfmgmtcluster)
    
    #==========================================================================
    # TASK 2: Start Supervisor Control Plane VMs
    #==========================================================================
    
    lsf.write_vpodprogress('Tanzu Control Plane', 'GOOD-8')
    
    if not dry_run:
        supvms = lsf.get_vm_match('Supervisor*')
        
        for vm in supvms:
            lsf.write_output(f'{vm.name} is {vm.runtime.powerState}')
            try:
                if vm.runtime.powerState != 'poweredOn':
                    lsf.start_nested([f'{vm.name}:{vm.runtime.host.name}'])
            except Exception as e:
                lsf.write_output(f'Error starting {vm.name}: {e}')
        
        # Reconnect NICs on Supervisor VMs
        for vm in supvms:
            verify_nic_connected(lsf, vm, simple=False)
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 3: Deploy Tanzu (if configured)
    #==========================================================================
    
    if lsf.config.has_option('VCFFINAL', 'tanzucreate'):
        tanzucreate_raw = lsf.config.get('VCFFINAL', 'tanzucreate')
        tanzucreate = [t.strip() for t in tanzucreate_raw.split('\n') if t.strip()]
        
        if tanzucreate:
            lsf.write_vpodprogress('Deploy Tanzu (25 Minutes)', 'GOOD-8')
            lsf.write_output('Deploy Tanzu - waiting for images (10 minutes)...')
            
            if not dry_run:
                lsf.labstartup_sleep(600)  # Wait for Tanzu images
                
                # Parse tanzucreate entry: host:account:script
                if ':' in tanzucreate[0]:
                    parts = tanzucreate[0].split(':')
                    if len(parts) >= 3:
                        tchost = parts[0].strip()
                        tcaccount = parts[1].strip()
                        tcscript = parts[2].strip()
                        
                        lsf.write_output(f'Running {tcscript} as {tcaccount}@{tchost}')
                        result = lsf.ssh(tcscript, f'{tcaccount}@{tchost}', lsf.password)
                        if hasattr(result, 'stdout'):
                            lsf.write_output(result.stdout)
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Start Aria Automation VMs (if configured)
    #==========================================================================
    
    if lsf.config.has_option('VCFFINAL', 'vravms'):
        # Connect to standard vCenters if needed
        if lsf.config.has_option('RESOURCES', 'vCenters'):
            vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
            vcenters = [v.strip() for v in vcenters_raw.split('\n') if v.strip()]
            
            if vcenters and not dry_run:
                lsf.write_vpodprogress('Connecting vCenters', 'GOOD-3')
                lsf.connect_vcenters(vcenters)
        
        vravms_raw = lsf.config.get('VCFFINAL', 'vravms')
        vravms = [v.strip() for v in vravms_raw.split('\n') if v.strip()]
        
        if vravms:
            lsf.write_vpodprogress('Starting Workspace Access', 'GOOD-8')
            lsf.write_output('Starting Aria Automation VMs...')
            
            if not dry_run:
                # Pre-start NIC verification
                for vravm in vravms:
                    parts = vravm.split(':')
                    vmname = parts[0].strip()
                    
                    try:
                        vms = lsf.get_vm_match(vmname)
                        for vm in vms:
                            verify_nic_connected(lsf, vm, simple=True)
                    except Exception as e:
                        lsf.write_output(f'Error with {vmname}: {e}')
                
                lsf.start_nested(vravms)
                
                # Wait for VMs to fully start
                for vravm in vravms:
                    parts = vravm.split(':')
                    vmname = parts[0].strip()
                    
                    vms = lsf.get_vm_match(vmname)
                    for vm in vms:
                        # Wait for power on
                        while vm.runtime.powerState != 'poweredOn':
                            try:
                                vm.PowerOnVM_Task()
                            except Exception:
                                pass
                            lsf.labstartup_sleep(lsf.sleep_seconds)
                        
                        # Wait for VMware Tools
                        while vm.summary.guest.toolsRunningStatus != 'guestToolsRunning':
                            lsf.write_output(f'Waiting for Tools in {vmname}...')
                            lsf.labstartup_sleep(lsf.sleep_seconds)
                            verify_nic_connected(lsf, vm, simple=False)
    
    if dashboard:
        dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'aria_urls', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: Verify Aria Automation URLs
    #==========================================================================
    
    if lsf.config.has_option('VCFFINAL', 'vraurls'):
        vraurls_raw = lsf.config.get('VCFFINAL', 'vraurls')
        vraurls = [u.strip() for u in vraurls_raw.split('\n') if u.strip()]
        
        if vraurls:
            lsf.write_vpodprogress('Aria Automation URL Checks', 'GOOD-8')
            lsf.write_output('Aria Automation URL checks...')
            
            if not dry_run:
                # Fix expired passwords if necessary
                lsf.write_output('Checking Aria password expiration...')
                lsf.run_command('/home/holuser/hol/Tools/vcfapwcheck.sh')
                
                # Run watchvcfa script
                lsf.run_command('/home/holuser/hol/Tools/watchvcfa.sh')
                
                for entry in vraurls:
                    parts = entry.split(',')
                    if len(parts) < 2:
                        continue
                    
                    url = parts[0].strip()
                    pattern = parts[1].strip()
                    
                    lsf.write_output(f'Testing {url} for pattern: {pattern}')
                    
                    max_attempts = 30
                    attempt = 0
                    
                    while not lsf.test_url(url, pattern=pattern, timeout=5):
                        attempt += 1
                        
                        if attempt >= max_attempts:
                            lsf.labfail('Automation URLs not accessible after 30 minutes')
                            return
                        
                        lsf.write_output(f'URL not ready ({attempt}/{max_attempts}), retrying in 60s...')
                        lsf.labstartup_sleep(60)
    
    if dashboard:
        dashboard.update_task('vcffinal', 'aria_urls', TaskStatus.COMPLETE)
        dashboard.generate_html()
    
    #==========================================================================
    # Cleanup
    #==========================================================================
    
    if not dry_run:
        for si in lsf.sis:
            try:
                connect.Disconnect(si)
            except Exception:
                pass
    
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
