#!/usr/bin/env python3
# VCFfinal.py - HOLFY27 Core VCF Final Tasks Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# VCF final tasks (Tanzu, Aria)

import os
import sys
import argparse
import logging
import ssl

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
    from pyVmomi import vim
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    # Verify VCF section exists (checks if VCF module was relevant)
    # We check VCFFINAL section for specific tasks
    if not lsf.config.has_section('VCFFINAL'):
        lsf.write_output('No VCFFINAL section in config.ini - skipping VCFfinal')
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
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Connect to VCF Management Cluster Hosts (if needed)
    #==========================================================================
    
    vcfmgmtcluster = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        vcfmgmtcluster_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        vcfmgmtcluster = [h.strip() for h in vcfmgmtcluster_raw.split('\n') if h.strip()]
    
    if vcfmgmtcluster and not dry_run:
        lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3')
        lsf.connect_vcenters(vcfmgmtcluster)
    
    #==========================================================================
    # TASK 2: Start Supervisor Control Plane VMs
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.RUNNING)
        dashboard.generate_html()
        
    lsf.write_vpodprogress('Tanzu Start', 'GOOD-3')
    
    # Check for Tanzu VMs
    tanzu_vms = []
    
    # We can check for Supervisor Control Plane VMs by name pattern
    if not dry_run:
        try:
            lsf.write_vpodprogress('Tanzu Control Plane', 'GOOD-3')
            
            # Find all Supervisor Control Plane VMs
            # They usually start with the cluster name or similar pattern
            # For now, we'll look for VMs with 'Supervisor' in the name or specific config
            
            # Use lsfunctions to find VMs
            # This requires connected sessions
            pass
            
        except Exception as e:
            lsf.write_output(f'Error checking Tanzu VMs: {e}')
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 3: Check Aria Automation (vRA)
    #==========================================================================
    
    if lsf.config.has_option('VCFFINAL', 'vravms'):
        lsf.write_vpodprogress('Aria Automation', 'GOOD-3')
        
        # Connect to vCenters if not already connected
        vcenters = []
        if lsf.config.has_option('RESOURCES', 'vCenters'):
            vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
            vcenters = [v.strip() for v in vcenters_raw.split('\n') if v.strip()]
            
            if vcenters and not dry_run:
                lsf.write_vpodprogress('Connecting vCenters', 'GOOD-3')
                lsf.connect_vcenters(vcenters)
        
        vravms_raw = lsf.config.get('VCFFINAL', 'vravms')
        vravms = [v.strip() for v in vravms_raw.split('\n') if v.strip()]
        
        if vravms and not dry_run:
            lsf.write_output('Checking Aria Automation VMs...')
            lsf.start_nested(vravms)
            
        # Check URLs
        if lsf.config.has_option('VCFFINAL', 'vraurls'):
            vraurls_raw = lsf.config.get('VCFFINAL', 'vraurls')
            vraurls = [u.strip() for u in vraurls_raw.split('\n') if u.strip()]
            
            for url_spec in vraurls:
                if ',' in url_spec:
                    parts = url_spec.split(',', 1)
                    url = parts[0].strip()
                    expected = parts[1].strip()
                else:
                    url = url_spec.strip()
                    expected = None
                
                lsf.test_url(url, expected_text=expected)
    
    if dashboard:
        dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'aria_urls', TaskStatus.COMPLETE)
        dashboard.generate_html()
    
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
    
    # Example: Add custom VCF final checks here
    
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
