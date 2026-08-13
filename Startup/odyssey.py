#!/usr/bin/env python3
# odyssey.py - HOLFY27 Core Odyssey Installation Module
# Version 3.5 - 2026-08-13
# Author - Burke Azbill and HOL Core Team
# VMware Odyssey client installation for VLP deployments

import os
import sys
import argparse
import logging
import shutil
import time

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

MODULE_NAME = 'odyssey'
MODULE_DESCRIPTION = 'VMware Odyssey client installation'

# Odyssey Configuration
ODYSSEY_APP_LINUX = 'odyssey-client-linux.AppImage'
ODYSSEY_LAUNCHER_URL = f'https://odyssey.vmware.com/client/{ODYSSEY_APP_LINUX}'
ODYSSEY_SHORTCUT = 'launch_odyssey.desktop'
ODYSSEY_LAUNCHER = 'odyssey-launch.sh'
ODYSSEY_ICON = 'icon-256.png'

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

def cleanup_old_odyssey(lsf, mc, desktop, odyssey_dst):
    """
    Remove existing Odyssey files
    
    :param lsf: lsfunctions module
    :param mc: Main console path
    :param desktop: Desktop path
    :param odyssey_dst: Odyssey destination path
    """
    files_to_remove = [
        f'{mc}/{desktop}/{ODYSSEY_SHORTCUT}',
        f'{mc}/{odyssey_dst}/{ODYSSEY_APP_LINUX}',
        f'/tmp/{ODYSSEY_APP_LINUX}'
    ]
    
    for filepath in files_to_remove:
        if os.path.isfile(filepath):
            try:
                lsf.write_output(f'Removing: {filepath}')
                os.remove(filepath)
            except Exception as e:
                lsf.write_output(f'Could not remove {filepath}: {e}')


def download_odyssey(lsf, proxies):
    """
    Download the Odyssey client
    
    :param lsf: lsfunctions module
    :param proxies: Proxy configuration
    :return: True if downloaded successfully
    """
    import requests
    
    local_path = f'/tmp/{ODYSSEY_APP_LINUX}'
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.get(
                ODYSSEY_LAUNCHER_URL, 
                stream=True, 
                proxies=proxies, 
                timeout=30
            )
            
            if response.status_code == 200:
                with open(local_path, 'wb') as out_file:
                    shutil.copyfileobj(response.raw, out_file)
                
                os.chmod(local_path, 0o755)
                lsf.write_output(f'Downloaded {ODYSSEY_APP_LINUX}')
                return True
            else:
                lsf.write_output(f'Download failed (HTTP {response.status_code})')
        
        except Exception as e:
            lsf.write_output(f'Download attempt {attempt + 1} failed: {e}')
        
        lsf.labstartup_sleep(lsf.sleep_seconds)
    
    return False


def kill_odyssey_processes(lsf, lmcuser):
    """
    Best-effort kill of any running Odyssey process on the console

    A leftover odyssey-client-linux.AppImage or extracted AppRun process
    (from a prior boot, a user launching it manually, or an earlier
    partial extraction attempt) holds the AppImage file open, which
    causes ETXTBSY ("Text file busy") when we try to overwrite or
    re-exec it. pkill returns nonzero when nothing matches, so that's
    not an error - just log the outcome either way.

    :param lsf: lsfunctions module
    :param lmcuser: SSH target for the console, e.g. 'holuser@console'
    """
    for pattern in (ODYSSEY_APP_LINUX, 'squashfs-root/AppRun'):
        result = lsf.ssh(f'pkill -f {pattern}', lmcuser, lsf.password)
        rc = getattr(result, 'returncode', None)
        if rc == 0:
            lsf.write_output(f'Killed running process(es) matching: {pattern}')
        else:
            lsf.write_output(f'No running process matched: {pattern} (rc={rc})')


def install_odyssey_lmc(lsf, mc, desktop, odyssey_dst):
    """
    Install Odyssey on Linux Main Console
    
    :param lsf: lsfunctions module
    :param mc: Main console mount path
    :param desktop: Desktop path
    :param odyssey_dst: Odyssey destination directory
    """
    lmcuser = 'holuser@console'

    # Kill any running Odyssey process before touching the AppImage file -
    # a still-running instance holds it open and causes ETXTBSY below.
    kill_odyssey_processes(lsf, lmcuser)

    # Copy application
    src = f'/tmp/{ODYSSEY_APP_LINUX}'
    dst = f'{mc}/{odyssey_dst}/{ODYSSEY_APP_LINUX}'
    os.system(f'cp {src} {dst}')

    # Give the console's NFS server a moment to fully release the write
    # before we exec this same file below - copying over /lmchol and
    # immediately running it can hit ETXTBSY ("Text file busy") due to
    # NFS close-to-open consistency lag.
    time.sleep(5)

    # Copy launcher script
    launcher_src = f'{lsf.holroot}/Tools/{ODYSSEY_LAUNCHER}'
    launcher_dst = f'{mc}/{odyssey_dst}/{ODYSSEY_LAUNCHER}'
    if os.path.isfile(launcher_src):
        os.system(f'cp {launcher_src} {launcher_dst}')
    
    # Copy icon
    icon_src = f'{lsf.holroot}/Tools/{ODYSSEY_ICON}'
    icon_dst = f'{mc}/{odyssey_dst}/images/{ODYSSEY_ICON}'
    if os.path.isfile(icon_src):
        os.makedirs(os.path.dirname(icon_dst), exist_ok=True)
        os.system(f'cp {icon_src} {icon_dst}')
    
    # Copy desktop shortcut
    shortcut_src = f'{lsf.holroot}/Tools/{ODYSSEY_SHORTCUT}'
    shortcut_dst = f'{mc}/{desktop}/{ODYSSEY_SHORTCUT}'
    if os.path.isfile(shortcut_src):
        os.system(f'cp {shortcut_src} {shortcut_dst}')
    
    # Set permissions on console
    lsf.ssh(
        f'/usr/bin/gio set /home/holuser/Desktop/{ODYSSEY_SHORTCUT} metadata::trusted true',
        lmcuser, lsf.password
    )
    lsf.ssh(
        f'/usr/bin/chmod a+x /home/holuser/Desktop/{ODYSSEY_SHORTCUT}',
        lmcuser, lsf.password
    )
    
    # Extract AppImage for Ubuntu 24.04+ (fuse2 not available)
    squashfs_root = '/home/holuser/desktop-hol/squashfs-root'
    extract_script = '/lmchol/tmp/extract.sh'
    with open(extract_script, 'w') as script:
        script.write('#!/bin/sh\n')
        script.write(f'cd /home/holuser/desktop-hol\n')
        script.write(f'./{ODYSSEY_APP_LINUX} --appimage-extract\n')

    # Retry just the extraction a few times on ETXTBSY (rc=126) - that's
    # a transient NFS write/exec race, not a real failure, so it's much
    # cheaper to retry here than to fall back to the full outer
    # cleanup/download/install cycle in main().
    max_extract_attempts = 3
    extract_retry_delay = 3
    extract_result = None
    for extract_attempt in range(1, max_extract_attempts + 1):
        # A running AppRun from a prior attempt holds squashfs-root busy
        # too (both for the rm -rf below and for re-exec of the AppImage).
        kill_odyssey_processes(lsf, lmcuser)

        # Remove any stale extraction from a previous run - appimage-extract
        # will not overwrite an existing squashfs-root (it either errors or
        # extracts into squashfs-root-<pid> instead), leaving AppRun missing
        # from the expected path even though extraction "succeeded".
        lsf.ssh(f'rm -rf {squashfs_root}', lmcuser, lsf.password)

        extract_result = lsf.ssh('/bin/sh /tmp/extract.sh', lmcuser, lsf.password)
        rc = getattr(extract_result, 'returncode', None)
        lsf.write_output(
            f'Odyssey extract attempt {extract_attempt}/{max_extract_attempts}: rc={rc} '
            f'stdout={getattr(extract_result, "stdout", "")!r} '
            f'stderr={getattr(extract_result, "stderr", "")!r}'
        )

        if rc == 0:
            break
        if rc == 126 and extract_attempt < max_extract_attempts:
            lsf.write_output('Text file busy (ETXTBSY) - retrying extraction after brief pause')
            time.sleep(extract_retry_delay)
            continue
        break

    # Fix chrome-sandbox permissions
    sandbox = f'{squashfs_root}/chrome-sandbox'
    lsf.ssh(f'chown root:root {sandbox}', 'root@console', lsf.password)
    lsf.ssh(f'chmod 4755 {sandbox}', 'root@console', lsf.password)


#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for Odyssey module
    
    :param lsf: lsfunctions module (will be imported if None)
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    import requests
    
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    # Initialize dashboard early so we can update it for skip cases
    dashboard = None
    TaskStatus = None
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus as TS
        TaskStatus = TS
        dashboard = StatusDashboard(lsf.lab_sku)
    except Exception:
        pass
    
    # Check if Odyssey is enabled
    odyssey_enabled = lsf.config.getboolean('VPOD', 'odyssey', fallback=False)
    if not odyssey_enabled:
        lsf.write_output('Odyssey not enabled in config.ini - skipping')
        if dashboard and TaskStatus:
            dashboard.update_task('odyssey', 'install', TaskStatus.SKIPPED, 'Not enabled in config')
            dashboard.generate_html()
        return
    
    # Skip during labcheck
    if lsf.labcheck:
        lsf.write_output('Labcheck active - skipping Odyssey install')
        if dashboard and TaskStatus:
            dashboard.update_task('odyssey', 'install', TaskStatus.SKIPPED, 'Labcheck mode')
            dashboard.generate_html()
        return
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    
    # Update status dashboard - running
    if dashboard and TaskStatus:
        dashboard.update_task('odyssey', 'install', TaskStatus.RUNNING)
        dashboard.generate_html()
    
    lsf.write_vpodprogress('Odyssey Install', 'GOOD-8')
    
    #==========================================================================
    # Determine Console Type and Paths
    #==========================================================================
    
    if not lsf.LMC:
        lsf.write_output('Odyssey only supported on Linux Main Console - skipping')
        if dashboard and TaskStatus:
            dashboard.update_task('odyssey', 'install', TaskStatus.SKIPPED, 'Not Linux Main Console')
            dashboard.generate_html()
        return
    
    mc = lsf.mc
    desktop = '/home/holuser/Desktop'
    odyssey_dst = 'home/holuser/desktop-hol'
    
    #==========================================================================
    # Check Cloud Environment
    #==========================================================================
    
    the_cloud = lsf.get_cloudinfo() if hasattr(lsf, 'get_cloudinfo') else 'NOT REPORTED'
    
    if the_cloud == 'NOT REPORTED':
        lsf.write_output('Lab not deployed by VLP - skipping Odyssey')
        if dashboard and TaskStatus:
            dashboard.update_task('odyssey', 'install', TaskStatus.SKIPPED, 'Not VLP deployment')
            dashboard.generate_html()
        return
    
    lsf.write_output(f'Cloud: {the_cloud}')
    
    #==========================================================================
    # Clean Up, Download, Install and Validate Odyssey - retry the full
    # cycle if the AppImage extraction didn't actually produce AppRun
    # (see install_odyssey_lmc - a stale/failed extraction can leave
    # squashfs-root without AppRun even though earlier steps succeeded)
    #==========================================================================

    max_attempts = 5
    retry_delay_seconds = 15
    apprun_path = f'{mc}/{odyssey_dst}/squashfs-root/AppRun'
    extraction_verified = False

    if not dry_run:
        for attempt in range(1, max_attempts + 1):
            lsf.write_output(f'Odyssey install attempt {attempt}/{max_attempts}')

            cleanup_old_odyssey(lsf, mc, desktop, odyssey_dst)

            lsf.write_output('Downloading Odyssey client...')
            proxies = getattr(lsf, 'proxies', {})
            if not download_odyssey(lsf, proxies):
                lsf.write_output('Failed to download Odyssey')
                if attempt < max_attempts:
                    lsf.labstartup_sleep(retry_delay_seconds)
                    continue
                break

            lsf.write_output('Installing Odyssey on console...')
            install_odyssey_lmc(lsf, mc, desktop, odyssey_dst)

            if os.path.isfile(apprun_path):
                lsf.write_output(f'Verified {apprun_path} exists')
                extraction_verified = True
                break

            lsf.write_output(f'{apprun_path} not found after attempt {attempt}/{max_attempts}')
            if attempt < max_attempts:
                lsf.labstartup_sleep(retry_delay_seconds)

        if not extraction_verified:
            lsf.write_output(f'Odyssey installation failed - {apprun_path} not found after {max_attempts} attempts')
            lsf.write_vpodprogress('ODYSSEY FAIL', 'ODYSSEY-FAIL', color='red')
            if dashboard and TaskStatus:
                dashboard.update_task('odyssey', 'install', TaskStatus.FAILED, 'AppRun not found after retries')
                dashboard.generate_html()
            return

    #==========================================================================
    # Verify Installation
    #==========================================================================

    shortcut_path = f'{mc}/{desktop}/{ODYSSEY_SHORTCUT}'

    if os.path.isfile(shortcut_path) or dry_run:
        lsf.write_output('Odyssey installation complete')
        lsf.write_vpodprogress('READY', 'ODYSSEY-READY', color='green')
        
        if dashboard and TaskStatus:
            dashboard.update_task('odyssey', 'install', TaskStatus.COMPLETE)
            dashboard.generate_html()
    else:
        lsf.write_output('Odyssey installation failed - shortcut not created')
        lsf.write_vpodprogress('ODYSSEY FAIL', 'ODYSSEY-FAIL', color='red')
        
        if dashboard and TaskStatus:
            dashboard.update_task('odyssey', 'install', TaskStatus.FAILED, 'Shortcut not created')
            dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ##=========================================================================
    
    # Example: Add custom Odyssey configuration here
    # See prelim.py for detailed examples of common operations
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
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
    
    # Handle legacy arguments
    if args.run_seconds > 0:
        import datetime
        lsf.start_time = datetime.datetime.now() - datetime.timedelta(seconds=args.run_seconds)
    
    if args.labcheck == 'True':
        lsf.labcheck = True
    
    if args.standalone:
        print(f'Running {MODULE_NAME} in standalone mode')
        print(f'Lab SKU: {lsf.lab_sku}')
        print(f'Odyssey enabled: {lsf.config.getboolean("VPOD", "odyssey", fallback=False)}')
        print(f'Dry run: {args.dry_run}')
        print()
    
    main(lsf=lsf, standalone=args.standalone, dry_run=args.dry_run)
