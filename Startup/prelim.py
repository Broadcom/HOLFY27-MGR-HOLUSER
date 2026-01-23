#!/usr/bin/env python3
# prelim.py - HOLFY27 Core Preliminary Tasks Module
# Version 3.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Initial lab startup checks and configuration

import os
import sys
import argparse

# Add hol directory to path
sys.path.insert(0, '/home/holuser/hol')

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

MODULE_NAME = 'prelim'
MODULE_DESCRIPTION = 'Preliminary lab startup checks'

#==============================================================================
# MAIN FUNCTION
#==============================================================================

def main(lsf=None, standalone=False, dry_run=False):
    """
    Main entry point for prelim module
    
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
    
    # Update status dashboard
    try:
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
    # TASK 3: Firewall Verification (HOL labs only)
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'firewall', 'running')
        dashboard.generate_html()
    
    from labtypes import LabTypeLoader
    loader = LabTypeLoader(lsf.labtype, lsf.holroot, lsf.vpod_repo)
    
    if loader.requires_firewall():
        lsf.write_output('Verifying firewall status (HOL lab)...')
        
        if not dry_run:
            # Check if router is reachable
            if lsf.test_ping('router'):
                lsf.write_output('Router is reachable')
                
                # Verify firewall indicator file exists on router
                # (This is created by iptablescfg.sh)
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
    ## CUSTOM - Insert your code here using the file in your vPod_repo
    ## 
    ## To customize this module for your lab:
    ## 1. Copy this file to your vpodrepo/Startup/ folder
    ## 2. Uncomment and modify the examples below as needed
    ## 3. Add your custom code in this section
    ##
    ## The examples below demonstrate common operations. Uncomment and modify
    ## as needed for your specific lab requirements.
    ##=========================================================================
    
    ## Example 1: Check URL accessibility
    ## ----------------------------------
    ## Check if a web interface is accessible, optionally verify expected content
    #
    # url_to_check = 'https://vcsa.site-a.vcf.lab/ui/'
    # expected_text = 'VMware vSphere'  # Optional: verify this text appears
    # 
    # if lsf.test_url(url_to_check, expected_text=expected_text, verify_ssl=False, timeout=30):
    #     lsf.write_output(f'URL is accessible: {url_to_check}')
    # else:
    #     lsf.write_output(f'URL check failed: {url_to_check}')
    #     # Optionally fail the lab:
    #     # lsf.labfail(f'Required URL not accessible: {url_to_check}')
    
    ## Example 2: Check for expired password on SSH host and reset
    ## -----------------------------------------------------------
    ## Detect expired password and reset it on a remote Linux system
    #
    # target_host = 'root@gitlab.site-a.vcf.lab'
    # new_password = lsf.get_password()  # Or specify a different password
    # 
    # # Check if password is expired
    # result = lsf.ssh('chage -l root | grep "Password expires"', target_host)
    # if hasattr(result, 'stdout') and 'password must be changed' in result.stdout.lower():
    #     lsf.write_output(f'Password expired on {target_host}, resetting...')
    #     
    #     # Reset password using chpasswd
    #     reset_cmd = f'echo "root:{new_password}" | chpasswd'
    #     reset_result = lsf.ssh(reset_cmd, target_host)
    #     
    #     if reset_result.returncode == 0:
    #         lsf.write_output(f'Password reset successful on {target_host}')
    #     else:
    #         lsf.write_output(f'Password reset failed on {target_host}: {reset_result.stderr}')
    # else:
    #     lsf.write_output(f'Password is valid on {target_host}')
    
    ## Example 3: Copy a file to a remote system over SCP
    ## ---------------------------------------------------
    ## Copy configuration files or scripts to remote systems
    #
    # local_file = f'{lsf.vpod_repo}/files/custom-config.conf'
    # remote_dest = 'root@web-server.site-a.vcf.lab:/etc/myapp/config.conf'
    # 
    # if os.path.isfile(local_file):
    #     result = lsf.scp(local_file, remote_dest, recursive=False)
    #     if result.returncode == 0:
    #         lsf.write_output(f'Successfully copied {local_file} to {remote_dest}')
    #     else:
    #         lsf.write_output(f'SCP failed: {result.stderr}')
    # else:
    #     lsf.write_output(f'Source file not found: {local_file}')
    #
    # # For copying directories recursively:
    # # result = lsf.scp(local_dir, remote_dest, recursive=True)
    
    ## Example 4: Confirm if a service is running
    ## ------------------------------------------
    ## Check if a systemd service is running on a remote host
    #
    # target_host = 'root@harbor.site-a.vcf.lab'
    # service_name = 'docker'
    # 
    # result = lsf.ssh(f'systemctl is-active {service_name}', target_host)
    # if hasattr(result, 'stdout') and 'active' in result.stdout.strip():
    #     lsf.write_output(f'Service {service_name} is running on {target_host}')
    # else:
    #     lsf.write_output(f'Service {service_name} is NOT running on {target_host}')
    #     
    #     # Optionally start the service:
    #     # start_result = lsf.ssh(f'systemctl start {service_name}', target_host)
    #     # if start_result.returncode == 0:
    #     #     lsf.write_output(f'Started {service_name} on {target_host}')
    
    ## Example 5: Execute remote command over SSH, capture output, and process
    ## ------------------------------------------------------------------------
    ## Run a command remotely and process the output
    #
    # target_host = 'root@k8s-master.site-a.vcf.lab'
    # command = 'kubectl get nodes -o wide'
    # 
    # result = lsf.ssh(command, target_host)
    # if result.returncode == 0 and hasattr(result, 'stdout'):
    #     lsf.write_output(f'Command output from {target_host}:')
    #     
    #     # Process output line by line
    #     for line in result.stdout.split('\n'):
    #         if line.strip():
    #             lsf.write_output(f'  {line}')
    #             
    #             # Example: Check for specific conditions
    #             if 'NotReady' in line:
    #                 node_name = line.split()[0]
    #                 lsf.write_output(f'WARNING: Node {node_name} is not ready!')
    # else:
    #     lsf.write_output(f'Command failed on {target_host}: {result.stderr}')
    
    ## Example 6: Run Ansible Playbook
    ## --------------------------------
    ## Execute an Ansible playbook from the vpodrepo
    #
    # playbook_path = f'{lsf.vpod_repo}/ansible/site.yml'
    # inventory = f'{lsf.vpod_repo}/ansible/inventory.ini'
    # extra_vars = {'lab_sku': lsf.lab_sku, 'password': lsf.get_password()}
    # 
    # if os.path.isfile(playbook_path):
    #     result = lsf.run_ansible_playbook(
    #         playbook_path,
    #         inventory=inventory,
    #         extra_vars=extra_vars
    #     )
    #     if result.returncode == 0:
    #         lsf.write_output('Ansible playbook completed successfully')
    #     else:
    #         lsf.write_output(f'Ansible playbook failed: {result.stderr}')
    # 
    # # Alternatively, use the helper to search in standard locations:
    # # result = lsf.run_ansible_from_repo('site.yml')
    
    ## Example 7: Run Salt Configuration
    ## ----------------------------------
    ## Execute a Salt state from the vpodrepo
    #
    # state_name = 'webserver'  # Will look for webserver.sls in vpodrepo/salt/
    # 
    # try:
    #     result = lsf.run_salt_from_repo(state_name, test_mode=False)
    #     if result.returncode == 0:
    #         lsf.write_output(f'Salt state {state_name} applied successfully')
    #     else:
    #         lsf.write_output(f'Salt state {state_name} failed: {result.stderr}')
    # except FileNotFoundError as e:
    #     lsf.write_output(f'Salt state not found: {e}')
    # 
    # # Or run with test mode to see what would change:
    # # result = lsf.run_salt_from_repo('webserver', test_mode=True)
    
    ## Example 8: Run Custom Script
    ## ----------------------------
    ## Execute a custom script from the vpodrepo (auto-detects type by extension)
    #
    # # Run a bash script
    # script_name = 'setup.sh'
    # script_path = f'{lsf.vpod_repo}/scripts/{script_name}'
    # 
    # if os.path.isfile(script_path):
    #     result = lsf.run_command(f'/bin/bash {script_path}')
    #     if result.returncode == 0:
    #         lsf.write_output(f'Script {script_name} completed successfully')
    #         if result.stdout:
    #             lsf.write_output(f'Output: {result.stdout}')
    #     else:
    #         lsf.write_output(f'Script {script_name} failed: {result.stderr}')
    # 
    # # Or use the universal script runner (auto-detects: .sh, .py, .yml, .sls):
    # # result = lsf.run_repo_script('configure.sh')
    # # result = lsf.run_repo_script('setup.py', script_type='python')
    # # result = lsf.run_repo_script('playbook.yml', script_type='ansible')
    
    ## Example: Fail the lab if critical condition not met
    ## ----------------------------------------------------
    # lsf.labfail('PRELIM ISSUE - Critical check failed')
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
    
    print(f'Running {MODULE_NAME} in standalone mode')
    print(f'Lab SKU: {lsf.lab_sku}')
    print(f'LabType: {lsf.labtype}')
    print(f'Dry run: {args.dry_run}')
    print()
    
    main(lsf=lsf, standalone=True, dry_run=args.dry_run)
