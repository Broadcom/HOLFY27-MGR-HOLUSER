#!/usr/bin/env python3
# VCFfinal.py - HOLFY27 Core VCF Final Tasks Module
# Version 6.3.21 - 2026-06-17
# Author - Burke Azbill and HOL Core Team
# VCF final tasks (Tanzu, VCF Automation)
#
# v6.3.21 Changes:
# - Task 2e: Replaced conditional salt-raas fix with unconditional rollout-restart approach.
#   Root cause: After cold boot, cert rotation (vsp_cert_renewer) runs ~18s AFTER Redis
#   starts. Redis loads the old (expired) TLS cert into memory at startup and never
#   hot-reloads from the volume mount. RAAS Celery worker gets SSL CERTIFICATE_VERIFY_FAILED
#   when connecting to Redis and crash-loops for the entire startup window. salt-master gets
#   500/530 from the broken RAAS SSE API, salt-minion permanently stops.
#   Previous fix tried conditional detection (empty endpoints, CrashLoopBackOff, etc.) but
#   these checks are timing-dependent and missed the window on fresh labs.
#   New fix: unconditionally rollout-restart in dependency order:
#     (1) Fix pgdata permissions if pgdatabase-0 is not 3/3
#     (2) rollout restart redis (wait 30s)
#     (3) rollout restart raas  (wait 60s)
#     (4) rollout restart salt-master (wait 45s)
#     (5) rollout restart salt-minion
#   Each layer waits for kubectl rollout status to confirm completion before proceeding.
#   This ensures every component loads fresh post-rotation certs and connects to healthy deps.
#
# v6.3.20/6.3.19 Changes (superseded by v6.3.21):
# - Conditional salt-raas fixes (pgdata permissions, Redis cert endpoint check,
#   CrashLoopBackOff detection, salt-master/minion restart). Replaced by the
#   unconditional rollout-restart approach in v6.3.21 because conditional
#   detection was timing-dependent and missed the failure window on fresh labs.
# - kube-vip vip_preserve_on_leadership_loss + kube-controller-manager fix retained.
#
# v6.3.18 Changes:
# - End-of-startup fix for Fleet-LCM component friendly names in VCF Operations Manager.
#   Root cause: vcf-fleet-build-service ComponentPublicNameCache caches an empty VCF release
#   list (returned when vcf-fleet-upgrade-service is not yet ready at boot) for 2 hours via
#   Caffeine MemoizeWithFallback. The UI then displays internal type codes (OPS, VCFA) instead
#   of friendly product names. Fix: (1) POST /fleet-lcm/v1/depot-metadata?action=sync to
#   populate the upgrade-service release table, then (2) crictl stop+rm the fleet-build-service
#   container on the VSP node so kubelet restarts it and the cache reloads with correct names.
#   Replaces the unreliable "Fix JWT Parse Error" commented block. No DB or source changes.
#
# v6.3.12 Changes:
# - Task 4b Step 5b: Before the seaweedfs stale-pod check (Step 6), delete
#   any stuck support-bundle-cluster-info-dump-* jobs in vmsp-platform, verify
#   no dump pods remain, and suspend the support-bundle-cluster-info-dump
#   cronjob. These jobs accumulate during ungraceful shutdowns, consume resource
#   quota, and can block other pods from scheduling.
#
# v6.3.11 Changes:
# - Task 7 (NSX password expiration): Check current days-until-expiry via
#   NSX REST API (GET /api/v1/node/users/{id}) before updating each user.
#   Only applies 'set user {user} password-expiration 9999' when the current
#   expiry is ≤ 90 days.  Users that already expire far in the future or that
#   have no expiry configured are logged as SKIP, avoiding unnecessary CLI
#   writes on every lab startup.  Falls back to updating if the API returns
#   an unexpected value.  NSX user ID map: root=0, admin=10000, audit=10002.
#
# v6.3.10 Changes:
# - Task 5 (VCF Automation checks): After vcfapwcheck.sh, run 'chage -M -1
#   vmware-system-user' on all auto-* and opslogs-* VMs at every startup.
#   Root cause: confighol sets chage at template-prep time, but on first
#   deployment vcfapass.sh changes the password (resetting last_change) without
#   clearing maxdays — starting a fresh 365-day clock.  Running chage -M -1 at
#   startup is idempotent and corrects both the template state and any subsequent
#   password changes.  Hosts discovered from vraurls (auto-*) and
#   vcfcomponenturls (opslogs-*).
# - vcfapass.sh v1.3: Companion fix — added 'chage -M -1 vmware-system-user'
#   after passwd reset; switched to generic prompt patterns (dropped hardcoded
#   'auto-a-8fpl5' hostname that would never match real deployments).
#
# v6.3.9 Changes:
# - Task 8: After distribute_vault_ca_trust(), also import the Vault root CA
#   into Firefox on the console VM via confighol.import_ca_to_firefox_profile()
#   and confighol.find_firefox_profiles(). Previously this only happened when
#   confighol-9.1.py was run interactively; VCFfinal Task 8 skipped Firefox,
#   causing vpodchecker to report FAIL "Active Vault CA not found in Firefox
#   certificate store" on every lab boot.
#
# v6.3.8 Changes:
# - Gate ALL proxy and NO_PROXY operations on LabTypeLoader.requires_proxy_filter().
#   Only HOL labtype returns True; DISCOVERY, VXP, ATE, EDU skip proxy entirely.
#   Proxy and NO_PROXY are treated as a pair — if proxy is not required, neither
#   value is written to any target.
# - Task 2c: supervisor_stabilizer.py call now appends --skip-vcenter-proxy and
#   --skip-proxy when _proxy_required=False, so Phase 0 (vCenter PROXY/NO_PROXY)
#   and Phase 2 (SCP PROXY/NO_PROXY) are skipped; cert phases still run.
# - Task 2e: apply_proxy_to_nodes() calls (initial + new-node discovery) wrapped
#   in 'if _proxy_required:' so PROXY_URL and NO_PROXY are only pushed to VSP
#   nodes for HOL lab types.
#
# v6.3.7 Changes:
# - status_dashboard: add k8s_certs task (RUNNING/COMPLETE/FAILED/SKIPPED)
#   around vsp_cert_renewer.py calls in Task 2e; add SKIPPED to all
#   early-exit branches (no vCenters, no Supervisor configured).
#
# v6.3.6 Changes:
# - Task 2e: reduce vsp_cert_renewer streaming output indentation from 4
#   spaces to 1 space so cert log lines are not excessively deep.
#
# v6.3.5 Changes:
# - Task 2e: pass --no-timestamps to vsp_cert_renewer.py so its internal
#   [timestamp] prefix is suppressed and lsf.write_output's timestamp is
#   the only one shown (matches supervisor_stabilizer.py convention).
#
# v6.3.4 Changes:
# - Task 2e: added K8s certificate check/renewal block (vsp_cert_renewer.py)
#   immediately after the NO_PROXY configuration, before component scale-up.
#   Runs once for VSP and once for VCFA via streaming Popen (same pattern as
#   the supervisor_stabilizer call in Task 2c).  THRESHOLD=365d, renews to
#   5 years via kubeadm --config certificateValidityPeriod.  Non-fatal.
#
# v6.3.3 Changes:
# - Task 2c: replaced lsf.run_command() (blocks until completion, dumps all
#   output at once) with subprocess.Popen streaming so supervisor_stabilizer.py
#   output appears in the log in real time instead of after a ~5-minute gap.
#   Uses python3 -u + PYTHONUNBUFFERED=1 to disable Python stdio buffering.
# - Removed inline 'import subprocess' inside main() (Task 4 vcfa-stabilizer
#   block). The inline import made subprocess a local variable for the entire
#   main() scope, causing UnboundLocalError at the Task 2c Popen call.
#   subprocess is already imported at module level (line 145).
#
# v6.3.2 Changes:
# - Fixed dashboard updates
# v6.3.1 Changes:
# - Fixed Step 0 awk quoting bug: replaced awk '{print $1}' with cut -d" " -f1 to
#   avoid single-quote clash inside the bash -c '...' wrapper used by vcfa_ssh, and
#   to prevent $1 from being expanded to empty by the local shell inside lsf.ssh
#   double-quoted command argument.
#
# v6.3 Changes:
# - Fixed Step 14 prelude StatefulSet check: moved it outside the "if zero_deps:"
#   block so it always runs unconditionally. Previously, rabbitmq-ha, tenant-manager,
#   and vco-app StatefulSets (all 0/0 replicas after shutdown) were never scaled up
#   when all Deployments were already healthy. Without RabbitMQ and tenant-manager,
#   api-gateway-server crashes with OIDC JWKS "connection refused" and /login/ returns
#   HTTP 500 for the entire 30-minute URL-check window.
# - Step 14b (vmsp-operator re-cordon check) now triggers on "zero_deps or zero_sts"
#   instead of only on zero_deps.
#
# v6.2 Changes:
# - Added Step 0: Delete all stale system-shutdown Argo Workflows before any
#   uncordon/startup work. Each Fleet LCM shutdown creates an Argo Workflow in
#   vmsp-platform that is persisted across reboots. On startup, the Argo controller
#   resumes any Running workflow, which re-cordons the node, scales all prelude
#   deployments to 0, and runs shutdown scripts — making VCFA inaccessible (HTTP 500).
#   Up to 30+ stale workflows can accumulate, each waiting on lock-vmsp-platform
#   mutex. Deleting them BEFORE uncordon prevents the re-cordon race condition.
# - Fixed microservices scaling section to use sudo -S -i bash -c pattern (VCF 9.1
#   requires password for sudo; the old sudo -S kubectl form fails without login shell).
#
# v6.1 Changes:
# - VSP node proxy config now uses lsf.LAB_PROXY_URL and lsf.build_lab_no_proxy()
#   instead of a local hardcoded NO_PROXY_PARTS list.
#
# v6.0 Changes:
# - Replaced check_fix_wcp.sh with the new unified supervisor_stabilizer.py script
#   which handles multi-vCenter auto-discovery, proxy config, cert rotation, and
#   workload recovery in a single idempotent pass.
#
# v5.9 Changes:
# - Added Step 14b: Post-scale node re-check.
#   After scaling up prelude deployments (Step 14), vmsp-operator
#   (system:serviceaccount:vmsp:vmsp-operator) re-cordons the K8s node
#   ~90 seconds later via its startup reconciliation loop, which reads a
#   stale "maintenance in progress" state from the previous shutdown.
#   Step 14b waits 120s for this to happen, then overrides the cordon.
# - Added URL-loop node monitor: every 3 attempts in the VCFA URL check
#   loop, re-checks node scheduling status and uncordons if needed.
#   Catches any re-cordon that slips through Step 14b.
#
# v5.8 Changes:
# - opslogs rescue now retries on EVERY attempt >= 5 (was: only once at attempt 5).
#   The VSP cluster VIP (10.1.1.142) can take 90+ minutes to stabilize after cold boot,
#   so the single attempt at minute 5 silently failed every time. Now the rescale fires
#   on each retry loop and logs the kubectl output so failures are visible.
#   Only annotates the component CRD when the rescale actually succeeds (scaled/unchanged).
#
# v5.7 Changes:
# - Added a Failure Tracking Queue
# - Add retry logic for components that failed to scale initially, executing after the node provision wait loop.
#
# v5.6 Changes:
# - Add ops-logs StatefulSet rescale if the URL remains unreachable after 5 attempts.
#
# v5.5 Changes:
# - Moved Component CRD annotation updates (NotRunning -> Running) to execute BEFORE 
#   scaling up K8s workloads in Task 2e. This prevents a race condition where vmsp-operator 
#   immediately scales pods back down to 0 before the annotation can be applied.
# - Removed redundant explicit power-on logic for Ops Logs VMs, as they are already 
#   powered on during VCF.py phase.
#
# v5.1 Changes:
# - Moved JWT Parse Error fix (vcf-fleet-lcm and vidb pod restarts) in Task 2e to execute 
#   *after* Ops Logs and Ops Networks VMs are powered on, ensuring UI components show
#   friendly names rather than internal names.
#
# v5.0 Changes:
# - Added logic to Task 4 to isolate Automation VMs on a dedicated ESXi host.
#
# v4.9 Changes:
# - Added explicit power-on logic for Ops Logs VMs in Task 2e since opslogs is a mix
#   of K8s services and dedicated VMs (just like opsnet).
#
# v4.8 Changes:
# - Added dynamic NO_PROXY injection for VSP cluster nodes in Task 2e to prevent
#   ImagePullBackOff for opsnet/opslogs components during scale-up.
#
# v4.7 Changes:
# - Fixed Supervisor DNS Health Check: added missing newline after '='*60
#   to prevent merging with the next log message.
#
# v4.6 Changes:
# - Task 8b: Optional Authentik + VCF integration when [VCFFINAL] authentik_vcf_integration=true
#   (runs Tools/authentik_vcf_integration.py after Vault CA distribution).
# - Dynamically determine Supervisor Service namespaces to fix based on config.ini [VCFFINAL] supervisorservicedns.
# - Added Task 2c4: Supervisor Service vSphere Pod DNS Fix Cleanup
#   After the DNS fix, the kube-dns-lb LoadBalancer external VIP is no longer needed,
#   so it is removed from the carvel-services-overlay secret and the affected
#   Deployments/StatefulSets are patched to remove the dnsPolicy/dnsConfig fields.
#
# v4.5 Changes:
# - Now properly handles supervisor checking across management domains, 
#   workload domains, or mixed environments with the correct SSO 
#   authentication for each vCenter.
#
# v4.4 Changes:
# - Added Task 2c3: Supervisor Service vSphere Pod DNS Fix
#   On retrofitted VCF 9.0.x labs with the new Photon/FRR holorouter,
#   the NSX DLB cannot route to CoreDNS pod IPs (172.16.200.x) inside
#   the SCP Antrea overlay, breaking DNS for all vSphere Pods.  Task 2c3
#   detects this by checking dnsPolicy on workloads in namespaces listed
#   in config.ini [VCFFINAL] supervisorservicedns, then patches them to
#   use the kube-dns-lb LoadBalancer VIP.  To prevent kapp from reverting
#   the patch during its 10-minute reconciliation cycle, it also injects
#   kapp rebase rules into the carvel-services-overlay secret.


import os
import sys
import argparse
import logging
import ssl
import time
import json
import subprocess

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
            return []
        
        # Return ALL clusters, not just the first one
        all_clusters = []
        for cluster in clusters:
            cluster_info = {
                'config_status': cluster.get('config_status', ''),
                'kubernetes_status': cluster.get('kubernetes_status', ''),
                'api_servers': cluster.get('api_servers', []),
                'cluster_id': cluster.get('cluster', ''),
                'cluster_name': cluster.get('cluster_name', ''),
            }
            all_clusters.append(cluster_info)
        
        return all_clusters
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
    # wcp_vcenter is the first task that actually executes in VCFfinal, so mark it RUNNING at init
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    # Determine whether this lab type requires proxy/NO_PROXY configuration.
    # Only HOL lab types have firewall + proxy filtering; all others (DISCOVERY,
    # VXP, ATE, EDU) have direct internet access and need neither PROXY nor
    # NO_PROXY settings.  PROXY and NO_PROXY are always treated as a pair:
    # if proxy is not required, neither value is written to any target.
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from labtypes import LabTypeLoader
        _proxy_required = LabTypeLoader(
            lsf.labtype, '/home/holuser/hol'
        ).requires_proxy_filter()
    except Exception:
        _proxy_required = True  # safe default: always configure proxy if unsure
    lsf.write_output(
        f'Labtype: {lsf.labtype} — proxy/NO_PROXY configuration required: {_proxy_required}'
    )

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
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.FAILED,
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
            dashboard.update_task('vcffinal', 'wcp_dns', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'svc_dns', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'vcfa_vms', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'vcfa_k8s_health', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'vcfa_urls', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'k8s_certs', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.update_task('vcffinal', 'nsx_passwords', TaskStatus.SKIPPED,
                                  'No vCenters configured')
            dashboard.generate_html()
        lsf.write_output('VCFfinal completed (no VCF resources)')
        return True
    
    lsf.write_vpodprogress('Tanzu Start', 'GOOD-3')
    
    # Check for Supervisor Control Plane VMs - requires tanzucontrol option with valid (non-commented) values
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
        # TASK 2a2: POWER ON - Ensure Supervisor Control Plane VMs are running
        # On VCF 9.0.x, SCP VMs are EAM-managed on WLD cluster ESXi hosts.
        # They are powered off during clean shutdown and must be started
        # before the Supervisor can reach RUNNING/READY state.
        # vCenter may deny PowerOnVM_Task with NoPermission (EAM restriction),
        # so we fall back to direct ESXi host connections when needed.
        # On VCF 9.1.x with VSP cluster, SCP VMs are not present on WLD ESXi
        # hosts, so this is effectively a no-op.
        #----------------------------------------------------------------------
        lsf.write_output('='*60)
        lsf.write_output('Checking Supervisor Control Plane VM Power State')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('SCP VM Power Check', 'GOOD-3')

        try:
            import ssl as _ssl_scp
            scp_password = lsf.get_password()
            scp_ctx = _ssl_scp._create_unverified_context()

            scp_si = connect.SmartConnect(
                host=wcp_vcenter,
                user=f'administrator@{sso_domain}',
                pwd=scp_password,
                sslContext=scp_ctx
            )
            scp_content = scp_si.RetrieveContent()
            scp_container = scp_content.viewManager.CreateContainerView(
                scp_content.rootFolder, [vim.VirtualMachine], True
            )

            scp_vms_off = []
            for scp_vm in scp_container.view:
                if 'SupervisorControlPlane' in scp_vm.name:
                    host_name = scp_vm.runtime.host.name if scp_vm.runtime.host else 'unknown'
                    lsf.write_output(f'  {scp_vm.name}: power={scp_vm.runtime.powerState}, host={host_name}')
                    if scp_vm.runtime.powerState != 'poweredOn':
                        scp_vms_off.append((scp_vm, host_name))
            scp_container.Destroy()

            if not scp_vms_off:
                lsf.write_output('All Supervisor Control Plane VMs are already powered on')
            else:
                lsf.write_output(f'{len(scp_vms_off)} SCP VM(s) need to be powered on')

                # Try powering on via vCenter first
                vc_power_failed = False
                for scp_vm, host_name in scp_vms_off:
                    try:
                        scp_vm.PowerOnVM_Task()
                        lsf.write_output(f'  {scp_vm.name}: PowerOn submitted via vCenter')
                    except vim.fault.NoPermission:
                        lsf.write_output(f'  {scp_vm.name}: NoPermission via vCenter (EAM-managed)')
                        vc_power_failed = True
                        break
                    except Exception as scp_err:
                        lsf.write_output(f'  {scp_vm.name}: PowerOn via vCenter failed: {scp_err}')
                        vc_power_failed = True
                        break

                # Fallback: connect directly to ESXi hosts
                if vc_power_failed:
                    lsf.write_output('Falling back to direct ESXi host connections for SCP power-on...')
                    esxi_hosts_needed = set(h for _, h in scp_vms_off)

                    for esxi_host in esxi_hosts_needed:
                        try:
                            esxi_si = connect.SmartConnect(
                                host=esxi_host,
                                user='root',
                                pwd=scp_password,
                                sslContext=scp_ctx
                            )
                            esxi_content = esxi_si.RetrieveContent()
                            esxi_container = esxi_content.viewManager.CreateContainerView(
                                esxi_content.rootFolder, [vim.VirtualMachine], True
                            )
                            for esxi_vm in esxi_container.view:
                                if 'SupervisorControlPlane' in esxi_vm.name and esxi_vm.runtime.powerState != 'poweredOn':
                                    try:
                                        esxi_vm.PowerOnVM_Task()
                                        lsf.write_output(f'  {esxi_vm.name}: PowerOn submitted via {esxi_host}')
                                    except Exception as esxi_err:
                                        lsf.write_output(f'  {esxi_vm.name}: PowerOn via {esxi_host} FAILED: {esxi_err}')
                            esxi_container.Destroy()
                            connect.Disconnect(esxi_si)
                        except Exception as esxi_conn_err:
                            lsf.write_output(f'  WARNING: Could not connect to {esxi_host}: {esxi_conn_err}')

                lsf.write_output('SCP VM power-on tasks submitted, waiting 60s for boot...')
                time.sleep(60)

            connect.Disconnect(scp_si)
        except Exception as scp_task_err:
            lsf.write_output(f'WARNING: SCP power check/start failed: {scp_task_err}')
            lsf.write_output('  Continuing to Supervisor status polling...')

        #----------------------------------------------------------------------
        # TASK 2b: VERIFY - Multi-vCenter Supervisor Status Check
        # Enhanced supervisor verification that checks ALL vCenters for supervisors.
        # This supports environments with supervisors in both management and
        # workload domains, using the correct SSO domain for each vCenter.
        # Polls every WCP_POLL_INTERVAL seconds up to WCP_MAX_POLL_TIME.
        #----------------------------------------------------------------------
        lsf.write_output('='*60)
        lsf.write_output('Verifying Supervisor Control Plane Status (Multi-vCenter)')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('Supervisor Control Plane', 'GOOD-3')
        
        # Build list of vCenters to check for supervisors
        vcenter_targets = []
        for vc_line in vcenters_list:
            vc_parts = vc_line.split(':')
            vc_host = vc_parts[0].strip()
            
            # Determine SSO domain for this specific vCenter
            vc_sso_domain = 'vsphere.local'  # Default for management
            if len(vc_parts) >= 3:
                user_part = vc_parts[2].strip()
                if '@' in user_part:
                    vc_sso_domain = user_part.split('@')[1]
            
            vcenter_targets.append({
                'host': vc_host,
                'sso_domain': vc_sso_domain
            })
        
        lsf.write_output(f'Will check {len(vcenter_targets)} vCenter(s) for supervisors:')
        for target in vcenter_targets:
            lsf.write_output(f'  - {target["host"]} (SSO: {target["sso_domain"]})')
        
        supervisor_start_time = time.time()
        last_overall_status = "No supervisors found"
        wcp_active_vcenters = []
        
        try:
            while (time.time() - supervisor_start_time) < WCP_MAX_POLL_TIME:
                elapsed = int(time.time() - supervisor_start_time)
                
                # Check all vCenters for supervisors
                all_supervisor_clusters = {}
                total_clusters = 0
                ready_clusters = 0
                error_clusters = 0
                
                for target in vcenter_targets:
                    vc_host = target['host']
                    vc_sso_domain = target['sso_domain']
                    
                    sup_clusters = check_supervisor_status_api(lsf, vc_host, vc_sso_domain)
                    
                    if sup_clusters:  # sup_clusters is now a list
                        if vc_host not in wcp_active_vcenters:
                            wcp_active_vcenters.append(vc_host)
                        all_supervisor_clusters[vc_host] = sup_clusters
                        total_clusters += len(sup_clusters)
                        
                        for cluster in sup_clusters:
                            config_status = cluster.get('config_status', '')
                            k8s_status = cluster.get('kubernetes_status', '')
                            
                            if config_status == 'RUNNING' and k8s_status == 'READY':
                                ready_clusters += 1
                            elif config_status == 'ERROR':
                                error_clusters += 1
                
                if total_clusters == 0:
                    lsf.write_output(f'  No supervisor clusters found on any vCenter - waiting... ({elapsed}s / {WCP_MAX_POLL_TIME}s)')
                    last_overall_status = "No supervisor clusters found"
                else:
                    # Report status of all clusters
                    lsf.write_output(f'  Found {total_clusters} supervisor cluster(s): {ready_clusters} ready, {error_clusters} error, {total_clusters - ready_clusters - error_clusters} pending ({elapsed}s / {WCP_MAX_POLL_TIME}s)')
                    
                    for vc_host, clusters in all_supervisor_clusters.items():
                        for cluster in clusters:
                            config_status = cluster.get('config_status', '')
                            k8s_status = cluster.get('kubernetes_status', '')
                            cluster_name = cluster.get('cluster_name', 'unknown')
                            api_servers = cluster.get('api_servers', [])
                            
                            status_str = f'config={config_status}, k8s={k8s_status}'
                            lsf.write_output(f'    {vc_host}: "{cluster_name}" -> {status_str}')
                            
                            if api_servers and config_status == 'RUNNING' and k8s_status == 'READY':
                                lsf.write_output(f'      API servers: {", ".join(api_servers)}')
                    
                    last_overall_status = f'{ready_clusters}/{total_clusters} ready, {error_clusters} error'
                    
                    # Check if all clusters are ready
                    if ready_clusters == total_clusters and total_clusters > 0:
                        lsf.write_output(f'All {total_clusters} supervisor cluster(s) are RUNNING and READY!')
                        tanzu_verify_ok = True
                        break
                    elif error_clusters > 0:
                        lsf.write_output(f'  {error_clusters} supervisor cluster(s) in ERROR state')
                        lsf.write_output(f'    Check Supervisor Management in vCenter UI for details')
                        break
                
                time.sleep(WCP_POLL_INTERVAL)
            
            if not tanzu_verify_ok:
                if (time.time() - supervisor_start_time) >= WCP_MAX_POLL_TIME:
                    lsf.write_output(f'  Supervisors did not reach RUNNING/READY within {WCP_MAX_POLL_TIME // 60} minutes')
                lsf.write_output(f'  Final status: {last_overall_status}')
                
        except Exception as e:
            lsf.write_output(f'Error verifying Supervisor status: {e}')
        
        if tanzu_verify_ok:
            lsf.write_output('Supervisor Control Plane: All clusters RUNNING and READY')
        else:
            lsf.write_output('Supervisor Control Plane: Not all clusters ready')
            lsf.write_output('  check_fix_wcp.sh will attempt to wait and fix certificates')
        
        if dashboard:
            if tanzu_verify_ok:
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
            else:
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.FAILED,
                                      last_overall_status)
            dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.RUNNING)
            dashboard.update_task('vcffinal', 'wcp_dns', TaskStatus.RUNNING)
            dashboard.update_task('vcffinal', 'svc_dns', TaskStatus.RUNNING)
            dashboard.generate_html()
        
        #----------------------------------------------------------------------
        # TASK 2c: POST-VERIFY - Run supervisor_stabilizer.py for certificates/webhooks
        # This script has its own internal polling (30s intervals, 30m max).
        # It waits for SCP to become fully accessible before running fixes.
        # (the script that checks and starts the vapi-endpoint, trustmanagement, 
        # and wcp services) into Python natively within VCFfinal.py.
        #----------------------------------------------------------------------
        lsf.write_output('='*60)
        lsf.write_output('Supervisor Stabilization (post-verify)')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('Supervisor Stabilization', 'GOOD-3')
        
        supervisor_stabilizer_script = '/home/holuser/hol/Tools/supervisor_stabilizer.py'
        wcp_certs_ok = True
        
        if os.path.isfile(supervisor_stabilizer_script):
            # Use the vCenters where we actually found Supervisors
            if not wcp_active_vcenters:
                lsf.write_output('  No active Supervisor clusters found on any vCenter. Skipping stabilization script.')
                wcp_certs_ok = True
            else:
                lsf.write_output(f'Running: python3 {supervisor_stabilizer_script} --auto')
                lsf.write_output(f'  (script will auto-discover and stabilize all active Supervisors)')
                
                try:
                    # Stream output line-by-line so the log shows progress in
                    # real time rather than buffering the entire ~5-minute run
                    # and dumping it all at once after the script exits.
                    # -u forces Python unbuffered I/O; PYTHONUNBUFFERED=1 is a
                    # belt-and-suspenders complement for any C-level buffering.
                    env = os.environ.copy()
                    env['PYTHONUNBUFFERED'] = '1'
                    _stabilizer_cmd = ['python3', '-u', supervisor_stabilizer_script, '--auto']
                    if not _proxy_required:
                        # Non-HOL labtype: skip Phase 0 (vCenter PROXY/NO_PROXY)
                        # and Phase 2 (SCP PROXY/NO_PROXY); cert phases still run.
                        _stabilizer_cmd += ['--skip-vcenter-proxy', '--skip-proxy']
                        lsf.write_output(
                            '  Labtype does not require proxy — '
                            'skipping vCenter and SCP proxy phases'
                        )
                    proc = subprocess.Popen(
                        _stabilizer_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        env=env,
                    )
                    start_time = time.time()
                    for line in proc.stdout:
                        if time.time() - start_time > WCP_SCRIPT_TIMEOUT:
                            proc.kill()
                            lsf.write_output('  WARNING: Supervisor stabilization script timed out')
                            break
                        line_stripped = line.rstrip('\n')
                        if line_stripped.strip():
                            lsf.write_output(f'  {line_stripped.strip()}')
                    proc.wait()
                    exit_code = proc.returncode if proc.returncode is not None else 1

                    if exit_code == 0:
                        lsf.write_output(f'Supervisor stabilization completed successfully')
                    else:
                        lsf.write_output(f'WARNING: Supervisor stabilization script exited with code {exit_code}')
                        wcp_certs_ok = False

                    # ---- Target 3: Supervisor API cluster_proxy_config (SET or CLEAR) ----
                    # Runs after stabilizer so the vCenter API is reachable.
                    _wld_vc_for_proxy = wcp_vcenter if wcp_vcenter else 'vc-wld01-a.site-a.vcf.lab'
                    try:
                        if _proxy_required:
                            lsf.write_output('  Configuring Supervisor API proxy (Target 3)...')
                            lsf.set_supervisor_api_proxy(
                                _wld_vc_for_proxy, 'administrator@wld.sso',
                                lsf.get_password(), dry_run=dry_run,
                            )
                        else:
                            lsf.write_output('  Clearing Supervisor API proxy (Target 3)...')
                            lsf.clear_supervisor_api_proxy(
                                _wld_vc_for_proxy, 'administrator@wld.sso',
                                lsf.get_password(), dry_run=dry_run,
                            )
                    except Exception as _t3_err:
                        lsf.write_output(f'  WARNING: Supervisor API proxy step skipped: {_t3_err}')

                except Exception as wcp_err:
                    lsf.write_output(f'WARNING: Error running Supervisor stabilization script: {wcp_err}')
                    lsf.write_output('  Continuing with startup - WCP may need manual attention')
                    wcp_certs_ok = False
        else:
            lsf.write_output(f'Supervisor stabilization script not found: {supervisor_stabilizer_script}')
            lsf.write_output('  Skipping stabilization - manual intervention may be needed')
        
        #----------------------------------------------------------------------
        # Final Supervisor Status Reconciliation
        # If either tanzu_verify or wcp_certs failed during their initial check,
        # re-check the authoritative Supervisor API one final time.
        # This handles the case where the Supervisor was still starting up
        # during earlier checks but is now fully running.
        #----------------------------------------------------------------------
        if not tanzu_verify_ok or not wcp_certs_ok:
            lsf.write_output('='*60)
            lsf.write_output('Final Supervisor Status Check (Multi-vCenter)')
            lsf.write_output('='*60)
            
            # Re-check all vCenters for final status
            final_ready_clusters = 0
            final_total_clusters = 0
            final_all_ready = True
            
            for target in vcenter_targets:
                vc_host = target['host']
                vc_sso_domain = target['sso_domain']
                
                final_clusters = check_supervisor_status_api(lsf, vc_host, vc_sso_domain)
                
                if final_clusters:
                    final_total_clusters += len(final_clusters)
                    
                    for cluster in final_clusters:
                        config_status = cluster.get('config_status', '')
                        k8s_status = cluster.get('kubernetes_status', '')
                        cluster_name = cluster.get('cluster_name', 'unknown')
                        
                        if config_status == 'RUNNING' and k8s_status == 'READY':
                            final_ready_clusters += 1
                        else:
                            final_all_ready = False
                        
                        lsf.write_output(f'  {vc_host}: "{cluster_name}" -> config={config_status}, k8s={k8s_status}')
            
            if final_all_ready and final_total_clusters > 0:
                lsf.write_output(f'All {final_total_clusters} supervisor cluster(s) are now RUNNING and READY!')
                
                # Override dashboard status since all Supervisors are healthy
                if not tanzu_verify_ok:
                    lsf.write_output('  Updating Supervisor Control Plane status to COMPLETE')
                    tanzu_verify_ok = True
                if not wcp_certs_ok:
                    lsf.write_output('  Updating WCP Certificate Fix status to COMPLETE')
                    lsf.write_output('  (All supervisors are healthy - certificate fix may not have been needed)')
                    wcp_certs_ok = True
            else:
                lsf.write_output(f'Final supervisor status: {final_ready_clusters}/{final_total_clusters} clusters ready')
        
        if dashboard:
            if tanzu_verify_ok:
                dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.COMPLETE)
            # else: already marked FAILED above
            if wcp_certs_ok:
                dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.COMPLETE)
                dashboard.update_task('vcffinal', 'wcp_dns', TaskStatus.COMPLETE)
                dashboard.update_task('vcffinal', 'svc_dns', TaskStatus.COMPLETE)
            else:
                dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.FAILED, 'See log')
                dashboard.update_task('vcffinal', 'wcp_dns', TaskStatus.FAILED, 'See log')
                dashboard.update_task('vcffinal', 'svc_dns', TaskStatus.FAILED, 'See log')
            dashboard.update_task('vcffinal', 'tanzu_deploy', TaskStatus.RUNNING)
            dashboard.generate_html()

    else:
        lsf.write_output('No Supervisor Control Plane VMs configured')
        if dashboard:
            dashboard.update_task('vcffinal', 'wcp_vcenter', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'tanzu_control', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'wcp_certs', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'wcp_dns', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'svc_dns', TaskStatus.SKIPPED, 'Not configured')
            dashboard.update_task('vcffinal', 'k8s_certs', TaskStatus.SKIPPED, 'No Supervisor configured')
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
                lsf.write_output(f'Processing {len(vspvms)} VSP Platform VM entries...')
                lsf.write_vpodprogress('VSP Platform VMs', 'GOOD-3')
                
                # Resolve all regex/name patterns to actual VM objects once
                resolved_vsp_vms = []
                for vspvm in vspvms:
                    parts = vspvm.split(':')
                    vmname = parts[0].strip()
                    try:
                        vms = lsf.get_vm_match(vmname)
                        if vms:
                            for vm in vms:
                                resolved_vsp_vms.append(vm)
                                lsf.write_output(f'  Resolved: {vmname} -> {vm.name}')
                        else:
                            lsf.write_output(f'  Warning: No VMs matched pattern "{vmname}"')
                    except Exception as e:
                        lsf.write_output(f'  Warning: Error resolving pattern "{vmname}": {e}')
                
                if not resolved_vsp_vms:
                    lsf.write_output('No VSP Platform VMs found after resolving patterns')
                else:
                    lsf.write_output(f'Resolved {len(resolved_vsp_vms)} VSP Platform VMs')
                    
                    # Check if all VSP VMs are already running with Tools active
                    all_running = True
                    for vm in resolved_vsp_vms:
                        try:
                            if vm.runtime.powerState != 'poweredOn':
                                all_running = False
                                break
                            if vm.summary.guest.toolsRunningStatus != 'guestToolsRunning':
                                all_running = False
                                break
                        except Exception:
                            all_running = False
                            break
                    
                    if all_running:
                        lsf.write_output('All VSP Platform VMs already running with Tools active - skipping startup')
                    else:
                        # Connect NICs before starting
                        for vm in resolved_vsp_vms:
                            try:
                                if vm.runtime.powerState != 'poweredOn':
                                    verify_nic_connected(lsf, vm, simple=True)
                                else:
                                    lsf.write_output(f'{vm.name} already powered on, skipping NIC connect')
                            except Exception as e:
                                lsf.write_output(f'Warning: Error checking NICs for {vm.name}: {e}')
                        
                        # Start the VMs
                        try:
                            lsf.start_nested(vspvms)
                        except Exception as e:
                            error_msg = f'Failed to start VSP Platform VMs: {e}'
                            lsf.write_output(error_msg)
                            vsp_vms_errors.append(error_msg)
                        
                        # After starting, verify VMs are actually powered on and tools running
                        for vm in resolved_vsp_vms:
                            try:
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
                                    lsf.write_output(f'Waiting for Tools in {vm.name}...')
                                    lsf.labstartup_sleep(lsf.sleep_seconds)
                                    tools_attempt += 1
                                
                                # Verify NIC is connected after tools are running
                                try:
                                    verify_nic_connected(lsf, vm, simple=False)
                                except Exception as nic_err:
                                    lsf.write_output(f'Warning: Post-start NIC verification failed for {vm.name}: {nic_err}')
                            
                            except Exception as e:
                                lsf.write_output(f'Warning: Error waiting for {vm.name}: {e}')
                
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
                # ---- Restore VSP cluster kube-vip VIP if it dropped ----
                # After cold boot, kube-vip may not have re-claimed the VIP (10.1.1.142).
                # The control plane node we discovered has admin.conf and can run kubectl,
                # but kubectl will fail if it tries to reach the VIP and it isn't assigned.
                # Restore the VIP on eth0 and send a gratuitous ARP before proceeding.
                vsp_vip = '10.1.1.142'
                if not lsf.test_ping(vsp_vip):
                    lsf.write_output(f'  VSP VIP {vsp_vip} is unreachable — restoring via ip addr add + arping')
                    restore_cmd = (
                        f"ip addr add {vsp_vip}/32 dev eth0 2>/dev/null || true; "
                        f"arping -c 3 -U -I eth0 {vsp_vip} 2>/dev/null || true"
                    )
                    vip_ssh_cmd = f"echo '{lsf.get_password()}' | sudo -S -i bash -c '{restore_cmd}'"
                    lsf.ssh(vip_ssh_cmd, f'{vsp_user}@{vsp_control_plane_ip}', lsf.get_password())
                    lsf.write_output(f'  VSP VIP {vsp_vip} restored on {vsp_control_plane_ip}')
                else:
                    lsf.write_output(f'  VSP VIP {vsp_vip} is reachable — no restore needed')

                # ---- Fix kube-vip vip_preserve_on_leadership_loss + clear kube-controller-manager backoff ----
                # kube-vip panics during leader election when the K8s API is briefly busy,
                # removes the VIP (vip_preserve_on_leadership_loss=false default), then crashes.
                # Without the VIP: kube-scheduler and kube-controller-manager lose their API
                # connection, crash, and enter a 5-minute CrashLoopBackOff. The stalled
                # kube-controller-manager no longer updates Endpoints/EndpointSlices, so
                # restarted pods (e.g. Redis in salt-raas) are never added to their Services.
                # Fix step 1: set vip_preserve_on_leadership_loss=true so the VIP stays on
                # the interface even if kube-vip panics. This breaks the crash cascade.
                # Fix step 2: force-remove the kube-controller-manager container if it is in
                # CrashLoopBackOff so kubelet immediately creates a fresh one.
                lsf.write_output('  Fixing kube-vip and kube-controller-manager on VSP control plane...')
                try:
                    _cp_ssh = f'{vsp_user}@{vsp_control_plane_ip}'

                    # Step 1: patch kube-vip manifest
                    _kvip_result = lsf.ssh(
                        f"echo '{password}' | sudo -S sed -i "
                        f"'/vip_preserve_on_leadership_loss/{{n; s/\"false\"/\"true\"/}}' "
                        f"/etc/kubernetes/manifests/kube-vip.yaml 2>/dev/null && "
                        f"echo '{password}' | sudo -S grep -A1 vip_preserve "
                        f"/etc/kubernetes/manifests/kube-vip.yaml 2>/dev/null",
                        _cp_ssh
                    )
                    _kvip_out = getattr(_kvip_result, 'stdout', '') or ''
                    if 'true' in _kvip_out:
                        lsf.write_output('  kube-vip: vip_preserve_on_leadership_loss=true confirmed')
                    else:
                        lsf.write_output('  kube-vip: manifest patch returned no confirmation (may already be set)')

                    # Step 2: check whether kube-controller-manager is in CrashLoopBackOff
                    # (kubelet back-off 5m0s) and force-remove its container so kubelet
                    # immediately creates a fresh one without waiting for the backoff.
                    _kcm_ps = lsf.ssh(
                        f"echo '{password}' | sudo -S crictl ps -a 2>/dev/null | grep kube-controller",
                        _cp_ssh
                    )
                    _kcm_out = getattr(_kcm_ps, 'stdout', '') or ''
                    _kcm_running = 'Running' in _kcm_out
                    if not _kcm_running and _kcm_out.strip():
                        lsf.write_output(
                            '  kube-controller-manager is Exited/crashed — '
                            'removing containers to reset CrashLoopBackOff'
                        )
                        lsf.ssh(
                            f"echo '{password}' | sudo -S bash -c "
                            f"\"crictl ps -a 2>/dev/null | grep kube-controller | "
                            f"awk '{{print \\$1}}' | xargs -r crictl rm -f 2>/dev/null\" "
                            f"&& echo KCM_CLEARED",
                            _cp_ssh
                        )
                        # Give kubelet ~15s to spin up a fresh container
                        lsf.labstartup_sleep(15)
                        _kcm_check = lsf.ssh(
                            f"echo '{password}' | sudo -S crictl ps 2>/dev/null | grep kube-controller",
                            _cp_ssh
                        )
                        _kcm_check_out = getattr(_kcm_check, 'stdout', '') or ''
                        if 'Running' in _kcm_check_out:
                            lsf.write_output('  kube-controller-manager: Running after reset')
                        else:
                            lsf.write_output(
                                '  kube-controller-manager: not yet Running after reset — continuing'
                            )
                    elif _kcm_running:
                        lsf.write_output('  kube-controller-manager: already Running — no action needed')
                    else:
                        lsf.write_output('  kube-controller-manager: no container found — kubelet will create it')
                except Exception as _kfix_exc:
                    lsf.write_output(f'  WARNING: kube-vip/controller-manager fix failed: {_kfix_exc}')

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
                
                # ---- Apply PROXY and NO_PROXY to VSP cluster nodes (HOL only) ----
                # Inject proxy configuration to avoid ImagePullBackOff for opsnet/opslogs.
                # Skipped for non-HOL labtypes (DISCOVERY, VXP, ATE, EDU) that have direct
                # internet access and require neither PROXY nor NO_PROXY settings.
                if _proxy_required:
                    lsf.write_output('  Applying proxy and NO_PROXY configuration to VSP nodes...')
                else:
                    lsf.write_output('  Labtype does not require proxy — skipping VSP node proxy/NO_PROXY configuration')
                vsp_node_ips = []
                discover_cmd = "kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}'"
                result = vsp_kubectl(discover_cmd)
                
                if hasattr(result, 'stdout') and result.stdout:
                    for line in result.stdout.strip().split('\n'):
                        for token in line.split():
                            token = token.strip()
                            if token and token[0].isdigit() and '.' in token:
                                vsp_node_ips.append(token)
                
                if not vsp_node_ips:
                    lsf.write_output('  WARNING: Could not discover VSP node IPs via kubectl, probing fallback IPs')
                    for candidate_ip in ['10.1.1.143', '10.1.1.141', '10.1.1.144', '10.1.1.145', '10.1.1.146', '10.1.1.147']:
                        if lsf.test_ping(candidate_ip):
                            test = lsf.ssh('hostname', f'{vsp_user}@{candidate_ip}', password)
                            if hasattr(test, 'returncode') and test.returncode == 0:
                                vsp_node_ips.append(candidate_ip)

                def apply_proxy_to_nodes(node_ips):
                    if not node_ips:
                        return
                    lsf.write_output(f'  Applying proxy config to {len(node_ips)} VSP nodes: {", ".join(node_ips)}')
                    PROXY_URL = lsf.LAB_PROXY_URL
                    NO_PROXY = lsf.build_lab_no_proxy()
                    
                    proxy_script = f'''#!/bin/bash
PROXY_URL="{PROXY_URL}"
NO_PROXY="{NO_PROXY}"

cat > /etc/sysconfig/proxy << 'PROXYEOF'
PROXY_ENABLED="yes"
HTTP_PROXY="{PROXY_URL}"
HTTPS_PROXY="{PROXY_URL}"
FTP_PROXY=""
GOPHER_PROXY=""
SOCKS_PROXY=""
SOCKS5_SERVER=""
NO_PROXY="{NO_PROXY}"
PROXYEOF

touch /etc/environment
sed -i '/^http_proxy=/d;/^https_proxy=/d;/^no_proxy=/d;/^HTTP_PROXY=/d;/^HTTPS_PROXY=/d;/^NO_PROXY=/d' /etc/environment
cat >> /etc/environment << 'ENVEOF'
http_proxy={PROXY_URL}
https_proxy={PROXY_URL}
no_proxy={NO_PROXY}
HTTP_PROXY={PROXY_URL}
HTTPS_PROXY={PROXY_URL}
NO_PROXY={NO_PROXY}
ENVEOF

mkdir -p /etc/systemd/system/containerd.service.d
cat > /etc/systemd/system/containerd.service.d/http-proxy.conf << 'CTDEOF'
[Service]
Environment="HTTP_PROXY={PROXY_URL}"
Environment="HTTPS_PROXY={PROXY_URL}"
Environment="NO_PROXY={NO_PROXY}"
CTDEOF

mkdir -p /etc/systemd/system/kubelet.service.d
cat > /etc/systemd/system/kubelet.service.d/http-proxy.conf << 'KUBEOF'
[Service]
Environment="HTTP_PROXY={PROXY_URL}"
Environment="HTTPS_PROXY={PROXY_URL}"
Environment="NO_PROXY={NO_PROXY}"
KUBEOF

systemctl daemon-reload
systemctl restart containerd
systemctl restart kubelet
echo "PROXY_CONFIGURED"
'''
                    script_path = '/tmp/confighol_vsp_proxy.sh'
                    try:
                        with open(script_path, 'w') as f:
                            f.write(proxy_script)
                        os.chmod(script_path, 0o755)
                        
                        for node_ip in node_ips:
                            lsf.scp(script_path, f'{vsp_user}@{node_ip}:/tmp/confighol_vsp_proxy.sh', password)
                            if sudo_needs_password:
                                run_cmd = f"echo '{password}' | sudo -S bash /tmp/confighol_vsp_proxy.sh"
                            else:
                                run_cmd = "sudo bash /tmp/confighol_vsp_proxy.sh"
                            
                            lsf.ssh(run_cmd, f'{vsp_user}@{node_ip}', password)
                            
                            if sudo_needs_password:
                                lsf.ssh(f"echo '{password}' | sudo -S rm -f /tmp/confighol_vsp_proxy.sh", f'{vsp_user}@{node_ip}', password)
                            else:
                                lsf.ssh("sudo rm -f /tmp/confighol_vsp_proxy.sh", f'{vsp_user}@{node_ip}', password)
                            
                        os.remove(script_path)
                    except Exception as e:
                        lsf.write_output(f'  WARNING: Failed to apply proxy configuration: {e}')

                if vsp_node_ips:
                    lsf.write_output(f'  Found {len(vsp_node_ips)} VSP nodes: {", ".join(vsp_node_ips)}')
                    if _proxy_required:
                        apply_proxy_to_nodes(vsp_node_ips)
                    else:
                        lsf.write_output(f'  Clearing proxy from {len(vsp_node_ips)} VSP nodes...')
                        for _node_ip in vsp_node_ips:
                            lsf.clear_vsp_node_proxy(_node_ip, password)

                # ---- SDDC Manager proxy (SET or CLEAR) ──────────────────────
                _sddc_host = lsf.config.get('VCF', 'sddc_manager_host',
                                             fallback='sddcmanager-a.site-a.vcf.lab')
                _sddc_ip   = lsf.config.get('VCF', 'sddc_manager_ip',
                                             fallback='10.1.1.20')
                lsf.write_output(f'  {"Setting" if _proxy_required else "Clearing"} proxy on SDDC Manager {_sddc_host}...')
                if _proxy_required:
                    lsf.set_sddc_proxy(_sddc_host, _sddc_ip, 'admin@local', password)
                else:
                    lsf.clear_sddc_proxy(_sddc_host, _sddc_ip, 'admin@local', password)

                # ---- Ops Manager proxy (SET or CLEAR) ───────────────────────
                _ops_host = lsf.config.get('VCF', 'ops_manager_host',
                                            fallback='ops-a.site-a.vcf.lab')
                lsf.write_output(f'  {"Setting" if _proxy_required else "Clearing"} proxy on Ops Manager {_ops_host}...')
                if _proxy_required:
                    lsf.set_ops_proxy(_ops_host, 'admin', password)
                else:
                    lsf.clear_ops_proxy(_ops_host, 'admin', password)

                # ---- ESXi UserVars.HttpProxyHost (SET or CLEAR) ─────────────
                # Build a structured list from the vcenters_list config strings
                # (format: hostname:os_type:sso_user@domain).
                _vcenter_list = []
                for _vc_line in vcenters_list:
                    _vc_parts = _vc_line.split(':')
                    if not _vc_parts:
                        continue
                    _vcenter_list.append({
                        'host': _vc_parts[0].strip(),
                        'sso_user': _vc_parts[2].strip() if len(_vc_parts) >= 3 else 'administrator@vsphere.local',
                    })
                for _vc_entry in _vcenter_list:
                    lsf.write_output(
                        f'  {"Setting" if _proxy_required else "Clearing"} '
                        f'vCenter VAMI proxy (ESXi 9.x) via {_vc_entry["host"]}...'
                    )
                    if _proxy_required:
                        lsf.set_esxi_proxy(
                            _vc_entry['host'], _vc_entry['sso_user'], password,
                            f'{lsf.proxy}:3128',
                        )
                    else:
                        lsf.clear_esxi_proxy(_vc_entry['host'], _vc_entry['sso_user'], password)

                # ---- Kubernetes certificate check/renewal (VSP + VCFA) ----
                # Runs before component scale-up so the API server is healthy
                # for everything that follows.  Non-fatal per cluster — a
                # failure in one cluster does not abort the other or the boot.
                _k8s_cert_script = '/home/holuser/hol/Tools/vsp_cert_renewer.py'
                _k8s_cert_errors = []
                if dashboard:
                    dashboard.update_task('vcffinal', 'k8s_certs', TaskStatus.RUNNING)
                    dashboard.generate_html()
                if os.path.isfile(_k8s_cert_script):
                    for _cert_cluster in ('vsp', 'vcfa'):
                        lsf.write_output(
                            f'  Running K8s cert check/renewal for '
                            f'{_cert_cluster.upper()}...'
                        )
                        _cert_env = os.environ.copy()
                        _cert_env['PYTHONUNBUFFERED'] = '1'
                        _cert_proc = subprocess.Popen(
                            ['python3', '-u', _k8s_cert_script,
                             '--cluster', _cert_cluster,
                             '--no-timestamps'],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1,
                            env=_cert_env,
                        )
                        for _cert_line in _cert_proc.stdout:
                            _cert_line = _cert_line.rstrip('\n')
                            if _cert_line.strip():
                                lsf.write_output(f' {_cert_line.strip()}')
                        _cert_proc.wait()
                        if _cert_proc.returncode not in (0, None):
                            lsf.write_output(
                                f'  WARNING: {_cert_cluster.upper()} cert '
                                f'renewal exited {_cert_proc.returncode} '
                                f'— continuing'
                            )
                            _k8s_cert_errors.append(
                                f'{_cert_cluster.upper()} exited {_cert_proc.returncode}'
                            )
                    if dashboard:
                        if _k8s_cert_errors:
                            dashboard.update_task('vcffinal', 'k8s_certs', TaskStatus.FAILED,
                                                  f'Non-zero exit: {"; ".join(_k8s_cert_errors)}')
                        else:
                            dashboard.update_task('vcffinal', 'k8s_certs', TaskStatus.COMPLETE,
                                                  'VSP + VCFA cert check/renewal complete')
                        dashboard.generate_html()
                else:
                    lsf.write_output(
                        f'  K8s cert renewal script not found: '
                        f'{_k8s_cert_script} — skipping'
                    )
                    if dashboard:
                        dashboard.update_task('vcffinal', 'k8s_certs', TaskStatus.SKIPPED,
                                              'vsp_cert_renewer.py not found')
                        dashboard.generate_html()

                # ---- Unsuspend postgres instances managed by Zalando operator ----
                # The VMSP operator sets a "database.vmsp.vmware.com/suspended=true"
                # label on PostgresInstance CRDs when a component is stopped.  The
                # Zalando postgres operator honours this label and keeps the
                # statefulset at 0 replicas regardless of manual scaling.  We must:
                #   1. Remove the suspended label from the PostgresInstance CRD
                #   2. Restore the Zalando postgresql CRD numberOfInstances to its
                #      intended value (saved as annotation vcf.lab/original-instances
                #      by the shutdown script, defaulting to 1 if not present)
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

                            # Read the Zalando CRD to get current numberOfInstances
                            # and the saved annotation with the intended value
                            zalando_check = vsp_kubectl(
                                f'kubectl get postgresqls.acid.zalan.do {pg_name} -n {pg_ns} '
                                f'-o json 2>/dev/null'
                            )
                            current_instances = 0
                            intended_instances = 1
                            if hasattr(zalando_check, 'stdout') and zalando_check.stdout:
                                raw_z = zalando_check.stdout.strip()
                                json_start_z = raw_z.find('{')
                                if json_start_z >= 0:
                                    raw_z = raw_z[json_start_z:]
                                try:
                                    z_data = _json_pg.loads(raw_z)
                                    current_instances = z_data.get('spec', {}).get('numberOfInstances', 0)
                                    anno_val = z_data.get('metadata', {}).get('annotations', {}).get('vcf.lab/original-instances', '')
                                    if anno_val.isdigit() and int(anno_val) > 0:
                                        intended_instances = int(anno_val)
                                except (ValueError, _json_pg.JSONDecodeError):
                                    pass

                            if current_instances < intended_instances:
                                lsf.write_output(f'  Scaling Zalando postgres {pg_ns}/{pg_name} to {intended_instances} instance(s) (was {current_instances})')
                                patch_json = f'{{"spec":{{"numberOfInstances":{intended_instances}}}}}'
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
                
                # ---- Stabilize Salt infrastructure (salt-raas + salt namespaces) ----
                # Multiple cascading issues break Salt after cold boot:
                #   1. Postgres pgdata permissions may be != 0700 → postgres refuses to start
                #   2. Redis loads its TLS cert at startup but cert rotation (vsp_cert_renewer)
                #      happens ~18s later → Redis serves an expired cert in memory → RAAS
                #      Celery worker gets SSL CERTIFICATE_VERIFY_FAILED → RAAS crash-loops
                #   3. salt-master gets 500/530 from RAAS SSE API → broken event queue
                #   4. salt-minion can't auth with broken master → permanently stops
                # Conditional detection of each failure mode is fragile because the checks
                # can run before or after the failure window. The reliable fix is:
                #   Step 1: Fix pgdata permissions if pgdatabase-0 is not 3/3
                #   Step 2: Unconditionally rollout-restart redis, raas, salt-master, salt-minion
                #           in dependency order, with waits between each layer
                # This ensures every component loads fresh certs and connects to healthy deps.
                lsf.write_output('  Stabilizing Salt infrastructure...')
                try:
                    import json as _json_salt

                    # Step 1: Fix pgdata permissions if needed (same as before)
                    _pg_result = vsp_kubectl(
                        'kubectl get pod -n salt-raas pgdatabase-0 -o json 2>/dev/null'
                    )
                    _pg_out = getattr(_pg_result, 'stdout', '') or ''
                    _pg_js = _pg_out.find('{')
                    if _pg_js >= 0:
                        try:
                            _pg_data = _json_salt.loads(_pg_out[_pg_js:])
                            _cstats = _pg_data.get('status', {}).get('containerStatuses', [])
                            _ready = sum(1 for cs in _cstats if cs.get('ready', False))
                            _total = len(_cstats)
                            if _ready < _total and _total > 0:
                                lsf.write_output(
                                    f'  pgdatabase-0 is {_ready}/{_total} — fixing pgdata permissions'
                                )
                                _chmod = vsp_kubectl(
                                    'kubectl exec -n salt-raas pgdatabase-0 -c walg -- '
                                    'chmod 700 /home/postgres/pgdata/pgroot/data 2>/dev/null '
                                    '&& echo CHMOD_OK'
                                )
                                if 'CHMOD_OK' in (getattr(_chmod, 'stdout', '') or ''):
                                    lsf.write_output('  pgdata permissions fixed — restarting pgdatabase-0')
                                    vsp_kubectl(
                                        'kubectl delete pod -n salt-raas pgdatabase-0 '
                                        '--grace-period=0 2>/dev/null'
                                    )
                                    for _pw in range(18):
                                        lsf.labstartup_sleep(5)
                                        _pwr = vsp_kubectl(
                                            'kubectl get pod -n salt-raas pgdatabase-0 '
                                            '-o jsonpath="{.status.containerStatuses[*].ready}" '
                                            '2>/dev/null'
                                        )
                                        _pwo = (getattr(_pwr, 'stdout', '') or '').strip()
                                        if _pwo.count('true') == 3:
                                            lsf.write_output('  pgdatabase-0 healthy (3/3)')
                                            break
                            else:
                                lsf.write_output(f'  pgdatabase-0 is {_ready}/{_total} — OK')
                        except (ValueError, Exception) as _e:
                            lsf.write_output(f'  WARNING: pgdatabase-0 check failed: {_e}')

                    # Step 2: Rollout-restart Redis → wait → RAAS → wait → salt-master → wait → salt-minion
                    # Using rollout restart ensures a clean zero-downtime pod replacement.
                    # Each step waits for the previous layer to stabilize.
                    _salt_steps = [
                        ('redis',       'salt-raas', 'deployment/redis',       30),
                        ('raas',        'salt-raas', 'deployment/raas',        60),
                        ('salt-master', 'salt',      'deployment/salt-master', 45),
                        ('salt-minion', 'salt',      'deployment/salt-minion', 0),
                    ]
                    for _sname, _sns, _sres, _swait in _salt_steps:
                        lsf.write_output(f'  Rolling restart {_sname} ({_sns}/{_sres})...')
                        vsp_kubectl(
                            f'kubectl rollout restart {_sres} -n {_sns} 2>/dev/null'
                        )
                        if _swait > 0:
                            # Wait for rollout to complete (timeout = _swait seconds)
                            lsf.write_output(f'  Waiting up to {_swait}s for {_sname} rollout...')
                            _ro = vsp_kubectl(
                                f'kubectl rollout status {_sres} -n {_sns} '
                                f'--timeout={_swait}s 2>/dev/null'
                            )
                            _ro_out = (getattr(_ro, 'stdout', '') or '').strip()
                            if 'successfully rolled out' in _ro_out:
                                lsf.write_output(f'  {_sname}: rollout complete')
                            else:
                                lsf.write_output(
                                    f'  {_sname}: rollout did not complete within '
                                    f'{_swait}s — continuing'
                                )

                    lsf.write_output('  Salt infrastructure stabilization complete')

                except Exception as _salt_exc:
                    lsf.write_output(
                        f'  WARNING: Salt infrastructure stabilization failed: {_salt_exc}'
                    )

                # ---- Update Component CRD annotations to Running ----
                # The VCF Services Runtime UI reads the annotation
                # "component.vmsp.vmware.com/operational-status" on each
                # Component CRD to determine the displayed state.  Scaling
                # pods alone does not update this annotation, so the UI
                # would still show "Stopped" without this step.
                # We do this BEFORE scaling up the pods, so the VMSP operator
                # doesn't immediately scale them back down to 0!
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

                # ---- Scale up each component ----
                lsf.write_output(f'  Processing {len(vcfcomponents)} component resources...')
                
                scaled_components = []
                failed_components = []
                for entry in vcfcomponents:
                    # Format: namespace:resource_type/resource_name
                    parts = entry.split(':', 1)
                    if len(parts) != 2 or '/' not in parts[1]:
                        lsf.write_output(f'  WARNING: Invalid vcfcomponents entry: {entry}')
                        vcf_comp_errors.append(f'Invalid entry: {entry}')
                        continue
                    
                    namespace = parts[0].strip()
                    resource = parts[1].strip()  # e.g. "deployment/salt-master"
                    
                    # Read current replicas and the saved annotation with intended value
                    check_cmd = f'kubectl get {resource} -n {namespace} -o json 2>/dev/null'
                    check_result = vsp_kubectl(check_cmd)
                    current_replicas = 0
                    intended_replicas = 1
                    if hasattr(check_result, 'stdout') and check_result.stdout:
                        try:
                            import json as _json_comp
                            raw_comp = check_result.stdout.strip()
                            json_start_comp = raw_comp.find('{')
                            if json_start_comp >= 0:
                                raw_comp = raw_comp[json_start_comp:]
                            comp_data = _json_comp.loads(raw_comp)
                            current_replicas = comp_data.get('spec', {}).get('replicas', 0)
                            anno_val = comp_data.get('metadata', {}).get('annotations', {}).get('vcf.lab/original-replicas', '')
                            if anno_val.isdigit() and int(anno_val) > 0:
                                intended_replicas = int(anno_val)
                        except (ValueError, Exception):
                            pass
                    
                    if current_replicas >= intended_replicas:
                        lsf.write_output(f'  {namespace}/{resource}: already running (replicas={current_replicas})')
                        vcf_comp_already_running += 1
                        continue
                    
                    scale_cmd = f'kubectl scale {resource} -n {namespace} --replicas={intended_replicas}'
                    lsf.write_output(f'  Scaling up: {namespace}/{resource} to {intended_replicas} (was {current_replicas})')
                    scale_result = vsp_kubectl(scale_cmd)
                    
                    if hasattr(scale_result, 'stdout') and 'scaled' in scale_result.stdout:
                        vcf_comp_scaled += 1
                        scaled_components.append({'namespace': namespace, 'resource': resource, 'intended': intended_replicas})
                    elif hasattr(scale_result, 'returncode') and scale_result.returncode == 0:
                        vcf_comp_scaled += 1
                        scaled_components.append({'namespace': namespace, 'resource': resource, 'intended': intended_replicas})
                    else:
                        err = ''
                        if hasattr(scale_result, 'stderr') and scale_result.stderr:
                            err = scale_result.stderr.strip()[:200]
                        elif hasattr(scale_result, 'stdout') and scale_result.stdout:
                            err = scale_result.stdout.strip()[:200]
                        lsf.write_output(f'  WARNING: Failed to scale {namespace}/{resource}: {err}')
                        failed_components.append({'namespace': namespace, 'resource': resource, 'intended': intended_replicas})
                

                # ---- Wait for scaled components to become ready and check for VSP node provisioning ----
                pending_check = vsp_kubectl('kubectl get pods -A --field-selector=status.phase=Pending 2>/dev/null')
                has_pending = hasattr(pending_check, 'stdout') and pending_check.stdout and ('ops-logs' in pending_check.stdout or 'vodap' in pending_check.stdout)
                
                if scaled_components or has_pending:
                    if scaled_components:
                        lsf.write_output(f'  Waiting up to 10 minutes for {len(scaled_components)} component(s) to complete scale up...')
                    if has_pending:
                        lsf.write_output('  Detected Pending pods in ops-logs/vodap, monitoring vCenter for new VSP node provisioning...')
                        
                    max_wait = 600  # 10 minutes
                    start_time = time.time()
                    last_log_time = time.time()
                    
                    while time.time() - start_time < max_wait:
                        newly_ready = []
                        
                        # Check status of each component
                        for comp in list(scaled_components):
                            check_cmd = f'kubectl get {comp["resource"]} -n {comp["namespace"]} -o json 2>/dev/null'
                            check_result = vsp_kubectl(check_cmd)
                            ready_replicas = 0
                            if hasattr(check_result, 'stdout') and check_result.stdout:
                                try:
                                    import json as _json_comp
                                    raw_comp = check_result.stdout.strip()
                                    json_start_comp = raw_comp.find('{')
                                    if json_start_comp >= 0:
                                        raw_comp = raw_comp[json_start_comp:]
                                    comp_data = _json_comp.loads(raw_comp)
                                    ready_replicas = comp_data.get('status', {}).get('readyReplicas', 0)
                                except (ValueError, Exception):
                                    pass
                            
                            if ready_replicas >= comp['intended']:
                                newly_ready.append(f"{comp['namespace']}/{comp['resource']}")
                                scaled_components.remove(comp)
                                
                        if newly_ready:
                            for ready_comp in newly_ready:
                                lsf.write_output(f'  Completed scale up: {ready_comp}')
                                
                        # Check for VSP node provisioning
                        current_vms = lsf.get_vm_match('vsp-01a-.*')
                        provisioning_vms = [v for v in current_vms if v.runtime.powerState != 'poweredOn' or getattr(getattr(v, 'guest', None), 'toolsRunningStatus', '') != 'guestToolsRunning']
                        
                        if not provisioning_vms:
                            # Let's check if the number of nodes in K8s increased
                            current_k8s_nodes_cmd = "kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}' 2>/dev/null"
                            res = vsp_kubectl(current_k8s_nodes_cmd)
                            current_ips = []
                            if hasattr(res, 'stdout') and res.stdout:
                                for line in res.stdout.strip().split('\n'):
                                    for token in line.split():
                                        token = token.strip()
                                        if token and token[0].isdigit() and '.' in token:
                                            current_ips.append(token)
                            
                            new_ips = [ip for ip in current_ips if ip not in vsp_node_ips]
                            if new_ips:
                                lsf.write_output(f'  New VSP nodes joined K8s: {new_ips}')
                                if _proxy_required:
                                    apply_proxy_to_nodes(new_ips)
                                    lsf.write_output('  Applied proxy and NO_PROXY to new node(s).')
                                vsp_node_ips.extend(new_ips)
                        
                        # Re-check pending status to see if we can exit early
                        check_again = vsp_kubectl('kubectl get pods -A --field-selector=status.phase=Pending 2>/dev/null')
                        still_pending = hasattr(check_again, 'stdout') and check_again.stdout and ('ops-logs' in check_again.stdout or 'vodap' in check_again.stdout)
                        
                        if not scaled_components and not still_pending:
                            lsf.write_output('  All components have completed scale up and no pending pods remain.')
                            break
                            
                        # Log status every 30 seconds
                        if time.time() - last_log_time >= 30:
                            if scaled_components:
                                waiting_for = [f"{c['namespace']}/{c['resource']}" for c in scaled_components]
                                if len(waiting_for) > 5:
                                    lsf.write_output(f'  Still waiting for {len(waiting_for)} components to scale up (e.g. {", ".join(waiting_for[:5])}...)')
                                else:
                                    lsf.write_output(f'  Still waiting for components to scale up: {", ".join(waiting_for)}')
                            
                            if provisioning_vms:
                                prov_names = [v.name for v in provisioning_vms]
                                lsf.write_output(f'  Waiting for {len(prov_names)} VSP node(s) to provision: {prov_names}')
                            elif still_pending:
                                lsf.write_output('  Still waiting for pending pods to schedule...')
                                
                            last_log_time = time.time()
                            
                        time.sleep(10) # check every 10 seconds
                        
                    if time.time() - start_time >= max_wait:
                        lsf.write_output(f'  WARNING: Timeout reached ({max_wait}s). Proceeding while components finish scaling in background...')

                if failed_components:
                    lsf.write_output('  Retrying failed components after cluster stabilized...')
                    for comp in failed_components:
                        ns = comp['namespace']
                        res = comp['resource']
                        intended = comp['intended']
                        scale_cmd = f'kubectl scale {res} -n {ns} --replicas={intended}'
                        lsf.write_output(f'  Retrying scale up: {ns}/{res} to {intended}')
                        retry_result = vsp_kubectl(scale_cmd)
                        
                        if hasattr(retry_result, 'stdout') and 'scaled' in retry_result.stdout:
                            vcf_comp_scaled += 1
                            lsf.write_output(f'  Successfully scaled up on retry: {ns}/{res}')
                        elif hasattr(retry_result, 'returncode') and retry_result.returncode == 0:
                            vcf_comp_scaled += 1
                            lsf.write_output(f'  Successfully scaled up on retry: {ns}/{res}')
                        else:
                            err = ''
                            if hasattr(retry_result, 'stderr') and retry_result.stderr:
                                err = retry_result.stderr.strip()[:200]
                            elif hasattr(retry_result, 'stdout') and retry_result.stdout:
                                err = retry_result.stdout.strip()[:200]
                            lsf.write_output(f'  WARNING: Retry failed for {ns}/{res}: {err}')
                            vcf_comp_errors.append(f'Failed: {ns}/{res}')

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
                
                # ---- Power on VCF Operations for Networks VMs ----
                # In VCF 9.x, ops_networks (vRNI) is a mix of K8s services (vodap) and
                # dedicated VMs. The K8s services were scaled up above, but the VMs
                # must also be explicitly powered on to complete the "Running" state.
                lsf.write_output('  Powering on VCF Operations for Networks VMs...')
                opsnet_vms = lsf.get_config_list('SHUTDOWN', 'vcf_ops_networks_vms')
                if not opsnet_vms:
                    # Fallback to standard VCF 9.1 and 9.0 lab names
                    opsnet_vms = ['ops_networks-platform.*', 'ops_networks-collector.*', 'opsnet-a', 'opsnet-01a', 'opsnetcollector-01a']
                
                opsnet_resolved = []
                for vm_pattern in opsnet_vms:
                    # Handle optional :vcenter suffix used in some config formats
                    vm_name_only = vm_pattern.split(':')[0]
                    vms_found = lsf.get_vm_match(vm_name_only)
                    if vms_found:
                        opsnet_resolved.extend(vms_found)

                opsnet_started = 0
                opsnet_already_on = 0
                for vm in opsnet_resolved:
                    try:
                        if vm.runtime.powerState == 'poweredOn':
                            lsf.write_output(f'  {vm.name} is already powered on')
                            opsnet_already_on += 1
                        else:
                            lsf.write_output(f'  Powering on {vm.name}...')
                            vm.PowerOnVM_Task()
                            opsnet_started += 1
                    except Exception as e:
                        lsf.write_output(f'  WARNING: Failed to power on {vm.name}: {e}')
                
                lsf.write_output(f'  Ops Networks VMs: {opsnet_started} started, {opsnet_already_on} already running')
                
                
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
                lsf.write_output(f'Processing {len(vravms)} VCF Automation VM entries...')
                lsf.write_vpodprogress('Starting VCF Automation VMs', 'GOOD-8')
                
                # Resolve all regex/name patterns to actual VM objects once
                resolved_vcfa_vms = []
                for vravm in vravms:
                    parts = vravm.split(':')
                    vmname = parts[0].strip()
                    try:
                        vms = lsf.get_vm_match(vmname)
                        if vms:
                            for vm in vms:
                                resolved_vcfa_vms.append(vm)
                                lsf.write_output(f'  Resolved: {vmname} -> {vm.name}')
                        else:
                            lsf.write_output(f'  Warning: No VMs matched pattern "{vmname}"')
                    except Exception as e:
                        lsf.write_output(f'  Warning: Error resolving pattern "{vmname}": {e}')
                
                if not resolved_vcfa_vms:
                    lsf.write_output('No VCF Automation VMs found after resolving patterns')
                else:
                    lsf.write_output(f'Resolved {len(resolved_vcfa_vms)} VCF Automation VMs')
                    
                    # Isolate Automation VMs on dedicated host
                    try:
                        from pyVmomi import vim
                        from pyVim.task import WaitForTask
                        import random
                        
                        # Find the ESXi host for the first automation VM
                        auto_vm = resolved_vcfa_vms[0]
                        auto_host = auto_vm.runtime.host
                        if auto_host:
                            lsf.write_output(f'Automation VM {auto_vm.name} is on host {auto_host.name}')
                            
                            # Gather all Automation VMs to ensure we don't move them
                            auto_vm_names = [v.name for v in resolved_vcfa_vms]
                            
                            # Find other VMs on this host
                            vms_to_move = []
                            for vm in auto_host.vm:
                                if vm.name not in auto_vm_names and not vm.config.template and not vm.name.startswith('vCLS'):
                                    vms_to_move.append(vm)
                            
                            if vms_to_move:
                                lsf.write_output(f'Found {len(vms_to_move)} other VMs on {auto_host.name}. Evacuating them to dedicate automation host...')
                                
                                # Find alternate hosts in the same cluster
                                cluster = auto_host.parent
                                if isinstance(cluster, vim.ClusterComputeResource):
                                    alt_hosts = [h for h in cluster.host if h != auto_host and h.runtime.connectionState == 'connected' and not h.runtime.inMaintenanceMode]
                                    
                                    if alt_hosts:
                                        for move_vm in vms_to_move:
                                            dest_host = random.choice(alt_hosts)
                                            lsf.write_output(f'  Migrating {move_vm.name} to {dest_host.name}...')
                                            try:
                                                relocate_spec = vim.vm.RelocateSpec()
                                                relocate_spec.host = dest_host
                                                task = move_vm.RelocateVM_Task(relocate_spec)
                                                WaitForTask(task)
                                                lsf.write_output(f'    SUCCESS: Migrated {move_vm.name}')
                                            except Exception as mig_err:
                                                lsf.write_output(f'    WARNING: Could not migrate {move_vm.name}: {mig_err}')
                                    else:
                                        lsf.write_output(f'WARNING: No alternate hosts found in cluster to evacuate VMs from {auto_host.name}')
                                else:
                                    lsf.write_output(f'WARNING: Host parent is not a cluster ({type(cluster)})')
                            else:
                                lsf.write_output(f'Host {auto_host.name} is already dedicated to Automation VMs.')
                    except Exception as ev_err:
                        lsf.write_output(f'Warning: Error while trying to evacuate automation host: {ev_err}')
                    
                    # Removed: Do not use verify_nic_connected for automation VMs
                    
                    # Start the VMs
                    try:
                        lsf.start_nested(vravms)
                    except Exception as e:
                        error_msg = f'Failed to start VCF Automation VMs: {e}'
                        lsf.write_output(error_msg)
                        vcfa_vms_errors.append(error_msg)
                    
                    # After starting, verify VMs are actually powered on and tools running
                    for vm in resolved_vcfa_vms:
                        try:
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
                                lsf.write_output(f'Waiting for Tools in {vm.name}...')
                                lsf.labstartup_sleep(lsf.sleep_seconds)
                                tools_attempt += 1
                            
                            # Removed: Do not use verify_nic_connected for automation VMs
                        
                        except Exception as e:
                            lsf.write_output(f'Warning: Error waiting for {vm.name}: {e}')
                
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
    # TASK 4b: VCF Automation K8s Health Check & Remediation
    # Consolidated from the former watchvcfa.sh script and original health
    # check. After the VCF Automation VM is started, the internal K8s
    # cluster may have issues that prevent services from coming up:
    #   1. kube-vip crash loop: VIP released when istio-ingressgateway
    #      fails to start (ImagePullBackOff during Antrea CNI init).
    #   2. Containerd Ready,SchedulingDisabled: node cordoned by stale state.
    #   3. CAPI/CAPV webhook failure: CNI socket issue prevents pod sandboxes.
    #   4. kube-scheduler stuck at 0/1 Running.
    #   5. Stale seaweedfs-master-0 pod blocking other pods.
    #   6. Stuck volume attachments with deletionTimestamp.
    #   7. vCenter vAPI endpoint stopped (CSI controller dependency).
    #   8. CSI controller CrashLoopBackOff (leases, password, CRD errors).
    #   9. RabbitMQ .erlang.cookie permissions (fsGroup breaks Erlang).
    #  10. provisioning-service Spring Boot deadlock.
    #  11. Prelude deployments/statefulsets at 0 replicas after cold boot.
    #  12. Prelude pods stuck in ContainerCreating after volume/CSI fixes.
    #==========================================================================
    
    vcfa_k8s_remediation_ok = True
    
    if vcfa_vms_configured and not dry_run:
        lsf.write_output('='*60)
        lsf.write_output('VCF Automation K8s Health Check')
        lsf.write_output('='*60)
        lsf.write_vpodprogress('VCFA K8s Health', 'GOOD-8')
        
        if dashboard:
            dashboard.update_task('vcffinal', 'vcfa_k8s_health', TaskStatus.RUNNING)
            dashboard.generate_html()
        
        try:
            import json as _json_k8s
            
            password = lsf.get_password()
            vcfa_k8s_ip = '10.1.1.71'  # VCF 9.0 default
            vcfa_vip = '10.1.1.70'
            vcfa_user = 'vmware-system-user'
            vcenter_host = 'vc-mgmt-a.site-a.vcf.lab'
            
            # Auto-detect K8s API IP from known VCF Automation IPs
            for candidate_ip in ['10.1.1.71', '10.1.1.72', '10.1.1.73', '10.1.1.74']:
                if lsf.test_tcp_port(candidate_ip, 22, timeout=5):
                    vcfa_k8s_ip = candidate_ip
                    break
            
            lsf.write_output(f'VCF Automation K8s API node: {vcfa_k8s_ip}')
            
            if not lsf.test_tcp_port(vcfa_k8s_ip, 22, timeout=10):
                lsf.write_output('WARNING: VCF Automation K8s node not reachable via SSH')
                vcfa_k8s_remediation_ok = False
            else:
                # Helper to run commands on auto-a
                def vcfa_ssh(cmd):
                    return lsf.ssh(
                        f"echo '{password}' | sudo -S -i bash -c '{cmd}'",
                        f'{vcfa_user}@{vcfa_k8s_ip}'
                    )
                
                def _get_stdout(result):
                    """Extract stdout string from ssh result, or empty string."""
                    if hasattr(result, 'stdout') and result.stdout:
                        return result.stdout
                    return ''
                
                # ---- Step 0: Delete stale Fleet LCM system-shutdown Argo Workflows ----
                # Each shutdown cycle creates a system-shutdown-{id} Argo Workflow in
                # vmsp-platform. Argo persists workflow state across reboots so the
                # controller resumes them on startup. A resumed shutdown workflow
                # cordons the node, scales prelude to 0, and runs its shutdown script,
                # breaking VCFA. Multiple stale workflows queue on lock-vmsp-platform
                # mutex, creating a cascade. Delete them ALL before any uncordon.
                stale_wf_out = _get_stdout(vcfa_ssh(
                    'kubectl get workflow -n vmsp-platform --no-headers 2>/dev/null'
                    ' | grep system-shutdown'
                    ' | cut -d" " -f1'
                ))
                stale_wfs = [w.strip() for w in stale_wf_out.splitlines() if w.strip()]
                if stale_wfs:
                    lsf.write_output(f'  Deleting {len(stale_wfs)} stale system-shutdown Argo Workflow(s)...')
                    # Patch Running workflows to stopped first (releases mutex lock cleanly)
                    for wf in stale_wfs:
                        vcfa_ssh(
                            f'kubectl patch workflow {wf} -n vmsp-platform'
                            r" --type=merge -p '{\"spec\":{\"shutdown\":\"Stop\"}}' 2>/dev/null"
                        )
                    import time as _time_wf
                    _time_wf.sleep(2)
                    # Delete in batches of 10
                    for _i in range(0, len(stale_wfs), 10):
                        batch = ' '.join(stale_wfs[_i:_i+10])
                        vcfa_ssh(
                            f'kubectl delete workflow -n vmsp-platform {batch}'
                            ' --grace-period=0 2>/dev/null'
                        )
                    _time_wf.sleep(3)
                    # Verify
                    remaining_out = _get_stdout(vcfa_ssh(
                        'kubectl get workflow -n vmsp-platform --no-headers 2>/dev/null'
                        ' | grep -c system-shutdown || echo 0'
                    ))
                    remaining = remaining_out.strip() or '0'
                    lsf.write_output(f'  Stale shutdown workflows remaining: {remaining}')
                else:
                    lsf.write_output('  No stale system-shutdown workflows found')

                # ---- Step 1: Check/fix kube-vip VIP ----
                lsf.write_output('Checking kube-vip VIP status...')
                vip_check = vcfa_ssh(f'ip addr show eth0 | grep {vcfa_vip}')
                vip_present = False
                if vcfa_vip in _get_stdout(vip_check):
                    vip_present = True
                    lsf.write_output(f'  VIP {vcfa_vip} is present on eth0')
                else:
                    lsf.write_output(f'  VIP {vcfa_vip} is MISSING from eth0 - adding manually')
                    vcfa_ssh(f'ip addr add {vcfa_vip}/32 dev eth0')
                    time.sleep(2)
                    vip_recheck = vcfa_ssh(f'ip addr show eth0 | grep {vcfa_vip}')
                    if vcfa_vip in _get_stdout(vip_recheck):
                        lsf.write_output(f'  VIP {vcfa_vip} added successfully')
                        vip_present = True
                    else:
                        lsf.write_output(f'  WARNING: Failed to add VIP {vcfa_vip}')
                        vcfa_k8s_remediation_ok = False
                
                if not vip_present:
                    lsf.write_output('Cannot proceed without VIP - skipping K8s checks')
                else:
                    # ---- Step 2: Verify kubectl works ----
                    lsf.write_output('Verifying kubectl access...')
                    kctl_prefix = 'export KUBECONFIG=/etc/kubernetes/super-admin.conf;'
                    node_check = vcfa_ssh(f'{kctl_prefix} kubectl get nodes --no-headers 2>&1')
                    kubectl_ok = False
                    node_stdout = _get_stdout(node_check)
                    if 'Ready' in node_stdout:
                        kubectl_ok = True
                        lsf.write_output('  kubectl access verified')
                    else:
                        lsf.write_output('  kubectl not responding, waiting 30s and retrying...')
                        time.sleep(30)
                        node_check2 = vcfa_ssh(f'{kctl_prefix} kubectl get nodes --no-headers 2>&1')
                        if 'Ready' in _get_stdout(node_check2):
                            kubectl_ok = True
                            lsf.write_output('  kubectl access verified on retry')
                        else:
                            lsf.write_output('  kubectl still failing - restarting containerd and kubelet')
                            vcfa_ssh('systemctl restart containerd && sleep 3 && systemctl restart kubelet')
                            time.sleep(30)
                            vcfa_ssh(f'ip addr show eth0 | grep {vcfa_vip} || ip addr add {vcfa_vip}/32 dev eth0')
                            time.sleep(15)
                            node_check3 = vcfa_ssh(f'{kctl_prefix} kubectl get nodes --no-headers 2>&1')
                            if 'Ready' in _get_stdout(node_check3):
                                kubectl_ok = True
                                lsf.write_output('  kubectl access restored after service restart')
                            else:
                                lsf.write_output('  WARNING: kubectl still not working')
                                vcfa_k8s_remediation_ok = False
                    
                    if kubectl_ok:
                        # Track whether volume attachments or CSI were fixed
                        # (used later to recover stuck prelude pods)
                        stuck_va_fixed = False
                        csi_fixed = False
                        
                        # ---- Step 3: Containerd Ready,SchedulingDisabled fix ----
                        lsf.write_output('Checking for node Ready,SchedulingDisabled...')
                        for attempt in range(3):
                            nd_out = _get_stdout(
                                vcfa_ssh(f'{kctl_prefix} kubectl get nodes --no-headers 2>&1')
                            )
                            if 'Ready,SchedulingDisabled' not in nd_out:
                                if attempt == 0:
                                    lsf.write_output('  Node scheduling status OK')
                                break
                            lsf.write_output(f'  Node is Ready,SchedulingDisabled (attempt {attempt+1}/3)')
                            # Try uncordon first (cheapest fix), escalate to containerd restart
                            for line in nd_out.strip().split('\n'):
                                if 'SchedulingDisabled' in line:
                                    node_name = line.split()[0]
                                    lsf.write_output(f'  Uncordoning node {node_name}')
                                    vcfa_ssh(f'{kctl_prefix} kubectl uncordon {node_name}')
                            time.sleep(5)
                            # Recheck after uncordon
                            nd_recheck = _get_stdout(
                                vcfa_ssh(f'{kctl_prefix} kubectl get nodes --no-headers 2>&1')
                            )
                            if 'Ready,SchedulingDisabled' not in nd_recheck:
                                lsf.write_output('  Node scheduling resolved after uncordon')
                                break
                            if attempt >= 1:
                                lsf.write_output('  Uncordon did not resolve, restarting containerd...')
                                vcfa_ssh('systemctl restart containerd')
                                time.sleep(10)
                        
                        # ---- Step 4: CAPI/CAPV controller health ----
                        lsf.write_output('Checking CAPI/CAPV controller health...')
                        
                        def _check_capi_capv_ready():
                            capv_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get deployment capv-controller-manager '
                                f'-n vmsp-platform -o jsonpath="{{.status.readyReplicas}}" 2>/dev/null'
                            )).strip()
                            capi_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get deployment capi-controller-manager '
                                f'-n vmsp-platform -o jsonpath="{{.status.readyReplicas}}" 2>/dev/null'
                            )).strip()
                            capv_val = capv_out.split('\n')[-1].strip() if capv_out else ''
                            capi_val = capi_out.split('\n')[-1].strip() if capi_out else ''
                            return (capv_val and capv_val != '0'), (capi_val and capi_val != '0')
                        
                        capv_ok, capi_ok = _check_capi_capv_ready()
                        if capv_ok and capi_ok:
                            lsf.write_output('  CAPI/CAPV controllers are ready')
                        else:
                            # Controllers may just be starting — check for actual pod failures
                            # before restarting services
                            lsf.write_output('  CAPI/CAPV controllers not ready yet, checking pod status...')
                            capv_pod_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pods -n vmsp-platform --no-headers 2>/dev/null '
                                f'| grep -E "capv-controller|capi-controller"'
                            ))
                            pods_crashing = any(
                                state in capv_pod_out
                                for state in ['CrashLoopBackOff', 'Error', 'ImagePullBackOff', 'CreateContainerError']
                            )
                            if pods_crashing:
                                lsf.write_output('  CAPI/CAPV pods in failure state - restarting containerd and kubelet')
                                vcfa_ssh('systemctl restart containerd kubelet')
                                time.sleep(30)
                                lsf.write_output('  Waiting for CAPI/CAPV conollers to recover...')
                                vcfa_ssh(
                                    f'{kctl_prefix} kubectl rollout status deployment '
                                    f'capv-controller-manager -n vmsp-platform --timeout=60s 2>/dev/null'
                                )
                            else:
                                # Pods exist but aren't ready yet — give them time to converge
                                lsf.write_output('  CAPI/CAPV pods are starting, waiting up to 60s for readiness...')
                                for _wait in range(4):
                                    time.sleep(15)
                                    capv_ok, capi_ok = _check_capi_capv_ready()
                                    if capv_ok and capi_ok:
                                        lsf.write_output('  CAPI/CAPV controllers became ready')
                                        break
                                else:
                                    lsf.write_output('  CAPI/CAPV controllers still not ready after 60s (will continue)')
                        
                        # ---- Step 5: kube-scheduler check/fix ----
                        lsf.write_output('Checking kube-scheduler...')
                        for attempt in range(3):
                            sched_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pods -n kube-system --no-headers 2>/dev/null '
                                f'| grep kube-scheduler'
                            ))
                            if '0/1' not in sched_out:
                                if attempt == 0:
                                    lsf.write_output('  kube-scheduler is running')
                                else:
                                    lsf.write_output('  kube-scheduler recovered')
                                break
                            if attempt == 0:
                                # First detection: wait 30s for natural recovery before intervening
                                lsf.write_output('  kube-scheduler is 0/1, waiting 30s for recovery...')
                                time.sleep(30)
                            else:
                                lsf.write_output(f'  kube-scheduler still 0/1 (attempt {attempt+1}/3) - restarting containerd')
                                vcfa_ssh('systemctl restart containerd')
                                time.sleep(30)
                        
                        # ---- Step 5b: Stuck support-bundle-cluster-info-dump jobs ----
                        # These jobs accumulate in vmsp-platform when the cluster info
                        # dump cronjob fires during or after an ungraceful shutdown. They
                        # hold resource quota and can block other pods from scheduling.
                        lsf.write_output('Cleaning up stuck support-bundle-cluster-info-dump jobs...')

                        # List stuck jobs first so we can log each name
                        dump_jobs_raw = _get_stdout(vcfa_ssh(
                            f'{kctl_prefix} kubectl get jobs -n vmsp-platform -o name 2>/dev/null '
                            f'| grep "support-bundle-cluster-info-dump-"'
                        ))
                        dump_jobs = [j.strip() for j in dump_jobs_raw.strip().splitlines() if j.strip()]

                        if dump_jobs:
                            lsf.write_output(f'  Found {len(dump_jobs)} stuck job(s):')
                            for dj in dump_jobs:
                                lsf.write_output(f'    {dj}')
                            # Delete all matching jobs via xargs in one SSH call
                            del_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get jobs -n vmsp-platform -o name 2>/dev/null '
                                f'| grep "support-bundle-cluster-info-dump-" '
                                f'| xargs -r kubectl delete -n vmsp-platform 2>/dev/null'
                            ))
                            for line in del_out.strip().splitlines():
                                if line.strip():
                                    lsf.write_output(f'    {line.strip()}')
                            lsf.write_output(f'  Deleted {len(dump_jobs)} support-bundle job(s).')
                        else:
                            lsf.write_output('  No stuck support-bundle-cluster-info-dump jobs found.')

                        # Verify no dump pods remain after job deletion
                        lsf.write_output('  Verifying no support-bundle-cluster-info-dump pods remain...')
                        dump_pods_raw = _get_stdout(vcfa_ssh(
                            f'{kctl_prefix} kubectl get pods -n vmsp-platform 2>/dev/null '
                            f'| grep "support-bundle-cluster-info-dump"'
                        ))
                        if dump_pods_raw.strip():
                            lsf.write_output('  WARNING: dump pods still present:')
                            for line in dump_pods_raw.strip().splitlines():
                                if line.strip():
                                    lsf.write_output(f'    {line.strip()}')
                        else:
                            lsf.write_output('  No support-bundle-cluster-info-dump pods found.')

                        # Suspend the cronjob to prevent new dump jobs during startup
                        lsf.write_output('  Suspending support-bundle-cluster-info-dump cronjob...')
                        suspend_out = _get_stdout(vcfa_ssh(
                            f'{kctl_prefix} kubectl patch cronjob -n vmsp-platform '
                            f'support-bundle-cluster-info-dump '
                            f'-p \'{{"spec":{{"suspend":true}}}}\' 2>/dev/null'
                        ))
                        if suspend_out.strip():
                            lsf.write_output(f'  {suspend_out.strip()}')
                        else:
                            lsf.write_output('  Cronjob patched (or not found — skipped).')

                        # ---- Step 6: Stale seaweedfs-master-0 pod ----
                        lsf.write_output('Checking seaweedfs-master-0 for stale pod (>1hr old)...')
                        for attempt in range(3):
                            sw_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pod seaweedfs-master-0 '
                                f'-n vmsp-platform -o json 2>/dev/null'
                            ))
                            sw_json_start = sw_out.find('{')
                            if sw_json_start < 0:
                                lsf.write_output('  seaweedfs-master-0 pod not found or not parseable')
                                break
                            try:
                                sw_data = _json_k8s.loads(sw_out[sw_json_start:])
                                import datetime
                                created = sw_data.get('metadata', {}).get('creationTimestamp', '')
                                if created:
                                    createdt = datetime.datetime.strptime(
                                        created, '%Y-%m-%dT%H:%M:%SZ'
                                    ).replace(tzinfo=datetime.timezone.utc)
                                    age_secs = (datetime.datetime.now(datetime.timezone.utc) - created_dt).total_seconds()
                                    if age_secs > 3600:
                                        lsf.write_output(f'  Stale seaweedfs-master-0 found ({int(age_secs)}s old), deleting...')
                                        vcfa_ssh(
                                            f'{kctl_prefix} kubectl delete pod seaweedfs-master-0 '
                                            f'-n vmsp-platform 2>/dev/null'
                                        )
                                        time.sleep(5)
                                        continue
                                    else:
                                        lsf.write_output(f'  seaweedfs-master-0 is fresh ({int(age_secs)}s old)')
                                        break
                                else:
                                    lsf.write_output('  Could not determine seaweedfs-master-0 age')
                                    break
                            except Exception:
                                lsf.write_output('  Could not parse seaweedfs-master-0 pod JSON')
                                break
                        
                        # ---- Step 7: ImagePullBackOff pods ----
                        lsf.write_output('Checking for ImagePullBackOff pods...')
                        ipb_check = vcfa_ssh(
                            f'{kctl_prefix} kubectl get pods -A --no-headers 2>/dev/null '
                            f'| grep ImagePullBackOff'
                        )
                        ipb_out = _get_stdout(ipb_check)
                        if 'ImagePullBackOff' in ipb_out:
                            for line in ipb_out.strip().split('\n'):
                                if not line.strip():
                                    continue
                                cols = line.split()
                                if len(cols) >= 2:
                                    ipb_ns, ipb_pod = cols[0], cols[1]
                                    lsf.write_output(f'  Deleting ImagePullBackOff pod: {ipb_ns}/{ipb_pod}')
                                    vcfa_ssh(
                                        f'{kctl_prefix} kubectl delete pod {ipb_pod} -n {ipb_ns} '
                                        f'--force --grace-period=0 2>/dev/null'
                                    )
                            time.sleep(10)
                        else:
                            lsf.write_output('  No ImagePullBackOff pods found')
                        
                        # ---- Step 8: Unknown pods ----
                        lsf.write_output('Checking for Unknown pods...')
                        unknown_check = vcfa_ssh(
                            f'{kctl_prefix} kubectl get pods -A --no-headers 2>/dev/null '
                            f'| grep Unknown'
                        )
                        unk_out = _get_stdout(unknown_check)
                        if 'Unknown' in unk_out:
                            unk_count = len([l for l in unk_out.strip().split('\n') if l.strip()])
                            lsf.write_output(f'  Found {unk_count} pods in Unknown state, force deleting...')
                            for line in unk_out.strip().split('\n'):
                                if not line.strip():
                                    continue
                                cols = line.split()
                                if len(cols) >= 2:
                                    unk_ns, unk_pod = cols[0], cols[1]
                                    lsf.write_output(f'  Deleting Unknown pod: {unk_ns}/{unk_pod}')
                                    vcfa_ssh(
                                        f'{kctl_prefix} kubectl delete pod {unk_pod} -n {unk_ns} '
                                        f'--force --grace-period=0 2>/dev/null'
                                    )
                        else:
                            lsf.write_output('  No Unknown pods found')
                        
                        # ---- Step 9: Stuck volume attachments ----
                        lsf.write_output('Checking for stuck volume attachments...')
                        for attempt in range(3):
                            va_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get volumeattachments -o json 2>/dev/null'
                            ))
                            va_json_start = va_out.find('{')
                            if va_json_start < 0:
                                break
                            try:
                                va_data = _json_k8s.loads(va_out[va_json_start:])
                                stuck_vas = [
                                    item['metadata']['name']
                                    for item in va_data.get('items', [])
                                    if item.get('metadata', {}).get('deletionTimestamp')
                                ]
                            except Exception:
                                lsf.write_output('  Could not parse volume attachment JSON')
                                break
                            
                            if not stuck_vas:
                                if attempt == 0:
                                    lsf.write_output('  No stuck volume attachments found')
                                break
                            
                            lsf.write_output(f'  Found {len(stuck_vas)} stuck volume attachment(s), removing finalizers...')
                            for va_name in stuck_vas:
                                lsf.write_output(f'  Removing finalizer from: {va_name}')
                                vcfa_ssh(
                                    f'{kctl_prefix} kubectl patch volumeattachment {va_name} '
                                    f'-p \'{{"metadata":{{"finalizers":null}}}}\' --type=merge 2>/dev/null'
                                )
                            stuck_va_fixed = True
                            time.sleep(5)
                        
                        # ---- Step 10: vCenter vAPI endpoint service ----
                        lsf.write_output('Checking vCenter vAPI endpoint service...')
                        vapi_check = lsf.run_command(
                            f'curl -s -k -o /dev/null -w "%{{http_code}}" '
                            f'"https://{vcenter_host}/rest/com/vmware/cis/session"',
                            timeout=15
                        )
                        vapi_status = _get_stdout(vapi_check).strip()
                        if vapi_status == '503':
                            lsf.write_output('  vAPI endpoint returning 503 - starting service...')
                            lsf.ssh(
                                'service-control --start vmware-vapi-endpoint',
                                f'root@{vcenter_host}'
                            )
                            time.sleep(10)
                            vapi_recheck = lsf.run_command(
                                f'curl -s -k -o /dev/null -w "%{{http_code}}" '
                                f'"https://{vcenter_host}/rest/com/vmware/cis/session"',
                                timeout=15
                            )
                            vapi_status2 = _get_stdout(vapi_recheck).strip()
                            if vapi_status2 != '503':
                                lsf.write_output(f'  vAPI endpoint started successfully (HTTP {vapi_status2})')
                            else:
                                lsf.write_output('  WARNING: vAPI endpoint still returning 503')
                        else:
                            lsf.write_output(f'  vAPI endpoint is responding (HTTP {vapi_status})')
                        
                        # ---- Step 11: CSI controller health ----
                        lsf.write_output('Checking vsphere-csi-controller health...')
                        csi_ready_out = _get_stdout(vcfa_ssh(
                            f'{kctl_prefix} kubectl get pods -n kube-system '
                            f'-l app=vsphere-csi-controller '
                            f'-o jsonpath="{{.items[0].status.containerStatuses[*].ready}}" 2>/dev/null'
                        ))
                        if 'false' not in csi_ready_out and csi_ready_out.strip():
                            lsf.write_output('  CSI controller is healthy')
                        else:
                            # CSI not fully ready — diagnose before taking action
                            lsf.write_output('  CSI controller not fully ready, diagnosing...')
                            
                            csi_pod_name = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pods -n kube-system '
                                f'-l app=vsphere-csi-controller '
                                f'-o jsonpath="{{.items[0].metadata.name}}" 2>/dev/null'
                            )).strip().split('\n')[-1].strip()
                            
                            # Check pod status to distinguish "still starting" from "actually broken"
                            csi_pod_status = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pods -n kube-system '
                                f'-l app=vsphere-csi-controller --no-headers 2>/dev/null'
                            ))
                            csi_is_crashing = any(
                                state in csi_pod_status
                                for state in ['CrashLoopBackOff', 'Error', 'CreateContainerError']
                            )
                            
                            csi_remediation_taken = False
                            
                            if csi_is_crashing and csi_pod_name:
                                # Pod is in a failure state — check specific root causes
                                
                                # Check for stale leases held by non-existent pods
                                lease_out = _get_stdout(vcfa_ssh(
                                    f'{kctl_prefix} kubectl get leases -n kube-system -o json 2>/dev/null'
                                ))
                                lease_json_start = lease_out.find('{')
                                if lease_json_start >= 0:
                                    try:
                                        lease_data = _json_k8s.loads(lease_out[lease_json_start:])
                                        for lease_item in lease_data.get('items', []):
                                            holder = lease_item.get('spec', {}).get('holderIdentity', '')
                                            lease_name = lease_item.get('metadata', {}).get('name', '')
                                            if ('vsphere-csi-controller' in holder and
                                                    holder != csi_pod_name and csi_pod_name):
                                                lsf.write_output(f'  Deleting stale CSI lease: {lease_name}')
                                                vcfa_ssh(
                                                    f'{kctl_prefix} kubectl delete lease {lease_name} '
                                                    f'-n kube-system 2>/dev/null'
                                                )
                                                csi_remediation_taken = True
                                    except Exception:
                                        pass
                                
                                # Check if CSI controller is failing due to vCenter password
                                csi_log_out = _get_stdout(vcfa_ssh(
                                    f'{kctl_prefix} kubectl logs -n kube-system {csi_pod_name} '
                                    f'-c vsphere-csi-controller --tail=20 2>/dev/null'
                                ))
                                if 'Cannot complete login due to an incorrect user name or password' in csi_log_out:
                                    lsf.write_output('  CSI controller failing due to vCenter password - fixing via dir-cli...')
                                    csi_conf_decoded = _get_stdout(vcfa_ssh(
                                        f'{kctl_prefix} kubectl get secret vsphere-config-secret '
                                        f'-n kube-system -o jsonpath="{{.data.csi-vsphere\\.conf}}" '
                                        f'2>/dev/null | base64 -d | grep user'
                                    ))
                                    csi_cloud_pass = _get_stdout(vcfa_ssh(
                                        f'{kctl_prefix} kubectl get secret vsphere-cloud-secret '
                                        f'-n kube-system -o jsonpath='
                                        f'"{{.data.vc-mgmt-a\\.site-a\\.vcf\\.lab\\.password}}" 2>/dev/null '
                                        f'| base64 -d'
                                    )).strip().split('\n')[-1].strip()
                                    
                                    if csi_conf_decoded and csi_cloud_pass:
                                        import re as _re_csi
                                        csi_user_match = _re_csi.search(r'"([^"]+@[^"]+)"', csi_conf_decoded)
                                        if csi_user_match:
                                            csi_account = csi_user_match.group(1).split('@')[0]
                                            lsf.write_output(f'  Resetting password for {csi_account} via dir-cli')
                                            lsf.ssh(
                                                f'/usr/lib/vmware-vmafd/bin/dir-cli password reset '
                                                f'--account {csi_account} --new \'{csi_cloud_pass}\' '
                                                f'--login administrator@vsphere.local --password \'{password}\'',
                                                f'root@{vcenter_host}'
                                            )
                                            csi_remediation_taken = True
                                
                                # Only force-delete if we found and fixed a root cause,
                                # or if the pod is in CrashLoopBackOff (won't recover on its own)
                                if csi_remediation_taken or 'CrashLoopBackOff' in csi_pod_status:
                                    lsf.write_output(f'  Force-deleting CSI controller pod: {csi_pod_name}')
                                    vcfa_ssh(
                                        f'{kctl_prefix} kubectl delete pod {csi_pod_name} -n kube-system '
                                        f'--grace-period=0 --force 2>/dev/null'
                                    )
                                    csi_fixed = True
                                    time.sleep(30)
                                    
                                    lsf.write_output('  Waiting for new CSI controller pod...')
                                    for wait_s in range(0, 120, 15):
                                        new_csi_out = _get_stdout(vcfa_ssh(
                                            f'{kctl_prefix} kubectl get pods -n kube-system '
                                            f'-l app=vsphere-csi-controller --no-headers 2>/dev/null '
                                            f'| grep -v Terminating'
                                        ))
                                        if '7/7' in new_csi_out:
                                            lsf.write_output('  CSI controller is now fully ready (7/7)')
                                            break
                                        ready_col = ''
                                        for ln in new_csi_out.strip().split('\n'):
                                            parts = ln.split()
                                            if len(parts) >= 2:
                                                ready_col = parts[1]
                                        lsf.write_output(f'  CSI controller status: {ready_col or "pending"} ({wait_s}/120s)')
                                        time.sleep(15)
                                else:
                                    lsf.write_output('  CSI pod in Error state but no actionable root cause found, skipping force-delete')
                            else:
                                # Pod exists but is still initializing — give it time
                                lsf.write_output('  CSI controller pods are starting, waiting up to 60s...')
                                for _csi_wait in range(4):
                                    time.sleep(15)
                                    csi_recheck = _get_stdout(vcfa_ssh(
                                        f'{kctl_prefix} kubectl get pods -n kube-system '
                                        f'-l app=vsphere-csi-controller '
                                        f'-o jsonpath="{{.items[0].status.containerStatuses[*].ready}}" 2>/dev/null'
                                    ))
                                    if 'false' not in csi_recheck and csi_recheck.strip():
                                        lsf.write_output('  CSI controller became ready')
                                        break
                                else:
                                  lsf.write_output('  CSI controller still initializing after 60s (will continue)')
                        
                        # ---- Step 12: RabbitMQ .erlang.cookie permissions ----
                        lsf.write_output('Checking RabbitMQ status...')
                        rmq_check = vcfa_ssh(
                            f'{kctl_prefix} kubectl get pod rabbitmq-ha-0 -n prelude --no-headers 2>/dev/null'
                        )
                        rmq_out = _get_stdout(rmq_check)
                        rmq_needs_fix = False
                        if 'CrashLoopBackOff' in rmq_out or 'Error' in rmq_out:
                            rmq_logs = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl logs rabbitmq-ha-0 -n prelude --tail 10 2>/dev/null'
                            ))
                            if 'erlang.cookie' in rmq_logs and 'accessible by owner only' in rmq_logs:
                                rmq_needs_fix = True
                                lsf.write_output('  RabbitMQ .erlang.cookie has wrong permissions - fixing')
                        
                        if rmq_needs_fix:
                            rmq_img_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pod rabbitmq-ha-0 -n prelude '
                                f'-o jsonpath="{{.spec.containers[0].image}}" 2>/dev/null'
                            ))
                            rmq_image = rmq_img_out.strip().split('\n')[-1].strip() if rmq_img_out else ''
                            
                            if rmq_image and 'registry' in rmq_image:
                                lsf.write_output('  Deploying cookie fix pod...')
                                vcfa_ssh(
                                    f'{kctl_prefix} kubectl run rabbitmq-cookie-fix -n prelude '
                                    f'--image={rmq_image} --restart=Never '
                                    f'--overrides=\'{{"spec":{{"securityContext":{{"runAsUser":0}},'
                                    f'"containers":[{{"name":"fix","image":"{rmq_image}",'
                                    f'"command":["sh","-c","chmod 400 /var/lib/rabbitmq/.erlang.cookie && echo FIXED"],'
                                    f'"volumeMounts":[{{"name":"rabbit-pvc","mountPath":"/var/lib/rabbitmq"}}]}}],'
                                    f'"volumes":[{{"name":"rabbit-pvc","persistentVolumeClaim":'
                                    f'{{"claimName":"rabbit-pvc-rabbitmq-ha-0"}}}}]}}}}\' 2>/dev/null'
                                )
                                time.sleep(20)
                                
                                fix_logs = _get_stdout(vcfa_ssh(
                                    f'{kctl_prefix} kubectl logs rabbitmq-cookie-fix -n prelude 2>/dev/null'
                                ))
                                if 'FIXED' in fix_logs:
                                    lsf.write_output('  RabbitMQ cookie permissions fixed')
                                    vcfa_ssh(f'{kctl_prefix} kubectl delete pod rabbitmq-cookie-fix -n prelude 2>/dev/null')
                                    vcfa_ssh(f'{kctl_prefix} kubectl delete pod rabbitmq-ha-0 -n prelude 2>/dev/null')
                                    lsf.write_output('  Restarted rabbitmq-ha-0')
                                else:
                                    lsf.write_output('  WARNING: Cookie fix pod did not report success')
                                    vcfa_ssh(f'{kctl_prefix} kubectl delete pod rabbitmq-cookie-fix -n prelude --force 2>/dev/null')
                            else:
                                lsf.write_output('  WARNING: Could not determine RabbitMQ image for fix pod')
                        elif 'Running' in rmq_out:
                            lsf.write_output('  RabbitMQ is running normally')
                        else:
                            lsf.write_output(f'  RabbitMQ status: {rmq_out.split()[2] if len(rmq_out.split()) > 2 else "not found"}')
                        
                        # ---- Step 13: provisioning-service deadlock fix ----
                        lsf.write_output('Checking provisioning-service for deadlock...')
                        prov_raw = _get_stdout(vcfa_ssh(
                            f'{kctl_prefix} kubectl get deployment provisioning-service-app '
                            f'-n prelude -o jsonpath="{{.status.readyReplicas}}" 2>/dev/null'
                        ))
                        prov_ready_out = prov_raw.strip().split('\n')[-1].strip() if prov_raw.strip() else ''
                        
                        if prov_ready_out and prov_ready_out != '0':
                            lsf.write_output('  provisioning-service is ready')
                        else:
                            # Not ready — but only patch if the pod is actually stuck, not just starting
                            prov_pod_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pods -n prelude --no-headers 2>/dev/null '
                                f'| grep provisioning-service-app'
                            ))
                            prov_is_failing = any(
                                state in prov_pod_out
                                for state in ['CrashLoopBackOff', 'Error', 'CreateContainerError']
                            )
                            if not prov_is_failing and prov_pod_out.strip():
                                lsf.write_output('  provisioning-service is still starting (not in failure state)')
                            else:
                                prov_java_opts = _get_stdout(vcfa_ssh(
                                    f'{kctl_prefix} kubectl get deployment provisioning-service-app '
                                    f'-n prelude -o jsonpath='
                                    f'"{{.spec.template.spec.containers[0].env[?(@.name==\\"JAVA_OPTS\\")].value}}" '
                                    f'2>/dev/null'
                                ))
                                if 'exemplars.enabled=false' in prov_java_opts:
                                    lsf.write_output('  provisioning-service already has exemplars fix applied')
                                elif not prov_pod_out.strip():
                                    lsf.write_output('  provisioning-service pod not found (deployment may be at 0 replicas)')
                                else:
                                    lsf.write_output('  Patching provisioning-service to disable Prometheus exemplars...')
                                    prov_dep_out = _get_stdout(vcfa_ssh(
                                        f'{kctl_prefix} kubectl get deployment provisioning-service-app '
                                        f'-n prelude -o json 2>/dev/null'
                                    ))
                                    prov_json_start = prov_dep_out.find('{')
                                    if prov_json_start >= 0:
                                        try:
                                            prov_data = _json_k8s.loads(prov_dep_out[prov_json_start:])
                                            fix_flag = '-Dmanagement.prometheus.metrics.export.exemplars.enabled=false'
                                            for container in prov_data.get('spec', {}).get('template', {}).get('spec', {}).get('containers', []):
                                                if container.get('name') == 'provisioning-service-app':
                                                    for env_var in container.get('env', []):
                                                        if env_var.get('name') == 'JAVA_OPTS':
                                                            if fix_flag not in env_var.get('value', ''):
                                                                env_var['value'] = env_var['value'].rstrip() + '\n' + fix_flag
                                                            break
                                                    break
                                            
                                            import tempfile
                                            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp_f:
                                                _json_k8s.dump(prov_data, tmp_f)
                                                tmp_path = tmp_f.name
                                            
                                            lsf.run_command(
                                                f'sshpass -p "{password}" scp -o StrictHostKeyChecking=no '
                                                f'{tmp_path} {vcfa_user}@{vcfa_k8s_ip}:/tmp/prov-deploy-patched.json'
                                            )
                                            vcfa_ssh(f'{kctl_prefix} kubectl apply -f /tmp/prov-deploy-patched.json 2>/dev/null')
                                            lsf.write_output('  provisioning-service patched, new pod will roll out')
                                            
                                            try:
                                                os.unlink(tmp_path)
                                            except Exception:
                                                pass
                                        except Exception as prov_err:
                                            lsf.write_output(f'  WARNING: Could not patch provisioning-service: {prov_err}')
                        
                        # ---- Step 14: Scale up zero-replica prelude deployments ----
                        # For each deployment, check if it has a saved
                        # vcf.lab/original-replicas annotation (set during a
                        # previous startup when it was healthy). If not, default
                        # to 1. Save the annotation on healthy deployments so
                        # future startups can restore the correct value.
                        lsf.write_output('Checking prelude deployments...')
                        dep_json = vcfa_ssh(
                            f'{kctl_prefix} kubectl get deployments -n prelude -o json 2>/dev/null'
                        )
                        zero_deps = []
                        dep_out = _get_stdout(dep_json)
                        if dep_out:
                            try:
                                raw_dep = dep_out.strip()
                                json_start_dep = raw_dep.find('{')
                                if json_start_dep >= 0:
                                    raw_dep = raw_dep[json_start_dep:]
                                dep_data = _json_k8s.loads(raw_dep)
                                total_deps = len(dep_data.get('items', []))
                                for d in dep_data.get('items', []):
                                    d_name = d['metadata']['name']
                                    d_replicas = d['spec'].get('replicas', 1)
                                    d_annos = d.get('metadata', {}).get('annotations', {}) or {}
                                    saved_val = d_annos.get('vcf.lab/original-replicas', '')
                                    if d_replicas == 0:
                                        intended = int(saved_val) if saved_val.isdigit() and int(saved_val) > 0 else 1
                                        zero_deps.append((d_name, intended))
                                    elif d_replicas > 0:
                                        # Save current healthy replica count for future restores
                                        if saved_val != str(d_replicas):
                                            vcfa_ssh(
                                                f'{kctl_prefix} kubectl annotate deployment {d_name} '
                                                f'-n prelude vcf.lab/original-replicas={d_replicas} '
                                                f'--overwrite 2>/dev/null'
                                            )
                                lsf.write_output(f'  {len(zero_deps)} of {total_deps} deployments at 0 replicas')
                            except Exception as dep_err:
                                lsf.write_output(f'  WARNING: Could not parse deployment JSON: {dep_err}')
                        
                        if zero_deps:
                            lsf.write_output(f'  Scaling up {len(zero_deps)} deployments...')
                            batch_size = 10
                            for i in range(0, len(zero_deps), batch_size):
                                batch = zero_deps[i:i+batch_size]
                                batch_cmd = f'{kctl_prefix} ' + ' '.join(
                                    f'kubectl scale deployment {d_name} -n prelude --replicas={d_intended} 2>/dev/null;'
                                    for d_name, d_intended in batch
                                )
                                result = vcfa_ssh(batch_cmd)
                                scaled_count = _get_stdout(result).count('scaled')
                                lsf.write_output(f'  Batch {i//batch_size + 1}: scaled {scaled_count} deployments')
                            
                            lsf.write_output('  Prelude deployment scale-up complete')
                        else:
                            lsf.write_output('  All prelude deployments already have replicas > 0')

                        # ---- Always check prelude StatefulSets ----
                        # rabbitmq-ha, tenant-manager, and vco-app are StatefulSets that can be
                        # at 0 replicas even when all Deployments are healthy (e.g. after a
                        # graceful shutdown that only scaled Deployments to 0). The old code
                        # nested this check inside "if zero_deps:" so it was silently skipped
                        # when Deployments were already running. Without RabbitMQ and
                        # tenant-manager, api-gateway-server crashes on OIDC JWKS fetch
                        # (connection refused) causing /login/ to return HTTP 500 for the
                        # entire URL-check window.
                        zero_sts = False
                        lsf.write_output('Checking prelude StatefulSets...')
                        ss_check = vcfa_ssh(
                            f'{kctl_prefix} kubectl get statefulsets -n prelude -o json 2>/dev/null'
                        )
                        ss_out = _get_stdout(ss_check)
                        if ss_out:
                            try:
                                raw_ss = ss_out.strip()
                                json_start_ss = raw_ss.find('{')
                                if json_start_ss >= 0:
                                    raw_ss = raw_ss[json_start_ss:]
                                ss_data = _json_k8s.loads(raw_ss)
                                for ss in ss_data.get('items', []):
                                    ss_name = ss['metadata']['name']
                                    ss_replicas = ss['spec'].get('replicas', 1)
                                    ss_annos = ss.get('metadata', {}).get('annotations', {}) or {}
                                    ss_saved = ss_annos.get('vcf.lab/original-replicas', '')
                                    if ss_replicas == 0:
                                        ss_intended = int(ss_saved) if ss_saved.isdigit() and int(ss_saved) > 0 else 1
                                        lsf.write_output(f'  Scaling up StatefulSet {ss_name} to {ss_intended} (was 0)')
                                        vcfa_ssh(
                                            f'{kctl_prefix} kubectl scale statefulset {ss_name} '
                                            f'-n prelude --replicas={ss_intended} 2>/dev/null'
                                        )
                                        zero_sts = True
                                    elif ss_replicas > 0:
                                        if ss_saved != str(ss_replicas):
                                            vcfa_ssh(
                                                f'{kctl_prefix} kubectl annotate statefulset {ss_name} '
                                                f'-n prelude vcf.lab/original-replicas={ss_replicas} '
                                                f'--overwrite 2>/dev/null'
                                            )
                            except Exception:
                                pass

                        # ---- Step 14b: Post-scale node re-check ----
                        # vmsp-operator (system:serviceaccount:vmsp:vmsp-operator) re-cordons
                        # the VCFA K8s node ~90s after startup when its reconciliation loop
                        # runs and finds a stale "maintenance in progress" state from the
                        # previous shutdown. Wait for it, then override the cordon.
                        # Triggered if either deployments OR statefulsets were scaled up.
                        if zero_deps or zero_sts:
                            lsf.write_output('  Waiting 120s for vmsp-operator to settle after scale-up...')
                            time.sleep(120)
                            _post_nd = _get_stdout(
                                vcfa_ssh(f'{kctl_prefix} kubectl get nodes --no-headers 2>/dev/null')
                            )
                            _re_cordon_found = False
                            if _post_nd:
                                for _post_nd_line in _post_nd.strip().split('\n'):
                                    if 'SchedulingDisabled' in _post_nd_line and _post_nd_line.strip():
                                        _post_nd_name = _post_nd_line.split()[0]
                                        _re_cordon_found = True
                                        lsf.write_output(
                                            f'  [POST-SCALE RE-CORDON] {_post_nd_name} was re-cordoned '
                                            f'by vmsp-operator — overriding...'
                                        )
                                        vcfa_ssh(f'{kctl_prefix} kubectl uncordon {_post_nd_name}')
                                        lsf.write_output(f'  {_post_nd_name} uncordoned (post-scale override)')
                            if not _re_cordon_found:
                                lsf.write_output('  No post-scale re-cordon detected — node scheduling is clean')
                        
                        # ---- Step 15: Recover prelude pods stuck after volume/CSI fixes ----
                        if stuck_va_fixed or csi_fixed:
                            lsf.write_output('Volume attachments or CSI controller were fixed, checking prelude pods...')
                            time.sleep(10)
                            
                            stuck_pods_out = _get_stdout(vcfa_ssh(
                                f'{kctl_prefix} kubectl get pods -n prelude --no-headers 2>/dev/null '
                                f'| grep -E "ContainerCreating|Init:"'
                            ))
                            if stuck_pods_out.strip():
                                stuck_pod_names = [
                                    line.split()[0] for line in stuck_pods_out.strip().split('\n')
                                    if line.strip() and len(line.split()) >= 1
                                ]
                                lsf.write_output(f'  Found {len(stuck_pod_names)} stuck prelude pods, deleting for fresh mount...')
                                for sp_name in stuck_pod_names:
                                    lsf.write_output(f'  Deleting stuck pod: {sp_name}')
                                    vcfa_ssh(
                                        f'{kctl_prefix} kubectl delete pod {sp_name} -n prelude 2>/dev/null'
                                    )
                                lsf.write_output('  Waiting 60s for pods to recreate with fresh volume mounts...')
                                time.sleep(60)
                            else:
                                lsf.write_output('  No stuck prelude pods found, services should recover normally')
                
                lsf.write_output('VCF Automation K8s health check complete')
                
        except Exception as k8s_err:
            lsf.write_output(f'WARNING: VCF Automation K8s health check failed: {k8s_err}')
            vcfa_k8s_remediation_ok = False
        
        if dashboard:
            if vcfa_k8s_remediation_ok:
                dashboard.update_task('vcffinal', 'vcfa_k8s_health', TaskStatus.COMPLETE)
            else:
                dashboard.update_task('vcffinal', 'vcfa_k8s_health', TaskStatus.FAILED,
                                      'See log for details')
            dashboard.generate_html()
    else:
        if dashboard:
            dashboard.update_task('vcffinal', 'vcfa_k8s_health', TaskStatus.SKIPPED,
                                  'VCF Automation not configured')
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
        lsf.write_output('Checking VCF Automation Checks...')
        lsf.write_vpodprogress('VCF Automation Checks', 'GOOD-8')
        
        # Run remediation scripts before URL checks
        # Check VCF Automation ssh for password expiration and fix if expired
        lsf.write_output('Fixing expired automation password if necessary...')
        vcfapwcheck_script = '/home/holuser/hol/Tools/vcfapwcheck.sh'
        if os.path.isfile(vcfapwcheck_script) and not dry_run:
            lsf.run_command(vcfapwcheck_script)

        # Ensure vmware-system-user never expires on VCF Automation (auto-*)
        # and VCF Ops Logs (opslogs-*) VMs.  Both use vmware-system-user for
        # SSH with password-based sudo.  On initial deployment the account
        # maxdays may be 365 (OS default); vcfapass.sh resets the password but
        # previously did not clear maxdays, starting a fresh 365-day clock.
        # Running chage -M -1 at every startup is idempotent and corrects
        # both the initial template state and any subsequent password changes.
        if not dry_run:
            _chage_hosts = []
            # VCF Automation hosts from vraurls (auto-a, auto-platform-a, etc.)
            for _us in vraurls:
                _uh = _us.split(',')[0].strip()
                if '://' in _uh:
                    _uh = _uh.split('://')[1].split('/')[0].split(':')[0]
                if _uh and _uh.startswith('auto-') and _uh not in _chage_hosts:
                    _chage_hosts.append(_uh)
            # opslogs hosts from vcfcomponenturls
            _comp_urls_raw = lsf.get_config_list('VCFFINAL', 'vcfcomponenturls')
            for _us in _comp_urls_raw:
                _uh = _us.split(',')[0].strip()
                if '://' in _uh:
                    _uh = _uh.split('://')[1].split('/')[0].split(':')[0]
                if _uh and 'opslogs' in _uh.lower() and _uh not in _chage_hosts:
                    _chage_hosts.append(_uh)
            for _chage_host in _chage_hosts:
                if not lsf.test_tcp_port(_chage_host, 22, timeout=5):
                    lsf.write_output(
                        f'  {_chage_host}: SSH not reachable — skipping chage')
                    continue
                lsf.write_output(
                    f'  {_chage_host}: Ensuring vmware-system-user '
                    f'password never expires...')
                try:
                    _chage_cmd = (
                        f"echo '{password}' | sudo -S chage -M -1 "
                        f"vmware-system-user 2>&1"
                    )
                    _cr = lsf.ssh(
                        _chage_cmd,
                        f'vmware-system-user@{_chage_host}',
                        password,
                    )
                    if _cr.returncode == 0:
                        lsf.write_output(
                            f'  {_chage_host}: vmware-system-user '
                            f'password expiration set to never')
                    else:
                        lsf.write_output(
                            f'  {_chage_host}: WARNING — chage -M -1 '
                            f'returned exit {_cr.returncode}')
                except Exception as _ce:
                    lsf.write_output(
                        f'  {_chage_host}: WARNING — could not run '
                        f'chage: {_ce}')

        # If the lab_sku = HOL-2701, then run vcfa-stabilizer.sh
        if lsf.lab_sku == 'HOL-2701':
            lsf.write_output('Running vcfa-stabilizer.sh...')
            try:
                proc = subprocess.Popen(
                    ['/bin/bash', '/home/holuser/hol/Tools/vcfa-stabilizer.sh'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                for line in proc.stdout:
                    lsf.write_output(f'  {line.rstrip()}')
                proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                lsf.write_output('  [STDERR] Timeout executing vcfa-stabilizer.sh')
                proc.kill()
            except Exception as e:
                lsf.write_output(f'  [STDERR] Error executing vcfa-stabilizer.sh: {e}')

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
                            # Every 3 attempts re-check node scheduling — catches vmsp-operator
                            # re-cordons that slip through Step 14b (e.g. very slow startup).
                            if attempt % 3 == 0:
                                try:
                                    _url_nd = _get_stdout(vcfa_ssh(
                                        f'{kctl_prefix} kubectl get nodes --no-headers 2>/dev/null'
                                    ))
                                    for _url_nd_line in _url_nd.split('\n'):
                                        if 'SchedulingDisabled' in _url_nd_line and _url_nd_line.strip():
                                            _url_nd_name = _url_nd_line.split()[0]
                                            lsf.write_output(
                                                f'  [NODE MONITOR] {_url_nd_name} is re-cordoned '
                                                f'(attempt {attempt}) — uncordoning...'
                                            )
                                            vcfa_ssh(
                                                f'{kctl_prefix} kubectl uncordon {_url_nd_name} 2>/dev/null'
                                            )
                                except (NameError, Exception):
                                    pass
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
                            if attempt >= 5 and 'opslogs' in url.lower():
                                lsf.write_output(f'  [WARNING] opslogs unreachable at attempt {attempt}. Re-scaling StatefulSets to 1 (VSP VIP 10.1.1.142)...')
                                pwd = lsf.get_password()
                                scale_result = lsf.ssh(f"echo '{pwd}' | sudo -S -i bash -c 'kubectl scale statefulset/log-processor statefulset/log-store -n ops-logs --replicas=1 2>&1'", 'vmware-system-user@10.1.1.142', pwd)
                                scale_out = scale_result.stdout.strip() if hasattr(scale_result, 'stdout') and scale_result.stdout else '(no output / SSH failed)'
                                lsf.write_output(f'  StatefulSet rescale result: {scale_out}')
                                if 'scaled' in scale_out or 'unchanged' in scale_out:
                                    lsf.ssh(f"echo '{pwd}' | sudo -S -i bash -c 'kubectl annotate components.api.vmsp.vmware.com ops-logs component.vmsp.vmware.com/operational-status=Running --overwrite 2>&1'", 'vmware-system-user@10.1.1.142', pwd)
                            
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
    
    if dashboard:
        dashboard.update_task('vcffinal', 'nsx_passwords', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    nsx_mgr_entries = lsf.get_config_list('VCF', 'vcfnsxmgr')
    nsx_users = ['admin', 'root', 'audit']
    nsx_expiry_days = 999
    nsx_expiry_threshold_days = 90   # Only update if current expiry < this
    nsx_task_failed = False
    password = lsf.get_password()

    # NSX user IDs used by GET /api/v1/node/users/{id}
    _nsx_user_id = {'root': 0, 'admin': 10000, 'audit': 10002}

    _NSX_EXPIRY_API_ERROR = -1   # sentinel: REST call failed, fall back to update

    def _nsx_days_until_expiry(fqdn, admin_pwd, username):
        """Return days until NSX user password expires via REST API.

        Returns:
            int  > 0               — days remaining until expiry
            0                      — NSX "no expiry" sentinel in response
            None                   — key absent from response (no expiry configured)
            _NSX_EXPIRY_API_ERROR  — REST call failed; caller should update
        """
        import urllib.request as _ureq
        import ssl as _ssl
        import base64 as _b64
        import json as _json
        uid = _nsx_user_id.get(username)
        if uid is None:
            return None
        _ctx = _ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = _ssl.CERT_NONE
        _creds = _b64.b64encode(f'admin:{admin_pwd}'.encode()).decode()
        _req = _ureq.Request(
            f'https://{fqdn}/api/v1/node/users/{uid}',
            headers={'Authorization': f'Basic {_creds}',
                     'Accept': 'application/json'},
        )
        try:
            with _ureq.urlopen(_req, timeout=10, context=_ctx) as _r:
                return _json.loads(_r.read().decode()).get(
                    'days_until_password_expiry')
        except Exception:
            return _NSX_EXPIRY_API_ERROR

    if nsx_mgr_entries:
        lsf.write_output(
            f'Checking NSX Manager password expiration '
            f'(threshold: {nsx_expiry_threshold_days}d)...'
        )
        lsf.write_vpodprogress('NSX Password Config', 'GOOD-8')

        for entry in nsx_mgr_entries:
            nsx_host = entry.split(':')[0].strip()
            nsx_fqdn = f'{nsx_host}.site-a.vcf.lab' if '.' not in nsx_host else nsx_host

            if not dry_run:
                if not lsf.test_tcp_port(nsx_fqdn, 22, timeout=5):
                    lsf.write_output(f'  {nsx_fqdn}: SSH not reachable - skipping')
                    continue

                for user in nsx_users:
                    current = _nsx_days_until_expiry(nsx_fqdn, password, user)

                    if current == _NSX_EXPIRY_API_ERROR:
                        # REST call failed — can't verify; update as safe fallback
                        lsf.write_output(
                            f'  {nsx_fqdn}: {user} — REST API check failed, '
                            f'updating as safe fallback')
                    elif current is None:
                        # Key absent in response → no expiry configured → skip
                        lsf.write_output(
                            f'  {nsx_fqdn}: {user} — no expiry configured '
                            f'setting to {nsx_expiry_days}d')
                        #     f'(password_change_frequency=0) — SKIP')
                        # continue
                    # elif current == 0:
                    #     # NSX "no expiry" sentinel value → skip
                    #     lsf.write_output(
                    #         f'  {nsx_fqdn}: {user} — API reports 0 days '
                    #         f'(non-expiring) — SKIP')
                    #     continue
                    elif current > nsx_expiry_threshold_days:
                        lsf.write_output(
                            f'  {nsx_fqdn}: {user} — expires in {current}d '
                            f'(> {nsx_expiry_threshold_days}d threshold) — SKIP')
                        continue
                    else:
                        # Expires within threshold — update
                        lsf.write_output(
                            f'  {nsx_fqdn}: {user} — expires in {current}d '
                            f'(≤ {nsx_expiry_threshold_days}d) — setting to '
                            f'{nsx_expiry_days}d')
                    result = lsf.ssh(
                        f'set user {user} password-expiration {nsx_expiry_days}',
                        f'admin@{nsx_fqdn}', password
                    )
                    if result.returncode == 0:
                        lsf.write_output(
                            f'  {nsx_fqdn}: {user} password expiration '
                            f'updated to {nsx_expiry_days} days')
                    else:
                        lsf.write_output(
                            f'  {nsx_fqdn}: WARNING — could not set {user} '
                            f'password expiration (exit {result.returncode})')
                        nsx_task_failed = True
            else:
                lsf.write_output(
                    f'  Would check NSX user expiry on {nsx_fqdn} and update '
                    f'any user expiring within {nsx_expiry_threshold_days}d '
                    f'to {nsx_expiry_days}d'
                )
    
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
    
    if dashboard:
        if nsx_mgr_entries:
            if nsx_task_failed:
                dashboard.update_task('vcffinal', 'nsx_passwords', TaskStatus.FAILED, 'See log')
            else:
                dashboard.update_task('vcffinal', 'nsx_passwords', TaskStatus.COMPLETE)
        else:
            dashboard.update_task('vcffinal', 'nsx_passwords', TaskStatus.SKIPPED,
                                  'No NSX managers configured')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 8: DISTRIBUTE VAULT CA TRUST
    #==========================================================================
    
    lsf.write_output('Fetching Vault Root CA certificate...')
    import urllib.request
    try:
        req = urllib.request.Request('http://10.1.1.1:32000/v1/pki/ca/pem')
        with urllib.request.urlopen(req, timeout=10) as response:
            vault_ca_pem = response.read().decode('utf-8').strip()
            
        if vault_ca_pem and 'BEGIN CERTIFICATE' in vault_ca_pem:
            lsf.write_output('Successfully fetched Vault CA certificate.')
            
            # Dynamically import confighol-9.1.py to reuse its trust distribution logic
            import importlib.util
            confighol_path = '/home/holuser/hol/Tools/confighol-9.1.py'
            if os.path.exists(confighol_path):
                spec = importlib.util.spec_from_file_location("confighol", confighol_path)
                confighol = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(confighol)
                
                # Execute the distribution function
                lsf.write_output('Distributing Vault CA trust across VCF suite...')
                confighol.distribute_vault_ca_trust(vault_ca_pem, lsf.get_password(), dry_run=dry_run)

                # Import the active Vault CA into Firefox on the console VM.
                # distribute_vault_ca_trust() handles VCF infrastructure (vCenter,
                # ESXi, NSX, SDDC Manager, VCFA, Ops) but not the Firefox cert store.
                # import_ca_to_firefox_profile() deletes any stale entry first, then
                # re-imports, so repeated runs are safe and always keep Firefox in sync
                # with the current active CA — fixing the vpodchecker FAIL
                # "Active Vault CA not found in Firefox certificate store".
                lsf.write_output('Importing Vault CA into Firefox on console VM...')
                try:
                    ff_profiles = confighol.find_firefox_profiles()
                    if ff_profiles:
                        imported = 0
                        for ff_profile in ff_profiles:
                            if confighol.import_ca_to_firefox_profile(
                                vault_ca_pem, ff_profile,
                                confighol.VAULT_CA_NAME, dry_run=dry_run,
                            ):
                                imported += 1
                        lsf.write_output(
                            f'Vault CA imported into {imported}/{len(ff_profiles)} '
                            f'Firefox profile(s).'
                        )
                    else:
                        lsf.write_output(
                            'WARNING: No Firefox profiles found on console VM — '
                            'skipping Firefox CA import.'
                        )
                except Exception as ff_exc:
                    lsf.write_output(
                        f'WARNING: Firefox CA import failed: {ff_exc}'
                    )
            else:
                lsf.write_output(f'WARNING: Could not find {confighol_path} to distribute CA trust.')
        else:
            lsf.write_output('WARNING: Invalid Vault CA certificate received.')
    except Exception as e:
        lsf.write_output(f'WARNING: Failed to fetch Vault CA: {e}')

    #==========================================================================
    # TASK 8b: Authentik + VCF integration (optional, [VCFFINAL] config-gated)
    #==========================================================================
    try:
        if lsf.config.has_option('VCFFINAL', 'authentik_vcf_integration'):
            raw = lsf.config.get('VCFFINAL', 'authentik_vcf_integration', fallback='').strip().lower()
            if raw in ('1', 'true', 'yes', 'on'):
                import importlib.util
                ak_path = '/home/holuser/hol/Tools/authentik_vcf_integration.py'
                if os.path.isfile(ak_path):
                    spec = importlib.util.spec_from_file_location(
                        'authentik_vcf_integration', ak_path)
                    akmod = importlib.util.module_from_spec(spec)
                    assert spec.loader is not None
                    spec.loader.exec_module(akmod)
                    lsf.write_output('Running Authentik + VCF integration (authentik_vcf_integration)...')
                    ok_ak = akmod.run_authentik_vcf_integration(
                        lsf, dry_run=dry_run, config_path='/tmp/config.ini')
                    if not ok_ak:
                        lsf.write_output(
                            'WARNING: authentik_vcf_integration completed with errors (see messages above).')
                else:
                    lsf.write_output(
                        f'WARNING: {ak_path} not found — skipping authentik_vcf_integration')
    except Exception as e:
        lsf.write_output(f'WARNING: authentik_vcf_integration failed: {e}')

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
    
    if not dry_run:
        # ---- Fix VCFA Microservices Scaling ----
        # If VCFA was improperly shut down, its deployments and statefulsets may be at 0 replicas.
        # This causes fleet-lcm to fail capability sync because it cannot authenticate against VCFA.
        lsf.write_output('Checking VCFA microservices scaling in prelude namespace...')
        try:
            # Build a helper using the correct sudo -S -i bash -c pattern (required for VCF 9.1
            # where sudo requires a password, and kubectl is only on root's PATH via login shell).
            def _vcfa_kctl(cmd):
                full = (
                    f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no"
                    f" vmware-system-user@10.1.1.70"
                    f" \"echo '{password}' | sudo -S -i bash -c '{cmd}'\""
                )
                return subprocess.run(full, shell=True, capture_output=True, text=True, timeout=30)

            # Safety net: delete any remaining stale system-shutdown Argo Workflows
            # (handles the case where Task 4b VCFA SSH was unavailable at its earlier step).
            wf_out = subprocess.run(
                f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no"
                f" vmware-system-user@10.1.1.70"
                f" \"echo '{password}' | sudo -S -i bash -c"
                r" 'kubectl get workflow -n vmsp-platform --no-headers 2>/dev/null | grep system-shutdown | awk \"{print \\$1}\"'\"",
                shell=True, capture_output=True, text=True, timeout=30
            ).stdout
            stale = [w.strip() for w in wf_out.splitlines() if w.strip()]
            if stale:
                lsf.write_output(f'  Deleting {len(stale)} stale system-shutdown workflow(s) (safety net)...')
                for i in range(0, len(stale), 10):
                    batch = ' '.join(stale[i:i+10])
                    _vcfa_kctl(f'kubectl delete workflow -n vmsp-platform {batch} --grace-period=0 2>/dev/null')
                time.sleep(3)

            # Ensure VCFA node is uncordoned (do this AFTER workflow cleanup)
            _vcfa_kctl('kubectl uncordon auto-platform-a-b7nps 2>/dev/null')
            
            # Scale critical statefulsets to 1
            _vcfa_kctl('kubectl scale statefulset rabbitmq-ha tenant-manager vco-app -n prelude --replicas=1 2>/dev/null')
            
            # Scale all 0-replica deployments to 1
            dep_out = _vcfa_kctl('kubectl get deployments -n prelude -o json 2>/dev/null').stdout
            if '{' in dep_out:
                dep_out = '{' + dep_out.split('{', 1)[1]
                data = json.loads(dep_out)
                for d in data.get('items', []):
                    name = d['metadata']['name']
                    if d['spec'].get('replicas', 1) == 0:
                        lsf.write_output(f'  Scaling VCFA deployment {name} to 1 replica...')
                        _vcfa_kctl(f'kubectl scale deployment {name} -n prelude --replicas=1 2>/dev/null')
        except Exception as e:
            lsf.write_output(f'WARNING: Failed to check or scale VCFA microservices: {e}')

        # ---- Fix Fleet-LCM Component Friendly Names ----
        # Root cause: On cold boot, vcf-fleet-build-service starts before vcf-fleet-upgrade-service
        # is fully ready. Its ComponentPublicNameCache gets a ConnectException on first load, then
        # retries after 5 minutes, but upgrade-service returns 0 VCF releases because FDS
        # download-service was still initializing. The empty result is cached with a 2-hour success
        # TTL, so the UI displays internal type codes (OPS, VCFA) instead of friendly product names.
        # Fix: (1) trigger depot-metadata sync to populate the VCF release table with public names,
        # (2) restart fleet-build-service container so ComponentPublicNameCache reloads fresh data.
        # No source code or database modifications required — all via REST API + crictl.
        # Only applies to HOL-2701, HOL-2702, HOL-2703.
        _fleet_fix_skus = {'HOL-2701', 'HOL-2702', 'HOL-2703'}
        if lsf.lab_sku not in _fleet_fix_skus:
            lsf.write_output(
                f'Skipping Fleet-LCM friendly names fix (not applicable for {lsf.lab_sku})')
        else:
            lsf.write_output('Fixing Fleet-LCM component friendly names...')
            try:
                import requests as _req
                import base64 as _b64
                import socket as _sock
                import re as _re

                _fleet_fqdn = 'fleet-01a.site-a.vcf.lab'
                _fleet_url = f'https://{_fleet_fqdn}'
                _vsp_user = 'vmware-system-user'

                # --- Step 1: Discover VSP control plane IP and read IAM client credentials ---
                _vsp_worker_ip = _sock.gethostbyname('vsp-01a.site-a.vcf.lab')
                _node_conf = lsf.ssh(
                    f"echo '{password}' | sudo -S grep server: /etc/kubernetes/node-agent.conf",
                    f'{_vsp_user}@{_vsp_worker_ip}'
                ).stdout.strip()
                _vsp_cp_ip = None
                for _line in _node_conf.splitlines():
                    if 'server:' in _line:
                        _m = _re.search(r'https?://([0-9.]+):', _line)
                        if _m:
                            _vsp_cp_ip = _m.group(1)
                            break
                if not _vsp_cp_ip:
                    raise ValueError('Could not determine VSP control plane IP from node-agent.conf')

                _secret_out = lsf.ssh(
                    f"echo '{password}' | sudo -S -i kubectl get secret vcf-iam-vcfa-admin "
                    f"-n vcf-fleet-lcm -o jsonpath='{{.data}}' 2>/dev/null",
                    f'{_vsp_user}@{_vsp_cp_ip}'
                ).stdout.strip()
                _json_start = _secret_out.find('{')
                _secret_data = json.loads(_secret_out[_json_start:])
                _client_id = _b64.b64decode(_secret_data['clientId']).decode()
                _client_secret = _b64.b64decode(_secret_data['clientSecret']).decode()
                _basic_creds = _b64.b64encode(f'{_client_id}:{_client_secret}'.encode()).decode()

                # --- Step 2: Obtain fleet-lcm JWT token (OAuth2 password grant) ---
                _tok_resp = _req.post(
                    f'{_fleet_url}/api/v1/identity/token',
                    data={'grant_type': 'password', 'username': 'admin', 'password': password},
                    headers={
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Authorization': f'Basic {_basic_creds}',
                    },
                    verify=False, timeout=20
                )
                _tok_resp.raise_for_status()
                _jwt = _tok_resp.json()['access_token']
                lsf.write_output('  Fleet-LCM JWT token obtained')

                # --- Step 3: Trigger depot-metadata sync to populate VCF release table ---
                # This ensures vcf-fleet-upgrade-service has publicName data for all VCF components
                # before fleet-build-service restarts and reloads its ComponentPublicNameCache.
                _sync_resp = _req.post(
                    f'{_fleet_url}/fleet-lcm/v1/depot-metadata?action=sync',
                    headers={'Authorization': f'Bearer {_jwt}'},
                    verify=False, timeout=30
                )
                if _sync_resp.status_code in (200, 202):
                    _task_id = None
                    try:
                        _sync_body = _sync_resp.json()
                        _task_id = _sync_body.get('id') or _sync_body.get('taskId')
                    except Exception:
                        pass
                    lsf.write_output(f'  Depot-metadata sync triggered (task={_task_id})')
                    if _task_id:
                        for _ in range(24):  # Poll up to 120 seconds
                            time.sleep(5)
                            _tr = _req.get(
                                f'{_fleet_url}/fleet-lcm/v1/tasks/{_task_id}',
                                headers={'Authorization': f'Bearer {_jwt}'},
                                verify=False, timeout=15
                            )
                            _ts = _tr.json().get('status', '')
                            if _ts in ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ERROR'):
                                lsf.write_output(f'  Depot-metadata sync completed: {_ts}')
                                break
                    else:
                        time.sleep(5)
                else:
                    lsf.write_output(
                        f'  WARNING: Depot-metadata sync returned HTTP {_sync_resp.status_code}')

                # --- Step 4: Restart fleet-build-service container on VSP nodes ---
                # Scanning VSP node IP range for the fleetbuild crictl container.
                # After crictl stop + rm, kubelet immediately restarts a fresh container
                # that loads ComponentPublicNameCache from the now-populated release data.
                lsf.write_output('  Restarting fleet-build-service to reload name cache...')
                _fbs_restarted = False
                for _vsp_ip in [f'10.1.1.{i}' for i in range(128, 155)]:
                    try:
                        _cid_res = subprocess.run(
                            f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no"
                            f" -o ConnectTimeout=3 {_vsp_user}@{_vsp_ip}"
                            f" \"echo '{password}' | sudo -S crictl ps 2>/dev/null"
                            f" | grep fleetbuild | awk '{{print $1}}'\"",
                            shell=True, capture_output=True, text=True, timeout=8
                        )
                        _cid = _cid_res.stdout.strip()
                        if _cid:
                            subprocess.run(
                                f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no"
                                f" {_vsp_user}@{_vsp_ip}"
                                f" \"echo '{password}' | sudo -S crictl stop {_cid} 2>/dev/null;"
                                f" echo '{password}' | sudo -S crictl rm {_cid} 2>/dev/null\"",
                                shell=True, capture_output=True, timeout=15
                            )
                            lsf.write_output(
                                f'  Restarted fleet-build-service on {_vsp_ip} (container {_cid[:12]})')
                            _fbs_restarted = True
                            break
                    except Exception:
                        continue

                if not _fbs_restarted:
                    lsf.write_output(
                        '  WARNING: Could not find fleet-build-service container on any VSP node')
                else:
                    # --- Step 5: Verify friendly names are loaded (up to 90 seconds) ---
                    lsf.write_output(
                        '  Waiting up to 90s for fleet-build-service to reload name cache...')
                    for _attempt in range(18):
                        time.sleep(5)
                        try:
                            _vr = _req.get(
                                f'{_fleet_url}/fleet-lcm/v1/components',
                                headers={'Authorization': f'Bearer {_jwt}'},
                                verify=False, timeout=15
                            )
                            if _vr.status_code == 200:
                                _comps = _vr.json()
                                if isinstance(_comps, dict):
                                    _comps = _comps.get('components', [])
                                _named = [
                                    c for c in _comps
                                    if c.get('componentTypeDescription')
                                    and c['componentTypeDescription'] != c.get('componentType')
                                ]
                                if _named:
                                    _ex = _named[0]
                                    lsf.write_output(
                                        f'  Friendly names confirmed: '
                                        f'{_ex["componentType"]} → '
                                        f'"{_ex["componentTypeDescription"]}"'
                                    )
                                    break
                        except Exception:
                            pass
                    else:
                        lsf.write_output(
                            '  WARNING: Fleet-LCM components may still show internal names; '
                            'cache reload may still be in progress')
            except Exception as e:
                lsf.write_output(f'WARNING: Failed to fix fleet-lcm component friendly names: {e}')

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
