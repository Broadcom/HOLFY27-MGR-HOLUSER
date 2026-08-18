#!/usr/bin/env python3
# services.py - HOLFY27 Core Services Management Module
# Version 3.1 - 2026-08-18
# Author - Burke Azbill and HOL Core Team
# Manages Linux services and TCP port verification
#
# v3.1 Changes (2026-08-18):
# - TASK 2: Parallelized TCP port testing across configured TCPServices endpoints
#   using ThreadPoolExecutor, eliminating sequential wait loops.

import os
import sys
import argparse
import logging

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(
    level=logging.WARNING,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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
    
    # Use get_config_list to properly filter commented-out values
    linux_services = lsf.get_config_list('RESOURCES', 'LinuxServices')
    
    linux_succeeded = []
    linux_failed = []
    
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
            
            service_started = False
            for attempt in range(max_retries):
                lsf.write_output(f'Starting {service} on {host}...')
                
                try:
                    result = lsf.managelinuxservice(action, host, service, wait_sec, lsf.password)
                    
                    if hasattr(result, 'stdout') and result.stdout:
                        output = result.stdout.lower()
                        if 'running' in output or 'started' in output:
                            lsf.write_output(f'Service {service} started on {host}')
                            service_started = True
                            break
                except Exception as e:
                    lsf.write_output(f'Error starting {service} on {host}: {e}')
                
                lsf.labstartup_sleep(lsf.sleep_seconds)
            
            if service_started:
                linux_succeeded.append(f'{service}@{host}')
            else:
                linux_failed.append(f'{service}@{host}')
        
        lsf.write_output(f'Finished starting Linux services: {len(linux_succeeded)} succeeded, {len(linux_failed)} failed')
    
    if dashboard:
        if linux_services:
            total_linux = len(linux_services)
            if linux_failed:
                dashboard.update_task('services', 'linux_services', TaskStatus.FAILED,
                                      f'{len(linux_failed)} service(s) failed to start',
                                      total=total_linux, success=len(linux_succeeded), failed=len(linux_failed))
            else:
                dashboard.update_task('services', 'linux_services', TaskStatus.COMPLETE,
                                      total=total_linux, success=len(linux_succeeded), failed=0)
        else:
            dashboard.update_task('services', 'linux_services', TaskStatus.SKIPPED,
                                  'No Linux services configured',
                                  total=0, success=0, failed=0, skipped=0)
        dashboard.update_task('services', 'tcp_ports', TaskStatus.RUNNING)
    
    #==========================================================================
    # TASK 2: Verify TCP Services
    #==========================================================================
    
    # Use get_config_list to properly filter commented-out values
    tcp_services = lsf.get_config_list('RESOURCES', 'TCPServices')
    
    tcp_succeeded = []
    tcp_failed = []
    
    if tcp_services:
        lsf.write_vpodprogress('Testing TCP Ports', 'GOOD-6')
        lsf.write_output('Testing TCP ports')
        
        if dry_run:
            for entry in tcp_services:
                lsf.write_output(f'Would test TCP port: {entry}')
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import time as _svc_time

            def _test_tcp_service_entry(entry):
                parts = entry.split(':')
                if len(parts) < 2:
                    return entry, False, f'Invalid TCP entry: {entry}'
                
                host = parts[0].strip()
                try:
                    port = int(parts[1].strip())
                except ValueError:
                    return entry, False, f'Invalid port in TCP entry: {entry}'
                
                max_retries = 20
                for attempt in range(max_retries):
                    if lsf.test_tcp_port(host, port, timeout=5):
                        return f'{host}:{port}', True, f'TCP port {host}:{port} is responding'
                    if attempt < max_retries - 1:
                        _svc_time.sleep(lsf.sleep_seconds)
                return f'{host}:{port}', False, f'TCP port {host}:{port} failed after {max_retries} attempts'

            max_workers = min(10, max(1, len(tcp_services)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_test_tcp_service_entry, entry): entry for entry in tcp_services}
                for future in as_completed(futures):
                    target_str, success, msg = future.result()
                    lsf.write_output(msg)
                    if success:
                        tcp_succeeded.append(target_str)
                    else:
                        tcp_failed.append(target_str)
        
        lsf.write_output(f'Finished testing TCP ports: {len(tcp_succeeded)} succeeded, {len(tcp_failed)} failed')
    
    if dashboard:
        if tcp_services:
            total_tcp = len(tcp_services)
            if tcp_failed:
                dashboard.update_task('services', 'tcp_ports', TaskStatus.FAILED,
                                      f'{len(tcp_failed)} port(s) not responding',
                                      total=total_tcp, success=len(tcp_succeeded), failed=len(tcp_failed))
            else:
                dashboard.update_task('services', 'tcp_ports', TaskStatus.COMPLETE,
                                      total=total_tcp, success=len(tcp_succeeded), failed=0)
        else:
            dashboard.update_task('services', 'tcp_ports', TaskStatus.SKIPPED,
                                  'No TCP services configured',
                                  total=0, success=0, failed=0, skipped=0)
    
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
    
    if linux_failed or tcp_failed:
        return False


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
