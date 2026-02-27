#!/usr/bin/env python3
# VCFfinal.py - HOLFY27 Core VCF Final Tasks Module
# Version 4.2 - February 2026
# Author - Burke Azbill and HOL Core Team
# VCF final tasks (Tanzu, VCF Automation)

import os
import sys
import argparse
import logging
import ssl
import time
import json

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

MODULE_NAME = 'VCFfinal'
MODULE_DESCRIPTION = 'VCF final tasks (Tanzu, VCF Automation)'

# VCF Automation URL check configuration
VCFA_URL_MAX_RETRIES = 30  # Maximum attempts (30 minutes total)
VCFA_URL_RETRY_DELAY = 60  # Seconds between retries

# VCF Component URL check configuration
VCFC_URL_MAX_RETRIES = 30  # Maximum attempts (30 minutes total)
VCFC_URL_RETRY_DELAY = 60  # Seconds between retries

# WCP/Supervisor polling configuration
WCP_POLL_INTERVAL = 30     # seconds between polls
WCP_MAX_POLL_TIME = 1800   # 30 minutes maximum wait
WCP_SCRIPT_TIMEOUT = 1860  # 31 minutes (slightly more than max poll to allow script cleanup)

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


def check_supervisor_status_api(lsf, vcenter_host, sso_domain='wld.sso'):
    """
    Check Supervisor cluster status via vCenter REST API.
    Returns a dict with config_status, kubernetes_status, and api_servers.
    This is the authoritative check - it tells us what the vCenter UI shows.
    
    :param lsf: lsfunctions module reference
    :param vcenter_host: vCenter hostname
    :param sso_domain: SSO domain for authentication
    :return: dict with keys: config_status, kubernetes_status, api_servers, cluster_id
             Returns None on failure.
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    password = lsf.get_password()
    
    try:
        # Get API session token
        session_resp = requests.post(
            f'https://{vcenter_host}/api/session',
            auth=(f'administrator@{sso_domain}', password),
            verify=False,
            timeout=30
        )
        if session_resp.status_code != 201:
            lsf.write_output(f'  Failed to get vCenter API session (HTTP {session_resp.status_code})')
            return None
        
        session_token = session_resp.json()
        
        # Get supervisor clusters
        clusters_resp = requests.get(
            f'https://{vcenter_host}/api/vcenter/namespace-management/clusters',
            headers={'vmware-api-session-id': session_token},
            verify=False,
            timeout=30
        )
        
        if clusters_resp.status_code != 200:
            lsf.write_output(f'  Failed to query supervisor clusters (HTTP {clusters_resp.status_code})')
            # Clean up session
            try:
                requests.delete(
                    f'https://{vcenter_host}/api/session',
                    headers={'vmware-api-session-id': session_token},
                    verify=False,
                    timeout=10
                )
            except Exception:
                pass
            return None
        
        clusters = clusters_resp.json()
        
        # Clean up session
        try:
            requests.delete(
                f'https://{vcenter_host}/api/session',
                headers={'vmware-api-session-id': session_token},
                verify=False,
                timeout=10
            )
        except Exception:
            pass
        
        if not clusters or not isinstance(clusters, list) or len(clusters) == 0:
            return None
        
        cluster = clusters[0]
        return {
            'config_status': cluster.get('config_status', ''),
            'kubernetes_status': cluster.get('kubernetes_status', ''),
            'api_servers': cluster.get('api_servers', []),
            'cluster_id': cluster.get('cluster', ''),
            'cluster_name': cluster.get('cluster_name', ''),
        }
    except Exception as e:
        lsf.write_output(f'  Error querying Supervisor API: {e}')
        return None


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
    
    # Use get_config_list to properly filter commented-out values
    vcfmgmtcluster = lsf.get_config_list('VCF', 'vcfmgmtcluster')
    
    if vcfmgmtcluster and not dry_run:
        lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3')
        failed_hosts = lsf.connect_vcenters(vcfmgmtcluster)
        
        if failed_hosts:
            fail_msg = f'{len(failed_hosts)} ESXi host(s) unreachable: {", ".join(failed_hosts)}'
            lsf.write_output(f'FATAL: {fail_msg}')
            
            if dashboard:
                dashboard.update_task('vcffinal', 'tanzu', TaskStatus.FAILED,
                                      fail_msg)
                dashboard.generate_html()
            
            lsf.labfail(fail_msg)
            return
    
    #==========================================================================
    # TASK 2: Supervisor Control Plane (Tanzu/WCP)
    #==========================================================================
    
    # First check if we have any vCenters configured - without vCenters, 
    # there's no way to have Tanzu/WCP so skip these checks entirely
    vcenters_list = lsf.get_config_list('RESOURCES', 'vCenters')
    
    if not vcenters_list:
        lsf.write_output('No vCenters configured - skipping Tanzu/WCP checks')
        if dashboard:
            dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.SKIPPED, 
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'vcfa_vms', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'vcfa_urls', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.generate_html()
        lsf.write_output('VCFfinal completed (no VCF resources)')
        return True
    
    lsf.write_vpodprogress('Tanzu Start', 'GOOD-3')
    
    # Check for Tanzu Control Plane VMs - requires tanzucontrol option with valid (non-commented) values
    tanzu_control_values = lsf.get_config_list('VCFFINAL', 'tanzucontrol')
    tanzu_control_configured = len(tanzu_control_values) > 0
    tanzu_verify_ok = False
    last_config_status = ''
    last_k8s_status = ''
    
    if tanzu_control_configured and not dry_run:
        #----------------------------------------------------------------------
        # Determine vCenter host for WCP (look for wld vCenter in config)
        #----------------------------------------------------------------------
        wcp_vcenter = None
        for vc_line in vcenters_list:
            if 'wld' in vc_line.lower():
                # Extract just the hostname (before the colon)
                wcp_vcenter = vc_line.split(':')[0].strip()
                break
        
        if not wcp_vcenter:
            # No WLD vCenter found - use first available vCenter as fallback
            if vcenters_list:
                wcp_vcenter = vcenters_list[0].split(':')[0].strip()
                lsf.write_output(f'No WLD vCenter found, using first available: {wcp_vcenter}')
            else:
                lsf.write_output('No vCenters available for Tanzu checks - skipping')
                if dashboard:
                    dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.SKIPPED,
                                          'No vCenters available')
                    dashboard.generate_html()
                return True
        
        #----------------------------------------------------------------------
        # TASK 2a: PRE-START - Verify WCP vCenter Services
        # Check/start trustmanagement, wcp, and vapi-endpoint services
        # These must be running BEFORE starting Supervisor Control Plane VMs
        #----------------------------------------------------------------------
        if dashboard:
            dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.RUNNING)
            dashboard.generate_html()
        
        lsf.write_output('='*60)
        lsf.write_output('Checking WCP vCenter Services (pre-start)')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('WCP vCenter Check', 'GOOD-3')
        lsf.write_output(f'Target vCenter: {wcp_vcenter}')
        
        # Determine SSO domain from vCenter config entry
        sso_domain = 'wld.sso'  # Default
        # vcenters_list already filtered by get_config_list above
        for vc_line in vcenters_list:
            if 'wld' in vc_line.lower():
                parts = vc_line.split(':')
                if len(parts) >= 3:
                    # Extract domain from user like "administrator@wld.sso"
                    user_part = parts[2].strip()
                    if '@' in user_part:
                        sso_domain = user_part.split('@')[1]
                break
        
        wcp_vcenter_ok = True
        
        # Check if vCenter is reachable
        if not lsf.test_tcp_port(wcp_vcenter, 443, timeout=10):
            lsf.write_output(f'WARNING: Cannot reach vCenter at {wcp_vcenter}:443')
            wcp_vcenter_ok = False
        else:
            lsf.write_output(f'vCenter {wcp_vcenter} is reachable')
            
            # Critical WCP services to check/start
            wcp_services = ['vapi-endpoint', 'trustmanagement', 'wcp']
            
            for service in wcp_services:
                lsf.write_output(f'Checking {service} service...')
                
                try:
                    # Check service status via vmon-cli
                    # Use grep + head + sed to extract exactly the RunState value
                    # head -1 ensures we only get the first matching line (not CurrentRunStateDuration)
                    check_cmd = f"vmon-cli -s {service} 2>/dev/null | grep 'RunState:' | head -1 | sed 's/.*RunState: //'"
                    result = lsf.ssh(check_cmd, f'root@{wcp_vcenter}')
                    
                    status = ''
                    if hasattr(result, 'stdout') and result.stdout:
                        # Take only the first line to avoid multiline output contamination
                        status = result.stdout.strip().split('\n')[0].strip()
                    
                    if status == 'STARTED':
                        lsf.write_output(f'  {service}: RUNNING')
                    else:
                        lsf.write_output(f'  {service}: NOT RUNNING (status: {status})')
                        lsf.write_output(f'  Attempting to start {service}...')
                        
                        # Start the service
                        start_cmd = f'vmon-cli -i {service}'
                        lsf.ssh(start_cmd, f'root@{wcp_vcenter}')
                        
                        # Wait for service to start
                        time.sleep(15)
                        
                        # Re-check status
                        result = lsf.ssh(check_cmd, f'root@{wcp_vcenter}')
                        new_status = ''
                        if hasattr(result, 'stdout') and result.stdout:
                            new_status = result.stdout.strip().split('\n')[0].strip()
                        
                        if new_status == 'STARTED':
                            lsf.write_output(f'  {service}: Started successfully')
                        else:
                            lsf.write_output(f'  WARNING: {service} may still have issues (status: {new_status})')
                            if service == 'trustmanagement':
                                lsf.write_output('  NOTE: trustmanagement is critical for SCP encryption key delivery')
                            wcp_vcenter_ok = False
                            
                except Exception as svc_err:
                    lsf.write_output(f'  Error checking {service}: {svc_err}')
                    wcp_vcenter_ok = False
        
        if wcp_vcenter_ok:
            lsf.write_output('WCP vCenter Services: All services running')
        else:
            lsf.write_output('WCP vCenter Services: Had issues but attempted to start services')
        
        if dashboard:
            if wcp_vcenter_ok:
                dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.COMPLETE)
            else:
                dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.FAILED, 'Service issues')
            dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.RUNNING)
            dashboard.generate_html()
        
        #----------------------------------------------------------------------
        # TASK 2b: VERIFY - Confirm Supervisor is RUNNING via vCenter REST API
        # Instead of checking individual VMs (which requires elevated permissions
        # on system-managed EAM VMs), we use the authoritative vCenter Supervisor
        # Management API. This is what the vCenter UI shows and is the definitive
        # source for Supervisor health.
        # Polls every WCP_POLL_INTERVAL seconds up to WCP_MAX_POLL_TIME.
        #----------------------------------------------------------------------
        lsf.write_output('='*60)
        lsf.write_output('Verifying Supervisor Control Plane Status')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('Tanzu Control Plane', 'GOOD-3')
        
        supervisor_start_time = time.time()
        
        try:
            while (time.time() - supervisor_start_time) < WCP_MAX_POLL_TIME:
                elapsed = int(time.time() - supervisor_start_time)
                
                sup_status = check_supervisor_status_api(lsf, wcp_vcenter, sso_domain)
                
                if sup_status is None:
                    lsf.write_output(f'  Supervisor API not available yet - waiting... ({elapsed}s / {WCP_MAX_POLL_TIME}s)')
                else:
                    last_config_status = sup_status.get('config_status', '')
                    last_k8s_status = sup_status.get('kubernetes_status', '')
                    
                    if last_config_status == 'RUNNING' and last_k8s_status == 'READY':
                        cluster_name = sup_status.get('cluster_name', 'unknown')
                        api_servers = sup_status.get('api_servers', [])
                        lsf.write_output(f'  Supervisor "{cluster_name}": config_status=RUNNING, kubernetes_status=READY')
                        if api_servers:
                            lsf.write_output(f'  API servers: {", ".join(api_servers)}')
                        tanzu_verify_ok = True
                        break
                    elif last_config_status == 'ERROR':
                        lsf.write_output(f'  Supervisor config_status: ERROR')
                        lsf.write_output(f'    Check Supervisor Management in vCenter UI for details')
                        break
                    elif last_config_status == 'RUNNING':
                        lsf.write_output(f'  Supervisor config_status: RUNNING, kubernetes_status: {last_k8s_status} - waiting for READY ({elapsed}s / {WCP_MAX_POLL_TIME}s)')
                    else:
                        lsf.write_output(f'  Supervisor config_status: {last_config_status}, kubernetes_status: {last_k8s_status} - waiting... ({elapsed}s / {WCP_MAX_POLL_TIME}s)')
                
                time.sleep(WCP_POLL_INTERVAL)
            
            if not tanzu_verify_ok:
                if (time.time() - supervisor_start_time) >= WCP_MAX_POLL_TIME:
                    lsf.write_output(f'  Supervisor did not reach RUNNING/READY within {WCP_MAX_POLL_TIME // 60} minutes')
                lsf.write_output(f'  Last status: config={last_config_status or "unknown"}, k8s={last_k8s_status or "unknown"}')
                
        except Exception as e:
            lsf.write_output(f'Error verifying Supervisor status: {e}')
        
        if tanzu_verify_ok:
            lsf.write_output('Supervisor Control Plane: RUNNING and READY')
        else:
            lsf.write_output('Supervisor Control Plane: Not yet fully running')
            lsf.write_output('  check_fix_wcp.sh will attempt to wait and fix certificates')
        
        if dashboard:
            if tanzu_verify_ok:
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
            else:
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.FAILED,
                                      f'config={last_config_status or "unknown"}, k8s={last_k8s_status or "unknown"}')
            dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.RUNNING)
            dashboard.generate_html()
        
        #----------------------------------------------------------------------
        # TASK 2c: POST-VERIFY - Run check_fix_wcp.sh for certificates/webhooks
        # This script has its own internal polling (30s intervals, 30m max).
        # It waits for SCP to become fully accessible before running fixes.
        #----------------------------------------------------------------------
        lsf.write_output('='*60)
        lsf.write_output('WCP Certificate Fix (post-verify)')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('WCP Certificate Fix', 'GOOD-3')
        
        check_fix_wcp_script = '/home/holuser/hol/Tools/check_fix_wcp.sh'
        wcp_certs_ok = True
        
        if os.path.isfile(check_fix_wcp_script):
            # Verify the script is executable before attempting to run it
            if not os.access(check_fix_wcp_script, os.X_OK):
                lsf.write_output(f'  Script is not executable: {check_fix_wcp_script}')
                lsf.write_output(f'  Setting execute permission...')
                try:
                    os.chmod(check_fix_wcp_script, 0o755)
                    lsf.write_output(f'  Execute permission set successfully')
                except Exception as chmod_err:
                    lsf.write_output(f'  ERROR: Failed to set execute permission: {chmod_err}')
                    lsf.write_output(f'  Will run via bash interpreter as fallback')
            
            lsf.write_output(f'Running: {check_fix_wcp_script} {wcp_vcenter}')
            lsf.write_output(f'  (script has internal polling up to {WCP_MAX_POLL_TIME // 60}m)')
            
            try:
                # Run via /bin/bash explicitly to avoid execute permission issues
                # Use extended timeout since the script has internal polling
                # Pass --stdout-only so script does not also write to log file directly
                # (VCFfinal.py handles all logging via lsf.write_output)
                wcp_cmd = f'/bin/bash {check_fix_wcp_script} --stdout-only {wcp_vcenter}'
                result = lsf.run_command(wcp_cmd, timeout=WCP_SCRIPT_TIMEOUT)
                
                exit_code = result.returncode if hasattr(result, 'returncode') else (0 if result else 1)
                
                # Log stdout from the script (timestamps already included by script)
                if hasattr(result, 'stdout') and result.stdout:
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            lsf.write_output(f'  {line.strip()}')
                # Log stderr (errors/warnings from the script)
                if hasattr(result, 'stderr') and result.stderr:
                    for line in result.stderr.strip().split('\n'):
                        if line.strip():
                            lsf.write_output(f'  {line.strip()}')
                
                if exit_code == 0:
                    lsf.write_output('WCP certificate fix completed successfully')
                elif exit_code == 2:
                    lsf.write_output('WARNING: SCP services did not become active within timeout')
                    lsf.write_output('  hypercrypt/kubelet may still be initializing')
                    wcp_certs_ok = False
                elif exit_code == 3:
                    lsf.write_output('WARNING: Cannot connect to Supervisor / K8s API not available within timeout')
                    wcp_certs_ok = False
                elif exit_code == 4:
                    lsf.write_output('WARNING: kubectl commands failed - certificates may need attention')
                    wcp_certs_ok = False
                elif exit_code == 126:
                    lsf.write_output('ERROR: WCP script is not executable (exit code 126)')
                    lsf.write_output(f'  Fix: chmod +x {check_fix_wcp_script}')
                    wcp_certs_ok = False
                elif exit_code == 127:
                    lsf.write_output('ERROR: WCP script interpreter not found (exit code 127)')
                    wcp_certs_ok = False
                else:
                    lsf.write_output(f'WARNING: WCP script exited with code {exit_code}')
                    wcp_certs_ok = False
                    
            except Exception as wcp_err:
                lsf.write_output(f'WARNING: Error running WCP script: {wcp_err}')
                lsf.write_output('  Continuing with startup - WCP may need manual attention')
                wcp_certs_ok = False
        else:
            lsf.write_output(f'WCP script not found: {check_fix_wcp_script}')
            lsf.write_output('  Skipping certificate fix - manual intervention may be needed')
        
        #----------------------------------------------------------------------
        # Final Supervisor Status Reconciliation
        # If either tanzu_verify or wcp_certs failed during their initial check,
        # re-check the authoritative Supervisor API one final time.
        # This handles the case where the Supervisor was still starting up
        # during earlier checks but is now fully running.
        #----------------------------------------------------------------------
        if not tanzu_verify_ok or not wcp_certs_ok:
            lsf.write_output('='*60)
            lsf.write_output('Final Supervisor Status Check')
            lsf.write_output('='*60)
            
            final_status = check_supervisor_status_api(lsf, wcp_vcenter, sso_domain)
            
            if final_status and final_status.get('config_status') == 'RUNNING' and final_status.get('kubernetes_status') == 'READY':
                lsf.write_output('Supervisor is now RUNNING and READY (confirmed via vCenter API)')
                cluster_name = final_status.get('cluster_name', 'unknown')
                lsf.write_output(f'  Cluster: {cluster_name}')
                
                # Override dashboard status since the Supervisor is actually healthy
                if not tanzu_verify_ok:
                    lsf.write_output('  Updating Tanzu Control Plane status to COMPLETE')
                    tanzu_verify_ok = True
                if not wcp_certs_ok:
                    lsf.write_output('  Updating WCP Certificate Fix status to COMPLETE')
                    lsf.write_output('  (Supervisor is healthy - certificate fix may not have been needed)')
                    wcp_certs_ok = True
            else:
                cfg = final_status.get('config_status', 'unknown') if final_status else 'unknown'
                k8s = final_status.get('kubernetes_status', 'unknown') if final_status else 'unknown'
                lsf.write_output(f'Supervisor final status: config={cfg}, k8s={k8s}')
        
        if dashboard:
            if tanzu_verify_ok:
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
            # else: already marked FAILED above
            if wcp_certs_ok:
                dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.COMPLETE)
            else:
                dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.FAILED, 'See log')
            dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.RUNNING)
            dashboard.generate_html()
            
    else:
        lsf.write_output('No Tanzu Control Plane VMs configured')
        if dashboard:
            dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.RUNNING)
            dashboard.generate_html()
    
    #==========================================================================
    # TASK 2d: Start VSP Platform VMs
    # Processes vspvms from [VCF] section the same way vravms is processed
    # in TASK 4 - finds VMs by name/pattern via vCenter, verifies NICs,
    # powers them on, and waits for VMware Tools.
    #==========================================================================
    
    # Use get_config_list to properly filter commented-out values
    vspvms = lsf.get_config_list('VCF', 'vspvms')
    vsp_vms_configured = len(vspvms) > 0
    vsp_vms_errors = []
    vsp_vms_task_failed = False
    
    if dashboard:
        dashboard.update_task('vcffinal', 'vsp_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    try:
        if vsp_vms_configured:
            lsf.write_output('Checking VSP Platform VMs...')
            lsf.write_vpodprogress('VSP Platform VMs', 'GOOD-3')
            
            #------------------------------------------------------------------
            # Clear existing sessions and establish fresh vCenter connection
            # Previous tasks connected to ESXi hosts directly, but VSP
            # Platform VM operations must be done through vCenter.
            #------------------------------------------------------------------
            lsf.write_output('Clearing existing sessions for fresh vCenter connection...')
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()
            
            # Connect to vCenter(s) - required for VSP Platform VM operations
            vcenters = lsf.get_config_list('RESOURCES', 'vCenters')
            
            if not vcenters:
                lsf.write_output('ERROR: No vCenters configured in RESOURCES section')
                vsp_vms_errors.append('No vCenters configured')
            elif not dry_run:
                lsf.write_output(f'Connecting to vCenter(s): {vcenters}')
                failed_vcs = lsf.connect_vcenters(vcenters)
                lsf.write_output(f'vCenter sessions established: {len(lsf.sis)}')
                if failed_vcs:
                    lsf.write_output(f'WARNING: Failed to connect to vCenter(s): {", ".join(failed_vcs)}')
            
            if vspvms and not dry_run and not vsp_vms_errors:
                lsf.write_output(f'Processing {len(vspvms)} VSP Platform VMs...')
                lsf.write_vpodprogress('VSP Platform VMs', 'GOOD-3')
                
                # Check if all VSP VMs are already running with Tools active
                all_running = True
                for vspvm in vspvms:
                    parts = vspvm.split(':')
                    vmname = parts[0].strip()
                    try:
                        vms = lsf.get_vm_match(vmname)
                        if not vms:
                            all_running = False
                            break
                        for vm in vms:
                            if vm.runtime.powerState != 'poweredOn':
                                all_running = False
                                break
                            try:
                                if vm.summary.guest.toolsRunningStatus != 'guestToolsRunning':
                                    all_running = False
                                    break
                            except Exception:
                                all_running = False
                                break
                        if not all_running:
                            break
                    except Exception:
                        all_running = False
                        break
                
                if all_running:
                    lsf.write_output('All VSP Platform VMs already running with Tools active - skipping startup')
                else:
                    # Connect NICs before starting
                    for vspvm in vspvms:
                        parts = vspvm.split(':')
                        vmname = parts[0].strip()
                        try:
                            vms = lsf.get_vm_match(vmname)
                            for vm in vms:
                                if vm.runtime.powerState != 'poweredOn':
                                    verify_nic_connected(lsf, vm, simple=True)
                                else:
                                    lsf.write_output(f'{vm.name} already powered on, skipping NIC connect')
                        except Exception as e:
                            lsf.write_output(f'Warning: Error checking NICs for {vmname}: {e}')
                    
                    # Start the VMs
                    try:
                        lsf.start_nested(vspvms)
                    except Exception as e:
                        error_msg = f'Failed to start VSP Platform VMs: {e}'
                        lsf.write_output(error_msg)
                        vsp_vms_errors.append(error_msg)
                    
                    # After starting, verify VMs are actually powered on and tools running
                    for vspvm in vspvms:
                        parts = vspvm.split(':')
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
                            lsf.write_output(f'Warning: Error waiting for {vmname}: {e}')
                
                lsf.write_output('VSP Platform VMs processing complete')
        else:
            lsf.write_output('No VSP Platform VMs configured')
            
    except Exception as task_error:
        error_msg = f'VSP Platform VMs task failed with unexpected error: {task_error}'
        lsf.write_output(error_msg)
        vsp_vms_errors.append(error_msg)
        vsp_vms_task_failed = True
    
    # Update dashboard based on task results
    if dashboard:
        if vsp_vms_task_failed or vsp_vms_errors:
            dashboard.update_task('vcffinal', 'vsp_vms', TaskStatus.FAILED,
                                  f'{len(vsp_vms_errors)} errors')
        elif vsp_vms_configured:
            dashboard.update_task('vcffinal', 'vsp_vms', TaskStatus.COMPLETE)
        else:
            dashboard.update_task('vcffinal', 'vsp_vms', TaskStatus.SKIPPED,
                                  'No VSP Platform VMs configured')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 2e: Start VCF Components on VSP Management Cluster
    # These are Kubernetes workloads (Salt Master, Salt RaaS, Telemetry,
    # Software Depot, Identity Broker) that may remain scaled to 0 after
    # a cold boot. We SSH to the VSP control plane and scale them up.
    # Only runs if vcfcomponents is defined in [VCFFINAL] with values.
    #==========================================================================
    
    vcfcomponents = lsf.get_config_list('VCFFINAL', 'vcfcomponents')
    vcf_comp_configured = len(vcfcomponents) > 0
    vcf_comp_errors = []
    vcf_comp_scaled = 0
    vcf_comp_already_running = 0
    
    if dashboard and vcf_comp_configured:
        dashboard.update_task('vcffinal', 'vcf_components', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    if vcf_comp_configured and not dry_run:
        lsf.write_output('Starting VCF Components on VSP management cluster...')
        lsf.write_vpodprogress('VCF Components', 'GOOD-3')
        
        try:
            # ---- Discover the VSP control plane IP ----
            # Resolve a VSP worker node via DNS, SSH in, and read the
            # kubeconfig to find the K8s API server (kube-vip) VIP.
            # Retries handle the case where VSP VMs were just powered on
            # and DNS/SSH are not yet available.
            import socket
            import re
            
            vsp_control_plane_ip = None
            password = lsf.get_password()
            vsp_user = 'vmware-system-user'
            max_discovery_attempts = 20
            discovery_retry_delay = 30
            
            vspvms_list = lsf.get_config_list('VCF', 'vspvms')
            if not vspvms_list:
                lsf.write_output('  No vspvms configured in [VCF] - cannot discover VSP control plane')
                vcf_comp_errors.append('No vspvms configured - cannot discover VSP control plane')
            else:
                # The vspvms entry is like "vsp-01a-.*:vc-mgmt-a.site-a.vcf.lab"
                # The VM name prefix (vsp-01a) often matches the platform FQDN
                vsp_candidates = ['vsp-01a.site-a.vcf.lab']
                
                for attempt in range(1, max_discovery_attempts + 1):
                    # Step 1: Resolve a VSP worker node via DNS
                    vsp_worker_ip = None
                    for candidate in vsp_candidates:
                        try:
                            vsp_worker_ip = socket.gethostbyname(candidate)
                            lsf.write_output(f'  VSP worker candidate: {candidate} -> {vsp_worker_ip}')
                            break
                        except socket.gaierror as dns_err:
                            lsf.write_output(f'  DNS failed for {candidate}: {dns_err} (attempt {attempt}/{max_discovery_attempts})')
                    
                    if not vsp_worker_ip:
                        if attempt < max_discovery_attempts:
                            lsf.write_output(f'  Waiting {discovery_retry_delay}s for DNS to become available...')
                            lsf.labstartup_sleep(discovery_retry_delay)
                            continue
                        else:
                            break
                    
                    # Step 2: SSH to the worker and read the kubeconfig
                    lsf.write_output(f'  Reading kubeconfig from VSP worker {vsp_worker_ip}...')
                    result = lsf.ssh(
                        f"echo '{password}' | sudo -S grep server: /etc/kubernetes/node-agent.conf",
                        f'{vsp_user}@{vsp_worker_ip}'
                    )
                    
                    if hasattr(result, 'stdout') and result.stdout:
                        for line in result.stdout.strip().split('\n'):
                            if 'server:' in line:
                                # Extract IP from "    server: https://10.1.1.142:6443"
                                match = re.search(r'https?://([0-9.]+):', line)
                                if match:
                                    vsp_control_plane_ip = match.group(1)
                                    lsf.write_output(f'  VSP control plane IP: {vsp_control_plane_ip}')
                                    break
                    
                    if vsp_control_plane_ip:
                        break
                    
                    # Log why this attempt failed
                    if hasattr(result, 'stdout') and result.stdout:
                        lsf.write_output(f'  SSH succeeded but no server: line found in node-agent.conf (attempt {attempt}/{max_discovery_attempts})')
                    else:
                        ssh_rc = getattr(result, 'returncode', 'N/A')
                        ssh_err = ''
                        if hasattr(result, 'stderr') and result.stderr:
                            ssh_err = result.stderr.strip()[:200]
                        lsf.write_output(f'  SSH to VSP worker failed (rc={ssh_rc}): {ssh_err} (attempt {attempt}/{max_discovery_attempts})')
                    
                    if attempt < max_discovery_attempts:
                        lsf.write_output(f'  Waiting {discovery_retry_delay}s before retry...')
                        lsf.labstartup_sleep(discovery_retry_delay)
            
            if not vsp_control_plane_ip:
                lsf.write_output('WARNING: Could not determine VSP control plane IP after all attempts')
                vcf_comp_errors.append('Could not determine VSP control plane IP')
            else:
                # ---- Detect sudo mode on the control plane ----
                sudo_needs_password = True  # Default to password-required (VCF 9.1.x)
                sudo_check = lsf.ssh(
                    'sudo -n true',
                    f'{vsp_user}@{vsp_control_plane_ip}'
                )
                if hasattr(sudo_check, 'returncode') and sudo_check.returncode == 0:
                    sudo_needs_password = False
                lsf.write_output(f'  VSP sudo requires password: {sudo_needs_password}')
                
                # ---- Helper to run kubectl on the VSP control plane ----
                def vsp_kubectl(kubectl_cmd):
                    if sudo_needs_password:
                        ssh_cmd = f"echo '{password}' | sudo -S -i bash -c '{kubectl_cmd}'"
                    else:
                        ssh_cmd = f"sudo -i bash -c '{kubectl_cmd}'"
                    return lsf.ssh(ssh_cmd, f'{vsp_user}@{vsp_control_plane_ip}')
                
                # ---- Unsuspend postgres instances managed by Zalando operator ----
                # The VMSP operator sets a "database.vmsp.vmware.com/suspended=true"
                # label on PostgresInstance CRDs when a component is stopped.  The
                # Zalando postgres operator honours this label and keeps the
                # statefulset at 0 replicas regardless of manual scaling.  We must:
                #   1. Remove the suspended label from the PostgresInstance CRD
                #   2. Patch the Zalando postgresql CRD numberOfInstances back to 1
                # Both steps are required: removing the label alone isn't sufficient
                # because the operator previously set numberOfInstances=0 and won't
                # automatically restore it when the label is removed.
                lsf.write_output('  Checking for suspended Postgres instances...')
                # Use -o json and parse locally to avoid SSH escaping issues
                # with dotted label keys in custom-columns (see skill doc:
                # "SSH Escaping Pitfall" — dotted keys get mangled through
                # SSH+sudo+bash-c layers, silently returning <none>).
                pg_check = vsp_kubectl(
                    'kubectl get postgresinstances.database.vmsp.vmware.com -A -o json 2>/dev/null'
                )
                if hasattr(pg_check, 'stdout') and pg_check.stdout:
                    try:
                        import json as _json_pg
                        raw_pg = pg_check.stdout.strip()
                        json_start_pg = raw_pg.find('{')
                        if json_start_pg >= 0:
                            raw_pg = raw_pg[json_start_pg:]
                        pg_data = _json_pg.loads(raw_pg)
                        for pg_item in pg_data.get('items', []):
                            pg_ns = pg_item.get('metadata', {}).get('namespace', '')
                            pg_name = pg_item.get('metadata', {}).get('name', '')
                            pg_labels = pg_item.get('metadata', {}).get('labels', {})
                            suspended = pg_labels.get('database.vmsp.vmware.com/suspended', '')

                            if suspended == 'true':
                                lsf.write_output(f'  Unsuspending Postgres instance: {pg_ns}/{pg_name}')
                                vsp_kubectl(
                                    f'kubectl label postgresinstances.database.vmsp.vmware.com '
                                    f'{pg_name} -n {pg_ns} database.vmsp.vmware.com/suspended-'
                                )

                            # Always ensure Zalando numberOfInstances is 1
                            zalando_check = vsp_kubectl(
                                f'kubectl get postgresqls.acid.zalan.do {pg_name} -n {pg_ns} '
                                f'-o jsonpath="{{.spec.numberOfInstances}}" 2>/dev/null'
                            )
                            current_instances = ''
                            if hasattr(zalando_check, 'stdout') and zalando_check.stdout:
                                current_instances = zalando_check.stdout.strip().split('\n')[-1].strip()

                            if current_instances != '1':
                                lsf.write_output(f'  Scaling Zalando postgres {pg_ns}/{pg_name} to 1 instance (was {current_instances})')
                                patch_json = '{"spec":{"numberOfInstances":1}}'
                                vsp_kubectl(
                                    f"kubectl patch postgresqls.acid.zalan.do {pg_name} -n {pg_ns} "
                                    f"--type=merge -p '{patch_json}'"
                                )
                    except (ValueError, Exception) as pg_err:
                        lsf.write_output(f'  WARNING: Could not parse postgres JSON: {pg_err}')
                        lsf.write_output(f'  Falling back to unconditional unsuspend of known namespaces...')
                        for fallback_ns in ['salt-raas', 'vcf-fleet-lcm', 'vcf-sddc-lcm', 'vidb-external']:
                            vsp_kubectl(
                                f'kubectl label postgresinstances.database.vmsp.vmware.com '
                                f'--all -n {fallback_ns} database.vmsp.vmware.com/suspended- 2>/dev/null'
                            )
                
                # ---- Scale up each component ----
                lsf.write_output(f'  Processing {len(vcfcomponents)} component resources...')
                
                for entry in vcfcomponents:
                    # Format: namespace:resource_type/resource_name
                    parts = entry.split(':', 1)
                    if len(parts) != 2 or '/' not in parts[1]:
                        lsf.write_output(f'  WARNING: Invalid vcfcomponents entry: {entry}')
                        vcf_comp_errors.append(f'Invalid entry: {entry}')
                        continue
                    
                    namespace = parts[0].strip()
                    resource = parts[1].strip()  # e.g. "deployment/salt-master"
                    
                    # Check current replica count before scaling
                    check_cmd = f'kubectl get {resource} -n {namespace} -o jsonpath="{{.spec.replicas}}"'
                    check_result = vsp_kubectl(check_cmd)
                    current_replicas = ''
                    if hasattr(check_result, 'stdout') and check_result.stdout:
                        current_replicas = check_result.stdout.strip().split('\n')[-1].strip()
                    
                    if current_replicas == '1' or (current_replicas.isdigit() and int(current_replicas) > 0):
                        lsf.write_output(f'  {namespace}/{resource}: already running (replicas={current_replicas})')
                        vcf_comp_already_running += 1
                        continue
                    
                    # Scale to 1 replica
                    scale_cmd = f'kubectl scale {resource} -n {namespace} --replicas=1'
                    lsf.write_output(f'  Scaling up: {namespace}/{resource}')
                    scale_result = vsp_kubectl(scale_cmd)
                    
                    if hasattr(scale_result, 'stdout') and 'scaled' in scale_result.stdout:
                        vcf_comp_scaled += 1
                    elif hasattr(scale_result, 'returncode') and scale_result.returncode == 0:
                        vcf_comp_scaled += 1
                    else:
                        err = ''
                        if hasattr(scale_result, 'stderr') and scale_result.stderr:
                            err = scale_result.stderr.strip()[:200]
                        elif hasattr(scale_result, 'stdout') and scale_result.stdout:
                            err = scale_result.stdout.strip()[:200]
                        lsf.write_output(f'  WARNING: Failed to scale {namespace}/{resource}: {err}')
                        vcf_comp_errors.append(f'Failed: {namespace}/{resource}')
                
                # ---- Update Component CRD annotations to Running ----
                # The VCF Services Runtime UI reads the annotation
                # "component.vmsp.vmware.com/operational-status" on each
                # Component CRD to determine the displayed state.  Scaling
                # pods alone does not update this annotation, so the UI
                # would still show "Stopped" without this step.
                # Component CRDs are cluster-scoped (not namespaced).
                # We fetch JSON and parse locally to avoid SSH escaping
                # issues with dotted annotation keys in custom-columns/jsonpath.
                if vcf_comp_scaled > 0:
                    lsf.write_output('  Updating Component CRD annotations to Running...')
                    comp_json = vsp_kubectl(
                        'kubectl get components.api.vmsp.vmware.com -o json 2>/dev/null'
                    )
                    if hasattr(comp_json, 'stdout') and comp_json.stdout:
                        try:
                            import json as _json
                            raw = comp_json.stdout.strip()
                            json_start = raw.find('{')
                            if json_start >= 0:
                                raw = raw[json_start:]
                            comp_data = _json.loads(raw)
                            for comp_item in comp_data.get('items', []):
                                crd_name = comp_item.get('metadata', {}).get('name', '')
                                ann = comp_item.get('metadata', {}).get('annotations', {})
                                status = ann.get('component.vmsp.vmware.com/operational-status', '')
                                if status == 'NotRunning':
                                    lsf.write_output(f'  Annotating {crd_name} -> Running')
                                    vsp_kubectl(
                                        f'kubectl annotate components.api.vmsp.vmware.com '
                                        f'{crd_name} '
                                        f'component.vmsp.vmware.com/operational-status=Running --overwrite'
                                    )
                        except (ValueError, Exception) as je:
                            lsf.write_output(f'  WARNING: Could not parse component JSON: {je}')
                
                # ---- Restart any CrashLoopBackOff pods ----
                # Pods that were crashing while their Postgres database was
                # down (0 replicas) will be in CrashLoopBackOff with long
                # exponential backoff delays. Delete them so the deployment
                # controller recreates them immediately.
                crash_check = vsp_kubectl(
                    'kubectl get pods -A --no-headers 2>/dev/null '
                    '| grep -E "CrashLoopBackOff|Init:CrashLoopBackOff|Error"'
                )
                if hasattr(crash_check, 'stdout') and crash_check.stdout and crash_check.stdout.strip():
                    crashed_count = 0
                    for line in crash_check.stdout.strip().split('\n'):
                        cols = line.split()
                        if len(cols) >= 2:
                            crash_ns, crash_pod = cols[0], cols[1]
                            lsf.write_output(f'  Restarting crashed pod: {crash_ns}/{crash_pod}')
                            vsp_kubectl(f'kubectl delete pod {crash_pod} -n {crash_ns}')
                            crashed_count += 1
                    if crashed_count > 0:
                        lsf.write_output(f'  Restarted {crashed_count} crashed pod(s)')
                
                # ---- Summary ----
                total = len(vcfcomponents)
                lsf.write_output(f'VCF Components: {vcf_comp_scaled} scaled up, '
                                 f'{vcf_comp_already_running} already running, '
                                 f'{len(vcf_comp_errors)} errors (of {total} total)')
                
        except Exception as comp_error:
            error_msg = f'VCF Components task failed: {comp_error}'
            lsf.write_output(error_msg)
            vcf_comp_errors.append(error_msg)
    elif vcf_comp_configured and dry_run:
        lsf.write_output(f'Would start {len(vcfcomponents)} VCF components (dry run)')
    else:
        lsf.write_output('No VCF Components configured')
    
    if dashboard:
        if vcf_comp_configured:
            if vcf_comp_errors:
                dashboard.update_task('vcffinal', 'vcf_components', TaskStatus.FAILED,
                                      f'{len(vcf_comp_errors)} errors',
                                      total=len(vcfcomponents), success=vcf_comp_scaled + vcf_comp_already_running,
                                      failed=len(vcf_comp_errors))
            else:
                dashboard.update_task('vcffinal', 'vcf_components', TaskStatus.COMPLETE,
                                      f'{vcf_comp_scaled} started, {vcf_comp_already_running} already running',
                                      total=len(vcfcomponents), success=vcf_comp_scaled + vcf_comp_already_running,
                                      failed=0)
        else:
            dashboard.update_task('vcffinal', 'vcf_components', TaskStatus.SKIPPED,
                                  'No VCF Components configured')
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
            # Use get_config_list to properly filter commented-out values
            tanzu_deploy_items = lsf.get_config_list('VCFFINAL', 'tanzudeploy')
            
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
        dashboard.update_task('vcffinal', 'vcfa_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Check VCF Automation VMs (vRA)
    #==========================================================================
    
    # Check for actual non-commented values, not just the presence of the option
    vravms = lsf.get_config_list('VCFFINAL', 'vravms')
    vcfa_vms_configured = len(vravms) > 0
    vcfa_vms_errors = []  # Track errors for this task
    vcfa_vms_task_failed = False  # Track if the entire task failed
    
    # Wrap entire VCF Automation VMs task in try/except to ensure URL checks always run
    try:
        if vcfa_vms_configured:
            lsf.write_output('Checking VCF Automation VMs...')
            lsf.write_vpodprogress('VCF Automation', 'GOOD-8')
            
            #------------------------------------------------------------------
            # Clear existing sessions and establish fresh vCenter connection
            # Previous tasks may have connected to ESXi hosts directly, but
            # VCF Automation VM operations must be done through vCenter
            #------------------------------------------------------------------
            lsf.write_output('Clearing existing sessions for fresh vCenter connection...')
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()
            
            # Connect to vCenter(s) - required for VCF Automation VM operations
            # Use get_config_list to properly filter commented-out values
            vcenters = lsf.get_config_list('RESOURCES', 'vCenters')
            
            if not vcenters:
                lsf.write_output('ERROR: No vCenters configured in RESOURCES section')
                vcfa_vms_errors.append('No vCenters configured')
            elif not dry_run:
                lsf.write_vpodprogress('Connecting vCenters', 'GOOD-3')
                lsf.write_output(f'Connecting to vCenter(s): {vcenters}')
                failed_vcs = lsf.connect_vcenters(vcenters)
                lsf.write_output(f'vCenter sessions established: {len(lsf.sis)}')
                if failed_vcs:
                    lsf.write_output(f'WARNING: Failed to connect to vCenter(s): {", ".join(failed_vcs)}')
            
            # vravms already retrieved above to check if configured
            if vravms and not dry_run and not vcfa_vms_errors:
                lsf.write_output(f'Processing {len(vravms)} VCF Automation VMs...')
                lsf.write_vpodprogress('Starting VCF Automation VMs', 'GOOD-8')
                
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
                    error_msg = f'Failed to start VCF Automation VMs: {e}'
                    lsf.write_output(error_msg)
                    vcfa_vms_errors.append(error_msg)
                
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
                
                lsf.write_output('VCF Automation VMs processing complete')
        else:
            lsf.write_output('No VCF Automation VMs configured')
            
    except Exception as task_error:
        # Catch any unexpected exception in the entire VCF Automation VMs task
        error_msg = f'VCF Automation VMs task failed with unexpected error: {task_error}'
        lsf.write_output(error_msg)
        vcfa_vms_errors.append(error_msg)
        vcfa_vms_task_failed = True
    
    # Update dashboard based on task results
    if dashboard:
        if vcfa_vms_task_failed or vcfa_vms_errors:
            dashboard.update_task('vcffinal', 'vcfa_vms', TaskStatus.FAILED,
                                  f'{len(vcfa_vms_errors)} errors')
        else:
            dashboard.update_task('vcffinal', 'vcfa_vms', TaskStatus.COMPLETE)
        dashboard.update_task('vcffinal', 'vcfa_urls', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 5: Check VCF Automation URLs
    #==========================================================================
    
    # Check for actual non-commented URL values, not just the presence of the option
    vraurls = lsf.get_config_list('VCFFINAL', 'vraurls')
    vcfa_urls_configured = len(vraurls) > 0
    urls_checked = 0
    urls_passed = 0
    urls_failed = 0
    
    if vcfa_urls_configured:
        lsf.write_output('Checking VCF Automation URLs...')
        lsf.write_vpodprogress('VCF Automation URL Checks', 'GOOD-8')
        
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
        
        # vraurls already retrieved above
        
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
                lsf.write_output(f'Testing VCF Automation URL: {url}')
                if expected:
                    lsf.write_output(f'  Expected text: {expected}')
                
                # Retry loop - wait up to VCFA_URL_MAX_RETRIES minutes for URL to become available
                url_success = False
                for attempt in range(1, VCFA_URL_MAX_RETRIES + 1):
                    result = lsf.test_url(url, expected_text=expected, verify_ssl=False, timeout=30)
                    if result:
                        lsf.write_output(f'  [SUCCESS] {url} (attempt {attempt})')
                        url_success = True
                        urls_passed += 1
                        break
                    else:
                        if attempt == VCFA_URL_MAX_RETRIES:
                            # Final attempt failed - fail the lab
                            lsf.write_output(f'  [FAILED] {url} after {VCFA_URL_MAX_RETRIES} attempts')
                            urls_failed += 1
                            lsf.labfail(f'VCF Automation URL {url} not accessible after {VCFA_URL_MAX_RETRIES} minutes - should be reached in under 8 minutes')
                        else:
                            lsf.write_output(f'  Sleeping and will try again... {attempt} / {VCFA_URL_MAX_RETRIES}')
                            lsf.labstartup_sleep(VCFA_URL_RETRY_DELAY)
        
        lsf.write_output(f'VCF Automation URL check complete: {urls_passed}/{urls_checked} passed')
    else:
        lsf.write_output('No VCF Automation URLs configured')
    
    if dashboard:
        if urls_failed > 0:
            dashboard.update_task('vcffinal', 'vcfa_urls', TaskStatus.FAILED, 
                                  f'{urls_failed}/{urls_checked} URLs failed')
        else:
            dashboard.update_task('vcffinal', 'vcfa_urls', TaskStatus.COMPLETE,
                                  f'{urls_passed} URLs verified' if urls_checked > 0 else '')
        dashboard.update_task('vcffinal', 'vcf_component_urls', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 6: Check VCF Component URLs
    #==========================================================================
    
    vcfcomponenturls = lsf.get_config_list('VCFFINAL', 'vcfcomponenturls')
    vcfc_urls_configured = len(vcfcomponenturls) > 0
    vcfc_urls_checked = 0
    vcfc_urls_passed = 0
    vcfc_urls_failed = 0
    
    if vcfc_urls_configured:
        lsf.write_output('Checking VCF Component URLs...')
        lsf.write_vpodprogress('VCF Component URL Checks', 'GOOD-8')
        
        for url_spec in vcfcomponenturls:
            if ',' in url_spec:
                parts = url_spec.split(',', 1)
                url = parts[0].strip()
                expected = parts[1].strip()
            else:
                url = url_spec.strip()
                expected = None
            
            if url and not dry_run:
                vcfc_urls_checked += 1
                lsf.write_output(f'Testing VCF Component URL: {url}')
                if expected:
                    lsf.write_output(f'  Expected text: {expected}')
                
                url_success = False
                for attempt in range(1, VCFC_URL_MAX_RETRIES + 1):
                    result = lsf.test_url(url, expected_text=expected, verify_ssl=False, timeout=30)
                    if result:
                        lsf.write_output(f'  [SUCCESS] {url} (attempt {attempt})')
                        url_success = True
                        vcfc_urls_passed += 1
                        break
                    else:
                        if attempt == VCFC_URL_MAX_RETRIES:
                            lsf.write_output(f'  [FAILED] {url} after {VCFC_URL_MAX_RETRIES} attempts')
                            vcfc_urls_failed += 1
                            lsf.labfail(f'VCF Component URL {url} not accessible after {VCFC_URL_MAX_RETRIES} minutes')
                        else:
                            lsf.write_output(f'  Sleeping and will try again... {attempt} / {VCFC_URL_MAX_RETRIES}')
                            lsf.labstartup_sleep(VCFC_URL_RETRY_DELAY)
        
        lsf.write_output(f'VCF Component URL check complete: {vcfc_urls_passed}/{vcfc_urls_checked} passed')
    else:
        lsf.write_output('No VCF Component URLs configured')
    
    if dashboard:
        if vcfc_urls_failed > 0:
            dashboard.update_task('vcffinal', 'vcf_component_urls', TaskStatus.FAILED,
                                  f'{vcfc_urls_failed}/{vcfc_urls_checked} URLs failed')
        else:
            dashboard.update_task('vcffinal', 'vcf_component_urls', TaskStatus.COMPLETE,
                                  f'{vcfc_urls_passed} URLs verified' if vcfc_urls_checked > 0 else '')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 7: Clear NSX Password Expiration & Fleet Policy Remediation
    # NSX password expiration can reappear after NSX service restarts.
    # The fleet password policy in ops-a caches old expiry dates until
    # remediation runs. This task re-clears expirations and triggers
    # remediation so the ops-a compliance dashboard shows green.
    #==========================================================================
    
    nsx_mgr_entries = lsf.get_config_list('VCF', 'vcfnsxmgr')
    nsx_users = ['admin', 'root', 'audit']
    nsx_expiry_days = 9999
    password = lsf.get_password()
    
    if nsx_mgr_entries:
        lsf.write_output(f'Setting NSX Manager password expiration to {nsx_expiry_days} days...')
        lsf.write_vpodprogress('NSX Password Config', 'GOOD-8')
        
        for entry in nsx_mgr_entries:
            nsx_host = entry.split(':')[0].strip()
            nsx_fqdn = f'{nsx_host}.site-a.vcf.lab' if '.' not in nsx_host else nsx_host
            
            if not dry_run:
                if not lsf.test_tcp_port(nsx_fqdn, 22, timeout=5):
                    lsf.write_output(f'  {nsx_fqdn}: SSH not reachable - skipping')
                    continue
                
                for user in nsx_users:
                    result = lsf.ssh(
                        f'set user {user} password-expiration {nsx_expiry_days}',
                        f'admin@{nsx_fqdn}', password
                    )
                    if result.returncode == 0:
                        lsf.write_output(f'  {nsx_fqdn}: {user} password expiration set to {nsx_expiry_days} days')
                    else:
                        lsf.write_output(f'  {nsx_fqdn}: WARNING - could not set {user} password expiration')
            else:
                lsf.write_output(f'  Would set {nsx_expiry_days}-day password expiration on {nsx_fqdn} for {nsx_users}')
    
    # Fleet password policy remediation via ops-a suite-api
    ops_fqdn = 'ops-a.site-a.vcf.lab'
    if lsf.test_tcp_port(ops_fqdn, 443, timeout=5):
        lsf.write_output('Triggering fleet password policy compliance check...')
        
        if not dry_run:
            import requests
            try:
                session = requests.Session()
                session.verify = False
                
                token_resp = session.post(
                    f'https://{ops_fqdn}/suite-api/api/auth/token/acquire',
                    json={'username': 'admin', 'password': password, 'authSource': 'local'},
                    headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                    timeout=30
                )
                token_resp.raise_for_status()
                token = token_resp.json()['token']
                
                headers = {
                    'Authorization': f'OpsToken {token}',
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-vRealizeOps-API-use-unsupported': 'true'
                }
                
                # Find MaxExpiration policy
                # This internal API may not exist on all VCF Operations versions
                # (e.g. absent in VCF 9.0.x, available in VCF 9.1+)
                query_resp = session.post(
                    f'https://{ops_fqdn}/suite-api/internal/passwordmanagement/policies/query',
                    headers=headers, json={}, timeout=30
                )
                if query_resp.status_code == 404:
                    lsf.write_output('  Password management API not available on this VCF Operations version - skipping')
                    policies = []
                else:
                    query_resp.raise_for_status()
                    policies = query_resp.json().get('vcfPolicies', [])
                
                policy_id = None
                for p in policies:
                    if p.get('policyInfo', {}).get('policyName') == 'MaxExpiration':
                        policy_id = p.get('policyId')
                        assigned = p.get('vcfPolicyAssignedResourceList', [])
                        lsf.write_output(f'  MaxExpiration policy found: {policy_id}')
                        lsf.write_output(f'  Assigned to {len(assigned)} resource(s)')
                        break
                
                if policy_id:
                    # Try remediation endpoint
                    rem_resp = session.post(
                        f'https://{ops_fqdn}/suite-api/internal/passwordmanagement/policies/{policy_id}/remediate',
                        headers=headers, json={}, timeout=60
                    )
                    if rem_resp.status_code in (200, 202, 204):
                        lsf.write_output('  Fleet policy remediation triggered successfully')
                    else:
                        lsf.write_output(f'  Fleet remediation API returned {rem_resp.status_code} '
                                         f'(normal for VCF 9.1 - remediation runs on next compliance scan)')
                else:
                    lsf.write_output('  MaxExpiration policy not found - skipping remediation')
                
            except Exception as e:
                lsf.write_output(f'  Fleet policy check failed: {e}')
        else:
            lsf.write_output('  Would trigger fleet policy compliance check/remediation')
    else:
        lsf.write_output(f'{ops_fqdn} not reachable - skipping fleet policy check')
    
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
    # But if VCF Automation VMs had critical errors AND no URLs were configured to verify,
    # we should still fail
    
    module_failed = False
    
    if vcfa_vms_task_failed:
        # Critical failure in VCF Automation VMs task
        if not vcfa_urls_configured:
            lsf.write_output('CRITICAL: VCF Automation VMs task failed and no URL checks configured to verify')
            module_failed = True
        elif urls_checked == 0:
            lsf.write_output('WARNING: VCF Automation VMs task failed but URL checks were skipped')
            module_failed = True
    
    # Supervisor Control Plane failure is critical
    if tanzu_control_configured and not tanzu_verify_ok:
        lsf.write_output(f'CRITICAL: Supervisor Control Plane did not reach RUNNING/READY state')
        lsf.write_output(f'  Last status: config={last_config_status or "unknown"}, k8s={last_k8s_status or "unknown"}')
        module_failed = True
    
    if module_failed and not dry_run:
        lsf.labfail(f'{MODULE_NAME} failed: Supervisor Control Plane not ready or VCF Automation errors')
    
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
