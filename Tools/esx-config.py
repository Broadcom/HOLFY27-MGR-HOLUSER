#!/usr/bin/env python3
# esx-config.py - HOLFY27 ESXi Host Configuration Tool
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Enables SSH on ESXi hosts and configures passwordless login
# Required to be run before capturing the vApp as a template

"""
ESXi Host Configuration Tool

This script configures ESXi hosts for lab operations:
1. Enables SSH service on each ESXi host
2. Configures SSH to start automatically with the host
3. Copies the holuser public key for passwordless SSH access

This is critical for the shutdown process which requires SSH access
to ESXi hosts for vSAN elevator operations.

Usage:
    python3 esx-config.py                    # Configure all hosts from config.ini
    python3 esx-config.py --hosts esx-01a.site-a.vcf.lab esx-02a.site-a.vcf.lab
    python3 esx-config.py --dry-run          # Show what would be done
    python3 esx-config.py --check            # Check current SSH status only
"""

import os
import sys
import argparse
import time
import ssl

# Add hol directory to path for imports
sys.path.insert(0, '/home/holuser/hol')

from pyVim import connect
from pyVmomi import vim

#==============================================================================
# CONFIGURATION
#==============================================================================

SCRIPT_VERSION = '1.0'
PUBLIC_KEY_FILE = '/home/holuser/.ssh/id_rsa.pub'
ESX_AUTH_KEYS_PATH = '/etc/ssh/keys-root/authorized_keys'
ESX_USERNAME = 'root'

# SSH service names on ESXi
SSH_SERVICE_NAME = 'TSM-SSH'  # Technical Support Mode - SSH

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def write_output(msg: str):
    """Write output to log and console"""
    try:
        import lsfunctions as lsf
        lsf.write_output(msg)
    except ImportError:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{timestamp}] {msg}')


def get_password() -> str:
    """Get the lab password from creds.txt or lsfunctions"""
    try:
        import lsfunctions as lsf
        return lsf.get_password()
    except ImportError:
        creds_file = '/home/holuser/creds.txt'
        if os.path.isfile(creds_file):
            with open(creds_file, 'r') as f:
                return f.read().strip()
        return ''


def get_public_key() -> str:
    """Read the public key from file"""
    if not os.path.isfile(PUBLIC_KEY_FILE):
        write_output(f'ERROR: Public key file not found: {PUBLIC_KEY_FILE}')
        return ''
    
    with open(PUBLIC_KEY_FILE, 'r') as f:
        return f.read().strip()


def remove_stale_ssh_host_key(hostname: str) -> bool:
    """
    Remove stale SSH host key from known_hosts file.
    This prevents "REMOTE HOST IDENTIFICATION HAS CHANGED" errors
    that occur when lab environments are rebuilt.
    
    :param hostname: Hostname to remove from known_hosts
    :return: True if key was removed or didn't exist
    """
    import subprocess
    
    known_hosts_file = os.path.expanduser('~/.ssh/known_hosts')
    
    if not os.path.isfile(known_hosts_file):
        return True
    
    try:
        # Check if host exists in known_hosts
        with open(known_hosts_file, 'r') as f:
            content = f.read()
        
        if hostname not in content:
            return True
        
        # Remove the host key using ssh-keygen
        result = subprocess.run(
            ['ssh-keygen', '-f', known_hosts_file, '-R', hostname],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            write_output(f'{hostname}: Removed stale SSH host key from known_hosts')
            return True
        else:
            write_output(f'{hostname}: Failed to remove SSH host key: {result.stderr}')
            return False
            
    except Exception as e:
        write_output(f'{hostname}: Error removing SSH host key: {e}')
        return False


def remove_stale_host_keys_for_all(hosts: list) -> None:
    """
    Remove stale SSH host keys for all hosts before configuration.
    
    :param hosts: List of hostnames
    """
    write_output('Checking for stale SSH host keys...')
    
    known_hosts_file = os.path.expanduser('~/.ssh/known_hosts')
    if not os.path.isfile(known_hosts_file):
        write_output('No known_hosts file found - skipping')
        return
    
    removed_count = 0
    for hostname in hosts:
        # Read known_hosts to check if host exists
        try:
            with open(known_hosts_file, 'r') as f:
                if hostname in f.read():
                    if remove_stale_ssh_host_key(hostname):
                        removed_count += 1
        except Exception:
            pass
    
    if removed_count > 0:
        write_output(f'Removed {removed_count} stale SSH host key(s)')
    else:
        write_output('No stale SSH host keys found')


def get_esx_hosts_from_config() -> list:
    """Get ESXi hosts from config.ini"""
    hosts = []
    
    try:
        import lsfunctions as lsf
        
        # Try SHUTDOWN section first (preferred for shutdown operations)
        if lsf.config.has_option('SHUTDOWN', 'esx_hosts'):
            hosts_raw = lsf.config.get('SHUTDOWN', 'esx_hosts')
            hosts = [h.strip() for h in hosts_raw.replace('\n', ' ').split() 
                    if h.strip() and not h.strip().startswith('#')]
        
        # Fall back to VCF section
        elif lsf.config.has_option('VCF', 'vcfmgmtcluster'):
            hosts_raw = lsf.config.get('VCF', 'vcfmgmtcluster')
            for line in hosts_raw.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    # Format is hostname:type
                    parts = line.split(':')
                    if parts:
                        hosts.append(parts[0].strip())
        
        # Fall back to RESOURCES section
        elif lsf.config.has_option('RESOURCES', 'ESXiHosts'):
            hosts_raw = lsf.config.get('RESOURCES', 'ESXiHosts')
            for line in hosts_raw.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    # Format is hostname:mm_flag
                    parts = line.split(':')
                    if parts:
                        hosts.append(parts[0].strip())
    
    except Exception as e:
        write_output(f'Error reading config: {e}')
    
    return hosts


def connect_to_host(hostname: str, username: str, password: str):
    """
    Connect directly to an ESXi host
    
    :param hostname: ESXi host FQDN or IP
    :param username: Username (usually root)
    :param password: Password
    :return: ServiceInstance or None
    """
    try:
        # Create SSL context that doesn't verify certificates
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        si = connect.SmartConnect(
            host=hostname,
            user=username,
            pwd=password,
            sslContext=context
        )
        return si
    except Exception as e:
        write_output(f'Failed to connect to {hostname}: {e}')
        return None


def get_ssh_service(host_system) -> vim.host.Service:
    """
    Get the SSH service from a host system
    
    :param host_system: vim.HostSystem object
    :return: SSH service object or None
    """
    try:
        service_system = host_system.configManager.serviceSystem
        services = service_system.serviceInfo.service
        
        for service in services:
            if service.key == SSH_SERVICE_NAME:
                return service
        
        return None
    except Exception as e:
        write_output(f'Error getting SSH service: {e}')
        return None


def enable_ssh_service(host_system, dry_run: bool = False) -> bool:
    """
    Enable SSH service on an ESXi host
    
    :param host_system: vim.HostSystem object
    :param dry_run: If True, don't make changes
    :return: True if successful
    """
    hostname = host_system.name
    
    try:
        service_system = host_system.configManager.serviceSystem
        ssh_service = get_ssh_service(host_system)
        
        if ssh_service is None:
            write_output(f'{hostname}: SSH service not found')
            return False
        
        # Check current status
        if ssh_service.running:
            write_output(f'{hostname}: SSH service already running')
        else:
            if dry_run:
                write_output(f'{hostname}: Would start SSH service')
            else:
                write_output(f'{hostname}: Starting SSH service')
                service_system.StartService(SSH_SERVICE_NAME)
        
        # Set SSH to start with host (policy: on = start automatically)
        if ssh_service.policy != 'on':
            if dry_run:
                write_output(f'{hostname}: Would set SSH to start automatically')
            else:
                write_output(f'{hostname}: Setting SSH to start automatically')
                service_system.UpdateServicePolicy(SSH_SERVICE_NAME, 'on')
        else:
            write_output(f'{hostname}: SSH already configured to start automatically')
        
        return True
        
    except Exception as e:
        write_output(f'{hostname}: Error enabling SSH - {e}')
        return False


def check_ssh_status(host_system) -> dict:
    """
    Check SSH service status on an ESXi host
    
    :param host_system: vim.HostSystem object
    :return: Dict with status info
    """
    hostname = host_system.name
    result = {
        'hostname': hostname,
        'running': False,
        'policy': 'unknown',
        'port_open': False
    }
    
    try:
        ssh_service = get_ssh_service(host_system)
        
        if ssh_service:
            result['running'] = ssh_service.running
            result['policy'] = ssh_service.policy
        
        # Also check if port 22 is reachable
        try:
            import lsfunctions as lsf
            result['port_open'] = lsf.test_tcp_port(hostname, 22, timeout=5)
        except ImportError:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result['port_open'] = sock.connect_ex((hostname, 22)) == 0
            sock.close()
        
    except Exception as e:
        write_output(f'{hostname}: Error checking SSH status - {e}')
    
    return result


def copy_public_key_to_host(hostname: str, username: str, password: str, 
                            public_key: str, dry_run: bool = False) -> bool:
    """
    Copy public key to ESXi host for passwordless SSH
    
    :param hostname: ESXi host FQDN
    :param username: SSH username (root)
    :param password: SSH password
    :param public_key: Public key content
    :param dry_run: If True, don't make changes
    :return: True if successful
    """
    if dry_run:
        write_output(f'{hostname}: Would copy public key to {ESX_AUTH_KEYS_PATH}')
        return True
    
    try:
        import lsfunctions as lsf
        
        # First, check if the key already exists
        check_cmd = f'grep -F "{public_key[:50]}" {ESX_AUTH_KEYS_PATH} 2>/dev/null'
        result = lsf.ssh(check_cmd, f'{username}@{hostname}', password)
        
        if result.returncode == 0:
            write_output(f'{hostname}: Public key already present in authorized_keys')
            return True
        
        # Create the directory if it doesn't exist
        mkdir_cmd = f'mkdir -p /etc/ssh/keys-root'
        lsf.ssh(mkdir_cmd, f'{username}@{hostname}', password)
        
        # Append the public key to authorized_keys
        # Using echo with the key content - need to escape properly
        escaped_key = public_key.replace('"', '\\"')
        append_cmd = f'echo "{escaped_key}" >> {ESX_AUTH_KEYS_PATH}'
        result = lsf.ssh(append_cmd, f'{username}@{hostname}', password)
        
        if result.returncode == 0:
            write_output(f'{hostname}: Public key added to authorized_keys')
            
            # Set proper permissions
            chmod_cmd = f'chmod 600 {ESX_AUTH_KEYS_PATH}'
            lsf.ssh(chmod_cmd, f'{username}@{hostname}', password)
            
            return True
        else:
            write_output(f'{hostname}: Failed to add public key')
            return False
            
    except ImportError:
        write_output(f'{hostname}: lsfunctions not available, cannot copy key via SSH')
        return False
    except Exception as e:
        write_output(f'{hostname}: Error copying public key - {e}')
        return False


def verify_passwordless_ssh(hostname: str, username: str) -> bool:
    """
    Verify that passwordless SSH works to the host
    
    :param hostname: ESXi host FQDN
    :param username: SSH username
    :return: True if passwordless SSH works
    """
    try:
        import subprocess
        
        # Try SSH without password using the key
        result = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
             '-o', 'ConnectTimeout=10', f'{username}@{hostname}', 'hostname'],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0:
            write_output(f'{hostname}: Passwordless SSH verified successfully')
            return True
        else:
            write_output(f'{hostname}: Passwordless SSH not working yet')
            return False
            
    except Exception as e:
        write_output(f'{hostname}: Error verifying passwordless SSH - {e}')
        return False


def configure_host(hostname: str, password: str, public_key: str, 
                   dry_run: bool = False) -> bool:
    """
    Configure a single ESXi host
    
    :param hostname: ESXi host FQDN
    :param password: Root password
    :param public_key: Public key content
    :param dry_run: If True, don't make changes
    :return: True if successful
    """
    write_output(f'')
    write_output(f'Configuring {hostname}...')
    write_output(f'-' * 50)
    
    # Connect to the host
    si = connect_to_host(hostname, ESX_USERNAME, password)
    if si is None:
        return False
    
    try:
        # Get the host system object
        content = si.content
        host_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        hosts = list(host_view.view)
        host_view.Destroy()
        
        if not hosts:
            write_output(f'{hostname}: No host system found')
            return False
        
        host_system = hosts[0]
        
        # Step 1: Enable SSH service
        if not enable_ssh_service(host_system, dry_run):
            write_output(f'{hostname}: Failed to enable SSH')
            return False
        
        # Give the service a moment to start
        if not dry_run:
            time.sleep(2)
        
        # Step 2: Copy public key for passwordless access
        if not copy_public_key_to_host(hostname, ESX_USERNAME, password, 
                                        public_key, dry_run):
            write_output(f'{hostname}: Failed to copy public key')
            return False
        
        # Step 3: Verify passwordless SSH works (only if not dry run)
        if not dry_run:
            time.sleep(1)
            verify_passwordless_ssh(hostname, ESX_USERNAME)
        
        write_output(f'{hostname}: Configuration complete')
        return True
        
    finally:
        connect.Disconnect(si)


def check_hosts(hosts: list, password: str) -> None:
    """
    Check SSH status on all hosts
    
    :param hosts: List of ESXi hostnames
    :param password: Root password
    """
    write_output('')
    write_output('ESXi Host SSH Status Check')
    write_output('=' * 60)
    
    for hostname in hosts:
        write_output(f'')
        write_output(f'Checking {hostname}...')
        
        si = connect_to_host(hostname, ESX_USERNAME, password)
        if si is None:
            continue
        
        try:
            content = si.content
            host_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.HostSystem], True
            )
            host_systems = list(host_view.view)
            host_view.Destroy()
            
            if host_systems:
                status = check_ssh_status(host_systems[0])
                write_output(f'  Running: {status["running"]}')
                write_output(f'  Policy: {status["policy"]} (on=auto-start)')
                write_output(f'  Port 22 Open: {status["port_open"]}')
                
                # Check for passwordless SSH
                if status['port_open']:
                    passwordless = verify_passwordless_ssh(hostname, ESX_USERNAME)
                    write_output(f'  Passwordless SSH: {passwordless}')
        finally:
            connect.Disconnect(si)


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='HOLFY27 ESXi Host Configuration Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 esx-config.py                    Configure all hosts from config.ini
  python3 esx-config.py --check            Check SSH status only
  python3 esx-config.py --dry-run          Show what would be done
  python3 esx-config.py --hosts esx-01a.site-a.vcf.lab esx-02a.site-a.vcf.lab
        """
    )
    
    parser.add_argument('--hosts', nargs='+', 
                        help='Specific ESXi hosts to configure')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--check', action='store_true',
                        help='Check SSH status only, no changes')
    parser.add_argument('--password', 
                        help='Root password (defaults to creds.txt)')
    parser.add_argument('--version', action='version', 
                        version=f'%(prog)s {SCRIPT_VERSION}')
    
    args = parser.parse_args()
    
    # Print banner
    write_output('')
    write_output('=' * 60)
    write_output('  HOLFY27 ESXi Host Configuration Tool')
    write_output(f'  Version {SCRIPT_VERSION}')
    write_output('=' * 60)
    
    # Get password
    password = args.password if args.password else get_password()
    if not password:
        write_output('ERROR: No password provided and creds.txt not found')
        sys.exit(1)
    
    # Get public key
    public_key = get_public_key()
    if not public_key and not args.check:
        write_output('ERROR: Could not read public key')
        sys.exit(1)
    
    # Get hosts list
    if args.hosts:
        hosts = args.hosts
        write_output(f'Using specified hosts: {hosts}')
    else:
        # Try to initialize lsfunctions to read config
        try:
            import lsfunctions as lsf
            lsf.init(router=False)
        except Exception as e:
            write_output(f'Note: Could not initialize lsfunctions: {e}')
        
        hosts = get_esx_hosts_from_config()
        if not hosts:
            write_output('ERROR: No ESXi hosts found in config.ini')
            write_output('Use --hosts to specify hosts manually')
            sys.exit(1)
        write_output(f'Found {len(hosts)} host(s) in config.ini')
    
    # Check mode
    if args.check:
        check_hosts(hosts, password)
        sys.exit(0)
    
    # Configuration mode
    if args.dry_run:
        write_output('')
        write_output('DRY RUN MODE - No changes will be made')
    
    # Remove stale SSH host keys before configuration
    # This prevents "REMOTE HOST IDENTIFICATION HAS CHANGED" errors
    if not args.dry_run:
        write_output('')
        remove_stale_host_keys_for_all(hosts)
    
    write_output('')
    write_output(f'Configuring {len(hosts)} ESXi host(s)...')
    
    success_count = 0
    fail_count = 0
    
    for hostname in hosts:
        if configure_host(hostname, password, public_key, args.dry_run):
            success_count += 1
        else:
            fail_count += 1
    
    # Summary
    write_output('')
    write_output('=' * 60)
    write_output('Summary')
    write_output('=' * 60)
    write_output(f'  Successful: {success_count}')
    write_output(f'  Failed: {fail_count}')
    write_output(f'  Total: {len(hosts)}')
    
    if fail_count > 0:
        write_output('')
        write_output('WARNING: Some hosts failed to configure')
        sys.exit(1)
    else:
        write_output('')
        write_output('All hosts configured successfully!')
        sys.exit(0)


if __name__ == '__main__':
    main()
