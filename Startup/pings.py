#!/usr/bin/env python3
# pings.py - HOLFY27 Core Network Connectivity Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Verifies network connectivity via ping

import os
import sys
import argparse

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'pings'
MODULE_DESCRIPTION = 'Network connectivity verification'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for pings module
    
    :param lsf: lsfunctions module
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
        from status_dashboard import StatusDashboard
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('services', 'pings', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # Get Ping Targets from Config
    #==========================================================================
    
    ping_targets = []
    
    if lsf.config.has_option('RESOURCES', 'Pings'):
        pings_raw = lsf.config.get('RESOURCES', 'Pings')
        ping_targets = [p.strip() for p in pings_raw.split(',') if p.strip()]
        # Handle both newline-separated and comma-separated formats
        if '\n' in pings_raw:
            ping_targets = [p.strip() for p in pings_raw.split('\n') if p.strip()]
        else:
            ping_targets = [p.strip() for p in pings_raw.split(',') if p.strip()]
    
    if not ping_targets:
        lsf.write_output('No ping targets configured')
        return
    
    lsf.write_output(f'Ping targets: {ping_targets}')
    
    #==========================================================================
    # Ping Each Target
    #==========================================================================
    
    failed = []
    succeeded = []
    
    for target in ping_targets:
        if dry_run:
            lsf.write_output(f'Would ping: {target}')
            continue
        
        lsf.write_output(f'Pinging {target}...')
        
        # Retry logic
        max_retries = 3
        success = False
        
        for attempt in range(max_retries):
            if lsf.test_ping(target, count=1, timeout=5):
                lsf.write_output(f'Ping OK: {target}')
                success = True
                succeeded.append(target)
                break
            else:
                lsf.write_output(f'Ping attempt {attempt + 1}/{max_retries} failed: {target}')
                lsf.labstartup_sleep(5)
        
        if not success:
            lsf.write_output(f'Ping FAILED after {max_retries} attempts: {target}')
            failed.append(target)
    
    #==========================================================================
    # Report Results
    #==========================================================================
    
    if not dry_run:
        lsf.write_output(f'Ping results: {len(succeeded)} succeeded, {len(failed)} failed')
        
        if failed:
            lsf.write_output(f'Failed targets: {failed}')
            # Note: We log failures but don't fail the lab - adjust as needed
    
    if dashboard:
        status = 'complete' if not failed else 'failed'
        dashboard.update_task('services', 'pings', status)
        dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom ping targets or network checks here
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
    
    args = parser.parse_args()
    
    import lsfunctions as lsf
    
    if not args.skip_init:
        lsf.init(router=False)
    
    print(f'Running {MODULE_NAME} in standalone mode')
    print(f'Lab SKU: {lsf.lab_sku}')
    print(f'Dry run: {args.dry_run}')
    print()
    
    main(lsf=lsf, standalone=True, dry_run=args.dry_run)
