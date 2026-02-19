#!/usr/bin/env python3
# Shutdown.py - HOLFY27 Lab Shutdown Orchestration
# Version 2.1 - February 2026
# Author - Burke Azbill and HOL Core Team
# Main shutdown script for graceful lab environment shutdown
#
# v 2.1 Changes:
# - Fixed --phase parameter: now correctly runs only the targeted VCF
#   shutdown phase instead of all phases from that point onward
# - Outer orchestrator phases (Docker Containers, Final Cleanup, Wait for
#   ESXi) are now skipped when --phase targets a single VCF phase
#
# v 2.0 Changes:
# - Added --phase parameter for single-phase VCF shutdown execution
# - Added VCF 9.0/9.1 dual-version support via VCFshutdown.py
# - Updated CLI help with phase ID list and examples

"""
Lab Shutdown Orchestration Script

This script provides a graceful, orderly shutdown of the HOL lab environment.
The shutdown order is the REVERSE of the startup order to ensure all
dependencies are properly handled.

Startup Order (labstartup.py):
1. prelim.py - Preliminary checks and services
2. ESXi.py - ESXi host startup
3. vSphere.py - vSphere infrastructure  
4. VCF.py - VCF management (NSX, vCenter)
5. services.py - Core services
6. Kubernetes.py - Kubernetes clusters
7. VCFfinal.py - Final VCF tasks (Tanzu, VCF Automation)
8. final.py - Final cleanup

Shutdown Order (this script):
1. Fleet Operations - VCF Operations Suite via SDDC Manager
2. Kubernetes/Tanzu workloads
3. VCF Final components (VCF Automation VMs)
4. Core services
5. VCF Management (vCenter, NSX)
6. vSphere infrastructure
7. ESXi hosts (with vSAN elevator)

Usage:
    python3 Shutdown.py                    # Full shutdown
    python3 Shutdown.py --dry-run          # Preview without changes
    python3 Shutdown.py --phase 1          # Run a single VCF shutdown phase
    python3 Shutdown.py --phase 1 --dry-run  # Preview a single phase
    python3 Shutdown.py --help             # Show help

Configuration:
    The script reads from /tmp/config.ini for lab-specific settings.
    Custom shutdown configuration can be added in the [SHUTDOWN] section.
"""

import os
import sys
import argparse
import logging
import time
import datetime

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')
sys.path.insert(0, '/home/holuser/hol/Shutdown')

# Default logging level
logging.basicConfig(
    level=logging.WARNING,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

#==============================================================================
# SCRIPT CONFIGURATION
#==============================================================================

SCRIPT_NAME = 'Shutdown'
SCRIPT_VERSION = '2.1'
SCRIPT_DESCRIPTION = 'HOLFY27 Lab Shutdown Orchestration'

# Log files
SHUTDOWN_LOG = '/home/holuser/hol/shutdown.log'
LABSTARTUP_LOG = '/home/holuser/hol/labstartup.log'

# Status file for console display
STATUS_FILE = '/lmchol/hol/startup_status.txt'

# Valid lab types that use VCF shutdown procedure
VCF_LAB_TYPES = ['VCF', 'HOL', 'DISCOVERY', 'ATE', 'VXP', 'EDU', 'NINJA']

#==============================================================================
# LOGGING FUNCTIONS
#==============================================================================

def init_shutdown_log(dry_run: bool = False):
    """
    Initialize the shutdown log file.
    Re-initializes labstartup.log with shutdown header.
    
    :param dry_run: If True, skip log initialization
    """
    if dry_run:
        return
    
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    header = f'Lab Shutdown: {timestamp}\n'
    
    # Re-initialize labstartup.log with shutdown header
    try:
        with open(LABSTARTUP_LOG, 'w') as f:
            f.write(header)
            f.write('=' * 70 + '\n')
    except Exception as e:
        print(f'Warning: Could not initialize {LABSTARTUP_LOG}: {e}')
    
    # Also initialize shutdown.log
    try:
        with open(SHUTDOWN_LOG, 'w') as f:
            f.write(header)
            f.write('=' * 70 + '\n')
    except Exception as e:
        print(f'Warning: Could not initialize {SHUTDOWN_LOG}: {e}')


def update_status(status: str, dry_run: bool = False):
    """
    Update the startup_status.txt file with current shutdown status.
    This is displayed on the console desktop widget.
    
    :param status: Status text to write
    :param dry_run: If True, skip status update
    """
    if dry_run:
        return
    
    try:
        # Ensure directory exists
        status_dir = os.path.dirname(STATUS_FILE)
        if status_dir and not os.path.exists(status_dir):
            os.makedirs(status_dir, exist_ok=True)
        
        with open(STATUS_FILE, 'w') as f:
            f.write(status)
    except Exception as e:
        print(f'Warning: Could not update status file: {e}')


def write_shutdown_output(msg: str, lsf=None):
    """
    Write output to both console and shutdown log file.
    
    :param msg: Message to write
    :param lsf: Optional lsfunctions module for additional logging (NFS locations)
    """
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted_msg = f'[{timestamp}] {msg}'
    
    # Print to console
    print(formatted_msg)
    
    # Write to shutdown log
    try:
        with open(SHUTDOWN_LOG, 'a') as f:
            f.write(formatted_msg + '\n')
    except Exception:
        pass
    
    # Write to labstartup log (local copy)
    try:
        with open(LABSTARTUP_LOG, 'a') as f:
            f.write(formatted_msg + '\n')
    except Exception:
        pass
    
    # Note: We don't call lsf.write_output() here to avoid duplicate entries
    # since lsf.write_output() also writes to labstartup.log
    # The NFS copy (/lmchol/hol/labstartup.log) is handled by lsf.logfiles


#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def print_banner(lsf):
    """Print shutdown script banner"""
    banner = f"""
================================================================================
    {SCRIPT_DESCRIPTION}
    Version {SCRIPT_VERSION} - February 2026
================================================================================
"""
    write_shutdown_output(banner, lsf)


def print_phase_header(lsf, phase_num: int, phase_name: str, dry_run: bool = False):
    """Print a phase header and update status file"""
    write_shutdown_output('', lsf)
    write_shutdown_output('=' * 70, lsf)
    write_shutdown_output(f'PHASE {phase_num}: {phase_name}', lsf)
    write_shutdown_output('=' * 70, lsf)
    
    # Update status file for console display
    update_status(f'Shutdown Phase {phase_num}: {phase_name}', dry_run)


def import_shutdown_module(module_name: str, lsf):
    """
    Dynamically import and run a shutdown module
    
    :param module_name: Name of the module (without .py)
    :param lsf: lsfunctions module
    :return: Module or None if not found
    """
    module_path = f'/home/holuser/hol/Shutdown/{module_name}.py'
    
    if not os.path.isfile(module_path):
        lsf.write_output(f'Shutdown module not found: {module_name}')
        return None
    
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        lsf.write_output(f'Failed to import {module_name}: {e}')
        return None


#==============================================================================
# SHUTDOWN PHASES
#==============================================================================

def shutdown_docker_containers(lsf, dry_run: bool = False) -> bool:
    """
    Shutdown Docker containers on remote Docker host
    
    :param lsf: lsfunctions module
    :param dry_run: Preview mode
    :return: Success status
    """
    docker_host = None
    docker_user = 'holuser'
    container_list = []
    
    if lsf.config.has_option('SHUTDOWN', 'docker_host'):
        docker_host = lsf.config.get('SHUTDOWN', 'docker_host')
    else:
        docker_host = 'docker.site-a.vcf.lab'
    
    if lsf.config.has_option('SHUTDOWN', 'docker_user'):
        docker_user = lsf.config.get('SHUTDOWN', 'docker_user')
    
    if lsf.config.has_option('SHUTDOWN', 'docker_containers'):
        containers_raw = lsf.config.get('SHUTDOWN', 'docker_containers')
        container_list = [c.strip() for c in containers_raw.split(',') if c.strip()]
    else:
        container_list = ['gitlab', 'ldap', 'poste.io', 'flask']
    
    if not lsf.test_tcp_port(docker_host, 22, timeout=5):
        write_shutdown_output(f'Docker host {docker_host} not reachable, skipping', lsf)
        return True
    
    password = lsf.get_password()
    
    for container in container_list:
        if dry_run:
            write_shutdown_output(f'Would stop container: {container}', lsf)
        else:
            write_shutdown_output(f'Stopping container: {container}', lsf)
            try:
                lsf.ssh(f'docker stop {container}', f'{docker_user}@{docker_host}', password)
            except Exception as e:
                write_shutdown_output(f'Warning: Failed to stop {container}: {e}', lsf)
    
    return True


def run_vcf_shutdown(lsf, dry_run: bool = False, phase=None) -> dict:
    """
    Run the VCF shutdown module
    
    :param lsf: lsfunctions module
    :param dry_run: Preview mode
    :param phase: If set, run only this specific VCF shutdown phase
    :return: Dictionary with 'success' status and 'esx_hosts' list
    """
    module = import_shutdown_module('VCFshutdown', lsf)
    
    if module is None:
        lsf.write_output('VCFshutdown module not available')
        return {'success': False, 'esx_hosts': []}
    
    try:
        result = module.main(lsf=lsf, dry_run=dry_run, phase=phase)
        if isinstance(result, dict):
            return result
        else:
            return {'success': result, 'esx_hosts': []}
    except Exception as e:
        lsf.write_output(f'VCFshutdown failed: {e}')
        return {'success': False, 'esx_hosts': []}


def wait_for_hosts_poweroff(lsf, hosts: list, dry_run: bool = False,
                            max_wait: int = 1800, poll_interval: int = 15):
    """
    Wait for ESXi hosts to stop responding to pings (fully powered off)
    
    :param lsf: lsfunctions module
    :param hosts: List of host FQDNs/IPs to monitor
    :param dry_run: Preview mode
    :param max_wait: Maximum wait time in seconds (default 30 minutes)
    :param poll_interval: Time between ping attempts in seconds (default 15)
    :return: True if all hosts are offline, False if timeout
    """
    import time
    
    if not hosts:
        write_shutdown_output('No ESXi hosts to monitor for power off', lsf)
        return True
    
    if dry_run:
        write_shutdown_output(f'Would wait for {len(hosts)} host(s) to power off: {hosts}', lsf)
        return True
    
    write_shutdown_output('', lsf)
    write_shutdown_output('ESXi hosts are powering off...', lsf)
    write_shutdown_output(f'Monitoring {len(hosts)} host(s) for up to {max_wait // 60} minutes', lsf)
    update_status('Waiting for ESXi Hosts to Power Off', dry_run)
    
    start_time = time.time()
    hosts_remaining = set(hosts)
    
    while (time.time() - start_time) < max_wait:
        elapsed = int(time.time() - start_time)
        hosts_still_up = set()
        
        for host in hosts_remaining:
            # Extract hostname if it contains additional info (like user:pass)
            hostname = host.split(':')[0] if ':' in host else host
            
            if lsf.test_ping(hostname, count=1, timeout=5):
                hosts_still_up.add(host)
        
        # Report any newly offline hosts
        newly_offline = hosts_remaining - hosts_still_up
        for host in newly_offline:
            hostname = host.split(':')[0] if ':' in host else host
            write_shutdown_output(f'  {hostname}: Powered off (elapsed: {elapsed}s)', lsf)
        
        hosts_remaining = hosts_still_up
        
        if not hosts_remaining:
            elapsed = int(time.time() - start_time)
            write_shutdown_output(f'All ESXi hosts powered off after {elapsed} seconds', lsf)
            return True
        
        # Show status
        remaining_time = max_wait - elapsed
        write_shutdown_output(f'  {len(hosts_remaining)} host(s) still running... '
                            f'(elapsed: {elapsed}s, remaining: {remaining_time}s)', lsf)
        
        time.sleep(poll_interval)
    
    # Timeout - report which hosts are still up
    elapsed = int(time.time() - start_time)
    write_shutdown_output(f'Timeout after {elapsed}s - {len(hosts_remaining)} host(s) still responding:', lsf)
    for host in hosts_remaining:
        hostname = host.split(':')[0] if ':' in host else host
        write_shutdown_output(f'  - {hostname}', lsf)
    
    return False


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, dry_run: bool = False, skip_vsan_wait: bool = False,
         skip_host_shutdown: bool = False, phase=None):
    """
    Main shutdown orchestration function
    
    :param lsf: lsfunctions module (will be imported if None)
    :param dry_run: Preview mode - show what would be done
    :param skip_vsan_wait: Skip the vSAN elevator wait period
    :param skip_host_shutdown: Skip ESXi host shutdown
    :param phase: If set, run only this specific VCF shutdown phase (e.g., '1', '13')
    """
    start_time = datetime.datetime.now()
    
    # Initialize log files (re-initialize labstartup.log with shutdown header)
    init_shutdown_log(dry_run)
    
    if lsf is None:
        import lsfunctions as lsf
        lsf.init(router=False)
    
    # Enable console output for real-time feedback during shutdown
    lsf.console_output = True
    
    # Set initial status
    update_status('Shutting Down', dry_run)
    
    print_banner(lsf)
    
    write_shutdown_output(f'Shutdown started at: {start_time.strftime("%Y-%m-%d %H:%M:%S")}', lsf)
    write_shutdown_output(f'Lab SKU: {lsf.lab_sku}', lsf)
    write_shutdown_output(f'Dry run mode: {dry_run}', lsf)
    
    if dry_run:
        write_shutdown_output('', lsf)
        write_shutdown_output('*** DRY RUN MODE - No changes will be made ***', lsf)
        write_shutdown_output('', lsf)
    
    #==========================================================================
    # Phase 0: Pre-shutdown checks
    #==========================================================================
    
    print_phase_header(lsf, 0, 'Pre-Shutdown Checks', dry_run)
    
    # Check if config.ini exists
    if not os.path.isfile(lsf.configini):
        write_shutdown_output('WARNING: config.ini not found - using defaults', lsf)
    else:
        write_shutdown_output(f'Config file: {lsf.configini}', lsf)
    
    # Determine lab type
    lab_type = 'VCF'  # Default
    if lsf.config.has_option('VPOD', 'labtype'):
        lab_type = lsf.config.get('VPOD', 'labtype').upper()
    
    write_shutdown_output(f'Lab type: {lab_type}', lsf)
    
    #==========================================================================
    # Phase 1: Docker Containers (Optional) - skip when --phase targets a VCF phase
    #==========================================================================
    
    if phase is None:
        print_phase_header(lsf, 1, 'Docker Containers', dry_run)
        
        if lsf.config.has_option('SHUTDOWN', 'shutdown_docker'):
            if lsf.config.getboolean('SHUTDOWN', 'shutdown_docker'):
                shutdown_docker_containers(lsf, dry_run)
            else:
                write_shutdown_output('Docker shutdown disabled in config', lsf)
        else:
            docker_host = 'docker.site-a.vcf.lab'
            if lsf.config.has_option('SHUTDOWN', 'docker_host'):
                docker_host = lsf.config.get('SHUTDOWN', 'docker_host')
            
            if lsf.test_tcp_port(docker_host, 22, timeout=5):
                shutdown_docker_containers(lsf, dry_run)
            else:
                write_shutdown_output(f'Docker host {docker_host} not reachable, skipping', lsf)
    
    #==========================================================================
    # Phase 2: VCF Shutdown (Main)
    #==========================================================================
    
    print_phase_header(lsf, 2, 'VCF Environment Shutdown', dry_run)
    
    # Track ESXi hosts for power-off monitoring
    esx_hosts = []
    vcf_result = {'success': False, 'esx_hosts': []}
    
    # Check if lab type uses VCF shutdown procedure
    # VCF_LAB_TYPES includes: VCF, HOL, DISCOVERY, ATE, VXP, EDU, NINJA
    if lab_type.upper() in VCF_LAB_TYPES:
        write_shutdown_output(f'Lab type {lab_type} uses VCF shutdown procedure', lsf)
        
        # Temporarily override vSAN wait if requested
        if skip_vsan_wait and not dry_run:
            if not lsf.config.has_section('SHUTDOWN'):
                lsf.config.add_section('SHUTDOWN')
            lsf.config.set('SHUTDOWN', 'vsan_timeout', '0')
        
        if skip_host_shutdown and not dry_run:
            if not lsf.config.has_section('SHUTDOWN'):
                lsf.config.add_section('SHUTDOWN')
            lsf.config.set('SHUTDOWN', 'shutdown_hosts', 'false')
        
        vcf_result = run_vcf_shutdown(lsf, dry_run, phase=phase)
    elif lab_type.upper() == 'VVF':
        write_shutdown_output('VVF lab type - using VVF shutdown procedure', lsf)
        vcf_result = run_vcf_shutdown(lsf, dry_run, phase=phase)
    else:
        write_shutdown_output(f'Lab type {lab_type} - using default VCF shutdown procedure', lsf)
        vcf_result = run_vcf_shutdown(lsf, dry_run, phase=phase)
    
    # Extract ESXi hosts list from VCF shutdown result
    esx_hosts = vcf_result.get('esx_hosts', [])
    
    #==========================================================================
    # Phase 3: Final Cleanup - skip when --phase targets a single VCF phase
    #==========================================================================
    
    if phase is None:
        print_phase_header(lsf, 3, 'Final Cleanup', dry_run)
        
        write_shutdown_output('Disconnecting vSphere sessions...', lsf)
        if not dry_run:
            from pyVim import connect
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()
        
        #======================================================================
        # Phase 4: Wait for ESXi Hosts to Power Off
        #======================================================================
        
        if esx_hosts and not skip_host_shutdown:
            print_phase_header(lsf, 4, 'Wait for ESXi Host Power Off', dry_run)
            wait_for_hosts_poweroff(lsf, esx_hosts, dry_run)
    
    #==========================================================================
    # Summary
    #==========================================================================
    
    end_time = datetime.datetime.now()
    elapsed = end_time - start_time
    
    write_shutdown_output('', lsf)
    write_shutdown_output('=' * 70, lsf)
    write_shutdown_output('SHUTDOWN COMPLETE', lsf)
    write_shutdown_output('=' * 70, lsf)
    write_shutdown_output(f'Start time: {start_time.strftime("%Y-%m-%d %H:%M:%S")}', lsf)
    write_shutdown_output(f'End time: {end_time.strftime("%Y-%m-%d %H:%M:%S")}', lsf)
    write_shutdown_output(f'Elapsed: {str(elapsed).split(".")[0]}', lsf)
    write_shutdown_output('', lsf)
    
    if dry_run:
        write_shutdown_output('*** DRY RUN COMPLETE - No changes were made ***', lsf)
    else:
        write_shutdown_output('Lab environment has been shut down.', lsf)
        write_shutdown_output('Please manually shutdown your manager, router, and console.', lsf)
        # Set final status
        update_status('Shutdown Complete', dry_run)
    
    return True


#==============================================================================
# COMMAND LINE INTERFACE
#==============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=SCRIPT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 Shutdown.py                    # Full shutdown (all phases)
    python3 Shutdown.py --dry-run          # Preview without changes
    python3 Shutdown.py --quick            # Skip vSAN wait (faster but less safe)
    python3 Shutdown.py --no-hosts         # Shutdown VMs but leave hosts running
    python3 Shutdown.py --phase 1          # Run only Fleet Operations phase
    python3 Shutdown.py --phase 13         # Run only VCF Operations shutdown
    python3 Shutdown.py --phase 1 --dry-run  # Preview a single phase

VCF Shutdown Phases (for --phase):
    1     Fleet Operations (VCF Operations Suite) Shutdown
    1b    VCF Automation VM fallback (if Fleet API failed)
    2     Connect to vCenters
    2b    Scale Down VCF Component Services (K8s on VSP)
    3     Stop Workload Control Plane (WCP)
    4     Shutdown Workload VMs (Tanzu, K8s)
    5     Shutdown Workload Domain NSX Edges
    6     Shutdown Workload Domain NSX Manager
    7     Shutdown Workload vCenters
    8     Shutdown VCF Operations for Networks VMs
    9     Shutdown VCF Operations Collector VMs
    10    Shutdown VCF Operations for Logs VMs
    11    Shutdown VCF Identity Broker VMs
    12    Shutdown VCF Operations Fleet Management VMs
    13    Shutdown VCF Operations (vrops) VMs
    14    Shutdown Management Domain NSX Edges
    15    Shutdown Management Domain NSX Manager
    16    Shutdown SDDC Manager
    17    Shutdown Management vCenter
    17b   Connect to ESXi Hosts directly
    18    Set Host Advanced Settings
    19    vSAN Elevator Operations
    19b   Shutdown VSP Platform VMs
    19c   Pre-ESXi Shutdown Audit
    20    Shutdown ESXi Hosts

Configuration:
    Add a [SHUTDOWN] section to /tmp/config.ini for customization:
    
    [SHUTDOWN]
    fleet_fqdn = opslcm-a.site-a.vcf.lab
    fleet_products = vra,vrni,vrops,vrli
    docker_host = docker.site-a.vcf.lab
    docker_containers = gitlab,ldap
    vsan_enabled = true
    vsan_timeout = 2700
    shutdown_hosts = true
"""
    )
    
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show what would be done without making changes')
    
    parser.add_argument('--quick', '-q', action='store_true',
                        help='Skip vSAN elevator wait period (faster but less safe)')
    
    parser.add_argument('--no-hosts', action='store_true',
                        help='Skip ESXi host shutdown (leave hosts running)')
    
    parser.add_argument('--phase', '-p', type=str, default=None,
                        help='Run only a specific VCF shutdown phase (e.g., 1, 1b, 13, 17b)')
    
    parser.add_argument('--version', '-v', action='version',
                        version=f'{SCRIPT_NAME} v{SCRIPT_VERSION}')
    
    parser.add_argument('--debug', '-d', action='store_true',
                        help='Enable debug logging')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG, force=True,
            format='[%(asctime)s] %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    # Run main shutdown
    try:
        success = main(
            dry_run=args.dry_run,
            skip_vsan_wait=args.quick,
            skip_host_shutdown=args.no_hosts,
            phase=args.phase
        )
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print('\n\nShutdown interrupted by user')
        sys.exit(130)
    except Exception as e:
        print(f'\nFATAL ERROR: {e}')
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)
