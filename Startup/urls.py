#!/usr/bin/env python3
# urls.py - HOLFY27 Core URL Verification Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Verifies web interface accessibility

import os
import sys
import argparse

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'urls'
MODULE_DESCRIPTION = 'URL accessibility verification'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for urls module
    
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
        from status_dashboard import StatusDashboard
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('final', 'url_checks', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # Get URL Targets from Config
    #==========================================================================
    
    url_targets = []
    
    if lsf.config.has_option('RESOURCES', 'URLs'):
        urls_raw = lsf.config.get('RESOURCES', 'URLs')
        url_targets = [u.strip() for u in urls_raw.split(',') if u.strip()]
    
    if not url_targets:
        lsf.write_output('No URL targets configured')
        return
    
    lsf.write_output(f'URL targets: {len(url_targets)}')
    
    #==========================================================================
    # Check Each URL
    #==========================================================================
    
    failed = []
    succeeded = []
    
    for url_spec in url_targets:
        # Parse url:expected_text format
        if url_spec.count(':') > 1:
            # URL contains colons (https://...), check for trailing :text
            parts = url_spec.rsplit(':', 1)
            if parts[1].startswith('//'):
                # The last part was part of the URL
                url = url_spec
                expected_text = None
            else:
                url = parts[0]
                expected_text = parts[1]
        else:
            url = url_spec
            expected_text = None
        
        if dry_run:
            lsf.write_output(f'Would check URL: {url}')
            continue
        
        lsf.write_output(f'Checking URL: {url}')
        if expected_text:
            lsf.write_output(f'  Expected text: {expected_text}')
        
        # Retry logic
        max_retries = 3
        success = False
        
        for attempt in range(max_retries):
            if lsf.test_url(url, expected_text=expected_text, verify_ssl=False, timeout=15):
                lsf.write_output(f'URL OK: {url}')
                success = True
                succeeded.append(url)
                break
            else:
                lsf.write_output(f'URL attempt {attempt + 1}/{max_retries} failed: {url}')
                lsf.labstartup_sleep(10)
        
        if not success:
            lsf.write_output(f'URL FAILED after {max_retries} attempts: {url}')
            failed.append(url)
    
    #==========================================================================
    # Report Results
    #==========================================================================
    
    if not dry_run:
        lsf.write_output(f'URL results: {len(succeeded)} succeeded, {len(failed)} failed')
        
        if failed:
            lsf.write_output(f'Failed URLs: {failed}')
    
    if dashboard:
        status = 'complete' if not failed else 'failed'
        dashboard.update_task('final', 'url_checks', status)
        dashboard.generate_html()
    
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
