#!/usr/bin/env python3
# dns_checks.py - HOLFY27 DNS Health Check Module
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Performs DNS resolution checks for Site A, Site B, and External DNS

import subprocess
import time
import sys
import os

# Add hol directory to path for imports
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# CONFIGURATION
#==============================================================================

DNS_SERVER = '10.1.10.129'  # Holorouter DNS

DNS_CHECKS = {
    'site_a': {
        'hostname': 'esx-01a.site-a.vcf.lab',
        'expected_ip': '10.1.1.101',
        'description': 'Site A DNS Resolution'
    },
    'site_b': {
        'hostname': 'esx-01b.site-b.vcf.lab',
        'expected_ip': '10.2.1.101',
        'description': 'Site B DNS Resolution'
    },
    'external': {
        'hostname': 'www.broadcom.com',
        'expected_ip': None,  # Any result is valid
        'description': 'External DNS Resolution'
    }
}

TIMEOUT_MINUTES = 5
CHECK_INTERVAL_SECONDS = 30

#==============================================================================
# FUNCTIONS
#==============================================================================

def write_output(msg):
    """Write output to log and console"""
    try:
        import lsfunctions as lsf
        lsf.write_output(msg)
    except ImportError:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{timestamp}] {msg}')


def resolve_dns(hostname: str, dns_server: str = DNS_SERVER) -> list:
    """
    Resolve hostname using specific DNS server
    
    :param hostname: Hostname to resolve
    :param dns_server: DNS server to use
    :return: List of resolved IP addresses, empty list on failure
    """
    try:
        result = subprocess.run(
            ['dig', '+short', f'@{dns_server}', hostname, 'A'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            # Filter out CNAME responses, keep only IP addresses
            lines = result.stdout.strip().split('\n')
            ips = [line for line in lines if line and not line.endswith('.')]
            return ips
        
        return []
        
    except subprocess.TimeoutExpired:
        write_output(f'DNS resolution timeout for {hostname}')
        return []
    except FileNotFoundError:
        # dig not available, try nslookup
        return resolve_dns_nslookup(hostname, dns_server)
    except Exception as e:
        write_output(f'DNS resolution error for {hostname}: {e}')
        return []


def resolve_dns_nslookup(hostname: str, dns_server: str) -> list:
    """
    Fallback DNS resolution using nslookup
    
    :param hostname: Hostname to resolve
    :param dns_server: DNS server to use
    :return: List of resolved IP addresses
    """
    try:
        result = subprocess.run(
            ['nslookup', hostname, dns_server],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            # Parse nslookup output for Address lines
            ips = []
            for line in result.stdout.split('\n'):
                if 'Address:' in line and dns_server not in line:
                    ip = line.split('Address:')[-1].strip()
                    if ip:
                        ips.append(ip)
            return ips
        
        return []
        
    except Exception as e:
        write_output(f'nslookup resolution error for {hostname}: {e}')
        return []


def check_dns_resolution(check_name: str, check_config: dict) -> bool:
    """
    Verify DNS resolution matches expected result
    
    :param check_name: Name of the check
    :param check_config: Check configuration dict
    :return: True if check passes
    """
    hostname = check_config['hostname']
    expected_ip = check_config['expected_ip']
    description = check_config['description']
    
    results = resolve_dns(hostname)
    
    if not results:
        write_output(f'{description}: FAILED - No results for {hostname}')
        return False
    
    if expected_ip is None:
        # Any result is valid (external DNS check)
        write_output(f'{description}: PASSED - {hostname} -> {results}')
        return True
    
    if expected_ip in results:
        write_output(f'{description}: PASSED - {hostname} -> {expected_ip}')
        return True
    
    write_output(f'{description}: FAILED - Expected {expected_ip}, got {results}')
    return False


def run_dns_checks(timeout_minutes: int = TIMEOUT_MINUTES) -> bool:
    """
    Run all DNS checks with timeout
    Fails the lab if any check does not pass within timeout
    
    :param timeout_minutes: Maximum time to wait for all checks to pass
    :return: True if all checks pass within timeout
    """
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    
    write_output(f'Starting DNS health checks (timeout: {timeout_minutes} minutes)')
    write_output(f'DNS Server: {DNS_SERVER}')
    
    # Try to update status dashboard
    try:
        import lsfunctions as lsf
        lsf.write_vpodprogress('DNS Health Checks', 'GOOD-1')
    except ImportError:
        pass
    
    while time.time() - start_time < timeout_seconds:
        all_passed = True
        results = {}
        
        for check_name, check_config in DNS_CHECKS.items():
            passed = check_dns_resolution(check_name, check_config)
            results[check_name] = passed
            if not passed:
                all_passed = False
        
        if all_passed:
            elapsed = int(time.time() - start_time)
            write_output(f'All DNS checks passed in {elapsed} seconds')
            return True
        
        # Log which checks failed
        failed = [name for name, passed in results.items() if not passed]
        write_output(f'Failed checks: {failed}. Retrying in {CHECK_INTERVAL_SECONDS} seconds...')
        
        # Wait before retry
        time.sleep(CHECK_INTERVAL_SECONDS)
    
    # Timeout - fail the lab
    write_output(f'DNS checks failed to pass within {timeout_minutes} minutes')
    
    try:
        import lsfunctions as lsf
        lsf.labfail('DNS RESOLUTION FAILURE')
    except ImportError:
        pass
    
    return False


def run_single_check(check_name: str) -> bool:
    """
    Run a single DNS check
    
    :param check_name: Name of the check (site_a, site_b, external)
    :return: True if check passes
    """
    if check_name not in DNS_CHECKS:
        write_output(f'Unknown check: {check_name}')
        return False
    
    return check_dns_resolution(check_name, DNS_CHECKS[check_name])


#==============================================================================
# STANDALONE EXECUTION
#==============================================================================

def main():
    """Main entry point for standalone execution"""
    import argparse
    
    parser = argparse.ArgumentParser(description='HOLFY27 DNS Health Checks')
    parser.add_argument('--check', '-c', choices=['site_a', 'site_b', 'external', 'all'],
                        default='all', help='Which check to run')
    parser.add_argument('--timeout', '-t', type=int, default=TIMEOUT_MINUTES,
                        help=f'Timeout in minutes (default: {TIMEOUT_MINUTES})')
    parser.add_argument('--dns-server', '-s', default=DNS_SERVER,
                        help=f'DNS server to use (default: {DNS_SERVER})')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')
    
    args = parser.parse_args()
    
    # Override DNS server if specified
    global DNS_SERVER
    DNS_SERVER = args.dns_server
    
    if args.check == 'all':
        success = run_dns_checks(timeout_minutes=args.timeout)
    else:
        success = run_single_check(args.check)
    
    print(f'\nDNS checks result: {"PASSED" if success else "FAILED"}')
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
