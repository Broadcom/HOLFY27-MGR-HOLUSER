#!/usr/bin/env python3
# urls.py - HOLFY27 Core URL Verification Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Verifies web interface accessibility

import os
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import urllib3

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'urls'
MODULE_DESCRIPTION = 'URL accessibility verification'
MAX_RETRIES = 20
RETRY_DELAY = 20
REQUEST_TIMEOUT = 15
MAX_WORKERS = 8

#==============================================================================
# URL CHECK FUNCTION
#==============================================================================

def check_url_with_retries(url, expected_text, max_retries, retry_delay, timeout):
    """
    Check a single URL with retry logic.
    
    :param url: URL to check
    :param expected_text: Text expected in response (or None)
    :param max_retries: Maximum number of retry attempts
    :param retry_delay: Seconds to wait between retries
    :param timeout: Request timeout in seconds
    :return: tuple (url, success, attempts_made, error_message)
    """
    import time
    
    session = requests.Session()
    session.trust_env = False  # Ignore proxy environment vars
    
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(
                url,
                verify=False,  # Ignore SSL certificate errors
                timeout=timeout,
                proxies=None,
                allow_redirects=True
            )
            
            if response.status_code != 200:
                last_error = f'HTTP {response.status_code}'
                if attempt < max_retries:
                    time.sleep(retry_delay)
                continue
            
            if expected_text and expected_text not in response.text:
                last_error = f'Expected text "{expected_text}" not found'
                if attempt < max_retries:
                    time.sleep(retry_delay)
                continue
            
            # Success
            return (url, True, attempt, None)
            
        except requests.exceptions.SSLError as e:
            last_error = f'SSL error: {str(e)[:50]}'
        except requests.exceptions.ConnectionError as e:
            last_error = f'Connection error: {str(e)[:50]}'
        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
        except Exception as e:
            last_error = f'Error: {str(e)[:50]}'
        
        if attempt < max_retries:
            time.sleep(retry_delay)
    
    # All retries exhausted
    return (url, False, max_retries, last_error)


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
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        from status_dashboard import StatusDashboard
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('urls', 'url_checks', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # Get URL Targets from Config
    #==========================================================================
    
    url_targets = []
    
    if lsf.config.has_option('RESOURCES', 'URLS'):
        urls_raw = lsf.config.get('RESOURCES', 'URLS')
        # Parse multi-line format: each line is "url,expected_text"
        for line in urls_raw.split('\n'):
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
            url_targets.append(line)
    
    if not url_targets:
        lsf.write_output('No URL targets configured')
        if dashboard:
            dashboard.update_task('urls', 'url_checks', 'skipped', 'No URL targets defined in config')
            dashboard.generate_html()
        return
    
    lsf.write_output(f'URL targets: {len(url_targets)}')
    
    #==========================================================================
    # Parse URL Entries
    #==========================================================================
    
    url_checks = []  # List of (url, expected_text) tuples
    
    for url_spec in url_targets:
        # Format is: url,expected_text
        if ',' in url_spec:
            parts = url_spec.split(',', 1)
            url = parts[0].strip()
            expected_text = parts[1].strip() if len(parts) > 1 else None
        else:
            url = url_spec.strip()
            expected_text = None
        
        if url:
            url_checks.append((url, expected_text))
    
    if dry_run:
        for url, expected_text in url_checks:
            lsf.write_output(f'Would check URL: {url}')
            if expected_text:
                lsf.write_output(f'  Expected text: {expected_text}')
        return
    
    #==========================================================================
    # Check URLs in Parallel
    #==========================================================================
    
    lsf.write_output(f'Checking {len(url_checks)} URLs in parallel (max {MAX_RETRIES} attempts each)...')
    
    failed = []
    succeeded = []
    results = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all URL checks
        future_to_url = {
            executor.submit(
                check_url_with_retries,
                url,
                expected_text,
                MAX_RETRIES,
                RETRY_DELAY,
                REQUEST_TIMEOUT
            ): (url, expected_text)
            for url, expected_text in url_checks
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_url):
            url, expected_text = future_to_url[future]
            try:
                result_url, success, attempts, error = future.result()
                results.append((result_url, success, attempts, error, expected_text))
            except Exception as e:
                results.append((url, False, 0, str(e), expected_text))
    
    #==========================================================================
    # Log Results (in order)
    #==========================================================================
    
    # Sort results by original order
    url_order = {url: i for i, (url, _) in enumerate(url_checks)}
    results.sort(key=lambda r: url_order.get(r[0], 999))
    
    lsf.write_output('')
    lsf.write_output('='*60)
    lsf.write_output('URL CHECK RESULTS')
    lsf.write_output('='*60)
    
    for url, success, attempts, error, expected_text in results:
        if success:
            lsf.write_output(f'[SUCCESS] {url}')
            lsf.write_output(f'          Attempts: {attempts}/{MAX_RETRIES}')
            if expected_text:
                lsf.write_output(f'          Expected text found: {expected_text}')
            succeeded.append(url)
        else:
            lsf.write_output(f'[FAILED]  {url}')
            lsf.write_output(f'          Attempts: {attempts}/{MAX_RETRIES}')
            if error:
                lsf.write_output(f'          Error: {error}')
            failed.append(url)
    
    lsf.write_output('='*60)
    
    #==========================================================================
    # Report Summary
    #==========================================================================
    
    lsf.write_output(f'URL results: {len(succeeded)} succeeded, {len(failed)} failed')
    
    if failed:
        lsf.write_output(f'Failed URLs:')
        for url in failed:
            lsf.write_output(f'  - {url}')
    
    if dashboard:
        if failed:
            dashboard.update_task('urls', 'url_checks', 'failed', f'{len(failed)} URL(s) failed')
        else:
            dashboard.update_task('urls', 'url_checks', 'complete')
        dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom URL checks here
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
