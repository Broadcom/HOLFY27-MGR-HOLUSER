#!/usr/bin/env python3
# Kubernetes.py - HOLFY27 Core Kubernetes Certificate Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Kubernetes certificate verification and renewal

import os
import sys
import argparse
import logging
import datetime
import io

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(level=logging.WARNING)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'Kubernetes'
MODULE_DESCRIPTION = 'Kubernetes certificate verification'

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def check_kubernetes_certs(lsf, entry, dry_run=False):
    """
    Evaluate Kubernetes SSL certificates and renew if needed
    
    :param lsf: lsfunctions module
    :param entry: host:account:renewcommand format
    :param dry_run: Whether to skip actual changes
    """
    renew = False
    now = datetime.datetime.now()
    tolerance_days = 5
    tolerance = datetime.timedelta(days=tolerance_days)
    
    # Parse entry
    parts = entry.split(':')
    if len(parts) < 3:
        lsf.write_output(f'Invalid Kubernetes entry: {entry}')
        return
    
    remotehost = parts[0].strip()
    account = parts[1].strip()
    renewcommand = parts[2].strip()
    
    lsf.write_output(f'Checking Kubernetes SSL certificates on {remotehost}...')
    
    if dry_run:
        lsf.write_output(f'Would check certs on {account}@{remotehost}')
        return
    
    # Create check script
    scriptname = 'checkcerts.sh'
    checkscriptfile = f'/tmp/{scriptname}'
    checkcmd = 'for i in $(ls /etc/kubernetes/pki/*.crt 2>/dev/null); do openssl x509 -text -noout -in $i | grep "Not After"; done'
    
    with io.open(checkscriptfile, 'w', newline='\n') as lf:
        lf.write(f'{checkcmd}\n')
    
    # Copy and run check script
    lsf.scp(checkscriptfile, f'{account}@{remotehost}:{scriptname}', lsf.password)
    output = lsf.ssh(f'/bin/bash ./{scriptname} 2>&1', f'{account}@{remotehost}', lsf.password)
    
    if not hasattr(output, 'stdout') or not output.stdout:
        lsf.write_output(f'No certificate output from {remotehost}')
        return
    
    # Parse results
    first_expiry = None
    
    for line in output.stdout.split('\n'):
        if 'Not After' not in line:
            continue
        
        try:
            # Parse: "Not After : Month Day HH:MM:SS Year Timezone"
            parts = line.split(':', 1)
            if len(parts) < 2:
                continue
            
            date_str = parts[1].strip()
            # Try multiple date formats
            for fmt in ['%b %d %H:%M:%S %Y %Z', '%b  %d %H:%M:%S %Y %Z']:
                try:
                    expiration = datetime.datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                lsf.write_output(f'Could not parse date: {date_str}')
                continue
            
            time_diff = expiration - now
            
            if first_expiry is None or time_diff < first_expiry:
                first_expiry = time_diff
            
            if time_diff < tolerance:
                lsf.write_output(f'Certificate expires soon ({time_diff.days} days)!')
                renew = True
            else:
                lsf.write_output(f'Certificate expires in {time_diff.days} days')
        
        except Exception as e:
            lsf.write_output(f'Error parsing certificate date: {e}')
    
    # Renew if needed
    if renew:
        lsf.write_output(f'Renewing Kubernetes certificates on {remotehost}...')
        try:
            output = lsf.ssh(renewcommand, f'{account}@{remotehost}', lsf.password)
            if hasattr(output, 'stdout'):
                lsf.write_output(output.stdout)
        except Exception as e:
            lsf.write_output(f'Certificate renewal failed: {e}')
    elif first_expiry:
        lsf.write_output(f'Kubernetes certificates valid for {first_expiry.days} days on {remotehost}')


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for Kubernetes module
    
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
        dashboard.update_task('kubernetes', 'cert_check', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    lsf.write_vpodprogress('Checking Kubernetes', 'GOOD-6')
    
    #==========================================================================
    # Get Kubernetes Entries from Config
    #==========================================================================
    
    kubernetes = []
    if lsf.config.has_option('RESOURCES', 'Kubernetes'):
        k8s_raw = lsf.config.get('RESOURCES', 'Kubernetes')
        if '\n' in k8s_raw:
            kubernetes = [k.strip() for k in k8s_raw.split('\n') if k.strip()]
        else:
            kubernetes = [k.strip() for k in k8s_raw.split(',') if k.strip()]
    
    if not kubernetes:
        lsf.write_output('No Kubernetes entries configured')
        if dashboard:
            dashboard.update_task('kubernetes', 'cert_check', TaskStatus.SKIPPED)
            dashboard.generate_html()
        return
    
    #==========================================================================
    # Check Each Kubernetes Cluster
    #==========================================================================
    
    for entry in kubernetes:
        check_kubernetes_certs(lsf, entry, dry_run)
    
    if dashboard:
        dashboard.update_task('kubernetes', 'cert_check', TaskStatus.COMPLETE)
        dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom Kubernetes configuration or checks here
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
        lsf.start_time = datetime.datetime.now() - datetime.timedelta(seconds=args.run_seconds)
    
    if args.labcheck == 'True':
        lsf.labcheck = True
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'Dry run: {args.dry_run}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run)
