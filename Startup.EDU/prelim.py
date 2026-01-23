#!/usr/bin/env python3
# prelim.py - EDU LabType Custom Override Module
# Version 1.0 - January 2026
# Author - HOL Core Team
# 
# This is a custom override prelim.py for EDU (Education) labs.
# Place this file in your vpodrepo/Startup/ folder to further customize.
#
# Override Priority (highest to lowest):
#   1. /vpodrepo/20XX-labs/XXXX/Startup/prelim.py  (Lab-specific override)
#   2. /home/holuser/hol/Startup.EDU/prelim.py     (This file - EDU labtype override)
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
MODULE_DESCRIPTION = 'EDU LabType Preliminary Tasks (Custom Override)'
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
    ## EDU LabType Custom Override - prelim.py
    ##=========================================================================
    
    lsf.write_output(f'*** Running {LABTYPE} LabType Custom Override: {MODULE_NAME} ***')
    lsf.write_output(f'Description: {MODULE_DESCRIPTION}')
    
    # Update status dashboard
    try:
        sys.path.insert(0, '/home/holuser/hol/Tools')
        from status_dashboard import StatusDashboard
        dashboard = StatusDashboard(lsf.lab_sku)
        dashboard.update_task('prelim', 'edu_custom', 'running')
        dashboard.generate_html()
    except Exception:
        dashboard = None
    
    ##=========================================================================
    ## CUSTOM CODE SECTION
    ##
    ## Add your EDU-specific preliminary tasks here.
    ## These will run INSTEAD of the default Startup/prelim.py tasks.
    ##
    ## EDU labs have firewall AND proxy filtering enabled (like HOL).
    ## EDU labs are designed for training environments.
    ##
    ## If you want to run the default tasks AND add custom code, you should:
    ## 1. Copy the code from Startup/prelim.py into this file
    ## 2. Add your custom code in the CUSTOM section below
    ##=========================================================================
    
    # Example: Log EDU-specific information
    lsf.write_output(f'EDU Lab SKU: {lsf.lab_sku}')
    lsf.write_output(f'EDU (Education) labs have full security features')
    lsf.write_output(f'EDU labs are training environments')
    
    ##=========================================================================
    ## CUSTOM - Insert your EDU-specific code here
    ##=========================================================================
    
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
    
    if dashboard:
        dashboard.update_task('prelim', 'edu_custom', 'complete')
        dashboard.generate_html()
    
    lsf.write_output(f'{LABTYPE} {MODULE_NAME} completed successfully')


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
