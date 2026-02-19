#!/usr/bin/env python3
# ESXi.py - HOLFY27 Core ESXi Host Verification Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Verifies ESXi hosts are online and responsive

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

MODULE_NAME = 'ESXi'
MODULE_DESCRIPTION = 'ESXi host verification'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for ESXi module
    
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
        dashboard.update_task('esxi', 'host_check', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # Get ESXi Hosts from Config
    #==========================================================================
    
    # Use get_config_list to properly filter commented-out values
    esx_hosts = lsf.get_config_list('RESOURCES', 'ESXiHosts')
    
    if not esx_hosts:
        lsf.write_output('No ESXi hosts configured in config.ini')
        if dashboard:
            dashboard.update_task('esxi', 'host_check', TaskStatus.SKIPPED, 'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
            dashboard.update_task('esxi', 'host_ports', TaskStatus.SKIPPED, 'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
        return
    
    lsf.write_output(f'ESXi hosts to check: {len(esx_hosts)}')
    lsf.write_vpodprogress('Checking ESXi hosts', 'GOOD-3')
    
    #==========================================================================
    # Check Each ESXi Host
    #==========================================================================
    
    failed_hosts = []
    successful_hosts = []
    maintenance_mode_hosts = []
    
    for entry in esx_hosts:
        # Parse host:maintenance_mode format
        if ':' in entry:
            parts = entry.split(':')
            host = parts[0].strip()
            mm_flag = parts[1].strip().lower() if len(parts) > 1 else 'no'
        else:
            host = entry.strip()
            mm_flag = 'no'
        
        if mm_flag == 'yes':
            maintenance_mode_hosts.append(host)
            lsf.write_output(f'Host {host} configured to stay in maintenance mode')
        
        if dry_run:
            lsf.write_output(f'Would check ESXi host: {host}')
            continue
        
        lsf.write_output(f'Checking ESXi host: {host}')
        
        # Retry logic with timeout awareness
        max_retries = 10
        retry_delay = 30
        success = False
        
        for attempt in range(max_retries):
            if lsf.test_ping(host):
                lsf.write_output(f'ESXi host responding: {host}')
                success = True
                successful_hosts.append(host)
                break
            else:
                lsf.write_output(f'ESXi host not responding (attempt {attempt + 1}/{max_retries}): {host}')
                lsf.labstartup_sleep(retry_delay)
        
        if not success:
            lsf.write_output(f'FAIL: ESXi host not responding after {max_retries} attempts: {host}')
            failed_hosts.append(host)
            
            # For HOL labs, fail on host timeout
            if lsf.labtype == 'HOL':
                lsf.write_vpodprogress(f'{host} TIMEOUT', 'TIMEOUT')
                if dashboard:
                    dashboard.update_task('esxi', 'host_check', TaskStatus.FAILED, f'{host} not responding')
                    dashboard.generate_html()
                return
    
    #==========================================================================
    # Report Results
    #==========================================================================
    
    if not dry_run:
        lsf.write_output(f'ESXi check results: {len(successful_hosts)} OK, {len(failed_hosts)} failed')
        
        if maintenance_mode_hosts:
            lsf.write_output(f'Hosts staying in maintenance mode: {maintenance_mode_hosts}')
    
    if dashboard:
        total_hosts = len(esx_hosts)
        if failed_hosts:
            dashboard.update_task('esxi', 'host_check', TaskStatus.FAILED, 
                                  f'{len(failed_hosts)} host(s) not responding',
                                  total=total_hosts, success=len(successful_hosts), failed=len(failed_hosts))
        else:
            dashboard.update_task('esxi', 'host_check', TaskStatus.COMPLETE,
                                  total=total_hosts, success=len(successful_hosts), failed=0)
    
    #==========================================================================
    # Check ESXi Management Ports
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('esxi', 'host_ports', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    if not esx_hosts:
        if dashboard:
            dashboard.update_task('esxi', 'host_ports', TaskStatus.SKIPPED, 'No hosts configured',
                                  total=0, success=0, failed=0, skipped=0)
    elif failed_hosts:
        # Skip port checks if hosts failed connectivity
        if dashboard:
            dashboard.update_task('esxi', 'host_ports', TaskStatus.SKIPPED, 
                                  'Skipped due to host connectivity failures',
                                  total=len(esx_hosts), success=0, failed=0, skipped=len(esx_hosts))
    else:
        lsf.write_output('Checking ESXi management ports (443, 902)...')
        
        port_failed_hosts = []
        port_successful_hosts = []
        port_failure_details = {}  # Track which ports failed for each host
        
        # Port check configuration
        ports_to_check = [443, 902]  # HTTPS/vSphere Client, vSphere Management
        max_port_retries = 10
        port_retry_delay = 30
        
        for entry in esx_hosts:
            # Parse host:maintenance_mode format
            if ':' in entry:
                parts = entry.split(':')
                host = parts[0].strip()
            else:
                host = entry.strip()
            
            if dry_run:
                lsf.write_output(f'Would check ports on: {host}')
                continue
            
            lsf.write_output(f'Checking management ports on {host}...')
            
            # Check each port with retry logic
            failed_ports = []
            for port in ports_to_check:
                port_ok = False
                
                for attempt in range(max_port_retries):
                    if lsf.test_tcp_port(host, port, timeout=10):
                        port_ok = True
                        break
                    else:
                        if attempt < max_port_retries - 1:
                            lsf.write_output(f'  Port {port} not responding (attempt {attempt + 1}/{max_port_retries}), retrying...')
                            lsf.labstartup_sleep(port_retry_delay)
                
                if not port_ok:
                    failed_ports.append(str(port))
                    lsf.write_output(f'  Port {port} FAILED after {max_port_retries} attempts')
                else:
                    lsf.write_output(f'  Port {port} OK')
            
            # Determine overall result for this host
            if not failed_ports:
                lsf.write_output(f'ESXi management ports OK: {host} (all ports responding)')
                port_successful_hosts.append(host)
            else:
                port_failure_details[host] = failed_ports
                lsf.write_output(f'ESXi port check FAILED: {host} (ports {", ".join(failed_ports)} not responding after {max_port_retries} attempts)')
                port_failed_hosts.append(host)
        
        # Report port check results
        if not dry_run:
            lsf.write_output(f'Port check results: {len(port_successful_hosts)} OK, {len(port_failed_hosts)} failed')
            if port_failed_hosts:
                lsf.write_output('Failed hosts and ports:')
                for host in port_failed_hosts:
                    lsf.write_output(f'  {host}: ports {", ".join(port_failure_details[host])}')
        
        if dashboard:
            if port_failed_hosts:
                # Create detailed message showing which hosts and ports failed
                failure_summary = '; '.join([f'{host} (ports {", ".join(port_failure_details[host])})' 
                                             for host in port_failed_hosts])
                dashboard.update_task('esxi', 'host_ports', TaskStatus.FAILED,
                                      f'{len(port_failed_hosts)} host(s) failed: {failure_summary}')
            else:
                dashboard.update_task('esxi', 'host_ports', TaskStatus.COMPLETE,
                                      f'{len(port_successful_hosts)} hosts OK')
            dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom ESXi host checks or configuration here
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
    
    # Handle legacy arguments from labstartup.py
    if args.run_seconds > 0:
        import datetime
        lsf.start_time = datetime.datetime.now() - datetime.timedelta(seconds=args.run_seconds)
    
    if args.labcheck == 'True':
        lsf.labcheck = True
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'LabType: {lsf.labtype}')
        print(f'Dry run: {args.dry_run}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run)
