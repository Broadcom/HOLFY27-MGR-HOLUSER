#!/usr/bin/env python3
# offline-ready.py - HOLFY27 Offline Lab Export Preparation Script
# Version 1.1 - February 2026
# Author - HOL Core Team
#
# Prepares a lab environment for offline/partner export by:
#   1. Creating offline-mode marker files to skip git operations on boot
#   2. Creating testing flag files to skip git clone/pull in labstartup.sh
#   3. Setting lockholuser = false in config.ini and holodeck/*.ini files
#   4. Removing external URLs from config.ini and holodeck/*.ini URLS
#   5. Setting passwords on manager, router, and console from creds.txt
#   6. Disabling VLP Agent startup
#
# USAGE:
#   python3 offline-ready.py              # Full preparation (with confirmation)
#   python3 offline-ready.py --dry-run    # Preview changes without applying
#   python3 offline-ready.py --yes        # Skip confirmation prompt
#   python3 offline-ready.py --verbose    # Verbose output
#
# PREREQUISITES:
#   - Run on the Manager VM as root or holuser with sudo
#   - Lab should have completed a successful startup at least once
#   - /vpodrepo should contain a valid local copy of the lab repository
#   - Console and router should be accessible via SSH
#
# IMPORTANT:
#   This script modifies files in-place on the live Manager VM.
#   It is idempotent -- running it multiple times produces the same result.

"""
Offline Lab Export Preparation Tool

Prepares a HOLFY27 lab environment for export to a partner who will run
the lab without internet access. Disables all network-dependent operations
(git clone/pull, tool downloads, VLP agent) and configures the lab to
start up cleanly using only local copies of repositories and configs.
"""

import os
import sys
import argparse
import subprocess
import datetime
import re
import shutil
from configparser import ConfigParser
from pathlib import Path

#==============================================================================
# CONFIGURATION
#==============================================================================

# Paths
HOLUSER_HOME = '/home/holuser'
HOLUSER_HOLROOT = f'{HOLUSER_HOME}/hol'
ROOT_HOLROOT = '/root/hol'
CREDS_FILE = f'{HOLUSER_HOME}/creds.txt'
CONFIG_INI = '/tmp/config.ini'
LMCHOL_ROOT = '/lmchol/hol'
LMCHOL_HOME = '/lmchol/home/holuser'

# Holodeck config directory
HOLODECK_DIR = f'{HOLUSER_HOLROOT}/holodeck'

# Log file
LOG_FILE = '/tmp/offline-ready.log'

# Marker files
OFFLINE_MARKERS = [
    f'{HOLUSER_HOLROOT}/.offline-mode',
    f'{ROOT_HOLROOT}/.offline-mode',
]
LMCHOL_OFFLINE_MARKER = f'{LMCHOL_ROOT}/offline-mode'
TESTING_FLAG_FILES = [
    f'{LMCHOL_ROOT}/testing',
    f'{HOLUSER_HOLROOT}/testing',
]

# SSH targets
ROUTER_HOST = 'router.site-a.vcf.lab'
CONSOLE_HOST = 'console.site-a.vcf.lab'
SSH_OPTIONS = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'

# Internal domain patterns (URLs matching these are kept, all others removed)
INTERNAL_PATTERNS = [
    r'\.vcf\.lab',
    r'\.site-a\.',
    r'\.site-b\.',
    r'10\.\d+\.\d+\.\d+',
    r'172\.(1[6-9]|2[0-9]|3[0-1])\.\d+\.\d+',
    r'192\.168\.\d+\.\d+',
]


#==============================================================================
# LOGGING
#==============================================================================

_verbose = False
_dry_run = False
_log_fh = None


def log(msg, level='INFO'):
    """Log a message to file and optionally to console."""
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted = f'[{timestamp}] [{level}] {msg}'

    if _log_fh:
        _log_fh.write(formatted + '\n')
        _log_fh.flush()

    if level == 'ERROR':
        print(f'  ERROR: {msg}')
    elif level == 'WARN':
        print(f'  WARNING: {msg}')
    elif _verbose or level == 'ACTION':
        print(f'  {msg}')


def action(msg):
    """Log an action being taken."""
    prefix = '[DRY-RUN] ' if _dry_run else ''
    log(f'{prefix}{msg}', 'ACTION')


#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def read_password():
    """Read the lab password from creds.txt."""
    if not os.path.isfile(CREDS_FILE):
        log(f'Credentials file not found: {CREDS_FILE}', 'ERROR')
        return None
    with open(CREDS_FILE, 'r') as f:
        return f.read().strip()


def run_cmd(cmd, check=False):
    """Run a shell command and return the result."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        if check and result.returncode != 0:
            log(f'Command failed: {cmd}\n  stderr: {result.stderr.strip()}', 'WARN')
        return result
    except subprocess.TimeoutExpired:
        log(f'Command timed out: {cmd}', 'WARN')
        return subprocess.CompletedProcess(cmd, 1, '', 'Timeout')
    except Exception as e:
        log(f'Command error: {cmd}: {e}', 'ERROR')
        return subprocess.CompletedProcess(cmd, 1, '', str(e))


def ssh_cmd(host, command, password):
    """Execute a command on a remote host via SSH."""
    cmd = (
        f'sshpass -p "{password}" ssh {SSH_OPTIONS} '
        f'root@{host} "{command}"'
    )
    return run_cmd(cmd)


def is_internal_url(url):
    """Check if a URL points to an internal lab resource."""
    for pattern in INTERNAL_PATTERNS:
        if re.search(pattern, url):
            return True
    return False


#==============================================================================
# PREPARATION STEPS
#==============================================================================

def step_create_offline_markers():
    """Create offline-mode marker files checked by gitpull.sh scripts."""
    print('\n--- Step 1: Create offline-mode marker files ---')

    for marker in OFFLINE_MARKERS:
        marker_dir = os.path.dirname(marker)
        if not os.path.isdir(marker_dir):
            log(f'Directory does not exist, skipping marker: {marker}', 'WARN')
            continue
        action(f'Creating marker: {marker}')
        if not _dry_run:
            with open(marker, 'w') as f:
                f.write(f'Offline mode enabled by offline-ready.py at '
                        f'{datetime.datetime.now().isoformat()}\n')
            os.chmod(marker, 0o644)

    # Console marker (may not be mounted)
    if os.path.isdir(LMCHOL_ROOT):
        action(f'Creating marker: {LMCHOL_OFFLINE_MARKER}')
        if not _dry_run:
            with open(LMCHOL_OFFLINE_MARKER, 'w') as f:
                f.write(f'Offline mode enabled by offline-ready.py at '
                        f'{datetime.datetime.now().isoformat()}\n')
            os.chmod(LMCHOL_OFFLINE_MARKER, 0o644)
    else:
        log(f'{LMCHOL_ROOT} not mounted, skipping console marker', 'WARN')


def step_create_testing_flag():
    """Create testing flag files used by labstartup.sh/gitpull.sh to skip git ops.

    Both labstartup.sh and gitpull.sh check two paths:
      - /lmchol/hol/testing  (NFS/Console path)
      - /home/holuser/hol/testing  (local Manager path)
    We create both to ensure git operations are skipped regardless of
    whether the NFS mount is available at the time of the check.
    """
    print('\n--- Step 2: Create testing flag files ---')

    created = 0
    for flag_path in TESTING_FLAG_FILES:
        flag_dir = os.path.dirname(flag_path)
        if os.path.isdir(flag_dir):
            action(f'Creating testing flag: {flag_path}')
            if not _dry_run:
                with open(flag_path, 'w') as f:
                    f.write(f'OFFLINE MODE - Set by offline-ready.py at '
                            f'{datetime.datetime.now().isoformat()}\n'
                            f'This file causes labstartup.sh/gitpull.sh to skip git '
                            f'clone/pull operations.\n')
                os.chmod(flag_path, 0o644)
            created += 1
        else:
            log(f'Directory does not exist, skipping flag: {flag_path}', 'WARN')

    if created == 0:
        log('Could not create any testing flag files', 'ERROR')
        log('Ensure /lmchol/hol or /home/holuser/hol exists.', 'ERROR')
        return False

    return True


def step_modify_config_lockholuser():
    """Set lockholuser = false in config.ini and holodeck/*.ini files."""
    print('\n--- Step 3: Set lockholuser = false ---')

    files_modified = []

    # Modify /tmp/config.ini if it exists
    if os.path.isfile(CONFIG_INI):
        if _update_lockholuser(CONFIG_INI):
            files_modified.append(CONFIG_INI)

    # Modify all holodeck/*.ini files
    if os.path.isdir(HOLODECK_DIR):
        for ini_file in sorted(Path(HOLODECK_DIR).glob('*.ini')):
            if _update_lockholuser(str(ini_file)):
                files_modified.append(str(ini_file))

    if files_modified:
        log(f'Modified lockholuser in {len(files_modified)} file(s)', 'ACTION')
    else:
        log('No config files found to modify', 'WARN')


def _update_lockholuser(filepath):
    """Update lockholuser to false in a single config file. Returns True if changed."""
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        # Check if lockholuser exists and is set to true
        if re.search(r'^lockholuser\s*=\s*true', content, re.MULTILINE):
            action(f'Setting lockholuser = false in {filepath}')
            if not _dry_run:
                new_content = re.sub(
                    r'^(lockholuser\s*=\s*)true',
                    r'\1false',
                    content,
                    flags=re.MULTILINE
                )
                with open(filepath, 'w') as f:
                    f.write(new_content)
            return True
        else:
            log(f'lockholuser already false or not present in {filepath}')
            return False
    except Exception as e:
        log(f'Error modifying {filepath}: {e}', 'ERROR')
        return False


def _remove_external_urls_from_file(filepath):
    """Remove external URLs from URLS key in a single config file.

    Returns the number of external URLs removed.
    """
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except Exception as e:
        log(f'Error reading {filepath}: {e}', 'ERROR')
        return 0

    lines = content.split('\n')
    new_lines = []
    in_urls_section = False
    urls_removed = []
    urls_kept = []

    for line in lines:
        stripped = line.strip()

        if re.match(r'^URLS\s*=', stripped):
            in_urls_section = True
            url_part = stripped.split('=', 1)[1].strip()
            if url_part and not url_part.startswith('#'):
                url_only = url_part.split(',')[0].strip()
                if is_internal_url(url_only):
                    new_lines.append(line)
                    urls_kept.append(url_only)
                else:
                    action(f'Removing external URL from {filepath}: {url_only}')
                    urls_removed.append(url_only)
                    new_lines.append(f'URLS = # External URLs removed by offline-ready.py')
            else:
                new_lines.append(line)
            continue

        if in_urls_section:
            if stripped and not stripped.startswith('#') and (
                line.startswith(' ') or line.startswith('\t')
            ):
                url_only = stripped.split(',')[0].strip()
                if is_internal_url(url_only):
                    new_lines.append(line)
                    urls_kept.append(url_only)
                else:
                    action(f'Removing external URL from {filepath}: {url_only}')
                    urls_removed.append(url_only)
                continue
            elif stripped.startswith('#') and (
                line.startswith(' ') or line.startswith('\t')
            ):
                new_lines.append(line)
                continue
            else:
                in_urls_section = False
                new_lines.append(line)
                continue
        else:
            new_lines.append(line)

    if urls_removed:
        action(f'{filepath}: Removed {len(urls_removed)} external URL(s), '
               f'kept {len(urls_kept)} internal URL(s)')
        if not _dry_run:
            with open(filepath, 'w') as f:
                f.write('\n'.join(new_lines))

    return len(urls_removed)


def step_remove_external_urls():
    """Remove external URLs from config.ini and holodeck/*.ini URLS sections."""
    print('\n--- Step 4: Remove external URLs from config files ---')

    total_removed = 0

    # Process /tmp/config.ini
    if os.path.isfile(CONFIG_INI):
        total_removed += _remove_external_urls_from_file(CONFIG_INI)
    else:
        log(f'{CONFIG_INI} not found, skipping', 'WARN')

    # Process holodeck/*.ini files (these are the source of truth for future boots)
    if os.path.isdir(HOLODECK_DIR):
        for ini_file in sorted(Path(HOLODECK_DIR).glob('*.ini')):
            total_removed += _remove_external_urls_from_file(str(ini_file))
    else:
        log(f'{HOLODECK_DIR} not found, skipping holodeck ini files', 'WARN')

    if total_removed == 0:
        log('No external URLs found in any config files')


def step_set_passwords(password):
    """Set passwords on manager, router, and console from creds.txt."""
    print('\n--- Step 5: Set passwords from creds.txt ---')

    if not password:
        log('No password available from creds.txt', 'ERROR')
        return

    # Manager - local accounts
    action('Setting root password on Manager')
    if not _dry_run:
        result = run_cmd(f'echo "root:{password}" | chpasswd')
        if result.returncode == 0:
            log('Manager root password set successfully')
        else:
            log(f'Failed to set Manager root password: {result.stderr}', 'ERROR')

    action('Setting holuser password on Manager')
    if not _dry_run:
        result = run_cmd(f'echo "holuser:{password}" | chpasswd')
        if result.returncode == 0:
            log('Manager holuser password set successfully')
        else:
            log(f'Failed to set Manager holuser password: {result.stderr}', 'ERROR')

    # Router - remote via SSH
    action(f'Setting root password on Router ({ROUTER_HOST})')
    if not _dry_run:
        result = ssh_cmd(ROUTER_HOST,
                         f'echo "root:{password}" | chpasswd',
                         password)
        if result.returncode == 0:
            log('Router root password set successfully')
        else:
            log(f'Failed to set Router root password: {result.stderr}', 'WARN')

    action(f'Setting holuser password on Router ({ROUTER_HOST})')
    if not _dry_run:
        result = ssh_cmd(ROUTER_HOST,
                         f'echo "holuser:{password}" | chpasswd',
                         password)
        if result.returncode == 0:
            log('Router holuser password set successfully')
        else:
            log(f'Failed to set Router holuser password: {result.stderr}', 'WARN')

    # Console - remote via SSH
    action(f'Setting root password on Console ({CONSOLE_HOST})')
    if not _dry_run:
        result = ssh_cmd(CONSOLE_HOST,
                         f'echo "root:{password}" | chpasswd',
                         password)
        if result.returncode == 0:
            log('Console root password set successfully')
        else:
            log(f'Failed to set Console root password: {result.stderr}', 'WARN')

    action(f'Setting holuser password on Console ({CONSOLE_HOST})')
    if not _dry_run:
        result = ssh_cmd(CONSOLE_HOST,
                         f'echo "holuser:{password}" | chpasswd',
                         password)
        if result.returncode == 0:
            log('Console holuser password set successfully')
        else:
            log(f'Failed to set Console holuser password: {result.stderr}', 'WARN')


def step_disable_vlp_agent():
    """Disable the VLP Agent from starting on boot.

    The VLP Agent is started by labstartup.sh (START VLP AGENT section)
    based on cloud environment detection. In offline mode, vmtoolsd will
    return "No value found" which causes it to not start. However, for
    extra safety we create a persistent marker that labstartup.sh and
    VLPagent.sh check before starting the agent.

    The marker is placed in HOLUSER_HOLROOT (survives reboots) rather
    than /tmp (cleared on reboot).
    """
    print('\n--- Step 6: Disable VLP Agent ---')

    vlp_persistent = f'{HOLUSER_HOLROOT}/.vlp-disabled'
    action(f'Creating VLP disable marker: {vlp_persistent}')
    if not _dry_run:
        if os.path.isdir(HOLUSER_HOLROOT):
            with open(vlp_persistent, 'w') as f:
                f.write(f'VLP Agent disabled by offline-ready.py at '
                        f'{datetime.datetime.now().isoformat()}\n')
            os.chmod(vlp_persistent, 0o644)
        else:
            log(f'{HOLUSER_HOLROOT} does not exist', 'ERROR')


def step_verify_vpodrepo():
    """Verify that /vpodrepo has a local copy of the lab repository."""
    print('\n--- Step 7: Verify local vpodrepo ---')

    vpodrepo = '/vpodrepo'
    if not os.path.isdir(vpodrepo):
        log(f'{vpodrepo} does not exist or is not mounted', 'ERROR')
        log('The lab must have a local copy of the vpodrepo for offline use', 'ERROR')
        return False

    if not os.path.isdir(f'{vpodrepo}/lost+found'):
        log(f'{vpodrepo} exists but does not appear to be a mounted volume', 'WARN')

    # Check for any lab directories
    lab_dirs = []
    for item in os.listdir(vpodrepo):
        item_path = os.path.join(vpodrepo, item)
        if os.path.isdir(item_path) and item != 'lost+found':
            lab_dirs.append(item)

    if lab_dirs:
        log(f'Found {len(lab_dirs)} lab directory(ies) in {vpodrepo}: '
            f'{", ".join(lab_dirs)}')
    else:
        log(f'No lab directories found in {vpodrepo}', 'WARN')
        log('Make sure the lab repository has been cloned to /vpodrepo '
            'before running a lab startup', 'WARN')

    return True


#==============================================================================
# MAIN
#==============================================================================

def print_banner():
    """Print the script banner."""
    print('=' * 62)
    print('  HOLFY27 Offline Lab Export Preparation Tool')
    print('  Version 1.1 - February 2026')
    print('=' * 62)
    if _dry_run:
        print('  *** DRY-RUN MODE - No changes will be made ***')
    print()


def print_summary():
    """Print a summary of what will be done."""
    print('This script will prepare the lab for offline/partner export:\n')
    print('  1. Create offline-mode marker files (skip git ops on boot)')
    print('  2. Create testing flag files (skip git clone/pull in labstartup.sh)')
    print('  3. Set lockholuser = false in config.ini and holodeck/*.ini')
    print('  4. Remove external URLs from config.ini and holodeck/*.ini')
    print('  5. Set passwords on manager, router, and console from creds.txt')
    print('  6. Disable VLP Agent startup')
    print('  7. Verify local vpodrepo exists')
    print()


def main():
    global _verbose, _dry_run, _log_fh

    parser = argparse.ArgumentParser(
        description='HOLFY27 Offline Lab Export Preparation Tool'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without applying them')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Skip confirmation prompt')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')
    args = parser.parse_args()

    _verbose = args.verbose
    _dry_run = args.dry_run

    # Open log file
    try:
        _log_fh = open(LOG_FILE, 'w')
    except Exception:
        _log_fh = None

    print_banner()
    print_summary()

    # Read password early for validation
    password = read_password()
    if not password:
        print('ERROR: Cannot read password from creds.txt. Aborting.')
        sys.exit(1)

    print(f'  Password source: {CREDS_FILE}')
    print(f'  Password value:  {"*" * min(4, len(password))}'
          f'{password[4:8] if len(password) > 4 else ""}...')
    print()

    # Confirmation
    if not args.yes and not _dry_run:
        try:
            response = input('Proceed with offline preparation? [y/N] ')
            if response.lower() not in ('y', 'yes'):
                print('Aborted.')
                sys.exit(0)
        except (KeyboardInterrupt, EOFError):
            print('\nAborted.')
            sys.exit(0)

    log(f'offline-ready.py started (dry_run={_dry_run})')

    # Execute steps
    errors = 0

    step_create_offline_markers()

    if not step_create_testing_flag():
        errors += 1

    step_modify_config_lockholuser()

    step_remove_external_urls()

    step_set_passwords(password)

    step_disable_vlp_agent()

    if not step_verify_vpodrepo():
        errors += 1

    # Final summary
    print()
    print('=' * 62)
    if _dry_run:
        print('  DRY-RUN COMPLETE - No changes were made')
        print('  Run without --dry-run to apply changes')
    elif errors > 0:
        print(f'  COMPLETED WITH {errors} WARNING(S)')
        print('  Review the output above for details')
        print('  Some warnings may be acceptable (e.g., vpodrepo not yet populated)')
    else:
        print('  PREPARATION COMPLETE')
        print('  The lab is ready for offline/partner export')
    print()
    print(f'  Log file: {LOG_FILE}')
    print('=' * 62)

    log(f'offline-ready.py completed (errors={errors})')

    if _log_fh:
        _log_fh.close()

    sys.exit(1 if errors > 0 and not _dry_run else 0)


if __name__ == '__main__':
    main()
