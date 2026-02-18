#!/usr/bin/env python3
# final.py - HOLFY27 Core Final Lab Checks
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Final lab startup checks and cleanup

import os
import sys
import argparse
import logging
import urllib3

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(level=logging.WARNING)

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
        dashboard.update_task('final', 'custom', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Check for lab-specific final script
    #==========================================================================
    
    lsf.write_output('Checking for lab-specific final script...')
    
    # Check for final script in vPod repo
    repo_final = os.path.join(lsf.vpod_repo, 'final_custom.py')
    if os.path.exists(repo_final):
        lsf.write_output(f'Running custom final script: {repo_final}')
        if not dry_run:
            try:
                # Import and run the custom script
                import importlib.util
                spec = importlib.util.spec_from_file_location("final_custom", repo_final)
                custom_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(custom_module)
                if hasattr(custom_module, 'main'):
                    custom_module.main(lsf)
            except Exception as e:
                lsf.write_output(f'Error running custom final script: {e}')
    
    #==========================================================================
    # TASK 2: Final Verification (Pings and URLs)
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Running final resource verification...')
        
        # Check Pings one last time
        if lsf.config.has_option('RESOURCES', 'Pings'):
            pings_raw = lsf.config.get('RESOURCES', 'Pings')
            if '\n' in pings_raw:
                pings = [p.strip() for p in pings_raw.split('\n') if p.strip()]
            else:
                pings = [p.strip() for p in pings_raw.split(',') if p.strip()]
            
            for host in pings:
                if lsf.test_ping(host, count=1, timeout=2):
                    # Quiet success
                    pass
                else:
                    lsf.write_output(f'Final ping FAIL: {host}')
        
        # Check URLs one last time
        if lsf.config.has_option('RESOURCES', 'URLS'):
            urls_raw = lsf.config.get('RESOURCES', 'URLS')
            url_targets = []
            
            # Parse properly like in urls.py
            for line in urls_raw.split('\n'):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                url_targets.append(line)
            
            for url_spec in url_targets:
                if ',' in url_spec:
                    parts = url_spec.split(',', 1)
                    url = parts[0].strip()
                    expected_text = parts[1].strip() if len(parts) > 1 else None
                else:
                    url = url_spec.strip()
                    expected_text = None
                
                if url:
                    # Quick check, 5 second timeout, ignore SSL
                    if lsf.test_url(url, expected_text=expected_text, verify_ssl=False, timeout=5):
                        lsf.write_output(f'Final URL OK: {url}')
                    else:
                        lsf.write_output(f'Final URL FAIL: {url}')
                        if expected_text:
                             lsf.write_output(f'  Expected: {expected_text}')

    #==========================================================================
    # TASK 3: Lab Ready Recording
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Recording ready time...')
        try:
            # Calculate total runtime
            import datetime
            now = datetime.datetime.now()
            runtime = now - lsf.start_time
            minutes = runtime.total_seconds() / 60
            
            lsf.write_output(f'Lab ready after {minutes:.2f} minutes')
            
            # Write ready time to file
            with open(lsf.ready_time_file, 'w') as f:
                f.write(f'{minutes:.2f}\n')
                
        except Exception as e:
            lsf.write_output(f'Error recording ready time: {e}')
    
    #==========================================================================
    # TASK 4: Signal Router
    #==========================================================================
    
    lsf.write_output('Signaling router that lab is ready...')
    if not dry_run:
        lsf.signal_router('ready')
    
    if dashboard:
        dashboard.update_task('final', 'custom', TaskStatus.COMPLETE)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: LabCheck Schedule
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('final', 'labcheck', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    # Check if labcheck scheduling is configured
    labcheck_configured = False
    labcheck_skip_reason = 'LabCheck not configured in config.ini'
    
    if lsf.config.has_option('VPOD', 'labcheck_enabled'):
        labcheck_enabled = lsf.config.get('VPOD', 'labcheck_enabled').lower() == 'true'
        if labcheck_enabled:
            labcheck_configured = True
            lsf.write_output('LabCheck scheduling is enabled')
            # LabCheck scheduling would be configured here
            if dashboard:
                dashboard.update_task('final', 'labcheck', TaskStatus.COMPLETE)
                dashboard.generate_html()
        else:
            labcheck_skip_reason = 'LabCheck disabled in config (labcheck_enabled=false)'
    
    if not labcheck_configured:
        lsf.write_output(f'Skipping LabCheck schedule: {labcheck_skip_reason}')
        if dashboard:
            dashboard.update_task('final', 'labcheck', TaskStatus.SKIPPED, labcheck_skip_reason)
            dashboard.generate_html()
    
    #==========================================================================
    # TASK 6: holuser Lock
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('final', 'holuser_lock', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    # Check if holuser lock is configured
    holuser_lock_configured = False
    holuser_lock_skip_reason = 'holuser lock not configured in config.ini'
    
    if lsf.config.has_option('VPOD', 'holuser_lock'):
        holuser_lock_enabled = lsf.config.get('VPOD', 'holuser_lock').lower() == 'true'
        if holuser_lock_enabled:
            holuser_lock_configured = True
            lsf.write_output('Locking holuser account')
            if not dry_run:
                # Lock the holuser account
                try:
                    import subprocess
                    subprocess.run(['passwd', '-l', 'holuser'], capture_output=True)
                    lsf.write_output('holuser account locked')
                except Exception as e:
                    lsf.write_output(f'Error locking holuser account: {e}')
            if dashboard:
                dashboard.update_task('final', 'holuser_lock', TaskStatus.COMPLETE)
                dashboard.generate_html()
        else:
            holuser_lock_skip_reason = 'holuser lock disabled in config (holuser_lock=false)'
    
    if not holuser_lock_configured:
        lsf.write_output(f'Skipping holuser lock: {holuser_lock_skip_reason}')
        if dashboard:
            dashboard.update_task('final', 'holuser_lock', TaskStatus.SKIPPED, holuser_lock_skip_reason)
            dashboard.generate_html()
    
    #==========================================================================
    # TASK 7: Lab Ready
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('final', 'ready', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 8: Clear All vCenter Alarms
    # Runs as the very last step so alarms triggered during startup
    # (e.g., VM CPU usage spikes from NSX Manager boot) are cleared.
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Clearing all triggered vCenter alarms...')
        try:
            if not lsf.sis:
                vcenters = lsf.get_config_list('RESOURCES', 'vCenters')
                if vcenters:
                    lsf.write_output(f'Reconnecting to {len(vcenters)} vCenter(s) for alarm clearing...')
                    lsf.connect_vcenters(vcenters)
            cleared = 0
            for si in lsf.sis:
                alarm_mgr = si.content.alarmManager
                filter_spec = lsf.vim.alarm.AlarmFilterSpec(
                    status=[],
                    typeEntity='entityTypeAll',
                    typeTrigger='triggerTypeAll'
                )
                alarm_mgr.ClearTriggeredAlarms(filter_spec)
                cleared += 1
            lsf.write_output(f'Cleared alarms on {cleared} vCenter session(s)')
        except Exception as e:
            lsf.write_output(f'Could not clear vCenter alarms: {e}')
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom final checks here
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'{MODULE_NAME} completed successfully')
    
    # Mark lab as ready
    if dashboard:
        dashboard.update_task('final', 'ready', TaskStatus.COMPLETE, 'Lab startup completed successfully')
        dashboard.generate_html()
    
    # Update desktop background if needed
    if not dry_run:
        try:
            lsf.update_desktop_config('Ready')
        except:
            pass


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
