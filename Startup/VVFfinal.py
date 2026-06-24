#!/usr/bin/env python3
# VVFfinal.py - HOLFY27 Core VVF Final Tasks Module
# Version 1.4 - 2026-06-24
# Author - Burke Azbill and HOL Core Team
# VVF final startup tasks: VSP platform VMs, Fleet component health, URL checks
#
# Runs after VVF.py and vSphere.py complete. Skips immediately if no [VVFFINAL]
# section is present in config.ini (safe for VCF labs).
#
# v1.4 Changes:
# - Task 1b enhanced with full manual recovery (KB 440862 steps 2-5):
#   Step B now fetches power-off-marker ConfigMap as JSON, parses vmspcontent
#   (vmsp-platform deployments, name=N) and content (tenant services,
#   namespace.kind.name=N) fields and scales each resource to its exact saved
#   replica count, then deletes the ConfigMap. Falls through to Task 2b generic
#   scaling for anything not covered (empty fields, parse error, or ConfigMap
#   absent). Step C now parses nodes as JSON to exclude any node with
#   ToBeDeletedByClusterAutoscaler taint from uncordon, with text-based fallback.
# - Added Task 1b: VSP Node Pre-Flight (KB 440862 deadlock prevention).
#   Runs BEFORE the Task 2 API health poll. Deletes stale system-shutdown Argo
#   Workflows in vmsp-platform, checks power-off-marker ConfigMap, and
#   uncordons any Ready,SchedulingDisabled nodes. Prevents the boot deadlock
#   where the power-off-marker auto-recovery workflow waits for schedulable
#   nodes while nodes remain cordoned, causing Task 2 to time out for 15 min.
#
# Task overview:
#   Task 1  - Start/verify VSP Platform VMs ([VVF] vvfvspvms) via vCenter
#   Task 1b - VSP node pre-flight: delete stale Argo Workflows, uncordon nodes
#   Task 2  - Wait for VSP management API (port 5480) to become healthy per site
#   Task 2b - Clean stale Argo Workflows, uncordon nodes, scale up zero-replica
#             deployments in vcf-fleet-lcm and vmsp-platform (non-fatal)
#   Task 3  - K8s certificate check/renewal on VSP clusters (non-fatal)
#   Task 4  - Verify VCF component URLs ([VVFFINAL] vcfcomponenturls)

import os
import sys
import argparse
import logging
import time
import ssl
import subprocess

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')
sys.path.insert(0, '/home/holuser/hol/Tools')

logging.basicConfig(
    level=logging.WARNING,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VVFfinal'
MODULE_DESCRIPTION = 'VVF Final Startup Tasks'

VSP_API_PORT = 5480
VSP_API_HEALTH_TIMEOUT = 900    # 15 minutes max wait for VSP API
VSP_API_POLL_INTERVAL = 20      # poll every 20s
VSP_COMPONENT_TIMEOUT = 600     # 10 minutes max for component URL health
URL_POLL_INTERVAL = 30          # URL health poll interval
URL_MAX_ATTEMPTS = 20           # max URL attempts per endpoint


#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def _poll_vsp_api_health(lsf, vip: str, password: str, timeout: int) -> bool:
    """
    Poll the VSP management API at port 5480 until authentication succeeds.

    A successful authentication (HTTP 200) means the K8s cluster is healthy
    and the power-off-marker auto-recovery workflow is running.

    :param lsf: lsfunctions module
    :param vip: VSP cluster management VIP address
    :param password: vmware-system-user password (from creds.txt)
    :param timeout: max seconds to wait
    :return: True if API healthy, False if timeout
    """
    try:
        import urllib.request
        import json as _json
    except ImportError:
        lsf.write_output('urllib not available for VSP API check')
        return False

    url = f'https://{vip}:{VSP_API_PORT}/api/v1/auth/login'
    payload = _json.dumps({
        'username': 'vmware-system-user',
        'password': password
    }).encode('utf-8')

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    start = time.time()
    attempt = 0
    while (time.time() - start) < timeout:
        attempt += 1
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
                if resp.status == 200:
                    elapsed = int(time.time() - start)
                    lsf.write_output(
                        f'  VSP API {vip}:{VSP_API_PORT} healthy '
                        f'(attempt {attempt}, elapsed {elapsed}s)')
                    return True
                else:
                    lsf.write_output(
                        f'  VSP API {vip}: HTTP {resp.status} (attempt {attempt})')
        except Exception as e:
            err_str = str(e)[:80]
            lsf.write_output(
                f'  VSP API {vip}: not yet ready ({err_str}) — attempt {attempt}')

        elapsed = int(time.time() - start)
        remaining = timeout - elapsed
        if remaining > VSP_API_POLL_INTERVAL:
            time.sleep(VSP_API_POLL_INTERVAL)
        else:
            break

    lsf.write_output(f'  TIMEOUT: VSP API {vip} did not become healthy within {timeout}s')
    return False


def _check_url_health(lsf, url: str, expected_text: str = None,
                      max_attempts: int = URL_MAX_ATTEMPTS,
                      poll_interval: int = URL_POLL_INTERVAL) -> bool:
    """
    Poll a URL until it returns HTTP 200 or 401 (auth gating = service up).

    :param lsf: lsfunctions module
    :param url: URL to check
    :param expected_text: Optional text to find in response body (200 only)
    :param max_attempts: Maximum number of attempts
    :param poll_interval: Seconds between attempts
    :return: True if service is up, False if all attempts exhausted
    """
    try:
        import urllib.request
        import urllib.error
    except ImportError:
        lsf.write_output(f'urllib not available for URL check: {url}')
        return False

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
                code = resp.status
                if code in (200, 201):
                    if expected_text:
                        body = resp.read(4096).decode('utf-8', errors='ignore')
                        if expected_text in body:
                            lsf.write_output(f'  OK [{code}]: {url}')
                            return True
                        else:
                            lsf.write_output(
                                f'  [{code}] but expected text not found '
                                f'(attempt {attempt}/{max_attempts}): {url}')
                    else:
                        lsf.write_output(f'  OK [{code}]: {url}')
                        return True
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # 401 = auth gating means the service is alive
                lsf.write_output(f'  OK [401-auth]: {url}')
                return True
            lsf.write_output(
                f'  HTTP {e.code} (attempt {attempt}/{max_attempts}): {url}')
        except Exception as e:
            lsf.write_output(
                f'  {str(e)[:80]} (attempt {attempt}/{max_attempts}): {url}')

        if attempt < max_attempts:
            time.sleep(poll_interval)

    lsf.write_output(f'  FAILED after {max_attempts} attempts: {url}')
    return False


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for VVFfinal module.

    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)

    # Skip immediately if no [VVFFINAL] section — safe for VCF labs
    if not lsf.config.has_section('VVFFINAL'):
        lsf.write_output('No VVFFINAL section in config.ini - skipping VVFfinal startup')
        return

    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================

    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')

    # Dashboard setup
    try:
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('vvffinal', 'vsp_vms', TaskStatus.RUNNING)
        dashboard.generate_html()
    except Exception:
        dashboard = None

    lsf.write_vpodprogress('VVFfinal Start', 'GOOD-3')

    # Read password once for all tasks
    password = lsf.get_password()

    #==========================================================================
    # TASK 1: Verify/Start VSP Platform VMs
    # vSphere.py already powers them on, but we re-verify here to confirm
    # all VSP VMs are running before waiting for the API to be healthy.
    #==========================================================================

    lsf.write_output('Task 1: VSP Platform VM verification')

    vvfvspvms = lsf.get_config_list('VVF', 'vvfvspvms')
    vsp_vms_ok = 0
    vsp_vms_failed = 0

    if vvfvspvms:
        # Connect to vCenters if not already connected
        if not lsf.sis:
            vcenters = lsf.get_config_list('RESOURCES', 'vCenters')
            if vcenters and not dry_run:
                lsf.write_output(f'Connecting to {len(vcenters)} vCenter(s) for VSP VM check...')
                lsf.connect_vcenters(vcenters)

        for entry in vvfvspvms:
            parts = entry.split(':')
            vm_pattern = parts[0].strip()
            vc_hint = parts[1].strip() if len(parts) > 1 else None

            if dry_run:
                lsf.write_output(f'Would verify VSP VM pattern: {vm_pattern}')
                vsp_vms_ok += 1
                continue

            lsf.write_output(f'Checking VSP VM pattern: {vm_pattern}')
            try:
                vms = lsf.get_vm_match(vm_pattern)
                if not vms:
                    lsf.write_output(f'  No VMs found for pattern: {vm_pattern}')
                    vsp_vms_failed += 1
                    continue

                for vm in vms:
                    vm_name = vm.config.name if vm.config else vm_pattern
                    if vm.runtime.powerState == 'poweredOn':
                        lsf.write_output(f'  {vm_name}: powered on')
                        vsp_vms_ok += 1
                    else:
                        lsf.write_output(
                            f'  {vm_name}: state={vm.runtime.powerState} — powering on...')
                        try:
                            from pyVim.task import WaitForTask
                            task = vm.PowerOnVM_Task()
                            WaitForTask(task)
                            lsf.write_output(f'  {vm_name}: powered on')
                            vsp_vms_ok += 1
                        except Exception as e:
                            lsf.write_output(f'  {vm_name}: power-on failed: {e}')
                            vsp_vms_failed += 1
            except Exception as e:
                lsf.write_output(f'  Error checking VSP VMs for {vm_pattern}: {e}')
                vsp_vms_failed += 1
    else:
        lsf.write_output('No VSP Platform VMs configured ([VVF] vvfvspvms)')

    if dashboard:
        if vsp_vms_failed > 0:
            dashboard.update_task('vvffinal', 'vsp_vms', TaskStatus.FAILED,
                                  f'{vsp_vms_failed} VM(s) failed',
                                  total=vsp_vms_ok + vsp_vms_failed,
                                  success=vsp_vms_ok, failed=vsp_vms_failed)
        elif vvfvspvms:
            dashboard.update_task('vvffinal', 'vsp_vms', TaskStatus.COMPLETE,
                                  total=vsp_vms_ok, success=vsp_vms_ok, failed=0)
        else:
            dashboard.update_task('vvffinal', 'vsp_vms', TaskStatus.SKIPPED,
                                  'No VSP VMs configured')
        dashboard.update_task('vvffinal', 'vsp_api_health', TaskStatus.RUNNING)
        dashboard.generate_html()

    #==========================================================================
    # TASK 1b: VSP Node Pre-Flight — KB 440862 Deadlock Prevention
    #
    # vcf_services_runtime_shutdown.sh creates a power-off-marker ConfigMap
    # that triggers an auto-recovery workflow on next boot. That workflow waits
    # for all nodes to be schedulable before scaling services up. If any node
    # is stuck in Ready,SchedulingDisabled (stale cordon from the previous
    # graceful shutdown), neither side can proceed — the API at port 5480 never
    # comes up and Task 2 below blocks for its full 15-minute timeout.
    #
    # This task runs BEFORE the API health poll (Task 2) to break any deadlock:
    #   1. Delete stale system-shutdown Argo Workflows (they re-cordon on resume)
    #   2. Log power-off-marker presence (informational)
    #   3. Uncordon any SchedulingDisabled nodes
    #
    # Non-fatal — logged and startup continues on any SSH/kubectl error.
    #==========================================================================
    _pf_vips = lsf.get_config_list('VVFFINAL', 'vspcontrolplaneips')
    if _pf_vips and not dry_run:
        lsf.write_output('Task 1b: VSP node pre-flight check (KB 440862 deadlock prevention)...')
        import shlex as _shlex_pf
        import subprocess as _sp_pf

        def _pf_ssh(ip, cmd):
            """Run cmd on VSP node via sshpass + sudo -S -i bash."""
            result = _sp_pf.run(
                ['sshpass', '-p', password, 'ssh',
                 '-o', 'StrictHostKeyChecking=accept-new',
                 '-o', 'ConnectTimeout=10',
                 f'vmware-system-user@{ip}',
                 f'echo {_shlex_pf.quote(password)} | sudo -S -i bash -c {_shlex_pf.quote(cmd)}'],
                capture_output=True, text=True, timeout=30
            )
            # Filter Photon OS MOTD and sudo prompt noise
            combined = result.stdout + result.stderr
            filtered = [
                ln for ln in combined.splitlines()
                if not any(x in ln for x in [
                    'Welcome to Photon', 'Photon 5.0', '[sudo]',
                    'password for', r'Kernel \r'
                ])
            ]
            return '\n'.join(filtered).strip()

        for _pf_vip in _pf_vips:
            lsf.write_output(f'  Pre-flight: {_pf_vip}')
            try:
                # Step A: delete stale system-shutdown Argo Workflows
                _pf_wf_raw = _pf_ssh(_pf_vip,
                    'kubectl get workflow -n vmsp-platform --no-headers 2>/dev/null')
                _pf_stale = [
                    ln.split()[0] for ln in _pf_wf_raw.splitlines()
                    if 'system-shutdown' in ln and ln.split()
                ]
                if _pf_stale:
                    for _pf_wf in _pf_stale:
                        _pf_ssh(_pf_vip,
                            f'kubectl delete workflow -n vmsp-platform '
                            f'{_pf_wf} --grace-period=0 2>/dev/null')
                    lsf.write_output(
                        f'  Pre-flight: deleted {len(_pf_stale)} stale Argo workflow(s)')
                else:
                    lsf.write_output('  Pre-flight: no stale Argo workflows found')

                # Step B: power-off-marker recovery (KB 440862 steps 2-5)
                # Fetch ConfigMap as JSON, parse vmspcontent (vmsp-platform deployments)
                # and content (tenant services, namespace.kind.name=replicas tuples), scale
                # each resource to its exact saved replica count, then delete the ConfigMap.
                # If a field is empty or the ConfigMap is absent, Task 2b's zero-replica
                # scaling below acts as the automatic fallback.
                import json as _pf_json
                _pf_pom_raw = _pf_ssh(_pf_vip,
                    'kubectl get configmap power-off-marker -n vmsp-platform'
                    ' -o json 2>/dev/null')
                _pf_pom_js = _pf_pom_raw.find('{')
                if _pf_pom_js >= 0:
                    lsf.write_output(
                        f'  Pre-flight: power-off-marker found on {_pf_vip}'
                        f' — applying manual recovery (KB 440862 steps 2-5)...')
                    try:
                        _pf_cm = _pf_json.loads(_pf_pom_raw[_pf_pom_js:])
                        _pf_vmsp_data   = _pf_cm.get('data', {}).get('vmspcontent', '')
                        _pf_tenant_data = _pf_cm.get('data', {}).get('content', '')

                        # Scale vmsp-platform deployments (vmspcontent: name=N,name=N,...)
                        _pf_vmsp_scaled = 0
                        if _pf_vmsp_data:
                            for _pf_e in _pf_vmsp_data.split(','):
                                _pf_n, _, _pf_r = _pf_e.strip().partition('=')
                                if _pf_n.strip() and _pf_r.strip().isdigit():
                                    _pf_ssh(_pf_vip,
                                        f'kubectl scale deploy {_pf_n.strip()}'
                                        f' --replicas={_pf_r.strip()}'
                                        f' -n vmsp-platform 2>/dev/null')
                                    _pf_vmsp_scaled += 1
                            lsf.write_output(
                                f'  Pre-flight: scaled {_pf_vmsp_scaled}'
                                f' vmsp-platform deployment(s) from ConfigMap data')
                        else:
                            lsf.write_output(
                                '  Pre-flight: vmspcontent empty'
                                ' — vmsp-platform uses Task 2b scaling fallback')

                        # Scale tenant services (content: namespace.kind.name=N,...)
                        _pf_tenant_scaled = 0
                        if _pf_tenant_data:
                            for _pf_e in _pf_tenant_data.split(','):
                                _pf_k, _, _pf_r = _pf_e.strip().partition('=')
                                _pf_parts = _pf_k.strip().split('.', 2)
                                if len(_pf_parts) == 3 and _pf_r.strip().isdigit():
                                    _pf_ns, _pf_kind, _pf_name = _pf_parts
                                    _pf_ssh(_pf_vip,
                                        f'kubectl scale {_pf_kind.lower().strip()}'
                                        f' {_pf_name.strip()}'
                                        f' --replicas={_pf_r.strip()}'
                                        f' -n {_pf_ns.strip()} 2>/dev/null')
                                    _pf_tenant_scaled += 1
                            lsf.write_output(
                                f'  Pre-flight: scaled {_pf_tenant_scaled}'
                                f' tenant service(s) from ConfigMap data')
                        else:
                            lsf.write_output(
                                '  Pre-flight: content field empty'
                                ' — tenant services use Task 2b scaling fallback')

                        # Delete ConfigMap — signals that recovery is complete
                        _pf_ssh(_pf_vip,
                            'kubectl delete configmap power-off-marker'
                            ' -n vmsp-platform 2>/dev/null')
                        lsf.write_output(
                            '  Pre-flight: power-off-marker ConfigMap deleted'
                            ' — manual recovery complete')

                    except Exception as _pf_pom_exc:
                        lsf.write_output(
                            f'  WARNING: power-off-marker parse/scale failed'
                            f' (non-fatal): {_pf_pom_exc}')
                        lsf.write_output(
                            '  Pre-flight: falling through to Task 2b generic scaling')
                else:
                    lsf.write_output(
                        f'  Pre-flight: power-off-marker absent on {_pf_vip}'
                        f' (cold boot or already recovered'
                        f' — Task 2b generic scaling is the active path)')

                # Step C: uncordon non-condemned SchedulingDisabled nodes
                # Parse nodes as JSON to exclude ToBeDeletedByClusterAutoscaler-tainted
                # nodes. Falls back to text-based parsing if JSON decode fails.
                _pf_nd_raw = _pf_ssh(_pf_vip, 'kubectl get nodes -o json 2>/dev/null')
                _pf_nd_js = _pf_nd_raw.find('{')
                try:
                    _pf_nodes = (
                        _pf_json.loads(_pf_nd_raw[_pf_nd_js:])
                        if _pf_nd_js >= 0 else {}
                    )
                    _pf_to_uncordon = []
                    _pf_condemned   = []
                    for _pf_item in _pf_nodes.get('items', []):
                        _pf_nm      = _pf_item.get('metadata', {}).get('name', '')
                        _pf_taints  = _pf_item.get('spec', {}).get('taints', [])
                        _pf_unsched = _pf_item.get('spec', {}).get('unschedulable', False)
                        if _pf_unsched:
                            if any(t.get('key') == 'ToBeDeletedByClusterAutoscaler'
                                   for t in _pf_taints):
                                _pf_condemned.append(_pf_nm)
                            else:
                                _pf_to_uncordon.append(_pf_nm)
                    if _pf_condemned:
                        lsf.write_output(
                            f'  Pre-flight: skipping condemned node(s)'
                            f' (ToBeDeletedByClusterAutoscaler):'
                            f' {", ".join(_pf_condemned)}')
                    for _pf_nd in _pf_to_uncordon:
                        _pf_ssh(_pf_vip, f'kubectl uncordon {_pf_nd} 2>/dev/null')
                        lsf.write_output(f'  Pre-flight: uncordoned {_pf_nd}')
                    if not _pf_to_uncordon:
                        lsf.write_output('  Pre-flight: no SchedulingDisabled nodes — OK')
                except Exception as _pf_nd_exc:
                    lsf.write_output(
                        f'  Pre-flight: JSON node parse failed, using text fallback:'
                        f' {_pf_nd_exc}')
                    for _pf_line in _pf_ssh(
                        _pf_vip, 'kubectl get nodes --no-headers 2>/dev/null'
                    ).splitlines():
                        if 'SchedulingDisabled' in _pf_line and _pf_line.split():
                            _pf_fb_nd = _pf_line.split()[0]
                            _pf_ssh(_pf_vip, f'kubectl uncordon {_pf_fb_nd} 2>/dev/null')
                            lsf.write_output(
                                f'  Pre-flight: uncordoned {_pf_fb_nd} (text fallback)')

            except Exception as _pf_exc:
                lsf.write_output(
                    f'  WARNING: pre-flight for {_pf_vip} failed (non-fatal): {_pf_exc}')

    #==========================================================================
    # TASK 2: Wait for VSP Management API Health (port 5480)
    # vcf_services_runtime_shutdown.sh sets a power-off-marker on shutdown.
    # When VSP VMs boot, the K8s cluster comes up and the marker triggers
    # automatic component recovery — no manual scale-up required.
    # We wait for the port-5480 API to respond to confirm the cluster is ready.
    #==========================================================================

    lsf.write_output('Task 2: VSP management API health check (port 5480)')

    vspcontrolplaneips = lsf.get_config_list('VVFFINAL', 'vspcontrolplaneips')
    api_health_ok = 0
    api_health_failed = 0

    if vspcontrolplaneips:
        for vip in vspcontrolplaneips:
            lsf.write_output(f'Waiting for VSP API at {vip}:{VSP_API_PORT}...')

            if dry_run:
                lsf.write_output(f'  Would poll VSP API: {vip}:{VSP_API_PORT}')
                api_health_ok += 1
                continue

            healthy = _poll_vsp_api_health(
                lsf, vip, password, timeout=VSP_API_HEALTH_TIMEOUT)
            if healthy:
                api_health_ok += 1
                lsf.write_output(
                    f'  VSP cluster at {vip} is ready — '
                    f'power-off-marker auto-recovery in progress')
            else:
                api_health_failed += 1
                lsf.write_output(
                    f'  WARNING: VSP API at {vip} did not become healthy '
                    f'(non-fatal, continuing)')
    else:
        lsf.write_output('No VSP control plane IPs configured ([VVFFINAL] vspcontrolplaneips)')

    if dashboard:
        total_sites = len(vspcontrolplaneips) if vspcontrolplaneips else 0
        if not vspcontrolplaneips:
            dashboard.update_task('vvffinal', 'vsp_api_health', TaskStatus.SKIPPED,
                                  'No VSP IPs configured')
        elif api_health_failed > 0:
            dashboard.update_task('vvffinal', 'vsp_api_health', TaskStatus.FAILED,
                                  f'{api_health_failed} site(s) did not respond',
                                  total=total_sites, success=api_health_ok,
                                  failed=api_health_failed)
        else:
            dashboard.update_task('vvffinal', 'vsp_api_health', TaskStatus.COMPLETE,
                                  total=total_sites, success=api_health_ok, failed=0)
        dashboard.update_task('vvffinal', 'k8s_certs', TaskStatus.RUNNING)
        dashboard.generate_html()

    #==========================================================================
    # TASK 2b: Clean Stale Argo Workflows + Restore Scaled-to-0 Deployments
    #
    # Each shutdown cycle creates a system-shutdown-{id} Argo Workflow in
    # vmsp-platform. On next boot, the Argo controller resumes these workflows,
    # re-cordons nodes, and scales prelude/fleet deployments to 0. This task:
    #   1. Deletes all stale system-shutdown Argo Workflows on each VSP cluster
    #   2. Uncordons any SchedulingDisabled nodes
    #   3. Scales zero-replica deployments in vcf-fleet-lcm back to 1
    #
    # Executed via SSH to each VSP control plane node using vmware-system-user
    # and sudo -S -i bash (required: kubectl only on root's login PATH).
    # Non-fatal — failures are logged but startup continues.
    #==========================================================================

    if vspcontrolplaneips and not dry_run:
        lsf.write_output('Task 2b: Clean stale VSP Argo Workflows + restore scaled-to-0 services')
        import shlex as _shlex
        import subprocess as _sp

        def _vsp_ssh(ip, cmd):
            """Run cmd on VSP node via SSH + sudo -S -i bash.
            Uses subprocess list form (no shell=True) with shlex.quote() to
            avoid shell escaping issues with awk/xargs through SSH layers.
            Filters out Photon OS MOTD and sudo prompt noise from output.
            """
            ssh_args = [
                'sshpass', '-p', password, 'ssh',
                '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ConnectTimeout=15',
                f'vmware-system-user@{ip}',
                f'echo {_shlex.quote(password)} | sudo -S -i bash -c {_shlex.quote(cmd)}'
            ]
            result = _sp.run(ssh_args, capture_output=True, text=True, timeout=60)
            combined = result.stdout + result.stderr
            # Filter Photon OS MOTD and sudo password prompt lines
            filtered = [
                l for l in combined.splitlines()
                if not any(x in l for x in [
                    'Welcome to Photon', 'Photon 5.0', '[sudo]',
                    'password for', r'Kernel \r'
                ])
            ]
            return '\n'.join(filtered).strip()

        for vip in vspcontrolplaneips:
            ctrl_ip = vip
            lsf.write_output(f'  Processing VSP cluster at {ctrl_ip}...')
            try:
                # Step 1: List and delete stale system-shutdown Argo Workflows
                wf_raw = _vsp_ssh(ctrl_ip,
                    'kubectl get workflow -n vmsp-platform --no-headers 2>/dev/null')
                workflows = [
                    line.split()[0] for line in wf_raw.splitlines()
                    if 'system-shutdown' in line and line.split()
                ]
                if workflows:
                    for wf in workflows:
                        out = _vsp_ssh(ctrl_ip,
                            f'kubectl delete workflow -n vmsp-platform {wf} --grace-period=0')
                        lsf.write_output(f'    Deleted Argo workflow: {wf}'
                                         + (f' — {out}' if out else ''))
                else:
                    lsf.write_output('    No stale system-shutdown workflows found')

                # Step 2: Uncordon any SchedulingDisabled nodes
                nodes_raw = _vsp_ssh(ctrl_ip,
                    'kubectl get nodes --no-headers 2>/dev/null')
                cordoned = [
                    line.split()[0] for line in nodes_raw.splitlines()
                    if 'SchedulingDisabled' in line and line.split()
                ]
                if cordoned:
                    for node in cordoned:
                        out = _vsp_ssh(ctrl_ip, f'kubectl uncordon {node}')
                        lsf.write_output(f'    Uncordoned: {node}'
                                         + (f' — {out}' if out else ''))
                else:
                    lsf.write_output('    No cordoned nodes found')

                # Step 3: Scale up zero-desired resources in vcf-fleet-lcm
                # Parse kubectl output in Python — no awk/xargs needed
                fleet_raw = _vsp_ssh(ctrl_ip,
                    'kubectl get deployments,statefulsets -n vcf-fleet-lcm'
                    ' --no-headers 2>/dev/null')
                fleet_zero = []
                for line in fleet_raw.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and '/' in parts[1]:
                        ratio = parts[1].split('/', 1)
                        desired = int(ratio[1]) if ratio[1].isdigit() else -1
                        if desired == 0:  # only scale if currently desired=0
                            fleet_zero.append(parts[0])
                if fleet_zero:
                    for res in fleet_zero:
                        replicas = 3 if 'db' in res.lower() else 1
                        out = _vsp_ssh(ctrl_ip,
                            f'kubectl scale -n vcf-fleet-lcm {res} --replicas={replicas}')
                        lsf.write_output(f'    Scaled vcf-fleet-lcm: {res} → {replicas}'
                                         + (f' — {out}' if out else ''))
                else:
                    lsf.write_output('    All vcf-fleet-lcm replicas already non-zero')

                # Step 4: Scale up zero-desired resources in vmsp-platform
                plat_raw = _vsp_ssh(ctrl_ip,
                    'kubectl get deployments,statefulsets -n vmsp-platform'
                    ' --no-headers 2>/dev/null')
                plat_zero = []
                for line in plat_raw.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and '/' in parts[1]:
                        ratio = parts[1].split('/', 1)
                        desired = int(ratio[1]) if ratio[1].isdigit() else -1
                        if desired == 0:
                            plat_zero.append(parts[0])
                if plat_zero:
                    for res in plat_zero:
                        out = _vsp_ssh(ctrl_ip,
                            f'kubectl scale -n vmsp-platform {res} --replicas=1')
                        lsf.write_output(f'    Scaled vmsp-platform: {res}'
                                         + (f' — {out}' if out else ''))
                else:
                    lsf.write_output('    All vmsp-platform replicas already non-zero')

                lsf.write_output(f'  VSP {ctrl_ip} cleanup complete')

            except Exception as e:
                lsf.write_output(
                    f'  WARNING: VSP cleanup failed for {ctrl_ip}: {e} (non-fatal)')
    elif dry_run:
        lsf.write_output(
            'Task 2b: Would clean stale Argo Workflows and restore scaled-to-0 services')

    #==========================================================================
    # TASK 3: K8s Certificate Check/Renewal on VSP Clusters (non-fatal)
    # Runs vsp_cert_renewer.py for each site if available.
    # Failure is non-fatal — lab continues regardless.
    #==========================================================================

    lsf.write_output('Task 3: VSP K8s certificate check/renewal')

    cert_renewer = '/home/holuser/hol/Tools/vsp_cert_renewer.py'
    k8s_cert_ok = 0
    k8s_cert_failed = 0

    if not os.path.isfile(cert_renewer):
        lsf.write_output(f'  vsp_cert_renewer.py not found at {cert_renewer} — skipping')
        if dashboard:
            dashboard.update_task('vvffinal', 'k8s_certs', TaskStatus.SKIPPED,
                                  'vsp_cert_renewer.py not available')
            dashboard.generate_html()
    elif dry_run:
        lsf.write_output(f'  Would run vsp_cert_renewer.py for {len(vspcontrolplaneips)} site(s)')
        if dashboard:
            dashboard.update_task('vvffinal', 'k8s_certs', TaskStatus.SKIPPED, 'Dry run mode')
            dashboard.generate_html()
    else:
        lsf.write_output(f'  Running cert renewer for all VSP clusters (--cluster vsp)...')
        try:
            cmd = [
                sys.executable, cert_renewer,
                '--cluster', 'vsp',
                '--no-timestamps'
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            for line in proc.stdout:
                lsf.write_output(f' {line.rstrip()}')
            proc.wait(timeout=300)
            if proc.returncode == 0:
                k8s_cert_ok += 1
                lsf.write_output(f'  Cert check complete for all VSP clusters')
            else:
                k8s_cert_failed += 1
                lsf.write_output(
                    f'  Cert check returned code {proc.returncode} (non-fatal)')
        except subprocess.TimeoutExpired:
            proc.kill()
            k8s_cert_failed += 1
            lsf.write_output(f'  Cert renewer timed out (non-fatal)')
        except Exception as e:
            k8s_cert_failed += 1
            lsf.write_output(f'  Cert renewer error: {e} (non-fatal)')

        if dashboard:
            total = len(vspcontrolplaneips) if vspcontrolplaneips else 0
            if k8s_cert_failed > 0 and k8s_cert_ok == 0:
                dashboard.update_task('vvffinal', 'k8s_certs', TaskStatus.FAILED,
                                      f'All sites failed (non-fatal)',
                                      total=total, success=k8s_cert_ok,
                                      failed=k8s_cert_failed)
            elif k8s_cert_failed > 0:
                dashboard.update_task('vvffinal', 'k8s_certs', TaskStatus.COMPLETE,
                                      f'{k8s_cert_failed} site(s) had issues (non-fatal)',
                                      total=total, success=k8s_cert_ok,
                                      failed=k8s_cert_failed)
            else:
                dashboard.update_task('vvffinal', 'k8s_certs', TaskStatus.COMPLETE,
                                      total=total, success=k8s_cert_ok, failed=0)
            dashboard.generate_html()

    #==========================================================================
    # TASK 4: Verify VCF Component URLs (Fleet LCM endpoints)
    # Polls fleet-01a and fleet-01b until HTTP 200/401 (service up).
    # These are the Fleet endpoints that serve VCF Lifecycle Management.
    #==========================================================================

    lsf.write_output('Task 4: VCF component URL verification')

    if dashboard:
        dashboard.update_task('vvffinal', 'vcf_component_urls', TaskStatus.RUNNING)
        dashboard.generate_html()

    vcfcomponenturls = lsf.get_config_list('VVFFINAL', 'vcfcomponenturls')
    url_ok = 0
    url_failed = 0

    if vcfcomponenturls:
        lsf.write_output(f'Checking {len(vcfcomponenturls)} VCF component URL(s)...')

        for entry in vcfcomponenturls:
            parts = entry.split(',', 1)
            url = parts[0].strip()
            expected = parts[1].strip() if len(parts) > 1 else None

            if dry_run:
                lsf.write_output(f'  Would check: {url}')
                url_ok += 1
                continue

            ok = _check_url_health(lsf, url, expected_text=expected)
            if ok:
                url_ok += 1
            else:
                url_failed += 1
    else:
        lsf.write_output('No VCF component URLs configured ([VVFFINAL] vcfcomponenturls)')

    if dashboard:
        total = len(vcfcomponenturls) if vcfcomponenturls else 0
        if not vcfcomponenturls:
            dashboard.update_task('vvffinal', 'vcf_component_urls', TaskStatus.SKIPPED,
                                  'No URLs configured')
        elif url_failed > 0:
            dashboard.update_task('vvffinal', 'vcf_component_urls', TaskStatus.FAILED,
                                  f'{url_failed} URL(s) unreachable',
                                  total=total, success=url_ok, failed=url_failed)
        else:
            dashboard.update_task('vvffinal', 'vcf_component_urls', TaskStatus.COMPLETE,
                                  total=total, success=url_ok, failed=0)
        dashboard.generate_html()

    ##=========================================================================
    ## End Core Team code
    ##=========================================================================

    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================

    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================

    lsf.write_vpodprogress('VVFfinal Finished', 'GOOD-3')
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
