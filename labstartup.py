#!/usr/bin/env python3
# labstartup.py - HOLFY27 Main Lab Startup Orchestrator
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# LabType-aware orchestrator with enhanced features

import datetime
import os
import sys
import logging
import argparse

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Import core functions
import lsfunctions as lsf
from Tools.labtypes import LabTypeLoader

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='HOLFY27 Lab Startup')
    parser.add_argument('mode', nargs='?', default='startup',
                        choices=['startup', 'labcheck'],
                        help='Execution mode (startup or labcheck)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Dry run - no actual changes')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')
    return parser.parse_args()

def run_dns_checks():
    """Run DNS health checks"""
    try:
        from Tools.dns_checks import run_dns_checks as dns_check
        return dns_check()
    except ImportError:
        lsf.write_output('DNS checks module not available')
        return True  # Continue without DNS checks
    except Exception as e:
        lsf.write_output(f'DNS checks failed: {e}')
        return False

def run_dns_import():
    """Run DNS record import from config.ini or new-dns-records.csv"""
    try:
        from Tools.tdns_import import import_dns_records
        return import_dns_records()
    except ImportError:
        lsf.write_output('DNS import module not available')
        return None
    except Exception as e:
        lsf.write_output(f'DNS import failed: {e}')
        return None

def initialize_dashboard():
    """Initialize the status dashboard"""
    try:
        from Tools.status_dashboard import StatusDashboard
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.generate_html()
        return dashboard
    except ImportError:
        lsf.write_output('Status dashboard module not available')
        return None
    except Exception as e:
        lsf.write_output(f'Dashboard initialization failed: {e}')
        return None

def main():
    """Main entry point"""
    args = parse_args()
    color = 'red'
    
    # Initialize lsfunctions (without router check initially)
    lsf.init(router=False)
    lsf.write_output(f'labtype is {lsf.labtype}')
    
    # For HOL labs, also check router connectivity
    if lsf.labtype == 'HOL':
        lsf.init(router=True)
    
    lsf.write_output(f'lsf.lab_sku: {lsf.lab_sku}')
    lsf.parse_labsku(lsf.lab_sku)
    lsf.postmanfix()
    
    # Initialize status dashboard
    dashboard = initialize_dashboard()
    
    # Handle labcheck mode
    if args.mode == 'labcheck':
        lsf.write_output('Running in labcheck mode')
        lsf.labcheck = True
        # Run labcheck logic here
        return
    
    # AutoLab check
    if lsf.start_autolab():
        lsf.write_output('Autolab executed successfully, exiting')
        sys.exit(0)
    else:
        lsf.write_output('No autolab found, continuing...')
    
    # Report initial state
    lsf.write_output('Beginning Main script')
    lsf.write_vpodprogress('Not Ready', 'STARTING', color=color)
    
    # Update dashboard - starting
    if dashboard:
        dashboard.update_task('prelim', 'dns', 'running')
        dashboard.generate_html()
    
    # Run DNS health checks (required for all lab types)
    lsf.write_output('Running DNS health checks...')
    if not run_dns_checks():
        lsf.labfail('DNS Health Checks Failed')
    
    if dashboard:
        dashboard.update_task('prelim', 'dns', 'complete')
        dashboard.update_task('final', 'dns_import', 'running')
        dashboard.generate_html()
    
    # Run DNS record import immediately after DNS checks
    # This ensures custom FQDNs are available for URL checks in startup modules
    lsf.write_output('Checking for DNS record import...')
    dns_result = run_dns_import()
    if dns_result:
        lsf.write_output(f'DNS import result: {dns_result}')
    
    if dashboard:
        dashboard.update_task('final', 'dns_import', 'complete')
        dashboard.generate_html()
    
    # Create LabType loader
    loader = LabTypeLoader(
        labtype=lsf.labtype,
        holroot=lsf.holroot,
        vpod_repo=lsf.vpod_repo
    )
    
    # Log lab type information
    info = loader.get_labtype_info()
    lsf.write_output(f'Lab Type: {info["name"]} - {info["description"]}')
    lsf.write_output(f'Firewall required: {loader.requires_firewall()}')
    lsf.write_output(f'Proxy filter required: {loader.requires_proxy_filter()}')
    
    # Push router files (for HOL labs via NFS)
    if loader.requires_firewall():
        lsf.write_output('Pushing router files via NFS...')
        lsf.push_router_files()
        lsf.push_vpodrepo_router_files()
        lsf.signal_router_gitdone()
    else:
        lsf.write_output('Firewall not required for this lab type')
        # Still signal router even if no firewall (allows router to continue)
        lsf.signal_router_gitdone()
    
    # Run startup sequence based on LabType
    lsf.write_output(f'Using startup sequence for labtype: {lsf.labtype}')
    
    try:
        loader.run_startup(lsf)
    except Exception as e:
        lsf.write_output(f'Startup sequence failed: {e}')
        lsf.labfail(f'Startup Failed: {e}')
    
    # Signal router that lab is ready
    lsf.signal_router_ready()
    
    # Report final ready state
    color = 'green'
    lsf.write_vpodprogress('Ready', 'READY', color=color)
    
    # Calculate and log runtime
    delta = datetime.datetime.now() - lsf.start_time
    run_mins = "{0:.2f}".format(delta.seconds / 60)
    lsf.write_output(f'LabStartup Finished - runtime was {run_mins} minutes')
    
    # Write ready time
    try:
        with open(lsf.ready_time_file, 'w') as f:
            f.write(f'{run_mins} minutes')
    except Exception:
        pass
    
    # Update dashboard - complete
    if dashboard:
        dashboard.set_complete()
        dashboard.generate_html()
    
    # AutoCheck support
    if lsf.start_autocheck():
        lsf.write_output('Autocheck complete.')


if __name__ == '__main__':
    main()
