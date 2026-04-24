#!/usr/bin/env python3
# tdns_import.py - HOLFY27 DNS Record Import Module
# Version 2.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Imports DNS records from config.ini [VPOD] new-dns-records or new-dns-records.csv
# Reference: https://github.com/burkeazbill/tdns-mgr

import os
import re
import sys
import json
import subprocess
import tempfile
from typing import Optional, Dict, Any, List

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# CONFIGURATION
#==============================================================================

TDNS_MGR_PATH = '/usr/local/bin/tdns-mgr'
DNS_RECORDS_FILENAME = 'new-dns-records.csv'
DEFAULT_CREDS_FILE = '/home/holuser/creds.txt'

# CSV header format per tdns-mgr specification
# See: https://raw.githubusercontent.com/burkeazbill/tdns-mgr/refs/heads/main/new-dns-records.csv
CSV_HEADER = 'zone,name,type,value'

#==============================================================================
# FUNCTIONS
#==============================================================================

def write_output(msg):
    """Write output to log and console"""
    try:
        import lsfunctions as lsf
        lsf.write_output(msg)
    except ImportError:
        import time
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{timestamp}] {msg}')


def get_creds_file() -> str:
    """Lab password file; override with CREDS_FILE environment variable."""
    return os.environ.get('CREDS_FILE', DEFAULT_CREDS_FILE)


def get_password() -> str:
    """Get Technitium admin password from CREDS_FILE (preferred), else lsfunctions."""
    path = get_creds_file()
    if os.path.isfile(path):
        with open(path, 'r') as f:
            pw = f.read().strip()
            if pw:
                return pw
    try:
        import lsfunctions as lsf
        return lsf.get_password()
    except ImportError:
        pass
    return ''


def tdns_mgr_conf_path() -> str:
    return os.path.expanduser('~/.config/tdns-mgr/.tdns-mgr.conf')


def clear_stored_tdns_token() -> None:
    """
    Remove DNS_TOKEN from tdns-mgr user config so a stale/expired token cannot
    short-circuit authentication (tdns-mgr skips password login when DNS_TOKEN is set).
    """
    conf_path = tdns_mgr_conf_path()
    if not os.path.isfile(conf_path):
        return
    token_line = re.compile(r'^\s*(export\s+)?DNS_TOKEN=')
    try:
        with open(conf_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        new_lines = [ln for ln in lines if not token_line.match(ln)]
        if len(new_lines) != len(lines):
            with open(conf_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            write_output('Cleared stale DNS_TOKEN from tdns-mgr config; re-authenticating with password')
    except OSError as e:
        write_output(f'WARNING: Could not update {conf_path} to clear DNS_TOKEN: {e}')


def tdns_mgr_env() -> dict:
    """
    Environment for tdns-mgr subprocesses.

    - Drop inherited DNS_TOKEN so an expired shell token does not override the refreshed file.
    - Drop DNS_PASS so a stale exported password cannot interact oddly with login -p.
    """
    env = os.environ.copy()
    env.pop('DNS_TOKEN', None)
    env.pop('DNS_PASS', None)
    # tdns-mgr.sh maps INSECURE_TDNS -> INSECURE_TLS -> curl -k (lab CA on dns.vcf.lab:443).
    # Do NOT pass CLI --insecure: it was added after v1.2.0 and breaks older tdns-mgr installs.
    if os.environ.get('TDNS_MGR_SECURE_TLS', '').lower() not in ('1', 'true', 'yes'):
        env['INSECURE_TDNS'] = 'true'
    return env


def tdns_mgr_cmd(*parts: str) -> List[str]:
    """Build argv: tdns-mgr <subcommand> ... (TLS insecure via INSECURE_TDNS in tdns_mgr_env)."""
    return [TDNS_MGR_PATH, *parts]


def get_vpod_repo() -> str:
    """Get the vpodrepo path"""
    try:
        import lsfunctions as lsf
        return lsf.vpod_repo
    except ImportError:
        return '/vpodrepo'


def get_config(ini_path: Optional[str] = None):
    """
    Get the config parser object.

    If ini_path is set, read only that file (used for post-boot imports from /tmp/config.ini
    even when lsfunctions is loaded). Otherwise prefer lsfunctions.config, then /tmp/config.ini.
    """
    if ini_path is not None:
        from configparser import ConfigParser
        config = ConfigParser()
        if os.path.isfile(ini_path):
            config.read(ini_path)
        else:
            write_output(f'WARNING: config ini not found: {ini_path}')
        return config
    try:
        import lsfunctions as lsf
        return lsf.config
    except ImportError:
        from configparser import ConfigParser
        config = ConfigParser()
        if os.path.isfile('/tmp/config.ini'):
            config.read('/tmp/config.ini')
        return config


def check_tdns_mgr_available() -> bool:
    """Check if tdns-mgr is available"""
    global TDNS_MGR_PATH
    
    # Check common locations
    paths_to_check = [
        TDNS_MGR_PATH,
        '/usr/bin/tdns-mgr',
        '/home/holuser/bin/tdns-mgr',
        os.path.expanduser('~/.local/bin/tdns-mgr')
    ]
    
    for path in paths_to_check:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            TDNS_MGR_PATH = path
            return True
    
    # Try to find in PATH
    result = subprocess.run(['which', 'tdns-mgr'], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        TDNS_MGR_PATH = result.stdout.strip()
        return True
    
    return False


def get_dns_records_from_config(ini_path: Optional[str] = None) -> List[str]:
    """
    Get DNS records from config.ini [VPOD] new-dns-records

    :param ini_path: If set, read VPOD section from this file only (see get_config).
    
    The config.ini can contain one or more lines of CSV-formatted records:
    new-dns-records = site-a.vcf.lab,gitlab,A,10.1.10.211
    
    Or multiple records separated by newlines or semicolons:
    new-dns-records = site-a.vcf.lab,gitlab,A,10.1.10.211
        site-a.vcf.lab,harbor,A,10.1.10.212
        site-a.vcf.lab,registry,CNAME,gitlab.site-a.vcf.lab
    
    Format: zone,name,type,value (per tdns-mgr CSV specification)
    
    :return: List of record lines (without header), empty list if none
    """
    config = get_config(ini_path)
    records = []
    
    if not config.has_option('VPOD', 'new-dns-records'):
        return records
    
    value = config.get('VPOD', 'new-dns-records').strip()
    
    # Skip if empty, commented, or boolean-like
    if not value or value.lower() in ['true', 'false', 'yes', 'no', '1', '0']:
        return records
    
    # Split by newlines and/or semicolons
    lines = value.replace(';', '\n').split('\n')
    
    for line in lines:
        line = line.strip()
        # Skip empty lines, comments, and header
        if not line or line.startswith('#') or line.lower().startswith('zone,'):
            continue
        
        # Validate it has 4 comma-separated fields (zone,name,type,value)
        parts = line.split(',')
        if len(parts) >= 4:
            records.append(line)
        else:
            write_output(f'WARNING: Invalid DNS record format (expected zone,name,type,value): {line}')
    
    return records


def find_dns_records_file() -> Optional[str]:
    """
    Find the new-dns-records.csv file in vpodrepo
    
    :return: Full path to file or None if not found
    """
    vpod_repo = get_vpod_repo()
    
    # Check locations in priority order
    search_paths = [
        os.path.join(vpod_repo, DNS_RECORDS_FILENAME),
        os.path.join(vpod_repo, 'dns', DNS_RECORDS_FILENAME),
        os.path.join(vpod_repo, 'config', DNS_RECORDS_FILENAME),
    ]
    
    for path in search_paths:
        if os.path.isfile(path):
            write_output(f'Found DNS records file: {path}')
            return path
    
    return None


def tdns_show_config():
    """
    Show tdns-mgr configuration for debugging
    Logs the output of 'tdns-mgr config' to help diagnose connection issues
    """
    write_output('Checking tdns-mgr configuration...')
    
    try:
        result = subprocess.run(
            tdns_mgr_cmd('config'),
            capture_output=True,
            text=True,
            timeout=10,
            env=tdns_mgr_env(),
        )
        
        if result.returncode == 0 and result.stdout.strip():
            write_output(f'tdns-mgr config: {result.stdout.strip()}')
        elif result.stderr.strip():
            write_output(f'tdns-mgr config error: {result.stderr.strip()}')
        else:
            write_output('tdns-mgr config returned no output')
            
    except subprocess.TimeoutExpired:
        write_output('tdns-mgr config timed out')
    except Exception as e:
        write_output(f'tdns-mgr config error: {e}')


def tdns_login(max_retries: int = 10, retry_delay: int = 15) -> bool:
    """
    Login to tdns-mgr using password from CREDS_FILE (or lsfunctions fallback).
    Clears any stale DNS_TOKEN in ~/.config/tdns-mgr/.tdns-mgr.conf first so
    expired tokens cannot block password-based re-authentication.

    Retries up to max_retries times with retry_delay seconds between attempts
    
    :param max_retries: Maximum number of login attempts (default: 10)
    :param retry_delay: Seconds to wait between retries (default: 15)
    :return: True if login successful
    """
    import time

    # Expired stored token causes tdns-mgr to skip real login; force password path
    clear_stored_tdns_token()
    
    # Show config before login attempt for debugging
    tdns_show_config()
    
    password = get_password()
    
    if not password:
        write_output(
            f'ERROR: No password available for tdns-mgr login '
            f'(checked {get_creds_file()} and lsfunctions)'
        )
        return False
    
    for attempt in range(1, max_retries + 1):
        write_output(f'Logging into tdns-mgr (attempt {attempt}/{max_retries})...')
        
        try:
            # Non-interactive: -p password (stdin alone can fail if a stale token short-circuits auth)
            result = subprocess.run(
                tdns_mgr_cmd('login', '-p', password),
                capture_output=True,
                text=True,
                timeout=30,
                env=tdns_mgr_env(),
            )
            
            if result.returncode == 0:
                write_output('tdns-mgr login successful')
                return True
            else:
                err_parts = []
                if result.stderr and result.stderr.strip():
                    err_parts.append(result.stderr.strip())
                if result.stdout and result.stdout.strip():
                    err_parts.append(result.stdout.strip())
                error_msg = ' | '.join(err_parts) if err_parts else 'Unknown error'
                write_output(f'tdns-mgr login failed: {error_msg}')
                
        except subprocess.TimeoutExpired:
            write_output('tdns-mgr login timed out')
        except Exception as e:
            write_output(f'tdns-mgr login error: {e}')
        
        # If not the last attempt, wait before retrying
        if attempt < max_retries:
            write_output(f'Waiting {retry_delay} seconds before retry...')
            time.sleep(retry_delay)
    
    write_output(f'ERROR: tdns-mgr login failed after {max_retries} attempts')
    return False


def import_records_from_file(csv_path: str) -> Dict[str, Any]:
    """
    Import DNS records from CSV file using tdns-mgr
    
    :param csv_path: Path to the new-dns-records.csv file
    :return: Result dictionary with import statistics
    """
    write_output(f'Importing DNS records from: {csv_path}')
    
    try:
        result = subprocess.run(
            tdns_mgr_cmd('import-records', csv_path, '--ptr'),
            capture_output=True,
            text=True,
            timeout=120,
            env=tdns_mgr_env(),
        )
        
        if result.stdout.strip():
            try:
                output = json.loads(result.stdout.strip())
                write_output(f'DNS import result: {output}')
                return output
            except json.JSONDecodeError:
                write_output(f'DNS import output: {result.stdout}')
                return {
                    'Message': result.stdout.strip(),
                    'New Records': 0,
                    'Errors': 0 if result.returncode == 0 else 1
                }
        
        if result.returncode != 0:
            write_output(f'DNS import error: {result.stderr}')
            return {'Message': result.stderr, 'New Records': 0, 'Errors': 1}
        
        return {'Message': 'Completed', 'New Records': 0, 'Errors': 0}
        
    except subprocess.TimeoutExpired:
        write_output('DNS record import timed out')
        return {'Message': 'Timeout', 'New Records': 0, 'Errors': 1}
    except Exception as e:
        write_output(f'DNS import error: {e}')
        return {'Message': str(e), 'New Records': 0, 'Errors': 1}


def import_records_from_config(records: List[str]) -> Dict[str, Any]:
    """
    Import DNS records from config.ini inline values
    Creates a temporary CSV file and imports it
    
    :param records: List of record lines (zone,name,type,value format)
    :return: Result dictionary with import statistics
    """
    if not records:
        return {'Message': 'No records', 'New Records': 0, 'Errors': 0}
    
    write_output(f'Importing {len(records)} DNS records from config.ini')
    
    # Create temporary CSV file with proper format
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(f'{CSV_HEADER}\n')
            for record in records:
                f.write(f'{record}\n')
            temp_path = f.name
        
        # Import using the temp file
        result = import_records_from_file(temp_path)
        
        # Clean up temp file
        os.unlink(temp_path)
        
        return result
        
    except Exception as e:
        write_output(f'Error creating temp CSV file: {e}')
        return {'Message': str(e), 'New Records': 0, 'Errors': 1}


def import_dns_records(
    config_ini: Optional[str] = None,
    csv_fallback: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Main function to check for and import DNS records
    This is called from labstartup.py
    
    Process:
    1. Check config.ini [VPOD] new-dns-records for inline values
    2. If config has values, import those (primary source)
    3. If no config values and csv_fallback, check for new-dns-records.csv file
    4. If file exists, import from file (fallback)
    5. Login to tdns-mgr and import with --ptr flag
    6. Log results (but don't fail the lab)

    :param config_ini: Optional path to ini; when set, VPOD new-dns-records are read only from this file
    :param csv_fallback: When False (e.g. --config-ini), do not import from new-dns-records.csv
    :return: Import result dictionary, or None if no import needed
    """
    # Check if tdns-mgr is available
    if not check_tdns_mgr_available():
        write_output('ERROR: tdns-mgr not found - DNS import FAILED')
        # Update status dashboard to show failure instead of skipped
        try:
            from Tools.status_dashboard import StatusDashboard
            import lsfunctions as lsf
            dashboard = StatusDashboard(lsf.lab_sku)
            dashboard.update_task('prelim', 'dns_import', 'failed', 'tdns-mgr command not found')
            dashboard.generate_html()
        except Exception:
            pass
        return {'Message': 'tdns-mgr not found', 'New Records': 0, 'Errors': 1}
    
    # Helper function to update dashboard status
    def update_dashboard_status(status: str, message: str = ""):
        try:
            from Tools.status_dashboard import StatusDashboard
            import lsfunctions as lsf
            dashboard = StatusDashboard(lsf.lab_sku)
            dashboard.update_task('prelim', 'dns_import', status, message)
            dashboard.generate_html()
        except Exception:
            pass
    
    # PRIORITY 1: Check config.ini for inline DNS records
    config_records = get_dns_records_from_config(config_ini)
    
    if config_records:
        src = config_ini or 'config.ini'
        write_output(f'Found {len(config_records)} DNS records in {src}')
        
        # Login to tdns-mgr
        if not tdns_login():
            write_output('ERROR: Could not login to tdns-mgr - DNS import FAILED')
            update_dashboard_status('failed', 'Login to DNS server failed')
            return {'Message': 'Login failed', 'New Records': 0, 'Errors': 1}
        
        # Import from config values
        result = import_records_from_config(config_records)
        
        # Log summary and update dashboard
        new_records = result.get('New Records', 0)
        existing_records = result.get('Existing Records', 0)
        errors = result.get('Errors', 0)
        message = result.get('Message', '')
        
        # Check if all records already existed (this is a success, not a failure)
        # tdns-mgr may return "already exists" messages or Existing Records count
        records_already_exist = (
            existing_records > 0 or 
            'already exist' in message.lower() or
            'exists' in message.lower()
        )
        
        if errors == 0 or records_already_exist:
            if new_records > 0:
                write_output(f'DNS import from config.ini completed: {new_records} new records added')
                update_dashboard_status('complete', f'{new_records} records imported')
            elif records_already_exist:
                write_output(f'DNS import from config.ini: all {len(config_records)} records already exist')
                update_dashboard_status('complete', f'All records already exist')
            else:
                write_output(f'DNS import from config.ini completed: no new records needed')
                update_dashboard_status('complete', 'No new records needed')
        else:
            write_output(f'DNS import from config.ini had errors: {message}')
            update_dashboard_status('failed', message or 'Import errors')
        
        return result
    
    # PRIORITY 2: Check for new-dns-records.csv file (only if no config values)
    if not csv_fallback:
        write_output(
            'No [VPOD] new-dns-records in the given config (--config-ini); '
            'skipping new-dns-records.csv fallback'
        )
        update_dashboard_status('skipped', 'No inline records in specified config')
        return None

    csv_path = find_dns_records_file()
    
    if csv_path:
        write_output('No inline config values, using new-dns-records.csv file')
        
        # Login to tdns-mgr
        if not tdns_login():
            write_output('ERROR: Could not login to tdns-mgr - DNS import FAILED')
            update_dashboard_status('failed', 'Login to DNS server failed')
            return {'Message': 'Login failed', 'New Records': 0, 'Errors': 1}
        
        # Import from file
        result = import_records_from_file(csv_path)
        
        # Log summary and update dashboard
        new_records = result.get('New Records', 0)
        existing_records = result.get('Existing Records', 0)
        errors = result.get('Errors', 0)
        message = result.get('Message', '')
        
        # Check if all records already existed (this is a success, not a failure)
        records_already_exist = (
            existing_records > 0 or 
            'already exist' in message.lower() or
            'exists' in message.lower()
        )
        
        if errors == 0 or records_already_exist:
            if new_records > 0:
                write_output(f'DNS import from file completed: {new_records} new records added')
                update_dashboard_status('complete', f'{new_records} records imported')
            elif records_already_exist:
                write_output(f'DNS import from file: all records already exist')
                update_dashboard_status('complete', 'All records already exist')
            else:
                write_output(f'DNS import from file completed: no new records needed')
                update_dashboard_status('complete', 'No new records needed')
        else:
            write_output(f'DNS import from file had errors: {message}')
            update_dashboard_status('failed', message or 'Import errors')
        
        return result
    
    # No DNS records to import
    write_output('No DNS records configured in config.ini or new-dns-records.csv')
    update_dashboard_status('skipped', 'No records to import')
    return None


#==============================================================================
# CSV FORMAT REFERENCE
#==============================================================================

"""
The new-dns-records.csv file and config.ini values should use the following format:

zone,name,type,value
site-a.vcf.lab,gitlab,A,10.1.10.210
site-a.vcf.lab,poste,A,10.1.10.211
site-a.vcf.lab,registry,CNAME,gitlab.site-a.vcf.lab

Fields:
- zone: The DNS zone name (e.g., site-a.vcf.lab)
- name: The record name/hostname (without the zone suffix)
- type: Record type (A, AAAA, CNAME, TXT, MX, etc.)
- value: The record value (IP address, target hostname, etc.)

Reference: https://raw.githubusercontent.com/burkeazbill/tdns-mgr/refs/heads/main/new-dns-records.csv

The --ptr flag causes tdns-mgr to also create PTR records for A/AAAA records.

In config.ini, specify records like:
[VPOD]
new-dns-records = site-a.vcf.lab,gitlab,A,10.1.10.211
    site-a.vcf.lab,harbor,A,10.1.10.212

Multiple records can be separated by newlines (with indentation) or semicolons.
"""


#==============================================================================
# STANDALONE EXECUTION
#==============================================================================

def main():
    """Main entry point for standalone execution"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='HOLFY27 DNS Record Import',
        epilog='Reference: https://github.com/burkeazbill/tdns-mgr'
    )
    parser.add_argument('--csv', '-c', help='Path to CSV file (overrides auto-detection)')
    parser.add_argument(
        '--config-ini',
        metavar='PATH',
        default=None,
        help=(
            'Read [VPOD] new-dns-records only from this file (e.g. /tmp/config.ini). '
            'Does not fall back to new-dns-records.csv. Use after boot to apply inline DNS only.'
        ),
    )
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show what would be imported without making changes')
    parser.add_argument('--show-config', action='store_true',
                        help='Show DNS records found in config.ini')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    if args.show_config:
        records = get_dns_records_from_config(args.config_ini)
        if records:
            src = args.config_ini or 'config.ini'
            print(f'Found {len(records)} DNS records in {src}:')
            for r in records:
                print(f'  {r}')
        else:
            print('No DNS records found in config.ini [VPOD] new-dns-records')
        return
    
    if args.dry_run:
        # Show what would be imported
        config_records = get_dns_records_from_config(args.config_ini)
        if config_records:
            src = args.config_ini or 'config.ini'
            print(f'Would import {len(config_records)} records from {src}:')
            for r in config_records:
                print(f'  {r}')
        else:
            if args.config_ini:
                print('No DNS records in [VPOD] new-dns-records for --config-ini (CSV fallback disabled)')
            else:
                csv_path = args.csv or find_dns_records_file()
                if csv_path:
                    print(f'Would import records from file: {csv_path}')
                    with open(csv_path, 'r') as f:
                        print(f.read())
                else:
                    print('No DNS records to import')
        return
    
    if args.csv:
        # Direct import from specified file
        if not check_tdns_mgr_available():
            print('ERROR: tdns-mgr not found')
            sys.exit(1)
        
        if not tdns_login():
            print('ERROR: Could not login to tdns-mgr')
            sys.exit(1)
        
        result = import_records_from_file(args.csv)
    else:
        # Standard auto-detection flow, or config-ini-only inline records
        result = import_dns_records(
            config_ini=args.config_ini,
            csv_fallback=args.config_ini is None,
        )
    
    if result:
        print(f'\nImport Result: {json.dumps(result, indent=2)}')
        sys.exit(0 if result.get('Errors', 0) == 0 else 1)
    else:
        print('No DNS records to import')
        sys.exit(0)


if __name__ == '__main__':
    main()
