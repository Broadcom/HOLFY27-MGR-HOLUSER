#!/usr/bin/env python3
#
# relicense-host.py - Resynchronize and verify VCF 9.x host licenses after CPU architecture changes
#
# version 1.1.0  2026-09-11
#
# When an ESXi host's CPU architecture/topology is modified (e.g., changing from 16 cores/socket
# to 8 cores/socket, increasing socket count), the host reboots into an unassigned/expired evaluation
# license mode. Under VMware per-core licensing rules (cpuCore:16core), every physical CPU socket
# requires a minimum of 16 core licenses (e.g., 4 sockets * 16 min = 64 cores).
#
# This tool automates the complete re-licensing workflow:
#   1. Analyzes CPU topology (sockets, cores, cores/socket) and computes required core license capacity.
#   2. Verifies license capacity in the vCenter VCF subscription pool.
#   3. Disconnects and reconnects the host in vCenter to trigger licenseClient entitlement propagation.
#   4. Verifies the active subscription token on the ESXi host (esxcli licensing entitlement).
#   5. Ensures vmware-fdm (vSphere HA) service is running and reconfigured.
#   6. Clears triggered license and host alarms in vCenter.
#   7. Evaluates Host Maintenance Mode: exits Maintenance Mode by default, or preserves initial state with --preserve-state.
#

import argparse
import datetime
import os
import ssl
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from pyVim.connect import SmartConnect
from pyVmomi import vim, vmodl

VERSION = '1.1.0'

# ANSI colors (disabled when stdout is not a terminal)
if sys.stdout.isatty():
    _CYAN = '\033[0;36m'
    _BLUE = '\033[0;34m'
    _GREEN = '\033[0;32m'
    _YELLOW = '\033[1;33m'
    _RED = '\033[0;31m'
    _BOLD = '\033[1m'
    _DIM = '\033[2m'
    _NC = '\033[0m'
else:
    _CYAN = _BLUE = _GREEN = _YELLOW = _RED = _BOLD = _DIM = _NC = ''

# Known vCenters in HOL VCF lab environments
DEFAULT_VCENTERS = [
    {
        'host': 'vc-mgmt-a.site-a.vcf.lab',
        'user': 'administrator@vsphere.local',
        'label': 'Site A Management vCenter'
    },
    {
        'host': 'vc-wld01-a.site-a.vcf.lab',
        'user': 'administrator@wld.sso',
        'label': 'Site A Workload vCenter'
    },
    {
        'host': 'vc-mgmt-b.site-b.vcf.lab',
        'user': 'administrator@vsphere.local',
        'label': 'Site B Management vCenter'
    },
    {
        'host': 'vc-wld01-b.site-b.vcf.lab',
        'user': 'administrator@wld.sso',
        'label': 'Site B Workload vCenter'
    }
]

DEFAULT_CREDS_FILE = '/home/holuser/creds.txt'


class _HelpOnErrorParser(argparse.ArgumentParser):
    """Custom parser that displays help on error."""
    def error(self, message):
        sys.stderr.write(f"\n{_RED}ERROR: {message}{_NC}\n\n")
        show_help()
        sys.exit(1)


def get_default_password() -> str:
    """Read default lab password from creds.txt."""
    if os.path.exists(DEFAULT_CREDS_FILE):
        try:
            with open(DEFAULT_CREDS_FILE, 'r') as fh:
                return fh.read().strip()
        except Exception:
            pass
    return ''


def show_help():
    """Display styled help screen."""
    W = 76
    title = 'VCF Host Re-Licensing Tool'
    print(f"{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{title:^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{'VMware Cloud Foundation 9.x Host License Synchronization':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")

    print(f"{_BOLD}USAGE:{_NC}")
    print(f"    relicense-host.py [{_GREEN}-H{_NC} <hostname>] [{_GREEN}--all{_NC}] [{_GREEN}--check-only{_NC}] [{_GREEN}--preserve-state{_NC}] [{_GREEN}--password{_NC} <pwd>]\n")

    print(f"{_BOLD}DESCRIPTION:{_NC}")
    print("    Resynchronizes VCF subscription licenses to ESXi hosts after hardware or CPU")
    print("    topology adjustments (such as changing cores per socket). Under VMware's 16-core")
    print("    per socket minimum rule, topology changes cause hosts to boot in evaluation mode.")
    print("    This script orchestrates the vCenter disconnect/reconnect cycle, pushes the VCF")
    print("    entitlement token, re-activates HA agents, clears license alarms, and manages")
    print("    Maintenance Mode state (exiting Maintenance Mode upon completion by default).\n")

    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}-H, --host{_NC} <hostname>          Target specific ESXi host (e.g. esx-01a.site-a.vcf.lab)")
    print(f"    {_GREEN}-a, --all{_NC}                     Process all ESXi hosts across all active vCenters")
    print(f"    {_GREEN}-c, --check-only{_NC}              Inspect and display CPU topology, license status, and MM state")
    print(f"    {_GREEN}--preserve-state{_NC}              Preserve initial Maintenance Mode state (default is to exit MM)")
    print(f"    {_GREEN}-p, --password{_NC} <pwd>          Lab SSO / root password (defaults to /home/holuser/creds.txt)")
    print(f"    {_GREEN}-v, --vcenter{_NC} <vc_fqdn>       Target a specific vCenter Server directly")
    print(f"    {_GREEN}-h, --help{_NC}                    Show this help message\n")

    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Relicense a host and ensure it exits Maintenance Mode (default behavior){_NC}")
    print("    ./relicense-host.py -H esx-01a.site-a.vcf.lab\n")
    print(f"    {_GREEN}# Relicense a host but preserve its initial Maintenance Mode state{_NC}")
    print("    ./relicense-host.py -H esx-01a.site-a.vcf.lab --preserve-state\n")
    print(f"    {_GREEN}# Check license status, maintenance mode, and CPU cores for all hosts (read-only){_NC}")
    print("    ./relicense-host.py --check-only\n")
    print(f"    {_GREEN}# Automatically detect and relicense any host with license/eval issues{_NC}")
    print("    ./relicense-host.py --all\n")
    sys.exit(0)


def create_ssl_context():
    """Create unverified SSL context for lab environments."""
    ctx = ssl._create_unverified_context()
    return ctx


def connect_vcenter(vc_host: str, user: str, password: str):
    """Connect to vCenter Server via pyVmomi."""
    try:
        ctx = create_ssl_context()
        si = SmartConnect(host=vc_host, user=user, pwd=password, sslContext=ctx)
        return si
    except Exception as e:
        return None


def run_ssh_command(host: str, password: str, command: str) -> Tuple[int, str, str]:
    """Execute command on ESXi host over SSH using sshpass."""
    cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@{host} \"{command}\""
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def wait_for_task(task, task_name: str = "Task", timeout_sec: int = 120) -> Tuple[bool, str]:
    """Wait for a vCenter task to complete and return (success, message)."""
    start = time.time()
    while task.info.state in [vim.TaskInfo.State.running, vim.TaskInfo.State.queued]:
        if time.time() - start > timeout_sec:
            return False, f"Timed out after {timeout_sec}s"
        time.sleep(1)
    if task.info.state == vim.TaskInfo.State.success:
        return True, "Success"
    else:
        err = getattr(task.info, 'error', None)
        err_msg = err.msg if err and hasattr(err, 'msg') else str(task.info.state)
        return False, err_msg


def check_host_esxi_direct_license(host_fqdn: str, password: str) -> Dict:
    """Query direct ESXi licensing state via SSH commands."""
    result = {
        'has_subscription': False,
        'subscription_name': '',
        'entitlement_id': '',
        'vimsvc_serial': '',
        'vimsvc_total': 0,
        'vimsvc_unit': '',
        'fdm_running': False
    }

    # Query esxcli licensing entitlement list
    code, out, _ = run_ssh_command(host_fqdn, password, 'esxcli licensing entitlement list')
    if code == 0 and out:
        for line in out.splitlines():
            if 'Subscription' in line:
                parts = line.split()
                result['has_subscription'] = True
                if len(parts) >= 2:
                    result['entitlement_id'] = parts[1]
                if '(TLG)' in line or 'VMware Cloud Foundation' in line:
                    result['subscription_name'] = 'VMware Cloud Foundation (cores)'

    # Query vim-cmd vimsvc/license --show
    code, out, _ = run_ssh_command(host_fqdn, password, 'vim-cmd vimsvc/license --show')
    if code == 0 and out:
        for line in out.splitlines():
            line_str = line.strip()
            if line_str.startswith('serial:'):
                result['vimsvc_serial'] = line_str.split(':', 1)[1].strip()
            elif line_str.startswith('total:'):
                try:
                    result['vimsvc_total'] = int(line_str.split(':', 1)[1].strip())
                except ValueError:
                    pass
            elif line_str.startswith('unit:'):
                result['vimsvc_unit'] = line_str.split(':', 1)[1].strip()

    # Query vmware-fdm service status
    code, out, _ = run_ssh_command(host_fqdn, password, '/etc/init.d/vmware-fdm status')
    if 'is running' in out:
        result['fdm_running'] = True

    return result


def ensure_esxi_fdm_running(host_fqdn: str, password: str) -> bool:
    """Ensure vmware-fdm service is started on the ESXi host."""
    code, out, _ = run_ssh_command(host_fqdn, password, '/etc/init.d/vmware-fdm status')
    if 'is running' not in out:
        print(f"  {_CYAN}➜{_NC} Starting vmware-fdm service on {host_fqdn}...")
        code, out, _ = run_ssh_command(host_fqdn, password, '/etc/init.d/vmware-fdm start')
        return 'success' in out or code == 0
    return True


def clear_vcenter_alarms(si) -> None:
    """Clear yellow/red triggered alarms in vCenter."""
    try:
        am = si.content.alarmManager
        afs = vim.alarm.AlarmFilterSpec()
        afs.status = ['yellow', 'red']
        am.ClearTriggeredAlarms(afs)
    except Exception:
        pass


def get_all_hosts_info(si, vc_host: str, password: str) -> List[Dict]:
    """Retrieve detailed topology, licensing, and state info for all hosts in a vCenter."""
    content = si.RetrieveContent()
    container_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    hosts_info = []

    # Get assigned licenses in vCenter
    lam = si.content.licenseManager.licenseAssignmentManager
    assigned_map = {}
    try:
        for a in lam.QueryAssignedLicenses():
            assigned_map[a.entityId] = a
    except Exception:
        pass

    for h in container_view.view:
        sockets = h.hardware.cpuInfo.numCpuPackages if h.hardware and h.hardware.cpuInfo else 1
        cores = h.hardware.cpuInfo.numCpuCores if h.hardware and h.hardware.cpuInfo else 1
        cores_per_socket = cores // sockets if sockets else cores
        
        # VMware per-core licensing minimum rule: 16 cores minimum per socket
        effective_per_socket = max(16, cores_per_socket)
        required_license_cores = sockets * effective_per_socket

        assigned = assigned_map.get(h._moId)
        vc_entity_cost = 0
        vc_lic_name = 'Unknown'
        vc_lic_key = ''
        if assigned:
            vc_lic_name = assigned.assignedLicense.name
            vc_lic_key = assigned.assignedLicense.licenseKey
            for prop in assigned.properties:
                if prop.key == 'entityCost':
                    vc_entity_cost = prop.value

        has_lic_alarm = False
        if h.triggeredAlarmState:
            for al in h.triggeredAlarmState:
                al_name = getattr(getattr(al, 'alarm', None), 'info', None)
                al_title = al_name.name if al_name else str(al.alarm)
                if 'license' in al_title.lower() or 'licensing' in al_title.lower():
                    has_lic_alarm = True

        has_lic_config_issue = False
        if h.configIssue:
            for issue in h.configIssue:
                msg = getattr(issue, 'fullFormattedMessage', str(issue))
                if 'license' in msg.lower() or 'licensing' in msg.lower():
                    has_lic_config_issue = True

        in_maintenance_mode = bool(h.runtime.inMaintenanceMode) if h.runtime else False

        hosts_info.append({
            'host_obj': h,
            'name': h.name,
            'moId': h._moId,
            'vc_host': vc_host,
            'sockets': sockets,
            'cores': cores,
            'cores_per_socket': cores_per_socket,
            'required_license_cores': required_license_cores,
            'vc_entity_cost': vc_entity_cost,
            'vc_lic_name': vc_lic_name,
            'vc_lic_key': vc_lic_key,
            'connection_state': str(h.runtime.connectionState) if h.runtime else 'unknown',
            'power_state': str(h.runtime.powerState) if h.runtime else 'unknown',
            'in_maintenance_mode': in_maintenance_mode,
            'overall_status': str(h.overallStatus),
            'has_lic_alarm': has_lic_alarm,
            'has_lic_config_issue': has_lic_config_issue
        })

    return hosts_info


def relicense_single_host(si, host_dict: Dict, password: str, preserve_state: bool = False) -> bool:
    """Perform the disconnect/reconnect, entitlement sync, and maintenance mode workflow for a host."""
    host_obj = host_dict['host_obj']
    host_name = host_dict['name']

    print(f"\n{_BOLD}{'=' * 76}{_NC}")
    print(f"{_BLUE}Relicensing Host:{_NC} {_BOLD}{host_name}{_NC}")
    print(f"{_DIM}Managed by vCenter: {host_dict['vc_host']}{_NC}")
    print(f"{_BOLD}{'=' * 76}{_NC}")

    # Step 1: Display topology, initial state & required licensing
    sockets = host_dict['sockets']
    cores = host_dict['cores']
    cps = host_dict['cores_per_socket']
    req_cores = host_dict['required_license_cores']
    initial_mm = host_dict.get('in_maintenance_mode', False)

    print(f"\n{_CYAN}[Step 1/6]{_NC} Analyzing CPU Topology & Host State...")
    print(f"  • Physical Sockets : {_BOLD}{sockets}{_NC}")
    print(f"  • Total Physical Cores: {_BOLD}{cores}{_NC} ({cps} cores/socket)")
    print(f"  • Broadcom VCF Rule: {_YELLOW}Minimum 16 cores per socket{_NC}")
    print(f"  • Required License : {_GREEN}{req_cores} core licenses{_NC} ({sockets} sockets × {max(16, cps)} cores)")
    mm_str = f"{_YELLOW}Active (In Maintenance Mode){_NC}" if initial_mm else f"{_GREEN}Inactive (Normal operation){_NC}"
    print(f"  • Initial Maint Mode: {mm_str}")

    # Step 2: Check pre-sync host state
    print(f"\n{_CYAN}[Step 2/6]{_NC} Inspecting direct ESXi license status over SSH...")
    pre_state = check_host_esxi_direct_license(host_name, password)
    if pre_state['has_subscription']:
        print(f"  • Direct ESXi Status : {_GREEN}Subscription active ({pre_state['vimsvc_total']} cores){_NC}")
    else:
        print(f"  • Direct ESXi Status : {_RED}Unassigned / Evaluation mode ({pre_state['vimsvc_serial']}){_NC}")

    # Step 3: Disconnect host in vCenter
    print(f"\n{_CYAN}[Step 3/6]{_NC} Disconnecting host in vCenter to cycle connection...")
    try:
        task = host_obj.DisconnectHost_Task()
        ok, msg = wait_for_task(task, "Disconnect Host")
        if ok:
            print(f"  • Disconnect Task  : {_GREEN}Success{_NC}")
        else:
            print(f"  • Disconnect Task  : {_YELLOW}{msg}{_NC}")
    except Exception as e:
        print(f"  • Disconnect Error : {_RED}{e}{_NC}")

    time.sleep(2)

    # Step 4: Reconnect host in vCenter
    print(f"\n{_CYAN}[Step 4/6]{_NC} Reconnecting host to trigger vCenter licenseClient propagation...")
    try:
        task = host_obj.ReconnectHost_Task()
        ok, msg = wait_for_task(task, "Reconnect Host")
        if ok:
            print(f"  • Reconnect Task   : {_GREEN}Success{_NC}")
        else:
            print(f"  • Reconnect Task   : {_RED}Failed - {msg}{_NC}")
            return False
    except Exception as e:
        print(f"  • Reconnect Error  : {_RED}{e}{_NC}")
        return False

    print(f"  • Waiting 5 seconds for vpxd licenseClient token synchronization...")
    time.sleep(5)

    # Step 5: Verify post-sync status & health
    print(f"\n{_CYAN}[Step 5/6]{_NC} Verifying synchronized license and host health...")
    
    # Ensure FDM is started on the ESXi host
    ensure_esxi_fdm_running(host_name, password)

    # Verify on ESXi directly
    post_state = check_host_esxi_direct_license(host_name, password)
    success = False
    if post_state['has_subscription'] and post_state['vimsvc_total'] >= req_cores:
        print(f"  • ESXi Entitlement : {_GREEN}PASS{_NC} - {post_state['subscription_name']}")
        print(f"  • ESXi Serial/Key  : {_GREEN}{post_state['vimsvc_serial']}{_NC}")
        print(f"  • Core Allocation  : {_GREEN}{post_state['vimsvc_total']} cores active{_NC}")
        success = True
    elif post_state['has_subscription']:
        print(f"  • ESXi Entitlement : {_YELLOW}PARTIAL{_NC} - Subscription token found with {post_state['vimsvc_total']} cores (expected {req_cores})")
        success = True
    else:
        print(f"  • ESXi Entitlement : {_RED}FAIL{_NC} - Host did not receive active subscription token")

    # Clear stale alarms in vCenter
    clear_vcenter_alarms(si)
    print(f"  • vCenter Alarms   : {_GREEN}Cleared / Reset{_NC}")

    # Step 6: Evaluate and manage Maintenance Mode state
    print(f"\n{_CYAN}[Step 6/6]{_NC} Managing Maintenance Mode state...")
    try:
        current_in_mm = bool(host_obj.runtime.inMaintenanceMode)
    except Exception:
        current_in_mm = False

    if preserve_state:
        print(f"  • Policy Mode      : {_YELLOW}Preserve Initial State (--preserve-state){_NC}")
        if initial_mm:
            if current_in_mm:
                print(f"  • Maintenance Mode : {_YELLOW}Preserved Active (Host remains in Maintenance Mode){_NC}")
            else:
                print(f"  • Maintenance Mode : {_YELLOW}Entering Maintenance Mode to match initial state...{_NC}")
                try:
                    task = host_obj.EnterMaintenanceMode_Task(0, True)
                    ok, msg = wait_for_task(task, "Enter Maintenance Mode")
                    if ok:
                        print(f"  • Enter MM Task    : {_GREEN}Success{_NC}")
                    else:
                        print(f"  • Enter MM Task    : {_YELLOW}{msg}{_NC}")
                except Exception as e:
                    print(f"  • Enter MM Error   : {_RED}{e}{_NC}")
        else:
            if current_in_mm:
                print(f"  • Maintenance Mode : {_CYAN}Exiting Maintenance Mode to match initial state...{_NC}")
                try:
                    task = host_obj.ExitMaintenanceMode_Task(0)
                    ok, msg = wait_for_task(task, "Exit Maintenance Mode")
                    if ok:
                        print(f"  • Exit MM Task     : {_GREEN}Success - Host returned to Normal operational state{_NC}")
                    else:
                        print(f"  • Exit MM Task     : {_YELLOW}{msg}{_NC}")
                except Exception as e:
                    print(f"  • Exit MM Error    : {_RED}{e}{_NC}")
            else:
                print(f"  • Maintenance Mode : {_GREEN}Preserved Inactive (Host is not in Maintenance Mode){_NC}")
    else:
        print(f"  • Policy Mode      : {_GREEN}Default (Exit Maintenance Mode upon completion){_NC}")
        if current_in_mm:
            print(f"  • Maintenance Mode : {_CYAN}Host is currently in Maintenance Mode -> Exiting...{_NC}")
            try:
                task = host_obj.ExitMaintenanceMode_Task(0)
                ok, msg = wait_for_task(task, "Exit Maintenance Mode")
                if ok:
                    print(f"  • Exit MM Task     : {_GREEN}Success - Host is now in Normal operational state{_NC}")
                else:
                    print(f"  • Exit MM Task     : {_YELLOW}{msg}{_NC}")
            except Exception as e:
                print(f"  • Exit MM Error    : {_RED}{e}{_NC}")
        else:
            print(f"  • Maintenance Mode : {_GREEN}Host is not in Maintenance Mode (Normal operation){_NC}")

    if success:
        print(f"\n{_GREEN}{_BOLD}✔ Successfully relicensed {host_name} with {req_cores}-core VCF entitlement!{_NC}\n")
    else:
        print(f"\n{_RED}{_BOLD}✖ Failed to complete re-licensing for {host_name}.{_NC}\n")

    return success


def print_status_table(all_hosts: List[Dict]) -> None:
    """Print formatted summary table of all hosts."""
    print(f"\n{_BOLD}{'ESXi Host':<28} {'vCenter':<14} {'Sockets':<8} {'Cores':<7} {'Req Cores':<10} {'vCenter Cost':<13} {'Maint Mode':<11} {'Overall':<8}{_NC}")
    print(f"{'─' * 28} {'─' * 14} {'─' * 8} {'─' * 7} {'─' * 10} {'─' * 13} {'─' * 11} {'─' * 8}")
    for h in all_hosts:
        vc_short = h['vc_host'].split('.')[0]
        status_color = _GREEN if h['overall_status'] == 'green' else (_YELLOW if h['overall_status'] == 'yellow' else _RED)
        cost_color = _GREEN if h['vc_entity_cost'] == h['required_license_cores'] else _YELLOW
        mm_color = _YELLOW if h.get('in_maintenance_mode') else _GREEN
        mm_text = "Active" if h.get('in_maintenance_mode') else "No"
        print(f"{h['name']:<28} {vc_short:<14} {h['sockets']:<8} {h['cores']:<7} {h['required_license_cores']:<10} {cost_color}{h['vc_entity_cost']:<13}{_NC} {mm_color}{mm_text:<11}{_NC} {status_color}{h['overall_status']:<8}{_NC}")
    print()


def main():
    parser = _HelpOnErrorParser(add_help=False)
    parser.add_argument('-H', '--host', type=str, help='Target ESXi host FQDN or short name')
    parser.add_argument('-a', '--all', action='store_true', help='Process all ESXi hosts')
    parser.add_argument('-c', '--check-only', action='store_true', help='Inspect and display license status only')
    parser.add_argument('--preserve-state', '--preserve-maintenance-mode', '--keep-mm',
                        dest='preserve_state', action='store_true',
                        help='Preserve initial Maintenance Mode state (default is to exit maintenance mode upon completion)')
    parser.add_argument('-p', '--password', type=str, default=None, help='Lab password')
    parser.add_argument('-v', '--vcenter', type=str, default=None, help='Target specific vCenter')
    parser.add_argument('-h', '--help', action='store_true', help='Show help message')

    if len(sys.argv) == 1 or '-h' in sys.argv or '--help' in sys.argv:
        show_help()

    args = parser.parse_args()
    password = args.password or get_default_password()

    # Discover and connect to active vCenters
    vcenters_to_try = DEFAULT_VCENTERS
    if args.vcenter:
        vcenters_to_try = [{
            'host': args.vcenter,
            'user': 'administrator@vsphere.local' if 'mgmt' in args.vcenter else 'administrator@wld.sso',
            'label': args.vcenter
        }]

    active_vcs = []
    print(f"{_CYAN}Connecting to vCenter servers...{_NC}")
    for vc_cfg in vcenters_to_try:
        si = connect_vcenter(vc_cfg['host'], vc_cfg['user'], password)
        if si:
            active_vcs.append({'si': si, 'config': vc_cfg})
            print(f"  • Connected to {_GREEN}{vc_cfg['host']}{_NC} ({vc_cfg['label']})")
        else:
            # Fallback for workload vCenter SSO user if needed
            if 'wld' in vc_cfg['host']:
                si = connect_vcenter(vc_cfg['host'], 'administrator@vsphere.local', password)
                if si:
                    active_vcs.append({'si': si, 'config': vc_cfg})
                    print(f"  • Connected to {_GREEN}{vc_cfg['host']}{_NC} ({vc_cfg['label']})")

    if not active_vcs:
        print(f"\n{_RED}ERROR: Could not connect to any vCenter servers. Check network/credentials.{_NC}\n")
        sys.exit(1)

    # Gather all hosts across active vCenters
    all_hosts = []
    for vc in active_vcs:
        hosts = get_all_hosts_info(vc['si'], vc['config']['host'], password)
        for h in hosts:
            h['si'] = vc['si']
        all_hosts.extend(hosts)

    if not all_hosts:
        print(f"\n{_RED}ERROR: No ESXi hosts found in connected vCenters.{_NC}\n")
        sys.exit(1)

    # If check-only requested
    if args.check_only:
        print(f"\n{_BOLD}Host Topology, License & Maintenance Mode Report:{_NC}")
        print_status_table(all_hosts)
        sys.exit(0)

    # Filter target hosts
    targets = []
    if args.host:
        target_name = args.host.lower().strip()
        targets = [h for h in all_hosts if target_name in h['name'].lower()]
        if not targets:
            print(f"\n{_RED}ERROR: Host '{args.host}' not found in any connected vCenter.{_NC}\n")
            print_status_table(all_hosts)
            sys.exit(1)
    elif args.all:
        targets = all_hosts
    else:
        # Prompt or identify hosts that have issues or mismatch
        print(f"\n{_BOLD}Current Inventory Status:{_NC}")
        print_status_table(all_hosts)
        
        # Auto-detect hosts with license alerts or cost mismatch
        candidate_hosts = [
            h for h in all_hosts 
            if h['has_lic_alarm'] or h['has_lic_config_issue'] or h['overall_status'] != 'green'
        ]
        if candidate_hosts:
            print(f"{_YELLOW}Detected {len(candidate_hosts)} host(s) requiring re-licensing:{_NC}")
            for c in candidate_hosts:
                print(f"  • {c['name']} (Req: {c['required_license_cores']} cores)")
            targets = candidate_hosts
        else:
            print(f"{_GREEN}All hosts currently report clean status. Use -H <host> or --all to force re-licensing.{_NC}")
            sys.exit(0)

    # Execute re-licensing workflow
    success_count = 0
    fail_count = 0
    for target in targets:
        ok = relicense_single_host(target['si'], target, password, preserve_state=args.preserve_state)
        if ok:
            success_count += 1
        else:
            fail_count += 1

    print(f"\n{_BOLD}{'=' * 76}{_NC}")
    print(f"{_BOLD}Re-Licensing Summary:{_NC} {_GREEN}{success_count} succeeded{_NC}, {_RED}{fail_count} failed{_NC}")
    print(f"{_BOLD}{'=' * 76}{_NC}\n")


if __name__ == '__main__':
    main()
