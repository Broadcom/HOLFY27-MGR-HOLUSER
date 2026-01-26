# lsfunctions.py - HOLFY27 Core Functions Library
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Enhanced with LabType support, NFS router communication, Ansible/Salt, tdns-mgr integration

import os
import subprocess
import errno
import socket
import requests
import datetime
import time
import fileinput
import glob
import shutil
import sys
import urllib3
import logging
import json
import psutil
import re
from pathlib import Path
from ipaddress import ip_network, ip_address
from pyVim import connect
from pyVmomi import vim
from pyVim.task import WaitForTask
from xml.dom.minidom import parseString
from pypsexec.client import Client
from requests.auth import HTTPBasicAuth
from configparser import ConfigParser

# Default logging level is WARNING (other levels are DEBUG, INFO, ERROR and CRITICAL)
logging.basicConfig(level=logging.WARNING)
# Until the SSL cert issues are resolved...
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#==============================================================================
# STATIC VARIABLES
#==============================================================================

sleep_seconds = 5
labcheck = False

home = '/home/holuser'
holroot = f'{home}/hol'  # Local to the Manager

bad_sku = 'HOL-BADSKU'
lab_sku = bad_sku
configname = 'config.ini'
configini = f'/tmp/{configname}'
creds = f'{home}/creds.txt'
router = 'router.site-a.vcf.lab'
proxy = 'proxy.site-a.vcf.lab'
holorouter_dir = '/tmp/holorouter'  # NFS exported directory for router communication

# Log file name
logfile = 'labstartup.log'

socket.setdefaulttimeout(300)

# Linux Main Console (LMC) - only option in HOLFY27 (WMC removed)
LMC = False
lmcholroot = '/lmchol/hol'  # NFS mount from Linux Main Console
mc = '/lmchol'
mcdesktop = f'{mc}/home/holuser/Desktop'
desktop_config = '/lmchol/home/holuser/desktop-hol/VMware.config'

# Log files - write to both local and LMC locations
# - /home/holuser/hol/labstartup.log (Manager local)
# - /lmchol/hol/labstartup.log (Main Console via NFS)
logfiles = [f'{holroot}/{logfile}', f'{lmcholroot}/{logfile}']
red = '${color red}Lab Status'
green = '${color green}Lab Status'

# Status dashboard
status_dashboard_path = '/lmchol/home/holuser/startup-status.htm'

resource_file_dir = f'{holroot}/Resources'
startup_file_dir = f'{holroot}/Startup'
lab_status = f'{lmcholroot}/startup_status.txt'
versiontxt = ''
max_minutes_before_fail = 60
ready_time_file = f'{lmcholroot}/readyTime.txt'
start_time = datetime.datetime.now()
vc_boot_minutes = datetime.timedelta(seconds=(10 * 60))
vcuser = 'administrator@vsphere.local'
linuxuser = 'root'
vsphereaccount = 'administrator@vsphere.local'
sis = []  # all vCenter session instances
sisvc = {}  # dictionary to hold all vCenter/ESXi session instances indexed by host name
sshpass = '/usr/bin/sshpass'

# VPodRepo path (set during init)
vpod_repo = ''
labtype = 'HOL'

# Config parser
config = ConfigParser()

# Password property
_password = None

# Console output flag (set to False when running via labstartup.sh to avoid double-logging)
console_output = True

#==============================================================================
# INITIALIZATION
#==============================================================================

def init(router=True, **kwargs):
    """
    Initialize the lsfunctions module
    
    :param router: Whether to check router connectivity
    :param kwargs: Additional options
    """
    global LMC, mc, mcdesktop, desktop_config, logfiles
    global config, lab_sku, labtype, vpod_repo, max_minutes_before_fail, _password
    
    # Wait for LMC (Linux Main Console) mount
    while True:
        if os.path.isdir(lmcholroot):
            LMC = True
            mc = '/lmchol'
            mcdesktop = f'{mc}/home/holuser/Desktop'
            desktop_config = '/lmchol/home/holuser/desktop-hol/VMware.config'
            logfiles = [f'{holroot}/{logfile}', f'{lmcholroot}/{logfile}']
            break
        time.sleep(5)
    
    # Ensure holorouter directory exists for NFS
    os.makedirs(holorouter_dir, exist_ok=True)
    
    # Read config.ini
    if os.path.isfile(configini):
        config.read(configini)
        
        if config.has_option('VPOD', 'vPod_SKU'):
            lab_sku = config.get('VPOD', 'vPod_SKU')
        
        if config.has_option('VPOD', 'labtype'):
            labtype = config.get('VPOD', 'labtype')
        
        if config.has_option('VPOD', 'maxminutes'):
            max_minutes_before_fail = config.getint('VPOD', 'maxminutes')
    
    # Calculate vpod_repo path using labtype-aware function
    if lab_sku != bad_sku:
        _, vpod_repo, _ = get_repo_info(lab_sku, labtype)
    
    # Load password
    if os.path.isfile(creds):
        with open(creds, 'r') as f:
            _password = f.read().strip()
    
    # Check router if requested and labtype is HOL
    if router and labtype == 'HOL':
        check_router()
    
    write_output(f'lsfunctions initialized: lab_sku={lab_sku}, labtype={labtype}')

def check_router():
    """Check router connectivity"""
    if test_ping('router'):
        write_output('Router is reachable')
    else:
        write_output('WARNING: Router not reachable')

#==============================================================================
# PASSWORD PROPERTY
#==============================================================================

@property
def pw():
    """Get the password from creds.txt"""
    return get_password()

def get_password():
    """Get the password from creds.txt"""
    global _password
    if _password is None:
        if os.path.isfile(creds):
            with open(creds, 'r') as f:
                _password = f.read().strip()
    return _password

#==============================================================================
# OUTPUT AND LOGGING
#==============================================================================

def write_output(msg, **kwargs):
    """
    Write output to log files and optionally to console
    
    :param msg: Message to write
    :param kwargs: 
        logfile - specific logfile path
        console - override console output setting (True/False)
    
    Output is written to:
    - /home/holuser/hol/labstartup.log (Manager)
    - /lmchol/hol/labstartup.log (Main Console via NFS)
    - Console (if console_output is True or console kwarg is True)
    """
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted_msg = f'[{timestamp}] {msg}'
    
    lfile = kwargs.get('logfile', None)
    print_to_console = kwargs.get('console', console_output)
    
    if lfile:
        try:
            with open(lfile, 'a') as f:
                f.write(formatted_msg + '\n')
        except Exception as e:
            print(f'Error writing to {lfile}: {e}')
    else:
        # Write to all configured log files
        for lf in logfiles:
            try:
                os.makedirs(os.path.dirname(lf), exist_ok=True)
                with open(lf, 'a') as f:
                    f.write(formatted_msg + '\n')
            except Exception:
                pass
    
    # Print to console if enabled
    # When running via labstartup.sh, console output is captured by tee and would
    # cause duplicate lines in log files. Set console_output=False in that case.
    if print_to_console:
        print(formatted_msg)

def write_vpodprogress(message, status, **kwargs):
    """
    Write vPod progress status to status file and update dashboard
    
    :param message: Status message
    :param status: Status code (STARTING, GOOD-1, READY, FAIL, etc.)
    :param kwargs: color - status color (red, green)
    """
    color = kwargs.get('color', 'red')
    
    # Write to status file
    try:
        with open(lab_status, 'w') as f:
            f.write(f'{status}: {message}')
    except Exception as e:
        write_output(f'Error writing status: {e}')
    
    # Update desktop config (conky)
    update_desktop_status(message, color)

def update_desktop_status(message, color='red'):
    """Update the desktop status display (conky)"""
    try:
        if os.path.isfile(desktop_config):
            # Read current content
            with open(desktop_config, 'r') as f:
                lines = f.readlines()
            
            # Get conky_title from config or use lab_sku
            conky_title = lab_sku
            if config.has_option('VPOD', 'conky_title'):
                conky_title = config.get('VPOD', 'conky_title')
            
            # Determine color tag for status line
            color_tag = '${color green}' if color == 'green' else '${color red}'
            
            # Update the lines
            updated_lines = []
            for line in lines:
                # Update the lab title line (contains HOL-#### or similar placeholder)
                # This is the line after "# labstartup sets the labname"
                if line.strip().startswith('${font weight:bold}${color0}${alignc}'):
                    updated_lines.append(f'${{font weight:bold}}${{color0}}${{alignc}}{conky_title}\n')
                # Update the status line at the bottom
                elif 'Lab Status' in line and '${exec cat' in line:
                    updated_lines.append(f'${{font weight:bold}}{color_tag}Lab Status ${{alignr}}${{font weight:bold}}${{exec cat /hol/startup_status.txt}}\n')
                else:
                    updated_lines.append(line)
            
            # Write updated content
            with open(desktop_config, 'w') as f:
                f.writelines(updated_lines)
            
            write_output(f'Updated desktop config: title="{conky_title}", status="{message}"')
    except Exception as e:
        write_output(f'Error updating desktop status: {e}')

#==============================================================================
# COMMAND EXECUTION
#==============================================================================

def run_command(cmd, **kwargs):
    """
    Execute a shell command
    
    :param cmd: Command string or list
    :param kwargs: timeout, shell, capture_output
    :return: subprocess.CompletedProcess
    """
    timeout = kwargs.get('timeout', 300)
    shell = kwargs.get('shell', True)
    capture = kwargs.get('capture_output', True)
    
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=capture,
            text=True,
            timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        write_output(f'Command timed out: {cmd}')
        return subprocess.CompletedProcess(cmd, 1, '', 'Timeout')
    except Exception as e:
        write_output(f'Command failed: {cmd} - {e}')
        return subprocess.CompletedProcess(cmd, 1, '', str(e))

def ssh(command, target, password=None, **kwargs):
    """
    Execute command via SSH
    
    :param command: Command to execute
    :param target: user@host
    :param password: SSH password (uses creds.txt if not provided)
    :return: subprocess.CompletedProcess
    """
    if password is None:
        password = get_password()
    
    ssh_options = kwargs.get('options', 'StrictHostKeyChecking=accept-new')
    
    cmd = f'{sshpass} -p {password} ssh -o {ssh_options} {target} "{command}"'
    return run_command(cmd)

def scp(source, destination, password=None, **kwargs):
    """
    Copy files via SCP
    
    :param source: Source path
    :param destination: Destination (can be user@host:path)
    :param password: SCP password
    :return: subprocess.CompletedProcess
    """
    if password is None:
        password = get_password()
    
    ssh_options = kwargs.get('options', 'StrictHostKeyChecking=accept-new')
    recursive = '-r' if kwargs.get('recursive', False) else ''
    
    cmd = f'{sshpass} -p {password} scp {recursive} -o {ssh_options} {source} {destination}'
    return run_command(cmd)

#==============================================================================
# NETWORK TESTING
#==============================================================================

def test_ping(host, **kwargs):
    """
    Test if a host is reachable via ping
    
    :param host: Hostname or IP
    :return: True if reachable
    """
    count = kwargs.get('count', 1)
    timeout = kwargs.get('timeout', 5)
    
    result = run_command(f'ping -c {count} -W {timeout} {host}')
    return result.returncode == 0

def test_tcp_port(host, port, **kwargs):
    """
    Test if a TCP port is open
    
    :param host: Hostname or IP
    :param port: Port number
    :return: True if port is open
    """
    timeout = kwargs.get('timeout', 5)
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def test_url(url, **kwargs):
    """
    Test if a URL is accessible
    
    :param url: URL to test
    :param kwargs: expected_text, verify_ssl, timeout
    :return: True if accessible
    """
    expected_text = kwargs.get('expected_text', None)
    verify_ssl = kwargs.get('verify_ssl', False)
    timeout = kwargs.get('timeout', 10)
    
    try:
        session = requests.Session()
        session.trust_env = False  # Ignore proxy environment vars
        
        response = session.get(url, verify=verify_ssl, timeout=timeout, proxies=None)
        
        if response.status_code != 200:
            return False
        
        if expected_text and expected_text not in response.text:
            return False
        
        return True
    except Exception:
        return False

#==============================================================================
# VSPHERE OPERATIONS
#==============================================================================

def connect_vc(host, user, password=None, **kwargs):
    """
    Connect to a vCenter or ESXi host
    
    :param host: vCenter/ESXi hostname
    :param user: Username
    :param password: Password
    :return: ServiceInstance or True on success, None/False on failure
    """
    if password is None:
        password = get_password()
    
    port = kwargs.get('port', 443)
    
    try:
        # Try pyVmomi 7.0 method first
        try:
            si = connect.SmartConnectNoSSL(
                host=host,
                user=user,
                pwd=password,
                port=port
            )
        except AttributeError:
            # pyVmomi 8.0+ uses SmartConnect with disableSslCertValidation
            si = connect.SmartConnect(
                host=host,
                user=user,
                pwd=password,
                port=port,
                disableSslCertValidation=True
            )
        
        sisvc[host] = si
        sis.append(si)
        write_output(f'Connected to {host}')
        return si
    except Exception as e:
        write_output(f'Failed to connect to {host}: {e}')
        return None

def disconnect_vcenters():
    """Disconnect all vCenter sessions"""
    for si in sis:
        try:
            connect.Disconnect(si)
        except Exception:
            pass
    sis.clear()
    sisvc.clear()

def get_vm(si, name):
    """
    Get a VM by name
    
    :param si: ServiceInstance
    :param name: VM name
    :return: VM object or None
    """
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    
    for vm in container.view:
        if vm.name == name:
            container.Destroy()
            return vm
    
    container.Destroy()
    return None

def start_vm(vm):
    """Power on a VM"""
    if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
        try:
            task = vm.PowerOnVM_Task()
            WaitForTask(task)
            write_output(f'Powered on VM: {vm.name}')
            return True
        except Exception as e:
            write_output(f'Failed to power on {vm.name}: {e}')
            return False
    return True


#==============================================================================
# VSPHERE HELPER FUNCTIONS (from legacy lsfunctions.py)
#==============================================================================

def get_all_objs(si_content, vimtype):
    """
    Method that populates objects of type vimtype such as
    vim.VirtualMachine, vim.HostSystem, vim.Datacenter, vim.Datastore, vim.ClusterComputeResource
    :param si_content: serviceinstance.content
    :param vimtype: VIM object type name (list)
    :return: dict of {object: name}
    """
    obj = {}
    container = si_content.viewManager.CreateContainerView(si_content.rootFolder, vimtype, True)
    for managed_object_ref in container.view:
        obj.update({managed_object_ref: managed_object_ref.name})
    container.Destroy()
    return obj


def connect_vcenters(entries):
    """
    Connect to multiple vCenters or ESXi hosts from a config list
    :param entries: list of vCenter entries from config.ini in format "hostname:type:user"
    """
    pwd = get_password()
    
    for entry in entries:
        # Skip comments and empty lines
        if not entry or entry.strip().startswith('#'):
            continue
        
        vc = entry.split(':')
        hostname = vc[0].strip()
        vc_type = vc[1].strip() if len(vc) > 1 else 'esx'
        
        if len(vc) >= 3:
            login_user = vc[2].strip()
        else:
            login_user = vcuser
        
        write_output(f'Connecting to {hostname}...')
        test_ping(hostname)
        
        if vc_type == 'esx':
            login_user = 'root'
        
        # Keep trying to connect until successful
        max_attempts = 10
        attempt = 0
        while not connect_vc(hostname, login_user, pwd):
            attempt += 1
            if attempt >= max_attempts:
                write_output(f'Failed to connect to {hostname} after {max_attempts} attempts')
                break
            labstartup_sleep(sleep_seconds)
        
        if vc_type == 'linux':
            write_output('Connected to linux vCenter')


def get_host(name):
    """
    Convenience function to retrieve an ESXi host by name from all session content
    :param name: string the name of the host to retrieve
    :return: vim.HostSystem or None
    """
    for si in sis:
        hosts = get_all_objs(si.content, [vim.HostSystem])
        for host in hosts:
            if host.name == name:
                return host
    return None


def get_all_hosts():
    """
    Convenience function to retrieve all ESX host systems
    :return: list of vim.HostSystem
    """
    all_hosts = []
    for si in sis:
        hosts = get_all_objs(si.content, [vim.HostSystem])
        all_hosts.extend(hosts.keys())
    return all_hosts


def get_datastore(ds_name):
    """
    Convenience function to retrieve a datastore by name
    :param ds_name: string - the name of the datastore to return
    :return: vim.Datastore or None
    """
    for si in sis:
        datastores = get_all_objs(si.content, [vim.Datastore])
        for datastore in datastores:
            if datastore.name == ds_name:
                return datastore
    return None


def get_vm_by_name(name, **kwargs):
    """
    Convenience function to retrieve VMs by name from a specific session or all sessions
    :param name: string the name of the VM to retrieve
    :param vc: optional - the specific vCenter hostname to search
    :return: list of matching VMs
    """
    vc = kwargs.get('vc', '')
    vmlist = []
    
    if vc and vc in sisvc:
        vms = get_all_objs(sisvc[vc].content, [vim.VirtualMachine])
        for vm in vms:
            if vm.name == name:
                vmlist.append(vm)
    else:
        for si in sis:
            vms = get_all_objs(si.content, [vim.VirtualMachine])
            for vm in vms:
                if vm.name == name:
                    vmlist.append(vm)
    return vmlist


def get_vm_match(name):
    """
    Convenience function to retrieve VMs matching a pattern from all session content
    :param name: string the name pattern of the VMs to retrieve (regex)
    :return: list of matching VMs
    """
    pattern = re.compile(name, re.IGNORECASE)
    vmsreturn = []
    for si in sis:
        vms = get_all_objs(si.content, [vim.VirtualMachine])
        for vm in vms:
            match = pattern.match(vm.name)
            if match:
                vmsreturn.append(vm)
    return vmsreturn


def get_vapp(name, **kwargs):
    """
    Convenience function to retrieve the vApp named from all session content
    :param name: string the name of the vApp to retrieve
    :param vc: optional - the specific vCenter hostname to search
    :return: list of matching vApps
    """
    vc = kwargs.get('vc', '')
    valist = []
    
    if vc and vc in sisvc:
        vapps = get_all_objs(sisvc[vc].content, [vim.VirtualApp])
        for vapp in vapps:
            if vapp.name == name:
                valist.append(vapp)
    else:
        for si in sis:
            vapps = get_all_objs(si.content, [vim.VirtualApp])
            for vapp in vapps:
                if vapp.name == name:
                    valist.append(vapp)
    return valist


def start_nested(records):
    """
    Start all the nested vApps or VMs in the records list passed in
    :param records: list of vApps or VMs to start. Format: "vmname:vcenter" or "Pause:seconds"
    """
    if not len(records):
        write_output('no records')
        return

    for record in records:
        # Skip comments and empty lines
        if not record or record.strip().startswith('#'):
            continue
        
        p = record.split(':')
        e_name = p[0].strip()
        vc_name = p[1].strip() if len(p) > 1 else ''
        
        # Handle pause entries
        if 'pause' in e_name.lower():
            if labcheck:
                continue
            write_output(f'Pausing {p[1]} seconds...')
            labstartup_sleep(int(p[1]))
            continue

        # Try to find as vApp first, then as VM
        va = get_vapp(e_name, vc=vc_name)
        vms = get_vm_by_name(e_name, vc=vc_name)
        
        if not vms:
            vms = get_vm_match(e_name)
        
        if not vms and not va:
            write_output(f'Unable to find entity {e_name}')
            continue
        
        # Process vApps
        if va:
            for vapp in va:
                if vapp.summary.vAppState == 'started':
                    write_output(f'{vapp.name} already powered on.')
                    continue
                write_output(f'Attempting to power on vApp {vapp.name}...')
                try:
                    task = vapp.PowerOnVApp_Task()
                    WaitForTask(task)
                    write_output(f'Powered on vApp: {vapp.name}')
                except Exception as e:
                    write_output(f'Failed to power on vApp {vapp.name}: {e}')
        
        # Process VMs
        for vm in vms:
            if vm.runtime.powerState == 'poweredOn':
                write_output(f'{vm.name} already powered on.')
                continue
            
            write_output(f'Attempting to power on VM {vm.name}...')
            
            # Wait for VM to be connected
            max_wait = 60
            waited = 0
            while vm.runtime.connectionState != 'connected' and waited < max_wait:
                write_output(f'VM {vm.name} connection state: {vm.runtime.connectionState}, waiting...')
                labstartup_sleep(5)
                waited += 5
            
            if vm.runtime.connectionState != 'connected':
                write_output(f'VM {vm.name} not connected after {max_wait}s, skipping')
                continue
            
            try:
                task = vm.PowerOnVM_Task()
                WaitForTask(task)
                write_output(f'Powered on VM: {vm.name}')
            except Exception as e:
                write_output(f'Failed to power on {vm.name}: {e}')


def get_cluster(cluster_name):
    """
    Get a cluster object by name from any connected vCenter
    :param cluster_name: Cluster name
    :return: ClusterComputeResource object or None
    """
    for si in sis:
        clusters = get_all_objs(si.content, [vim.ClusterComputeResource])
        for cluster in clusters:
            if cluster.name == cluster_name:
                return cluster
    return None


def wait_for_vcenter(hostname, timeout=600, interval=10):
    """
    Wait for vCenter to be fully available (API responding)
    :param hostname: vCenter hostname
    :param timeout: Maximum time to wait in seconds
    :param interval: Check interval in seconds
    :return: True if vCenter is available, False if timeout
    """
    start = datetime.datetime.now()
    pwd = get_password()
    
    while (datetime.datetime.now() - start).total_seconds() < timeout:
        write_output(f'Waiting for vCenter {hostname}...')
        
        # First check if TCP port is responding
        if test_tcp_port(hostname, 443, timeout=5):
            # Try to connect via API
            try:
                # Try pyVmomi 7.0 method first
                try:
                    si = connect.SmartConnectNoSSL(
                        host=hostname,
                        user='administrator@vsphere.local',
                        pwd=pwd,
                        port=443
                    )
                except AttributeError:
                    # pyVmomi 8.0+ uses SmartConnect with disableSslCertValidation
                    si = connect.SmartConnect(
                        host=hostname,
                        user='administrator@vsphere.local',
                        pwd=pwd,
                        port=443,
                        disableSslCertValidation=True
                    )
                
                if si:
                    # Disconnect this test connection
                    connect.Disconnect(si)
                    write_output(f'vCenter {hostname} is ready')
                    return True
            except Exception as e:
                write_output(f'vCenter {hostname} not ready yet: {e}')
        
        labstartup_sleep(interval)
    
    write_output(f'Timeout waiting for vCenter {hostname}')
    return False


def get_all_vms(si=None):
    """
    Get all VMs from a ServiceInstance (or all connected SIs)
    :param si: Optional specific ServiceInstance
    :return: List of VM objects
    """
    vms = []
    search_list = [si] if si else sis
    
    for service_instance in search_list:
        vm_objs = get_all_objs(service_instance.content, [vim.VirtualMachine])
        vms.extend(vm_objs.keys())
    
    return vms


#==============================================================================
# AUTOMATION FRAMEWORK SUPPORT
#==============================================================================

def run_ansible_playbook(playbook_path, inventory=None, extra_vars=None, **kwargs):
    """
    Execute an Ansible playbook
    
    :param playbook_path: Full path to playbook YAML file
    :param inventory: Optional inventory file path
    :param extra_vars: Optional dictionary of extra variables
    :param kwargs: Additional options (logfile, check_mode, etc.)
    :return: subprocess.CompletedProcess
    """
    lfile = kwargs.get('logfile', logfile)
    check_mode = kwargs.get('check_mode', False)
    
    cmd = ['ansible-playbook', playbook_path]
    
    if inventory:
        cmd.extend(['-i', inventory])
    
    if extra_vars:
        for key, value in extra_vars.items():
            cmd.extend(['-e', f'{key}={value}'])
    
    if check_mode:
        cmd.append('--check')
    
    # Add standard options
    cmd.extend(['--connection', 'local'])
    
    write_output(f'Running Ansible playbook: {playbook_path}')
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env={**os.environ, 'ANSIBLE_HOST_KEY_CHECKING': 'False'}
        )
        
        if result.stdout:
            write_output(f'Ansible stdout: {result.stdout}')
        if result.stderr:
            write_output(f'Ansible stderr: {result.stderr}')
        
        return result
        
    except Exception as e:
        write_output(f'Ansible playbook failed: {e}')
        raise

def run_ansible_from_repo(playbook_name, **kwargs):
    """
    Run an Ansible playbook from the vpodrepo
    
    :param playbook_name: Name of playbook file (will search in ansible/ directory)
    """
    search_paths = [
        f'{vpod_repo}/ansible/{playbook_name}',
        f'{vpod_repo}/Ansible/{playbook_name}',
        f'{vpod_repo}/{playbook_name}'
    ]
    
    for path in search_paths:
        if os.path.isfile(path):
            return run_ansible_playbook(path, **kwargs)
    
    raise FileNotFoundError(f"Ansible playbook {playbook_name} not found in vpodrepo")

def run_salt_state(state_file, target='*', **kwargs):
    """
    Execute a Salt state file
    
    :param state_file: Salt state file (*.sls) path or name
    :param target: Salt target specification
    :param kwargs: Additional options
    :return: subprocess.CompletedProcess
    """
    test_mode = kwargs.get('test_mode', False)
    
    # Determine if state_file is a path or state name
    if state_file.endswith('.sls'):
        cmd = ['salt-call', '--local', 'state.apply', 
               state_file.replace('.sls', '').replace('/', '.')]
    else:
        cmd = ['salt-call', '--local', 'state.apply', state_file]
    
    if test_mode:
        cmd.append('test=True')
    
    cmd.extend(['--out', 'yaml'])
    
    write_output(f'Running Salt state: {state_file}')
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.stdout:
            write_output(f'Salt output: {result.stdout}')
        if result.stderr:
            write_output(f'Salt stderr: {result.stderr}')
        
        return result
        
    except Exception as e:
        write_output(f'Salt state failed: {e}')
        raise

def run_salt_from_repo(state_name, **kwargs):
    """
    Run a Salt state from the vpodrepo
    
    :param state_name: Name of state file (will search in salt/ directory)
    """
    search_paths = [
        f'{vpod_repo}/salt/{state_name}',
        f'{vpod_repo}/{state_name}'
    ]
    
    for path in search_paths:
        if os.path.isfile(path) or os.path.isfile(f'{path}.sls'):
            return run_salt_state(path, **kwargs)
    
    raise FileNotFoundError(f"Salt state {state_name} not found in vpodrepo")

def run_repo_script(script_name, script_type='auto', **kwargs):
    """
    Universal script runner that auto-detects and runs scripts from vpodrepo
    
    :param script_name: Script filename
    :param script_type: 'bash', 'python', 'ansible', 'salt', or 'auto' (detect by extension)
    :param kwargs: Additional options
    """
    # Find the script
    script_path = None
    search_dirs = [
        f'{vpod_repo}',
        f'{vpod_repo}/scripts',
        f'{vpod_repo}/ansible',
        f'{vpod_repo}/salt',
        f'{vpod_repo}/lab-startup'
    ]
    
    for dir in search_dirs:
        candidate = f'{dir}/{script_name}'
        if os.path.isfile(candidate):
            script_path = candidate
            break
    
    if not script_path:
        raise FileNotFoundError(f"Script {script_name} not found in vpodrepo")
    
    # Auto-detect script type
    if script_type == 'auto':
        ext = os.path.splitext(script_path)[1].lower()
        type_map = {
            '.sh': 'bash',
            '.py': 'python',
            '.yml': 'ansible',
            '.yaml': 'ansible',
            '.sls': 'salt'
        }
        script_type = type_map.get(ext, 'bash')
    
    write_output(f'Running {script_type} script: {script_path}')
    
    # Execute based on type
    if script_type == 'bash':
        return run_command(f'/bin/bash {script_path}')
    elif script_type == 'python':
        return run_command(f'/usr/bin/python3 {script_path}')
    elif script_type == 'ansible':
        return run_ansible_playbook(script_path, **kwargs)
    elif script_type == 'salt':
        return run_salt_state(script_path, **kwargs)
    else:
        raise ValueError(f"Unknown script type: {script_type}")

#==============================================================================
# VPODREPO HELPERS
#==============================================================================

def get_repo_info(sku: str, lab_type: str = 'HOL') -> tuple:
    """
    Parse SKU and return repository information based on lab type.
    
    Supports multiple SKU patterns:
    - Standard (HOL, ATE, VXP, EDU): PREFIX-XXYY format (e.g., HOL-2701, ATE-2705)
      Returns year-based directory structure: /vpodrepo/20XX-labs/XXYY
    - Named (Discovery): PREFIX-Name format (e.g., Discovery-Demo)
      Returns name-based directory structure: /vpodrepo/Discovery-labs/Name
    
    :param sku: Lab SKU string (e.g., 'HOL-2701', 'ATE-2705', 'Discovery-Demo')
    :param lab_type: Lab type string (HOL, ATE, VXP, EDU, Discovery)
    :return: Tuple of (year_dir, repo_dir, git_url)
    
    Examples:
        >>> get_repo_info('HOL-2701', 'HOL')
        ('/vpodrepo/2027-labs', '/vpodrepo/2027-labs/2701', 'https://github.com/Broadcom/HOL-2701.git')
        
        >>> get_repo_info('ATE-2705', 'ATE')
        ('/vpodrepo/2027-labs', '/vpodrepo/2027-labs/2705', 'https://github.com/Broadcom/ATE-2705.git')
        
        >>> get_repo_info('Discovery-Demo', 'Discovery')
        ('/vpodrepo/Discovery-labs', '/vpodrepo/Discovery-labs/Demo', 'https://github.com/Broadcom/Discovery-Demo.git')
    """
    if not sku or sku == bad_sku:
        return ('', '', '')
    
    # Split SKU into prefix and suffix
    parts = sku.split('-', 1)
    if len(parts) < 2:
        # Invalid format, return empty
        return ('', '', '')
    
    prefix = parts[0]
    suffix = parts[1]
    
    # Normalize lab_type for comparison
    lab_type_upper = lab_type.upper() if lab_type else 'HOL'
    
    if lab_type_upper == 'DISCOVERY':
        # Discovery uses name-based pattern (no year extraction)
        year_dir = '/vpodrepo/Discovery-labs'
        repo_dir = f'{year_dir}/{suffix}'
        git_url = f'https://github.com/Broadcom/{sku}.git'
    else:
        # Standard pattern: PREFIX-XXYY where XX=year, YY=index
        # Supports HOL, ATE, VXP, EDU
        if len(suffix) >= 4:
            year = suffix[:2]
            index = suffix[2:4]
            year_dir = f'/vpodrepo/20{year}-labs'
            repo_dir = f'{year_dir}/{year}{index}'
            git_url = f'https://github.com/Broadcom/{prefix}-{year}{index}.git'
        else:
            # Fallback for short suffixes - treat as named
            year_dir = f'/vpodrepo/{prefix}-labs'
            repo_dir = f'{year_dir}/{suffix}'
            git_url = f'https://github.com/Broadcom/{sku}.git'
    
    return (year_dir, repo_dir, git_url)


def get_vpodrepo_file(filename):
    """
    Find a file in vpodrepo, checking multiple locations
    
    :param filename: Filename to find
    :return: Full path to file, or None if not found
    """
    search_paths = [
        f'{vpod_repo}/{filename}',
        f'{vpod_repo}/Startup/{filename}',
        f'{vpod_repo}/scripts/{filename}',
        f'{vpod_repo}/labfiles/{filename}',
        f'{vpod_repo}/ansible/{filename}'
    ]
    
    for path in search_paths:
        if os.path.isfile(path):
            return path
    
    return None

def choose_file(folder, name, ext, **kwargs):
    """
    Return HOL file based on vpodrepo override or git update.
    Enhanced to support LabType-specific overrides.
    
    :param folder: str - the vPod folder of the original file
    :param name: str - the name of the HOL file to check
    :param ext: str - the extension (typically txt or py)
    :param kwargs: labtype - override labtype for testing
    :return: the file path to use
    """
    filename = f'{name}.{ext}'
    lt = kwargs.get('labtype', labtype)
    
    # Search paths in priority order
    search_paths = []
    
    # VPodRepo overrides (highest priority)
    search_paths.append(os.path.join(vpod_repo, 'Startup', filename))
    search_paths.append(os.path.join(vpod_repo, filename))
    
    # LabType-specific core
    search_paths.append(f'{holroot}/Startup.{lt}/{filename}')
    
    # Default core (lowest priority)
    search_paths.append(f'{holroot}/{folder}/{filename}')
    search_paths.append(f'{holroot}/Startup/{filename}')
    
    for path in search_paths:
        if os.path.exists(path):
            write_output(f'Using {filename} from {path}')
            return path
    
    # Fallback to original path
    return f'{holroot}/{folder}/{filename}'

#==============================================================================
# LAB STATUS AND FAILURE
#==============================================================================

def labfail(reason):
    """
    Mark lab as failed and exit
    
    :param reason: Failure reason
    """
    write_output(f'LAB FAILED: {reason}')
    write_vpodprogress(f'FAILED: {reason}', 'FAIL', color='red')
    
    # Update status dashboard if available
    try:
        from Tools.status_dashboard import StatusDashboard
        dashboard = StatusDashboard(lab_sku)
        dashboard.set_failed(reason)
        dashboard.generate_html()
    except Exception:
        pass
    
    sys.exit(1)

def labstartup_sleep(seconds):
    """
    Sleep with timeout checking
    
    :param seconds: Seconds to sleep
    """
    elapsed = datetime.datetime.now() - start_time
    if elapsed.total_seconds() / 60 > max_minutes_before_fail:
        labfail(f'Timeout exceeded ({max_minutes_before_fail} minutes)')
    
    time.sleep(seconds)

#==============================================================================
# STARTUP MODULE SUPPORT
#==============================================================================

def startup(module_name, timeout=120, labcheck_mode=False):
    """
    Execute a startup module
    
    :param module_name: Name of the startup module (without .py)
    :param timeout: Maximum execution time in seconds
    :param labcheck_mode: Whether running in labcheck mode
    """
    global labcheck
    labcheck = labcheck_mode
    
    # Find the module using choose_file
    module_path = choose_file('Startup', module_name, 'py')
    
    if not os.path.isfile(module_path):
        write_output(f'Startup module not found: {module_name}')
        return False
    
    write_output(f'Starting module: {module_name} from {module_path}')
    
    try:
        # Import and execute the module
        import importlib.util
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        
        # Call main if it exists
        if hasattr(module, 'main'):
            module.main()
        
        write_output(f'Completed module: {module_name}')
        return True
        
    except Exception as e:
        write_output(f'Module {module_name} failed: {e}')
        return False

#==============================================================================
# ROUTER COMMUNICATION (NFS-BASED)
#==============================================================================

def push_router_files():
    """
    Copy router configuration files to NFS share for holorouter
    Uses /tmp/holorouter which is NFS exported to the router
    """
    # Find holorouter config directory
    router_dir = f'{holroot}/holorouter'
    
    if not os.path.isdir(router_dir):
        write_output('No holorouter directory found')
        return False
    
    # Ensure NFS export directory exists
    os.makedirs(holorouter_dir, exist_ok=True)
    
    # Copy files to NFS share
    for item in os.listdir(router_dir):
        src = os.path.join(router_dir, item)
        dst = os.path.join(holorouter_dir, item)
        
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            write_output(f'Copied {item} to holorouter NFS share')
        elif os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
    
    return True

def push_vpodrepo_router_files():
    """
    Copy lab-specific router files from vpodrepo to NFS share
    These override/extend the core team default files
    """
    sku_router_dir = f'{vpod_repo}/holorouter'
    
    if not os.path.isdir(sku_router_dir):
        write_output('No vpodrepo holorouter directory found')
        return False
    
    # Merge allowlist files (core + lab-specific)
    core_allowlist = f'{holroot}/holorouter/allowlist'
    sku_allowlist = f'{sku_router_dir}/allowlist'
    merged_allowlist = f'{holorouter_dir}/allowlist'
    
    if os.path.isfile(core_allowlist) and os.path.isfile(sku_allowlist):
        # Concatenate, sort, and unique
        with open(core_allowlist, 'r') as f1, open(sku_allowlist, 'r') as f2:
            all_entries = set(f1.read().splitlines() + f2.read().splitlines())
        
        with open(merged_allowlist, 'w') as f:
            f.write('\n'.join(sorted(all_entries)))
        
        write_output('Merged allowlist files')
    
    # Copy other files (overwrite)
    for item in os.listdir(sku_router_dir):
        if item == 'allowlist':
            continue  # Already handled
        
        src = os.path.join(sku_router_dir, item)
        dst = os.path.join(holorouter_dir, item)
        
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            write_output(f'Copied vpodrepo {item} to holorouter NFS share')
    
    return True

def signal_router_gitdone():
    """Signal to router that git pull is complete"""
    gitdone_file = os.path.join(holorouter_dir, 'gitdone')
    with open(gitdone_file, 'w') as f:
        f.write(str(datetime.datetime.now()))
    write_output('Signaled router: gitdone')

def signal_router_ready():
    """Signal to router that lab is ready"""
    ready_file = os.path.join(holorouter_dir, 'ready')
    with open(ready_file, 'w') as f:
        f.write(str(datetime.datetime.now()))
    write_output('Signaled router: ready')

#==============================================================================
# PARSE LAB SKU
#==============================================================================

def parse_labsku(sku, lab_type_override: str = None):
    """
    Parse the lab SKU and set related variables.
    
    Supports multiple SKU patterns based on lab type:
    - Standard (HOL, ATE, VXP, EDU): PREFIX-XXYY format
    - Named (Discovery): PREFIX-Name format
    
    :param sku: Lab SKU (e.g., HOL-2701, ATE-2705, Discovery-Demo)
    :param lab_type_override: Optional lab type override (defaults to global labtype)
    """
    global lab_sku, vpod_repo
    
    lab_sku = sku
    
    # Use override or global labtype
    lt = lab_type_override if lab_type_override else labtype
    
    if sku != bad_sku:
        _, vpod_repo, git_url = get_repo_info(sku, lt)
        if vpod_repo:
            write_output(f'VPodRepo path: {vpod_repo}')
            write_output(f'Git URL: {git_url}')

#==============================================================================
# MISC HELPERS
#==============================================================================

def postmanfix():
    """Apply Postman fix if needed"""
    # Placeholder for Postman configuration fixes
    pass

def start_autolab():
    """Check for and start autolab if present"""
    autolab_file = f'{lmcholroot}/autolab.py'
    if os.path.isfile(autolab_file):
        write_output('Autolab detected, executing...')
        result = run_command(f'/usr/bin/python3 {autolab_file}')
        return result.returncode == 0
    return False

def start_autocheck():
    """Check for and run autocheck if present"""
    autocheck_file = f'{vpod_repo}/autocheck.py'
    if os.path.isfile(autocheck_file):
        write_output('Autocheck detected, executing...')
        result = run_command(f'/usr/bin/python3 {autocheck_file}')
        return result.returncode == 0
    return False

def clear_atq():
    """Clear all at queue jobs"""
    run_command('for i in $(atq | awk \'{print $1}\'); do atrm "$i"; done')
