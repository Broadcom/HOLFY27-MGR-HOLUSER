#!/usr/bin/env python3
# services.py - HOLFY27 Core Services Management Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Manages Linux services and TCP port verification

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

MODULE_NAME = 'services'
MODULE_DESCRIPTION = 'Service management and TCP verification'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for services module
    
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
        dashboard.update_task('services', 'linux_services', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Manage Linux Services
    #==========================================================================
    
    linux_services = []
    if lsf.config.has_option('RESOURCES', 'LinuxServices'):
        linux_raw = lsf.config.get('RESOURCES', 'LinuxServices')
        if '\n' in linux_raw:
            linux_services = [s.strip() for s in linux_raw.split('\n') if s.strip()]
        else:
            linux_services = [s.strip() for s in linux_raw.split(',') if s.strip()]
    
    if linux_services:
        lsf.write_vpodprogress('Manage Linux Services', 'GOOD-6')
        lsf.write_output('Starting Linux services')
        
        for entry in linux_services:
            if dry_run:
                lsf.write_output(f'Would start service: {entry}')
                continue
            
            # Parse host:service:wait_seconds format
            parts = entry.split(':')
            if len(parts) < 2:
                lsf.write_output(f'Invalid service entry: {entry}')
                continue
            
            host = parts[0].strip()
            service = parts[1].strip()
            wait_sec = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else 5
            
            action = 'start'
            max_retries = 5
            
            for attempt in range(max_retries):
                lsf.write_output(f'Starting {service} on {host}...')
                
                try:
                    result = lsf.managelinuxservice(action, host, service, wait_sec, lsf.password)
                    
                    if hasattr(result, 'stdout') and result.stdout:
                        output = result.stdout.lower()
                        if 'running' in output or 'started' in output:
                            lsf.write_output(f'Service {service} started on {host}')
                            break
                except Exception as e:
                    lsf.write_output(f'Error starting {service} on {host}: {e}')
                
                lsf.labstartup_sleep(lsf.sleep_seconds)
        
        lsf.write_output('Finished starting Linux services')
    
    if dashboard:
        dashboard.update_task('services', 'linux_services', TaskStatus.COMPLETE)
        dashboard.update_task('services', 'tcp_ports', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 2: Verify TCP Services
    #==========================================================================
    
    tcp_services = []
    if lsf.config.has_option('RESOURCES', 'TCPServices'):
        tcp_raw = lsf.config.get('RESOURCES', 'TCPServices')
        if '\n' in tcp_raw:
            tcp_services = [s.strip() for s in tcp_raw.split('\n') if s.strip()]
        else:
            tcp_services = [s.strip() for s in tcp_raw.split(',') if s.strip()]
    
    if tcp_services:
        lsf.write_vpodprogress('Testing TCP Ports', 'GOOD-6')
        lsf.write_output('Testing TCP ports')
        
        for entry in tcp_services:
            if dry_run:
                lsf.write_output(f'Would test TCP port: {entry}')
                continue
            
            # Parse host:port format
            parts = entry.split(':')
            if len(parts) < 2:
                lsf.write_output(f'Invalid TCP entry: {entry}')
                continue
            
            host = parts[0].strip()
            port = parts[1].strip()
            
            while not lsf.test_tcp_port(host, port):
                lsf.write_output(f'Waiting for {host}:{port}...')
                lsf.labstartup_sleep(lsf.sleep_seconds)
            
            lsf.write_output(f'TCP port {host}:{port} is responding')
        
        lsf.write_output('Finished testing TCP ports')
    
    if dashboard:
        dashboard.update_task('services', 'tcp_ports', TaskStatus.COMPLETE)
        dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom service management or checks here
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
