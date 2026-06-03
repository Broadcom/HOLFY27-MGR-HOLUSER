#!/usr/bin/env python3
# VVFshutdown.py - HOLFY27 Core VVF Shutdown Module
# Version 1.2 - 2026-06-01
#
# CHANGELOG:
# v1.2 - 2026-06-01 (hung shutdown fix):
#   - Phase 1b (NEW): After VSP graceful K8s drain, SSH to each ESXi host and
#     force-power-off any remaining vsp-* VMs. vcf_services_runtime_shutdown.sh
#     fails to power off VMs when govc cannot locate them via vCenter API (MoRef
#     missing). Without Phase 1b, those VMs block all hosts from entering
#     maintenance mode, causing Phase 8 to wait forever (observed in production).
#   - shutdown_host() fix: Use --vsanmode noAction to skip vSAN data evacuation
#     (all VMs are off by Phase 8, no migration needed). Without this flag, ESXi
#     tries to evacuate vSAN objects and fails with "General vSAN error", leaving
#     the host out of maintenance mode and rejecting the poweroff command.
#   - shutdown_host() fix: Increase maintenance mode timeout from 60s to 300s.
# v1.1 - 2026-06-01 (full cycle test fixes):
#   - Phase 2: Replace lsf.connect_vcenters() with single-attempt lsf.connect_vc()
#     per host. connect_vcenters() retries 20×30s per host, causing 10-minute hangs
#     when a vCenter is unreachable (e.g., already shut down by VSP shutdown script).
#   - Phase 4: Same fix — ESXi direct connections now use single-attempt connect_vc().
#   - Phase 7: Same fix for re-auth blocks in Phase 7 and Phase 8.
#   - Phase 7: vSAN ESA auto-detection works correctly — ESA environments skip
#     elevator wait entirely (confirmed in test: esx-01a detected ESA).
#   - Observed: vcf_services_runtime_shutdown.sh fails with "Internal error" on
#     site-a consistently (~5min), succeeds or times out on site-b (~10min).
#     Both are treated as WARNING and shutdown continues to Phases 2-8.
#     Root cause is a VSP cluster health issue unrelated to the shutdown script.
# v1.0 - 2026-06-01: Initial release.
# Author - Burke Azbill and HOL Core Team
# VMware Validated Foundation graceful shutdown sequence
#
# VVF is a leaner deployment than VCF — no SDDC Manager, no NSX, no Tanzu,
# no VCF Automation. The shutdown sequence is correspondingly simpler.
#
# CRITICAL: License server power ordering constraint (applies to all VVF/VCF):
#   - License VMs must be running BEFORE vCenter
#   - License VMs must be powered off ONLY AFTER vCenter is fully shut down
#   - All vCenter and license VM power operations use direct ESXi connections
#     (established in Phase 4 before vCenter goes down in Phase 5)
#
# VSP cluster shutdown uses the Broadcom vcf_services_runtime_shutdown.sh source: https://knowledge.broadcom.com/external/article/440874/how-to-safely-shutdown-all-nodes-within.html
# script (Phase 1) which gracefully drains all K8s workloads via the port-5480
# management API and then powers off VSP VMs via govc. The script also sets
# a power-off-marker that triggers automatic component recovery on next boot.
#
# Shutdown order:
#   Phase 1:  vcf_services_runtime_shutdown.sh per site (drain + VSP VM poweroff)
#   Phase 2:  Connect to vCenters (still running after Phase 1)
#   Phase 3:  Shutdown ops VMs (ops-a, ops-b) via vCenter
#   Phase 4:  Establish ESXi direct connections (BEFORE vCenter shutdown)
#   Phase 5:  Shutdown vCenter VMs via ESXi direct (NOT vCenter API)
#   Phase 6:  Shutdown license VMs via ESXi direct (ONLY AFTER vCenter off)
#   Phase 7:  vSAN elevator operations (OSA only; ESA auto-detected and skipped)
#   Phase 8:  Shutdown ESXi hosts

"""
VVF Shutdown Module

8-phase graceful shutdown for VMware Validated Foundation 9.1 dual-site labs.

Configuration sections read:
  [VVF]      vvfmgmtcluster, vvfvCenter, vvfpostedgevms, vvfvspvms
  [VVFFINAL] vspcontrolplaneips
  [RESOURCES] vCenters
  [SHUTDOWN]  esx_username, vsan_enabled, vsan_timeout, shutdown_hosts
"""

import os
import sys
import argparse
import logging
import time
import datetime
import subprocess

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')
sys.path.insert(0, '/home/holuser/hol/Shutdown')

logging.basicConfig(
    level=logging.WARNING,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'VVFshutdown'
MODULE_DESCRIPTION = 'VVF Graceful Shutdown'
MODULE_VERSION = '1.1'

SHUTDOWN_LOG = '/home/holuser/hol/shutdown.log'
STATUS_FILE = '/lmchol/hol/startup_status.txt'

VSP_SHUTDOWN_SCRIPT = '/home/holuser/hol/Tools/vcf_services_runtime_shutdown.sh'

VSAN_ELEVATOR_TIMEOUT = 2700     # 45 minutes default
VSAN_ELEVATOR_POLL_INTERVAL = 30


#==============================================================================
# LOGGING HELPERS
#==============================================================================

def vvf_write(lsf, msg: str):
    """
    Write output to console and shutdown.log only (not labstartup.log).
    """
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted = f'[{timestamp}] {msg}'
    print(formatted)
    try:
        with open(SHUTDOWN_LOG, 'a') as f:
            f.write(formatted + '\n')
    except Exception:
        pass


def update_shutdown_status(phase_num, phase_name: str, dry_run: bool = False):
    """Update the status file displayed on the console desktop."""
    if dry_run:
        return
    try:
        status_dir = os.path.dirname(STATUS_FILE)
        if status_dir and not os.path.exists(status_dir):
            os.makedirs(status_dir, exist_ok=True)
        with open(STATUS_FILE, 'w') as f:
            f.write(f'VVF Shutdown Phase {phase_num}: {phase_name}')
    except Exception:
        pass


#==============================================================================
# vSAN HELPER FUNCTIONS (imported from VCFshutdown pattern)
#==============================================================================

def check_vsan_esa(lsf, host: str, username: str, password: str) -> bool:
    """
    Detect vSAN Express Storage Architecture (ESA) on the given host.
    ESA does not use plog — the elevator wait is not needed.
    Uses PyVmomi API first; falls back to SSH esxcli.

    :return: True if ESA, False if OSA or unknown
    """
    try:
        # PyVmomi API check — uses existing ESXi direct connection
        host_obj = lsf.get_host(host)
        if host_obj and hasattr(host_obj, 'config'):
            if hasattr(host_obj.config, 'vsanHostConfig'):
                vc = host_obj.config.vsanHostConfig
                if hasattr(vc, 'vsanEsaEnabled') and vc.vsanEsaEnabled:
                    return True
    except Exception:
        pass

    try:
        result = lsf.ssh('esxcli vsan cluster get 2>&1',
                         f'{username}@{host}', password)
        if result and 'ESA' in result:
            return True
    except Exception:
        pass

    return False


def set_vsan_elevator(lsf, host: str, username: str, password: str, enable: bool):
    """Enable or disable the vSAN elevator plog flush on a host via SSH."""
    value = '1' if enable else '0'
    cmd = (f'vsish -e set /storage/lsom/plogRunElevator {value}; '
           f'vsish -e get /storage/lsom/plogRunElevator')
    try:
        result = lsf.ssh(cmd, f'{username}@{host}', password)
        vvf_write(lsf, f'  {host}: elevator={value} → {str(result).strip()[:60]}')
    except Exception as e:
        vvf_write(lsf, f'  WARNING: Could not set elevator on {host}: {e}')


def wait_for_elevator_completion(lsf, hosts: list, username: str, password: str,
                                  max_wait: int, poll_interval: int):
    """Poll elevatorRunning on all hosts until all report 0 or timeout."""
    start = time.time()
    remaining = set(hosts)
    last_hb = time.time()

    while (time.time() - start) < max_wait and remaining:
        now = time.time()
        if (now - last_hb) >= 90:
            elapsed = int(now - start)
            vvf_write(lsf, f'STILL_RUNNING: vSAN elevator — {len(remaining)} host(s) '
                           f'still flushing, {max_wait - elapsed}s remaining')
            last_hb = now

        newly_done = set()
        for host in list(remaining):
            try:
                result = lsf.ssh('vsish -e get /storage/lsom/elevatorRunning',
                                  f'{username}@{host}', password)
                val = str(result).strip()
                if val == '0':
                    vvf_write(lsf, f'  {host}: elevator complete (elapsed {int(time.time()-start)}s)')
                    newly_done.add(host)
                else:
                    vvf_write(lsf, f'  {host}: still running ({val})')
            except Exception as e:
                vvf_write(lsf, f'  {host}: poll error ({e}) — will retry')

        remaining -= newly_done
        if remaining:
            time.sleep(poll_interval)

    if remaining:
        vvf_write(lsf, f'WARNING: elevator timeout after {max_wait}s — '
                       f'{len(remaining)} host(s) may not have completed flush')
    else:
        vvf_write(lsf, 'vSAN elevator flush complete on all hosts')


def shutdown_host(lsf, host: str, username: str, password: str):
    """Enter maintenance mode (vSAN noAction) then send poweroff to an ESXi host via SSH.

    --vsanmode noAction: skip vSAN data evacuation entirely. By Phase 8 all VMs
    are powered off so no data migration is needed. Without this flag ESXi tries
    to evacuate vSAN objects across remaining hosts, fails with "General vSAN
    error", and rejects the subsequent poweroff command.
    --timeout 300: allow 5 minutes for maintenance mode entry (60s was too short
    when cluster health checks are slow).
    """
    import subprocess as _sp
    import shlex as _shlex

    def _ssh_cmd(cmd_str):
        """Run a command on the ESXi host; return (returncode, combined_output)."""
        args = [
            'sshpass', '-p', password, 'ssh',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'ConnectTimeout=15',
            f'{username}@{host}',
            cmd_str,
        ]
        r = _sp.run(args, capture_output=True, text=True, timeout=320)
        return r.returncode, (r.stdout + r.stderr).strip()

    try:
        rc, out = _ssh_cmd(
            'esxcli system maintenanceMode set --enable true '
            '--vsanmode noAction --timeout 300')
        if rc != 0:
            vvf_write(lsf, f'  WARNING: {host}: maintenance mode non-zero rc={rc}: {out}')
    except Exception as e:
        vvf_write(lsf, f'  WARNING: {host}: maintenance mode SSH failed: {e}')

    try:
        rc, out = _ssh_cmd('esxcli system shutdown poweroff --reason "VVF Lab Shutdown"')
        if rc == 0:
            vvf_write(lsf, f'  {host}: shutdown command sent')
        else:
            vvf_write(lsf, f'  WARNING: {host}: poweroff rc={rc}: {out}')
    except Exception as e:
        vvf_write(lsf, f'  WARNING: Could not shutdown {host}: {e}')


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False, phase=None):
    """
    Main entry point for VVFshutdown module.

    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Preview mode — show what would be done
    :param phase: Run only this specific phase number (str, e.g. '1', '5')
    :return: dict with 'success' bool and 'esx_hosts' list
    """
    from pyVim import connect

    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)

    def should_run(phase_id: str) -> bool:
        """Return True when this phase should execute."""
        if phase is None:
            return True
        return str(phase).strip() == str(phase_id).strip()

    # ── Read configuration ──────────────────────────────────────────────────

    # ESXi hosts (both sites)
    esx_hosts = []
    vvfmgmtcluster = lsf.get_config_list('VVF', 'vvfmgmtcluster')
    for entry in vvfmgmtcluster:
        host = entry.split(':')[0].strip()
        if host:
            esx_hosts.append(host)

    # ESXi SSH username
    esx_username = 'root'
    if lsf.config.has_option('SHUTDOWN', 'esx_username'):
        esx_username = lsf.config.get('SHUTDOWN', 'esx_username').strip()

    # Lab password (from creds.txt)
    password = lsf.get_password()

    # vSAN settings
    vsan_enabled = True
    if lsf.config.has_option('SHUTDOWN', 'vsan_enabled'):
        vsan_enabled = lsf.config.getboolean('SHUTDOWN', 'vsan_enabled')
    vsan_timeout = VSAN_ELEVATOR_TIMEOUT
    if lsf.config.has_option('SHUTDOWN', 'vsan_timeout'):
        raw = lsf.config.get('SHUTDOWN', 'vsan_timeout').split('#')[0].strip()
        try:
            vsan_timeout = int(raw)
        except ValueError:
            pass

    # Host shutdown toggle
    shutdown_hosts = True
    if lsf.config.has_option('SHUTDOWN', 'shutdown_hosts'):
        shutdown_hosts = lsf.config.getboolean('SHUTDOWN', 'shutdown_hosts')

    # VSP control plane VIPs (one per site)
    vspcontrolplaneips = lsf.get_config_list('VVFFINAL', 'vspcontrolplaneips')

    vvf_write(lsf, '='*70)
    vvf_write(lsf, f'{MODULE_DESCRIPTION} v{MODULE_VERSION}')
    vvf_write(lsf, f'ESXi hosts: {len(esx_hosts)}, VSP sites: {len(vspcontrolplaneips)}')
    vvf_write(lsf, f'Dry run: {dry_run}')
    if phase is not None:
        vvf_write(lsf, f'Running single phase: {phase}')
    vvf_write(lsf, '='*70)

    #==========================================================================
    # PHASE 1: VSP Cluster Graceful Shutdown via vcf_services_runtime_shutdown.sh
    # Broadcom-provided script called once per site VIP. It:
    #   1. Authenticates to the port-5480 management API
    #   2. Gracefully drains all tenant workloads and platform controllers
    #   3. Sets a power-off-marker (auto-recovery on next boot)
    #   4. Powers off VSP VMs via govc (VCENTER_USERNAME/PASSWORD env vars)
    # No --skip-poweroff: govc is installed and handles VSP VM power-off.
    # No --skip-snapshot-check: enforced by component shutdown prechecks.
    #==========================================================================

    if should_run('1'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 1: VSP Cluster Graceful Shutdown')
        vvf_write(lsf, '='*60)
        update_shutdown_status(1, 'VSP Cluster Shutdown', dry_run)

        if not os.path.isfile(VSP_SHUTDOWN_SCRIPT):
            vvf_write(lsf, f'WARNING: VSP shutdown script not found: {VSP_SHUTDOWN_SCRIPT}')
            vvf_write(lsf, 'Skipping Phase 1 — VSP VMs will need manual attention')
        elif not vspcontrolplaneips:
            vvf_write(lsf, 'No VSP control plane IPs configured ([VVFFINAL] vspcontrolplaneips)')
            vvf_write(lsf, 'Skipping Phase 1')
        else:
            env = os.environ.copy()
            env['VMSP_PASSWORD'] = password
            env['VCENTER_USERNAME'] = 'administrator@vsphere.local'
            env['VCENTER_PASSWORD'] = password

            for vip in vspcontrolplaneips:
                vvf_write(lsf, f'Calling VSP shutdown script for site VIP: {vip}')

                if dry_run:
                    vvf_write(lsf, f'  [DRY-RUN] Would run: {VSP_SHUTDOWN_SCRIPT} '
                                   f'--node-ip {vip} --skip-snapshot-check')
                    continue

                try:
                    cmd = [
                        VSP_SHUTDOWN_SCRIPT,
                        '--node-ip', vip,
                        '--skip-snapshot-check'
                    ]
                    vvf_write(lsf, f'  Running: {" ".join(cmd)}')
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        env=env,
                        text=True,
                        bufsize=1
                    )
                    for line in proc.stdout:
                        vvf_write(lsf, f'  {line.rstrip()}')
                    proc.wait(timeout=1800)  # 30-minute timeout per site

                    if proc.returncode == 0:
                        vvf_write(lsf, f'  VSP shutdown complete for {vip}')
                    else:
                        vvf_write(lsf, f'  WARNING: VSP shutdown script returned code '
                                       f'{proc.returncode} for {vip} — continuing')
                except subprocess.TimeoutExpired:
                    proc.kill()
                    vvf_write(lsf, f'  ERROR: VSP shutdown script timed out for {vip} — continuing')
                except Exception as e:
                    vvf_write(lsf, f'  ERROR: VSP shutdown script failed for {vip}: {e} — continuing')

    #==========================================================================
    # PHASE 1b: Force Power-Off Remaining VSP VMs via ESXi SSH
    #
    # vcf_services_runtime_shutdown.sh gracefully drains all K8s workloads via
    # the 5480 management API, then powers off VSP VMs via govc. When govc
    # cannot locate a VM through vCenter (MoRef missing or vCenter API failure),
    # it logs "has no VM MoRef — skipping" or "Failed to power off VM" and
    # moves on. This leaves VSP VMs running, which blocks maintenance mode entry
    # and causes Phase 8 to wait indefinitely.
    #
    # After the K8s drain it is safe to hard-power-off any remaining vsp-* VMs
    # directly via ESXi SSH. This phase SSHs to every ESXi host and issues
    # vim-cmd vmsvc/power.off for any vsp-* VM that is still powered on.
    # Non-fatal: errors are logged and shutdown continues.
    #==========================================================================

    if should_run('1b'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 1b: Force Power-Off Remaining VSP VMs (ESXi SSH)')
        vvf_write(lsf, '='*60)
        update_shutdown_status('1b', 'Force Power-Off VSP VMs', dry_run)

        if not dry_run and esx_hosts:
            import subprocess as _sp1b

            def _force_off_vsp_on_host(host_fqdn):
                """SSH to host and hard-power-off any running vsp-* VMs.
                Uses two commands: list all VMs, then power-off each vsp-* VM
                that reports 'Powered on'. Returns count of VMs powered off.
                """
                forced = 0
                try:
                    list_args = [
                        'sshpass', '-p', password, 'ssh',
                        '-o', 'StrictHostKeyChecking=accept-new',
                        '-o', 'ConnectTimeout=15',
                        f'root@{host_fqdn}',
                        'vim-cmd vmsvc/getallvms 2>/dev/null',
                    ]
                    r = _sp1b.run(list_args, capture_output=True, text=True, timeout=30)
                    for line in r.stdout.splitlines()[1:]:  # skip header
                        parts = line.split()
                        if len(parts) < 2:
                            continue
                        vmid, vmname = parts[0], parts[1]
                        if not vmname.startswith('vsp-'):
                            continue
                        # Check power state
                        state_args = [
                            'sshpass', '-p', password, 'ssh',
                            '-o', 'StrictHostKeyChecking=accept-new',
                            '-o', 'ConnectTimeout=10',
                            f'root@{host_fqdn}',
                            f'vim-cmd vmsvc/power.getstate {vmid} 2>/dev/null | tail -1',
                        ]
                        sr = _sp1b.run(state_args, capture_output=True, text=True, timeout=15)
                        if 'Powered on' not in sr.stdout:
                            continue
                        # Force power off
                        off_args = [
                            'sshpass', '-p', password, 'ssh',
                            '-o', 'StrictHostKeyChecking=accept-new',
                            '-o', 'ConnectTimeout=10',
                            f'root@{host_fqdn}',
                            f'vim-cmd vmsvc/power.off {vmid}',
                        ]
                        _sp1b.run(off_args, capture_output=True, text=True, timeout=20)
                        vvf_write(lsf, f'  {host_fqdn}: forced off {vmname} (vmid={vmid})')
                        forced += 1
                except Exception as exc:
                    vvf_write(lsf, f'  WARNING: Phase 1b SSH failed for {host_fqdn}: {exc} (non-fatal)')
                return forced

            total_forced = 0
            vvf_write(lsf, f'Checking {len(esx_hosts)} ESXi host(s) for remaining vsp-* VMs...')
            for esx_host in esx_hosts:
                count = _force_off_vsp_on_host(esx_host)
                if count == 0:
                    vvf_write(lsf, f'  {esx_host}: no running vsp-* VMs found')
                total_forced += count

            vvf_write(lsf, f'Phase 1b complete: {total_forced} VSP VM(s) force-powered-off')
        elif dry_run:
            vvf_write(lsf, f'Would check {len(esx_hosts)} host(s) for remaining vsp-* VMs '
                           f'and force-power-off any still running after K8s drain')
        else:
            vvf_write(lsf, 'Phase 1b skipped (no ESXi hosts configured)')

    #==========================================================================
    # PHASE 2: Connect to vCenters (single-attempt per host — shutdown context)
    # vCenter is still running at this point (only VSP VMs were powered off).
    # NOTE: lsf.connect_vcenters() retries 20×30s per host — unsuitable for
    # shutdown where a host may already be down. We use connect_vc() directly
    # with one attempt per host and skip unreachable ones gracefully.
    #==========================================================================

    if should_run('2'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 2: Connect to vCenters (single-attempt, skip unreachable)')
        vvf_write(lsf, '='*60)
        update_shutdown_status(2, 'Connect to vCenters', dry_run)

        vcenters = lsf.get_config_list('RESOURCES', 'vCenters')

        if not dry_run:
            if vcenters and not lsf.sis:
                vvf_write(lsf, f'Attempting connection to {len(vcenters)} vCenter(s):')
                for vc_entry in vcenters:
                    vc_parts = vc_entry.split(':')
                    vc_host = vc_parts[0].strip()
                    vc_user = vc_parts[2].strip() if len(vc_parts) >= 3 else 'administrator@vsphere.local'
                    vvf_write(lsf, f'  Connecting to {vc_host}...')
                    try:
                        result = lsf.connect_vc(vc_host, vc_user, password)
                        if result:
                            vvf_write(lsf, f'  {vc_host}: connected')
                        else:
                            vvf_write(lsf, f'  {vc_host}: connect returned falsy — skipping')
                    except Exception as e:
                        vvf_write(lsf, f'  {vc_host}: connection failed ({e}) — skipping')
                vvf_write(lsf, f'Connected to {len(lsf.sis)} vSphere endpoint(s)')
            elif lsf.sis:
                vvf_write(lsf, f'Already connected to {len(lsf.sis)} vSphere endpoint(s)')
            else:
                vvf_write(lsf, 'No vCenters configured in [RESOURCES] vCenters')
        else:
            vvf_write(lsf, f'Would connect to vCenters (single attempt): {vcenters}')

    #==========================================================================
    # PHASE 3: Shutdown Ops VMs via vCenter
    # VCF Operations VMs (ops-a, ops-b) shut down while vCenter is still up.
    #==========================================================================

    if should_run('3'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 3: Shutdown VCF Operations VMs')
        vvf_write(lsf, '='*60)
        update_shutdown_status(3, 'Shutdown Ops VMs', dry_run)

        # Discover ops VMs from vSphere — match ops-a, ops-b, opslcm-*, etc.
        ops_patterns = ['ops-a', 'ops-b', 'opslcm-a', 'opslcm-b',
                        'opscollector-01a', 'opscollector-01b']

        if not dry_run:
            if lsf.sis:
                ops_shut = 0
                for pattern in ops_patterns:
                    vms = lsf.get_vm_by_name(pattern)
                    if not vms:
                        vms = lsf.get_vm_match(pattern)
                    for vm in vms:
                        if lsf.is_vm_powered_on(vm):
                            vvf_write(lsf, f'  Shutting down: {vm.name}')
                            try:
                                lsf.shutdown_vm_gracefully(vm)
                                ops_shut += 1
                            except Exception as e:
                                vvf_write(lsf, f'  WARNING: Failed to shut down {vm.name}: {e}')
                        else:
                            vvf_write(lsf, f'  {vm.name}: already powered off')

                vvf_write(lsf, f'Ops VM shutdown: {ops_shut} shut down')
                if ops_shut > 0:
                    vvf_write(lsf, 'Waiting 30s for ops VMs to shut down...')
                    time.sleep(30)
            else:
                vvf_write(lsf, 'No vCenter connections — cannot shut down ops VMs')
        else:
            vvf_write(lsf, f'Would shutdown ops VMs: {ops_patterns}')

    #==========================================================================
    # PHASE 4: Establish ESXi Direct Connections (BEFORE vCenter shutdown)
    # vCenter will be shut down in Phase 5. We must connect to ESXi hosts
    # NOW so we have direct access for Phase 5 (vCenter VMs) and Phase 6
    # (license VMs). Direct ESXi connections are used instead of vCenter API
    # because vCenter itself is one of the VMs we need to power off.
    #==========================================================================

    if should_run('4'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 4: Establish ESXi Direct Connections (before vCenter shutdown)')
        vvf_write(lsf, '='*60)
        update_shutdown_status(4, 'ESXi Direct Connect', dry_run)

        if not dry_run:
            # Disconnect existing vCenter sessions gracefully
            vvf_write(lsf, 'Disconnecting vCenter sessions...')
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()

            # Connect directly to ALL ESXi hosts — single attempt each (shutdown context)
            if esx_hosts:
                vvf_write(lsf, f'Connecting directly to {len(esx_hosts)} ESXi host(s) (single-attempt):')
                for host in esx_hosts:
                    vvf_write(lsf, f'  Connecting to {host}...')
                    try:
                        result = lsf.connect_vc(host, 'root', password)
                        if result:
                            vvf_write(lsf, f'  {host}: connected')
                        else:
                            vvf_write(lsf, f'  {host}: connect returned falsy — skipping')
                    except Exception as e:
                        vvf_write(lsf, f'  {host}: connection failed ({e}) — skipping')
                vvf_write(lsf, f'Connected to {len(lsf.sis)} ESXi endpoint(s) directly')
            else:
                vvf_write(lsf, 'No ESXi hosts configured in [VVF] vvfmgmtcluster')
        else:
            vvf_write(lsf, f'Would connect directly to {len(esx_hosts)} ESXi host(s): {esx_hosts}')

    #==========================================================================
    # PHASE 5: Shutdown vCenter VMs via ESXi Direct
    # vCenter is shut down using the ESXi direct connections from Phase 4.
    # We use lsf.shutdown_vm_gracefully() (VMware Tools shutdown) which works
    # when connected directly to ESXi hosts, not vCenter API.
    #==========================================================================

    if should_run('5'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 5: Shutdown vCenter VMs (via ESXi direct)')
        vvf_write(lsf, '='*60)
        update_shutdown_status(5, 'Shutdown vCenter VMs', dry_run)

        vvfvCenter = lsf.get_config_list('VVF', 'vvfvCenter')

        if not dry_run:
            if vvfvCenter and lsf.sis:
                vc_shut = 0
                for entry in vvfvCenter:
                    vc_name = entry.split(':')[0].strip()
                    vvf_write(lsf, f'  Searching for vCenter VM: {vc_name}')
                    vms = lsf.get_vm_by_name(vc_name)
                    if not vms:
                        # Prefix match for VMs with random suffixes
                        import re as _re
                        vms = [v for v in lsf.get_vm_match(f'^{_re.escape(vc_name)}(-|$)')
                               if v is not None]
                    for vm in vms:
                        if lsf.is_vm_powered_on(vm):
                            vvf_write(lsf, f'  Shutting down vCenter: {vm.name}')
                            try:
                                lsf.shutdown_vm_gracefully(vm)
                                vc_shut += 1
                                time.sleep(10)  # Longer pause for vCenter
                            except Exception as e:
                                vvf_write(lsf, f'  WARNING: Failed to shutdown {vm.name}: {e}')
                        else:
                            vvf_write(lsf, f'  {vm.name}: already powered off')

                if vc_shut > 0:
                    vvf_write(lsf, f'Waiting 60s for {vc_shut} vCenter(s) to shut down...')
                    time.sleep(60)
                vvf_write(lsf, f'vCenter shutdown: {vc_shut} shut down')
            else:
                vvf_write(lsf, 'No vCenter VMs configured or no ESXi connections available')
        else:
            vvf_write(lsf, f'Would shutdown vCenter VMs via ESXi direct: {vvfvCenter}')

    #==========================================================================
    # PHASE 6: Shutdown License VMs via ESXi Direct
    # CRITICAL: License VMs are powered off ONLY AFTER vCenter is shut down.
    # Uses direct ESXi connections from Phase 4 (vCenter no longer available).
    #==========================================================================

    if should_run('6'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 6: Shutdown License VMs (via ESXi direct, AFTER vCenter off)')
        vvf_write(lsf, '='*60)
        update_shutdown_status(6, 'Shutdown License VMs', dry_run)

        vvfpostedgevms = lsf.get_config_list('VVF', 'vvfpostedgevms')

        if not dry_run:
            if vvfpostedgevms and lsf.sis:
                import concurrent.futures as _cf

                vms_to_shutdown = []
                already_off = 0
                seen: set = set()

                for entry in vvfpostedgevms:
                    vm_name = entry.split(':')[0].strip()
                    vvf_write(lsf, f'  Searching for license VM: {vm_name}')
                    vms = lsf.get_vm_by_name(vm_name)
                    if not vms:
                        vms = lsf.get_vm_match(vm_name)
                    for vm in vms:
                        if vm.name in seen:
                            continue
                        seen.add(vm.name)
                        if lsf.is_vm_powered_on(vm):
                            vms_to_shutdown.append(vm)
                        else:
                            vvf_write(lsf, f'  {vm.name}: already powered off')
                            already_off += 1

                if vms_to_shutdown:
                    vvf_write(lsf, f'Shutting down {len(vms_to_shutdown)} license VM(s) in parallel...')

                    def _shutdown_license_vm(vm):
                        return lsf.shutdown_vm_gracefully(vm)

                    shut_count = 0
                    max_w = min(4, len(vms_to_shutdown))
                    with _cf.ThreadPoolExecutor(max_workers=max_w) as executor:
                        futures = {
                            executor.submit(_shutdown_license_vm, vm): vm.name
                            for vm in vms_to_shutdown
                        }
                        for future in _cf.as_completed(futures):
                            try:
                                if future.result():
                                    shut_count += 1
                                    vvf_write(lsf, f'  {futures[future]}: shut down')
                            except Exception as exc:
                                vvf_write(lsf, f'  WARNING: Shutdown error for {futures[future]}: {exc}')

                    vvf_write(lsf, f'License VMs: {shut_count} shut down, {already_off} already off')
                else:
                    vvf_write(lsf, f'License VMs: 0 to shut down, {already_off} already off')
            else:
                vvf_write(lsf, 'No license VMs configured or no ESXi connections available')
        else:
            vvf_write(lsf, f'Would shutdown license VMs via ESXi direct: {vvfpostedgevms}')

    #==========================================================================
    # PHASE 7: vSAN Elevator Operations
    # Required for vSAN OSA (Original Storage Architecture).
    # ESA (Express Storage Architecture) is auto-detected and skipped.
    #==========================================================================

    if should_run('7'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 7: vSAN Elevator Operations')
        vvf_write(lsf, '='*60)
        update_shutdown_status(7, 'vSAN Elevator Operations', dry_run)

        vvf_write(lsf, f'vSAN enabled: {vsan_enabled}, hosts: {len(esx_hosts)}, '
                       f'timeout: {vsan_timeout}s')

        if vsan_enabled and esx_hosts and not dry_run:
            vvf_write(lsf, 'Checking vSAN architecture (OSA vs ESA)...')
            is_esa = check_vsan_esa(lsf, esx_hosts[0], esx_username, password)
            if is_esa:
                vvf_write(lsf, f'vSAN ESA detected on {esx_hosts[0]} — elevator not required')
            else:
                vvf_write(lsf, f'vSAN OSA detected — running elevator on {len(esx_hosts)} host(s)')

                # Re-authenticate to ESXi hosts (sessions may have timed out)
                vvf_write(lsf, 'Re-authenticating to ESXi hosts (single-attempt each)...')
                for si in lsf.sis:
                    try:
                        connect.Disconnect(si)
                    except Exception:
                        pass
                lsf.sis.clear()
                lsf.sisvc.clear()
                for host in esx_hosts:
                    try:
                        lsf.connect_vc(host, 'root', password)
                    except Exception as e:
                        vvf_write(lsf, f'  {host}: re-auth failed ({e}) — skipping')

                for host in esx_hosts:
                    vvf_write(lsf, f'  Enabling elevator on {host}')
                    set_vsan_elevator(lsf, host, esx_username, password, enable=True)

                vvf_write(lsf, f'Polling vSAN elevator completion (timeout {vsan_timeout/60:.0f}min)...')
                wait_for_elevator_completion(
                    lsf, esx_hosts, esx_username, password,
                    max_wait=vsan_timeout,
                    poll_interval=VSAN_ELEVATOR_POLL_INTERVAL,
                )

                for host in esx_hosts:
                    vvf_write(lsf, f'  Disabling elevator on {host}')
                    set_vsan_elevator(lsf, host, esx_username, password, enable=False)

                vvf_write(lsf, 'vSAN elevator operations complete')
        elif dry_run:
            vvf_write(lsf, f'Would run vSAN elevator on: {esx_hosts}')
        elif not vsan_enabled:
            vvf_write(lsf, 'vSAN elevator skipped (vsan_enabled=false in config)')
        else:
            vvf_write(lsf, 'vSAN elevator skipped (no ESXi hosts configured)')

    #==========================================================================
    # PHASE 8: Shutdown ESXi Hosts
    #==========================================================================

    if should_run('8'):
        vvf_write(lsf, '='*60)
        vvf_write(lsf, 'PHASE 8: Shutdown ESXi Hosts')
        vvf_write(lsf, '='*60)
        update_shutdown_status(8, 'Shutdown ESXi Hosts', dry_run)

        vvf_write(lsf, f'Host shutdown: {shutdown_hosts}, hosts: {len(esx_hosts)}')

        if shutdown_hosts and esx_hosts and not dry_run:
            # Re-authenticate (sessions may have expired during elevator) — single-attempt
            vvf_write(lsf, 'Re-authenticating to ESXi hosts for shutdown (single-attempt each)...')
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()
            for host in esx_hosts:
                try:
                    lsf.connect_vc(host, 'root', password)
                except Exception as e:
                    vvf_write(lsf, f'  {host}: re-auth failed ({e}) — skipping')

            vvf_write(lsf, f'Shutting down {len(esx_hosts)} ESXi host(s)...')
            for i, host in enumerate(esx_hosts, 1):
                vvf_write(lsf, f'  [{i}/{len(esx_hosts)}] Initiating shutdown: {host}')
                shutdown_host(lsf, host, esx_username, password)
                time.sleep(5)

            vvf_write(lsf, 'ESXi host shutdown commands sent')
            vvf_write(lsf, '  Note: Hosts may take several minutes to fully power off')

            # Disconnect sessions (hosts are shutting down)
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
            lsf.sis.clear()
            lsf.sisvc.clear()

        elif dry_run:
            vvf_write(lsf, f'Would shutdown ESXi hosts: {esx_hosts}')
        elif not shutdown_hosts:
            vvf_write(lsf, 'Host shutdown skipped (shutdown_hosts=false in config)')
        else:
            vvf_write(lsf, 'Host shutdown skipped (no ESXi hosts configured)')

    ##=========================================================================
    ## End Core Team code
    ##=========================================================================

    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================

    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================

    vvf_write(lsf, f'{MODULE_NAME} completed')
    return {'success': True, 'esx_hosts': esx_hosts}


#==============================================================================
# STANDALONE EXECUTION
#==============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=MODULE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phase IDs for --phase:
  1     VSP Cluster Graceful Shutdown (vcf_services_runtime_shutdown.sh per site)
  1b    Force Power-Off Remaining VSP VMs (ESXi SSH — handles MoRef/govc failures)
  2     Connect to vCenters
  3     Shutdown VCF Operations VMs (ops-a, ops-b)
  4     Establish ESXi Direct Connections (BEFORE vCenter shutdown)
  5     Shutdown vCenter VMs (via ESXi direct)
  6     Shutdown License VMs (via ESXi direct, AFTER vCenter off)
  7     vSAN Elevator Operations (OSA only — ESA auto-detected and skipped)
  8     Shutdown ESXi Hosts

Examples:
  python3 VVFshutdown.py --dry-run          # Preview all phases
  python3 VVFshutdown.py --phase 1          # Run only VSP cluster shutdown
  python3 VVFshutdown.py --phase 7 --dry-run  # Preview vSAN elevator
"""
    )
    parser.add_argument('--standalone', action='store_true',
                        help='Run in standalone test mode')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--skip-init', action='store_true',
                        help='Skip lsf.init() call')
    parser.add_argument('--phase', '-p', type=str, default=None,
                        help='Run only a specific phase (e.g., 1, 5, 7)')

    args = parser.parse_args()

    import lsfunctions as lsf

    if not args.skip_init:
        lsf.init(router=False)

    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'Dry run: {args.dry_run}')
        if args.phase:
            print(f'Phase: {args.phase}')
        print()

    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run, phase=args.phase)
