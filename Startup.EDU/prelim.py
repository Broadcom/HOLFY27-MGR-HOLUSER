#!/usr/bin/env python3
# prelim.py - EDU LabType Preliminary Tasks Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# 
# This is the EDU labtype prelim.py which includes all core functionality
# plus EDU-specific customizations.
#
# Override Priority (highest to lowest):
#   1. /vpodrepo/20XX-labs/XXXX/Startup/prelim.py  (Lab-specific override)
#   2. /home/holuser/hol/Startup.EDU/prelim.py     (This file - EDU labtype)
#   3. /home/holuser/hol/Startup/prelim.py         (Default core module)

import os
import sys
import argparse

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'prelim'
MODULE_DESCRIPTION = 'EDU LabType Preliminary Tasks'
LABTYPE = 'EDU'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for EDU prelim module
    
    :param lsf: lsfunctions module
    :param standalone: Whether running in standalone test mode
    :param dry_run: Whether to skip actual changes
    """
    if lsf is None:
        import lsfunctions as lsf
        if not standalone:
            lsf.init(router=False)
    
    ##=========================================================================
    ## Core Team code - do not modify - place custom code in the CUSTOM section
    ##=========================================================================
    
    lsf.write_output(f'Starting {MODULE_NAME}: {MODULE_DESCRIPTION}')
    lsf.write_output(f'*** Running {LABTYPE} LabType Override ***')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard, TaskStatus
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('prelim', 'readme', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    #==========================================================================
    # TASK 1: Copy README to Console
    #==========================================================================
    
    lsf.write_output('Syncing README to console...')
    
    readme_sources = [
        f'{lsf.vpod_repo}/README.txt',
        f'{lsf.vpod_repo}/README.md',
        f'{lsf.holroot}/README.txt'
    ]
    
    readme_dest = f'{lsf.mcdesktop}/README.txt'
    
    if not dry_run:
        for src in readme_sources:
            if os.path.isfile(src):
                try:
                    import shutil
                    shutil.copy(src, readme_dest)
                    lsf.write_output(f'README copied from {src}')
                    break
                except Exception as e:
                    lsf.write_output(f'Could not copy README: {e}')
    
    if dashboard:
        dashboard.update_task('prelim', 'readme', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 2: Prevent Update Manager Banners (on Console via SSH)
    #==========================================================================
    
    lsf.write_output('Preventing update manager popups on console...')
    
    if not dry_run:
        # Disable Ubuntu update notifications and apt-daily timers on the console via SSH
        console_host = 'root@console.site-a.vcf.lab'
        
        # Disable update-notifier autostart
        update_notifier = '/etc/xdg/autostart/update-notifier.desktop'
        disable_notifier_cmd = f'test -f {update_notifier} && mv {update_notifier} {update_notifier}.disabled || true'
        result = lsf.ssh(disable_notifier_cmd, console_host)
        if result.returncode == 0:
            lsf.write_output('Disabled update-notifier autostart on console')
        else:
            lsf.write_output(f'Could not disable update-notifier on console: {result.stderr}')
        
        # Disable apt-daily timers to prevent automatic updates
        disable_timers_cmd = 'systemctl disable --now apt-daily.timer apt-daily-upgrade.timer'
        result = lsf.ssh(disable_timers_cmd, console_host)
        if result.returncode == 0:
            lsf.write_output('Disabled apt-daily timers on console')
        else:
            lsf.write_output(f'Could not disable apt-daily timers on console: {result.stderr}')
    
    #==========================================================================
    # TASK 3: Firewall Verification
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'firewall', 'running')
        dashboard.generate_html()
    
    from labtypes import LabTypeLoader
    loader = LabTypeLoader(lsf.labtype, lsf.holroot, lsf.vpod_repo)
    
    if loader.requires_firewall():
        lsf.write_output(f'Verifying firewall status ({LABTYPE} lab)...')
        
        if not dry_run:
            # Check if router is reachable
            if lsf.test_ping('router'):
                lsf.write_output('Router is reachable')
                lsf.write_output('Firewall verification passed')
            else:
                lsf.write_output('WARNING: Router not reachable for firewall check')
    else:
        lsf.write_output(f'Firewall not required for {lsf.labtype} lab type')
    
    if dashboard:
        dashboard.update_task('prelim', 'firewall', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 4: Clean Previous Odyssey Files
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'odyssey_cleanup', 'running')
        dashboard.generate_html()
    
    lsf.write_output('Cleaning previous Odyssey files...')
    
    odyssey_cleanup = [
        f'{lsf.lmcholroot}/odyssey_installed',
        f'{lsf.lmcholroot}/odyssey_error',
        '/tmp/odyssey.tar.gz'
    ]
    
    if not dry_run:
        for f in odyssey_cleanup:
            if os.path.isfile(f):
                try:
                    os.remove(f)
                    lsf.write_output(f'Removed {f}')
                except Exception:
                    pass
    
    if dashboard:
        dashboard.update_task('prelim', 'odyssey_cleanup', 'complete')
        dashboard.generate_html()
    
    ##=========================================================================
    ## End Core Team code
    ##=========================================================================
    
    ##=========================================================================
    ## CUSTOM - EDU LabType Specific Code
    ## 
    ## EDU (Education) labs have:
    ## - Firewall enabled
    ## - Proxy filtering enabled
    ## - Training environments
    ##
    ## Add your EDU-specific customizations below.
    ##=========================================================================
    
    lsf.write_output(f'{LABTYPE} Lab SKU: {lsf.lab_sku}')
    lsf.write_output(f'{LABTYPE} (Education) labs have full security features')
    
    ## Example 1: Check URL accessibility
    ## ----------------------------------
    # url_to_check = 'https://vcsa.site-a.vcf.lab/ui/'
    # if lsf.test_url(url_to_check, verify_ssl=False, timeout=30):
    #     lsf.write_output(f'URL is accessible: {url_to_check}')
    # else:
    #     lsf.write_output(f'URL check failed: {url_to_check}')
    
    ## Example 2: Check for expired password and reset
    ## ------------------------------------------------
    # target_host = 'root@gitlab.site-a.vcf.lab'
    # result = lsf.ssh('chage -l root | grep "Password expires"', target_host)
    # if hasattr(result, 'stdout') and 'password must be changed' in result.stdout.lower():
    #     lsf.write_output(f'Password expired on {target_host}, resetting...')
    #     new_password = lsf.get_password()
    #     lsf.ssh(f'echo "root:{new_password}" | chpasswd', target_host)
    
    ## Example 3: Copy file to remote system via SCP
    ## ----------------------------------------------
    # local_file = f'{lsf.vpod_repo}/files/custom-config.conf'
    # remote_dest = 'root@web-server.site-a.vcf.lab:/etc/myapp/'
    # if os.path.isfile(local_file):
    #     result = lsf.scp(local_file, remote_dest)
    #     lsf.write_output(f'SCP result: {result.returncode}')
    
    ## Example 4: Check if service is running
    ## --------------------------------------
    # target_host = 'root@harbor.site-a.vcf.lab'
    # result = lsf.ssh('systemctl is-active docker', target_host)
    # if hasattr(result, 'stdout') and 'active' in result.stdout.strip():
    #     lsf.write_output('Docker service is running')
    
    ## Example 5: Execute remote command and process output
    ## -----------------------------------------------------
    # target_host = 'root@k8s-master.site-a.vcf.lab'
    # result = lsf.ssh('kubectl get nodes -o wide', target_host)
    # if result.returncode == 0 and hasattr(result, 'stdout'):
    #     for line in result.stdout.split('\n'):
    #         if 'NotReady' in line:
    #             lsf.write_output(f'WARNING: Node not ready: {line}')
    
    ## Example 6: Run Ansible Playbook
    ## --------------------------------
    # playbook_path = f'{lsf.vpod_repo}/ansible/site.yml'
    # if os.path.isfile(playbook_path):
    #     result = lsf.run_ansible_playbook(playbook_path)
    #     lsf.write_output(f'Ansible result: {result.returncode}')
    
    ## Example 7: Run Salt Configuration
    ## ----------------------------------
    # result = lsf.run_salt_from_repo('webserver', test_mode=False)
    # lsf.write_output(f'Salt result: {result.returncode}')
    
    ## Example 8: Run Custom Script
    ## ----------------------------
    # result = lsf.run_repo_script('setup.sh')
    # lsf.write_output(f'Script result: {result.returncode}')
    
    ## Example: Fail the lab if critical condition not met
    ## ----------------------------------------------------
    # lsf.labfail('EDU PRELIM ISSUE - Critical check failed')
    # exit(1)
    
    ##=========================================================================
    ## End CUSTOM section
    ##=========================================================================
    
    #==========================================================================
    # COMPLETE
    #==========================================================================
    
    lsf.write_output(f'{MODULE_NAME} completed successfully')


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
    
    args = parser.parse_args()
    
    import lsfunctions as lsf
    
    if not args.skip_init:
        lsf.init(router=False)
    
    print(f'Running {LABTYPE} {MODULE_NAME} in standalone mode')
    print(f'Lab SKU: {lsf.lab_sku}')
    print(f'LabType: {lsf.labtype}')
    print(f'Dry run: {args.dry_run}')
    print()
    
    main(lsf=lsf, standalone=True, dry_run=args.dry_run)
