#!/usr/bin/env python3
# final.py - HOLFY27 Core Final Tasks Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Final lab startup checks and cleanup

import os
import sys
import argparse
import datetime

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'final'
MODULE_DESCRIPTION = 'Final lab startup checks and cleanup'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for final module
    
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
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('final', 'custom', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Run Lab-Specific Final Script
    #==========================================================================
    
    lsf.write_output('Checking for lab-specific final script...')
    
    final_scripts = [
        f'{lsf.vpod_repo}/scripts/final.sh',
        f'{lsf.vpod_repo}/final.sh'
    ]
    
    if not dry_run:
        for script in final_scripts:
            if os.path.isfile(script):
                lsf.write_output(f'Running final script: {script}')
                result = lsf.run_command(f'/bin/bash {script}')
                if result.returncode != 0:
                    lsf.write_output(f'Final script returned: {result.returncode}')
                break
    
    #==========================================================================
    # TASK 2: Verify All Resources Are Accessible
    #==========================================================================
    
    lsf.write_output('Running final resource verification...')
    
    # Re-check critical pings
    if lsf.config.has_option('RESOURCES', 'Pings'):
        pings = lsf.config.get('RESOURCES', 'Pings').split(',')
        for ping in pings:
            ping = ping.strip()
            if ping and not dry_run:
                if lsf.test_ping(ping):
                    lsf.write_output(f'Final ping OK: {ping}')
                else:
                    lsf.write_output(f'Final ping FAIL: {ping}')
    
    # Re-check critical URLs
    if lsf.config.has_option('RESOURCES', 'URLs'):
        urls = lsf.config.get('RESOURCES', 'URLs').split(',')
        for url in urls:
            url = url.strip()
            if url and not dry_run:
                # Handle url:expected_text format
                if ':' in url and not url.startswith('http'):
                    url_parts = url.split(':', 1)
                    url = url_parts[0]
                    expected = url_parts[1] if len(url_parts) > 1 else None
                else:
                    expected = None
                
                if lsf.test_url(url, expected_text=expected, verify_ssl=False):
                    lsf.write_output(f'Final URL OK: {url}')
                else:
                    lsf.write_output(f'Final URL FAIL: {url}')
    
    #==========================================================================
    # TASK 3: Write Ready Time
    #==========================================================================
    
    lsf.write_output('Recording ready time...')
    
    if not dry_run:
        delta = datetime.datetime.now() - lsf.start_time
        run_mins = "{0:.2f}".format(delta.seconds / 60)
        
        try:
            with open(lsf.ready_time_file, 'w') as f:
                f.write(f'{run_mins} minutes')
            lsf.write_output(f'Lab ready after {run_mins} minutes')
        except Exception as e:
            lsf.write_output(f'Could not write ready time: {e}')
    
    #==========================================================================
    # TASK 4: Signal Router Ready
    #==========================================================================
    
    lsf.write_output('Signaling router that lab is ready...')
    
    if not dry_run:
        lsf.signal_router_ready()
    
    #==========================================================================
    # TASK 5: Update Dashboard
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('final', 'custom', 'complete')
        dashboard.set_complete()
        dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add final custom checks or configuration here
    # See prelim.py for detailed examples of common operations
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
    #==========================================================================
    # COMPLETE
    #==========================================================================
    
    lsf.write_output(f'{MODULE_NAME} completed successfully')
    lsf.write_vpodprogress('Ready', 'READY', color='green')


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
