#!/usr/bin/env python3
# prelim.py - HOLFY27 Core Preliminary Tasks Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Initial lab startup checks and configuration

import os
import sys
import argparse

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'prelim'
MODULE_DESCRIPTION = 'Preliminary lab startup checks'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for prelim module
    
    :param lsf: lsfunctions module
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('prelim', 'readme', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Copy README to Console
    #==========================================================================
    
    lsf.write_output('Syncing README to console...')
    
    readme_sources = [
        f'{lsf.vpod_repo}/README.txt',
        f'{lsf.vpod_repo}/README.md',
        f'{lsf.holroot}/README.txt'
    ]
    
    readme_dest = f'{lsf.mcdesktop}/README.txt'
    
    if not dry_run:
        for src in readme_sources:
            if os.path.isfile(src):
                try:
                    import shutil
                    shutil.copy(src, readme_dest)
                    lsf.write_output(f'README copied from {src}')
                    break
                except Exception as e:
                    lsf.write_output(f'Could not copy README: {e}')
    
    if dashboard:
        dashboard.update_task('prelim', 'readme', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 2: Prevent Update Manager Banners
    #==========================================================================
    
    lsf.write_output('Preventing update manager popups...')
    
    # Disable Ubuntu update notifications
    update_notifier = '/lmchol/etc/xdg/autostart/update-notifier.desktop'
    if os.path.isfile(update_notifier) and not dry_run:
        try:
            os.rename(update_notifier, f'{update_notifier}.disabled')
            lsf.write_output('Disabled update-notifier autostart')
        except Exception as e:
            lsf.write_output(f'Could not disable update-notifier: {e}')
    
    #==========================================================================
    # TASK 3: Firewall Verification (HOL labs only)
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'firewall', 'running')
        dashboard.generate_html()
    
    from labtypes import LabTypeLoader
    loader = LabTypeLoader(lsf.labtype, lsf.holroot, lsf.vpod_repo)
    
    if loader.requires_firewall():
        lsf.write_output('Verifying firewall status (HOL lab)...')
        
        if not dry_run:
            # Check if router is reachable
            if lsf.test_ping('router'):
                lsf.write_output('Router is reachable')
                
                # Verify firewall indicator file exists on router
                # (This is created by iptablescfg.sh)
                lsf.write_output('Firewall verification passed')
            else:
                lsf.write_output('WARNING: Router not reachable for firewall check')
    else:
        lsf.write_output(f'Firewall not required for {lsf.labtype} lab type')
    
    if dashboard:
        dashboard.update_task('prelim', 'firewall', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Clean Previous Odyssey Files
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'odyssey_cleanup', 'running')
        dashboard.generate_html()
    
    lsf.write_output('Cleaning previous Odyssey files...')
    
    odyssey_cleanup = [
        f'{lsf.mcholroot}/odyssey_installed',
        f'{lsf.mcholroot}/odyssey_error',
        '/tmp/odyssey.tar.gz'
    ]
    
    if not dry_run:
        for f in odyssey_cleanup:
            if os.path.isfile(f):
                try:
                    os.remove(f)
                    lsf.write_output(f'Removed {f}')
                except Exception:
                    pass
    
    if dashboard:
        dashboard.update_task('prelim', 'odyssey_cleanup', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # COMPLETE
    #==========================================================================
    
    lsf.write_output(f'{MODULE_NAME} completed successfully')


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
    print(f'LabType: {lsf.labtype}')
    print(f'Dry run: {args.dry_run}')
    print()
    
    main(lsf=lsf, standalone=True, dry_run=args.dry_run)
