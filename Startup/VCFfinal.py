#!/usr/bin/env python3
# VCFfinal.py - HOLFY27 Core VCF Final Tasks Module
# Version 4.0 - February 2026
# Author - Burke Azbill and HOL Core Team
# VCF final tasks (Tanzu, Aria)

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
logging.basicConfig(level=logging.WARNING)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VCFfinal'
MODULE_DESCRIPTION = 'VCF final tasks (Tanzu, Aria)'

# Aria URL check configuration
ARIA_URL_MAX_RETRIES = 30  # Maximum attempts (30 minutes total)
ARIA_URL_RETRY_DELAY = 60  # Seconds between retries

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
    
    vcfmgmtcluster = []
    if lsf.config.has_option('VCF', 'vcfmgmtcluster'):
        vcfmgmtcluster_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
        vcfmgmtcluster = [h.strip() for h in vcfmgmtcluster_raw.split('\n') if h.strip()]
    
    if vcfmgmtcluster and not dry_run:
        lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3')
        lsf.connect_vcenters(vcfmgmtcluster)
    
    #==========================================================================
    # TASK 2: Supervisor Control Plane (Tanzu/WCP)
    #==========================================================================
    
    lsf.write_vpodprogress('Tanzu Start', 'GOOD-3')
    
    # Check for Tanzu Control Plane VMs
    tanzu_control_configured = lsf.config.has_option('VCFFINAL', 'tanzucontrol')
    
    if tanzu_control_configured and not dry_run:
        #----------------------------------------------------------------------
        # Determine vCenter host for WCP (look for wld vCenter in config)
        #----------------------------------------------------------------------
        wcp_vcenter = None
        if lsf.config.has_option('RESOURCES', 'vCenters'):
            vcenters_raw = lsf.config.get('RESOURCES', 'vCenters')
            for vc_line in vcenters_raw.split('\n'):
                vc_line = vc_line.strip()
                if vc_line and not vc_line.startswith('#') and 'wld' in vc_line.lower():
                    # Extract just the hostname (before the colon)
                    wcp_vcenter = vc_line.split(':')[0].strip()
                    break
        
        if not wcp_vcenter:
            wcp_vcenter = 'vc-wld01-a.site-a.vcf.lab'  # Default
        
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
        if lsf.config.has_option('RESOURCES', 'vCenters'):
            for vc_line in lsf.config.get('RESOURCES', 'vCenters').split('\n'):
                vc_line = vc_line.strip()
                if vc_line and 'wld' in vc_line.lower() and not vc_line.startswith('#'):
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
        
        tanzu_verify_ok = False
        supervisor_start_time = time.time()
        last_config_status = ''
        last_k8s_status = ''
        
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
                wcp_cmd = f'/bin/bash {check_fix_wcp_script} {wcp_vcenter}'
                result = lsf.run_command(wcp_cmd, timeout=WCP_SCRIPT_TIMEOUT)
                
                exit_code = result.returncode if hasattr(result, 'returncode') else (0 if result else 1)
                
                # Log stdout/stderr from the script for diagnostics
                if hasattr(result, 'stdout') and result.stdout:
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            lsf.write_output(f'  [WCP] {line.strip()}')
                if hasattr(result, 'stderr') and result.stderr:
                    for line in result.stderr.strip().split('\n'):
                        if line.strip():
                            lsf.write_output(f'  [WCP-ERR] {line.strip()}')
                
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
