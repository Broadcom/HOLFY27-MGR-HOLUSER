#!/usr/bin/env python3
# VCFfinal.py - HOLFY27 Core VCF Final Tasks Module
# Version 3.1 - January 2026
# Author - Burke Azbill and HOL Core Team
# VCF final tasks (Tanzu, Aria)

import os
import sys
import argparse
import logging
import ssl

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(level=logging.WARNING)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VCFfinal'
MODULE_DESCRIPTION = 'VCF final tasks (Tanzu, Aria)'

# Aria URL check configuration
ARIA_URL_MAX_RETRIES = 30  # Maximum attempts (30 minutes total)
ARIA_URL_RETRY_DELAY = 60  # Seconds between retries

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def verify_nic_connected(lsf, vm_obj, simple=False):
    """
    Loop through the NICs and verify connection.
    
    :param lsf: lsfunctions module reference
    :param vm_obj: the VM object to check
    :param simple: if True, just connect; if False, disconnect then reconnect if not connected
    """
    try:
        nics = lsf.get_network_adapter(vm_obj)
        for nic in nics:
            if simple:
                lsf.write_output(f'Connecting {nic.deviceInfo.label} on {vm_obj.name}')
                lsf.set_network_adapter_connection(vm_obj, nic, True)
                lsf.labstartup_sleep(lsf.sleep_seconds)
            elif nic.connectable.connected:
                lsf.write_output(f'{vm_obj.name} {nic.deviceInfo.label} is connected.')
            else:
                lsf.write_output(f'{vm_obj.name} {nic.deviceInfo.label} is NOT connected.')
                lsf.set_network_adapter_connection(vm_obj, nic, False)
                lsf.labstartup_sleep(lsf.sleep_seconds)
                lsf.write_output(f'Connecting {nic.deviceInfo.label} on {vm_obj.name}')
                lsf.set_network_adapter_connection(vm_obj, nic, True)
    except Exception as e:
        lsf.write_output(f'Error verifying NIC connection for {vm_obj.name}: {e}')


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for VCFfinal module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    from pyVim import connect
    from pyVmomi import vim
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    # Verify VCF section exists (checks if VCF module was relevant)
    # We check VCFFINAL section for specific tasks
    if not lsf.config.has_section('VCFFINAL'):
        lsf.write_output('No VCFFINAL section in config.ini - skipping VCFfinal')
        return True  # Not an error - just nothing to do
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Connect to VCF Management Cluster Hosts (if needed)
    #==========================================================================
    
    vcfmgmtcluster = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        vcfmgmtcluster_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        vcfmgmtcluster = [h.strip() for h in vcfmgmtcluster_raw.split('\n') if h.strip()]
    
    if vcfmgmtcluster and not dry_run:
        lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3')
        lsf.connect_vcenters(vcfmgmtcluster)
    
    #==========================================================================
    # TASK 2: Start Supervisor Control Plane VMs (Tanzu)
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.RUNNING)
        dashboard.generate_html()
        
    lsf.write_vpodprogress('Tanzu Start', 'GOOD-3')
    
    # Check for Tanzu Control Plane VMs
    tanzu_control_configured = lsf.config.has_option('VCFFINAL', 'tanzucontrol')
    
    if tanzu_control_configured and not dry_run:
        try:
            lsf.write_output('Starting Tanzu Control Plane VMs...')
            lsf.write_vpodprogress('Tanzu Control Plane', 'GOOD-3')
            
            tanzu_control_raw = lsf.config.get('VCFFINAL', 'tanzucontrol')
            tanzu_control_vms = [v.strip() for v in tanzu_control_raw.split('\n') if v.strip()]
            
            if tanzu_control_vms:
                lsf.start_nested(tanzu_control_vms)
                lsf.write_output(f'Tanzu Control Plane VMs started: {len(tanzu_control_vms)}')
            
        except Exception as e:
            lsf.write_output(f'Error starting Tanzu Control Plane VMs: {e}')
    else:
        lsf.write_output('No Tanzu Control Plane VMs configured')
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 3: Tanzu Deployment
    #==========================================================================
    
    tanzu_deploy_configured = lsf.config.has_option('VCFFINAL', 'tanzudeploy')
    
    if tanzu_deploy_configured and not dry_run:
        try:
            lsf.write_output('Running Tanzu Deployment...')
            lsf.write_vpodprogress('Tanzu Deploy', 'GOOD-3')
            
            # Tanzu deployment scripts can be specified as host:account:script
            tanzu_deploy_raw = lsf.config.get('VCFFINAL', 'tanzudeploy')
            tanzu_deploy_items = [t.strip() for t in tanzu_deploy_raw.split('\n') if t.strip()]
            
            for item in tanzu_deploy_items:
                parts = item.split(':')
                if len(parts) >= 3:
                    host = parts[0]
                    account = parts[1]
                    script = ':'.join(parts[2:])  # Handle scripts with colons in path
                    lsf.write_output(f'Running Tanzu script on {host}: {script}')
                    lsf.ssh(script, f'{account}@{host}', lsf.password)
                    
        except Exception as e:
            lsf.write_output(f'Error during Tanzu Deployment: {e}')
    else:
        lsf.write_output('No Tanzu Deployment configured')
    
    if dashboard:
        dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Check Aria Automation VMs (vRA)
    #==========================================================================
    
    aria_vms_configured = lsf.config.has_option('VCFFINAL', 'vravms')
    aria_vms_errors = []  # Track errors for this task
    aria_vms_task_failed = False  # Track if the entire task failed
    
    # Wrap entire Aria VMs task in try/except to ensure URL checks always run
    try:
        if aria_vms_configured:
            lsf.write_output('Checking Aria Automation VMs...')
            lsf.write_vpodprogress('Aria Automation', 'GOOD-8')
            
            #------------------------------------------------------------------
            # Clear existing sessions and establish fresh vCenter connection
            # Previous tasks may have connected to ESXi hosts directly, but
            # Aria VM operations must be done through vCenter
            #------------------------------------------------------------------
            lsf.write_output('Clearing existing sessions for fresh vCenter connection...')
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()
            
            # Connect to vCenter(s) - required for Aria VM operations
            vcenters = []
            if lsf.config.has_option('RESOURCES', 'vCenters'):
                vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
                vcenters = [v.strip() for v in vcenters_raw.split('\n') if v.strip() and not v.strip().startswith('#')]
            
            if not vcenters:
                lsf.write_output('ERROR: No vCenters configured in RESOURCES section')
                aria_vms_errors.append('No vCenters configured')
            elif not dry_run:
                lsf.write_vpodprogress('Connecting vCenters', 'GOOD-3')
                lsf.write_output(f'Connecting to vCenter(s): {vcenters}')
                lsf.connect_vcenters(vcenters)
                lsf.write_output(f'vCenter sessions established: {len(lsf.sis)}')
            
            vravms_raw = lsf.config.get('VCFFINAL', 'vravms')
            vravms = [v.strip() for v in vravms_raw.split('\n') if v.strip() and not v.strip().startswith('#')]
            
            if vravms and not dry_run and not aria_vms_errors:
                lsf.write_output(f'Processing {len(vravms)} Aria Automation VMs...')
                lsf.write_vpodprogress('Starting Aria VMs', 'GOOD-8')
                
                # Before starting, verify NICs are set to start connected
                for vravm in vravms:
                    parts = vravm.split(':')
                    vmname = parts[0].strip()
                    try:
                        vms = lsf.get_vm_match(vmname)
                        for vm in vms:
                            verify_nic_connected(lsf, vm, simple=True)
                    except Exception as e:
                        error_msg = str(e)
                        lsf.write_output(f'Warning: Error checking NICs for {vmname}: {error_msg}')
                
                # Start the VMs
                try:
                    lsf.start_nested(vravms)
                except Exception as e:
                    error_msg = f'Failed to start Aria VMs: {e}'
                    lsf.write_output(error_msg)
                    aria_vms_errors.append(error_msg)
                
                # After starting, verify VMs are actually powered on and tools running
                for vravm in vravms:
                    parts = vravm.split(':')
                    vmname = parts[0].strip()
                    try:
                        vms = lsf.get_vm_match(vmname)
                        for vm in vms:
                            # Ensure VM is powered on
                            max_power_attempts = 10
                            power_attempt = 0
                            while vm.runtime.powerState != 'poweredOn' and power_attempt < max_power_attempts:
                                lsf.write_output(f'Waiting for {vm.name} to power on...')
                                try:
                                    vm.PowerOnVM_Task()
                                except Exception:
                                    pass
                                lsf.labstartup_sleep(lsf.sleep_seconds)
                                power_attempt += 1
                            
                            # Wait for VMware Tools to be running
                            max_tools_attempts = 30
                            tools_attempt = 0
                            while tools_attempt < max_tools_attempts:
                                try:
                                    if vm.summary.guest.toolsRunningStatus == 'guestToolsRunning':
                                        lsf.write_output(f'VMware Tools running in {vm.name}')
                                        break
                                except Exception:
                                    pass
                                lsf.write_output(f'Waiting for Tools in {vmname}...')
                                lsf.labstartup_sleep(lsf.sleep_seconds)
                                tools_attempt += 1
                            
                            # Verify NIC is connected after tools are running
                            try:
                                verify_nic_connected(lsf, vm, simple=False)
                            except Exception as nic_err:
                                lsf.write_output(f'Warning: Post-start NIC verification failed for {vm.name}: {nic_err}')
                            
                    except Exception as e:
                        error_msg = str(e)
                        lsf.write_output(f'Warning: Error waiting for {vmname}: {error_msg}')
                
                lsf.write_output('Aria Automation VMs processing complete')
        else:
            lsf.write_output('No Aria Automation VMs configured')
            
    except Exception as task_error:
        # Catch any unexpected exception in the entire Aria VMs task
        error_msg = f'Aria VMs task failed with unexpected error: {task_error}'
        lsf.write_output(error_msg)
        aria_vms_errors.append(error_msg)
        aria_vms_task_failed = True
    
    # Update dashboard based on task results
    if dashboard:
        if aria_vms_task_failed or aria_vms_errors:
            dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.FAILED,
                                  f'{len(aria_vms_errors)} errors')
        else:
            dashboard.update_task('vcffinal', 'aria_vms', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'aria_urls', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: Check Aria Automation URLs
    #==========================================================================
    
    aria_urls_configured = lsf.config.has_option('VCFFINAL', 'vraurls')
    urls_checked = 0
    urls_passed = 0
    urls_failed = 0
    
    if aria_urls_configured:
        lsf.write_output('Checking Aria Automation URLs...')
        lsf.write_vpodprogress('Aria Automation URL Checks', 'GOOD-8')
        
        # Run remediation scripts before URL checks
        # Check VCF Automation ssh for password expiration and fix if expired
        lsf.write_output('Fixing expired automation password if necessary...')
        vcfapwcheck_script = '/home/holuser/hol/Tools/vcfapwcheck.sh'
        if os.path.isfile(vcfapwcheck_script) and not dry_run:
            lsf.run_command(vcfapwcheck_script)
        
        # Run the watchvcfa script to make sure the seaweedfs-master-0 pod is not stale
        watchvcfa_script = '/home/holuser/hol/Tools/watchvcfa.sh'
        if os.path.isfile(watchvcfa_script) and not dry_run:
            lsf.run_command(watchvcfa_script)
        
        vraurls_raw = lsf.config.get('VCFFINAL', 'vraurls')
        vraurls = [u.strip() for u in vraurls_raw.split('\n') if u.strip() and not u.strip().startswith('#')]
        
        for url_spec in vraurls:
            if ',' in url_spec:
                parts = url_spec.split(',', 1)
                url = parts[0].strip()
                expected = parts[1].strip()
            else:
                url = url_spec.strip()
                expected = None
            
            if url and not dry_run:
                urls_checked += 1
                lsf.write_output(f'Testing Aria URL: {url}')
                if expected:
                    lsf.write_output(f'  Expected text: {expected}')
                
                # Retry loop - wait up to ARIA_URL_MAX_RETRIES minutes for URL to become available
                url_success = False
                for attempt in range(1, ARIA_URL_MAX_RETRIES + 1):
                    result = lsf.test_url(url, expected_text=expected, verify_ssl=False, timeout=30)
                    if result:
                        lsf.write_output(f'  [SUCCESS] {url} (attempt {attempt})')
                        url_success = True
                        urls_passed += 1
                        break
                    else:
                        if attempt == ARIA_URL_MAX_RETRIES:
                            # Final attempt failed - fail the lab
                            lsf.write_output(f'  [FAILED] {url} after {ARIA_URL_MAX_RETRIES} attempts')
                            urls_failed += 1
                            lsf.labfail(f'Aria URL {url} not accessible after {ARIA_URL_MAX_RETRIES} minutes - should be reached in under 8 minutes')
                        else:
                            lsf.write_output(f'  Sleeping and will try again... {attempt} / {ARIA_URL_MAX_RETRIES}')
                            lsf.labstartup_sleep(ARIA_URL_RETRY_DELAY)
        
        lsf.write_output(f'Aria URL check complete: {urls_passed}/{urls_checked} passed')
    else:
        lsf.write_output('No Aria Automation URLs configured')
    
    if dashboard:
        if urls_failed > 0:
            dashboard.update_task('vcffinal', 'aria_urls', TaskStatus.FAILED, 
                                  f'{urls_failed}/{urls_checked} URLs failed')
        else:
            dashboard.update_task('vcffinal', 'aria_urls', TaskStatus.COMPLETE,
                                  f'{urls_passed} URLs verified' if urls_checked > 0 else '')
        dashboard.generate_html()
    
    #==========================================================================
    # Cleanup
    #==========================================================================
    
    if not dry_run:
        lsf.write_output('Disconnecting VCF hosts...')
        for si in lsf.sis:
            try:
                connect.Disconnect(si)
            except Exception:
                pass
        # Clear the session lists so subsequent modules start fresh
        lsf.sis.clear()
        lsf.sisvc.clear()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom VCF final checks here
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
    #==========================================================================
    # Final Status Check
    #==========================================================================
    
    # Determine if module succeeded or failed
    # URL failures already call labfail() which exits
    # If we get here, URLs passed (or were not configured)
    # But if Aria VMs had critical errors AND no URLs were configured to verify,
    # we should still fail
    
    module_failed = False
    
    if aria_vms_task_failed:
        # Critical failure in Aria VMs task
        if not aria_urls_configured:
            # No URL checks to verify success - must fail
            lsf.write_output('CRITICAL: Aria VMs task failed and no URL checks configured to verify')
            module_failed = True
        elif urls_checked == 0:
            # URL checks were configured but none were actually checked (dry_run or empty list)
            lsf.write_output('WARNING: Aria VMs task failed but URL checks were skipped')
            module_failed = True
    
    if module_failed and not dry_run:
        lsf.labfail(f'{MODULE_NAME} failed: Aria VMs task encountered critical errors')
    
    lsf.write_output(f'{MODULE_NAME} completed')
    return not module_failed


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
