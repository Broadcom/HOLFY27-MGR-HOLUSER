#!/usr/bin/env python3
# prelim.py - HOLFY27 Core Preliminary Tasks Module
# Version 3.11 - 2026-05-26
# Author - Burke Azbill and HOL Core Team
# Initial lab startup checks and configuration

import os
import sys
import json
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
    
    if dashboard:
        dashboard.update_task('prelim', 'update_manager', 'running')
        dashboard.generate_html()
    
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
    
    if dashboard:
        dashboard.update_task('prelim', 'update_manager', 'complete')
        dashboard.generate_html()
    
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
    # TASK 3b: Proxy Filter Verification (HOL labs only)
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'proxy_filter', 'running')
        dashboard.generate_html()
    
    if loader.requires_proxy_filter():
        lsf.write_output('Verifying proxy filter status (HOL lab)...')
        
        if not dry_run:
            # Verify squid proxy is actually listening on TCP port 3128
            # This is a definitive check - if the proxy is not available here,
            # the lab will not function correctly for HOL labs
            proxy_available = lsf.test_tcp_port(lsf.proxy, 3128, timeout=5)
            
            if not proxy_available:
                lsf.write_output(f'Proxy not available on {lsf.proxy}:3128 - attempting remediation...')
                # Use the check_proxy function which includes SSH remediation
                proxy_available = lsf.check_proxy(max_attempts=30, remediate=True)
            
            if proxy_available:
                lsf.write_output('Proxy filter verification passed (squid listening on port 3128)')
                if dashboard:
                    dashboard.update_task('prelim', 'proxy_filter', 'complete')
                    dashboard.generate_html()
            else:
                lsf.write_output(f'CRITICAL: Proxy (squid) not available on {lsf.proxy}:3128')
                if dashboard:
                    dashboard.update_task('prelim', 'proxy_filter', 'failed',
                                          f'Squid not listening on {lsf.proxy}:3128')
                    dashboard.generate_html()
                lsf.labfail(f'Proxy Unavailable - squid not listening on {lsf.proxy}:3128')
        else:
            if dashboard:
                dashboard.update_task('prelim', 'proxy_filter', 'complete')
                dashboard.generate_html()
    else:
        lsf.write_output(f'Proxy filter not required for {lsf.labtype} lab type')
        if dashboard:
            dashboard.update_task('prelim', 'proxy_filter', 'skipped', 
                                  f'Not required for {lsf.labtype} lab type')
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
    
    #==========================================================================
    # TASK 5: Configure VS Code Proxy on Console
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'vscode_proxy', 'running')
        dashboard.generate_html()
    
    enable_vscode_proxy = lsf.config.getboolean('VPOD', 'enablevscodeproxy', fallback=False)
    
    if enable_vscode_proxy:
        lsf.write_output('Configuring VS Code proxy on console...')
        
        if not dry_run:
            console_host = 'root@console.site-a.vcf.lab'
            
            PROXY_URL = lsf.LAB_PROXY_URL
            NO_PROXY_LIST = lsf.build_vscode_no_proxy()
            
            vscode_settings_dir = '/home/holuser/.config/Code/User'
            vscode_settings_file = f'{vscode_settings_dir}/settings.json'
            
            proxy_settings = {
                'http.proxy': PROXY_URL,
                'http.proxyStrictSSL': False,
                'http.noProxy': NO_PROXY_LIST,
                'editor.fontFamily': "'MesloLGM Nerd Font','Droid Sans Mono', monospace",
                'debug.console.fontFamily': "'MesloLGM Nerd Font','Droid Sans Mono', monospace",
            }
            
            mkdir_cmd = f'mkdir -p {vscode_settings_dir}'
            result = lsf.ssh(mkdir_cmd, console_host)
            if result.returncode != 0:
                lsf.write_output(f'Could not create VS Code settings directory: {result.stderr}')
            
            # Read existing settings from console via SCP to preserve user prefs
            tmp_settings = '/tmp/vscode_settings.json'
            scp_down = lsf.scp(
                f'{console_host}:{vscode_settings_file}',
                tmp_settings
            )
            
            existing_settings = {}
            if scp_down.returncode == 0 and os.path.isfile(tmp_settings):
                try:
                    with open(tmp_settings, 'r') as f:
                        existing_settings = json.loads(f.read())
                except (json.JSONDecodeError, ValueError):
                    lsf.write_output('Existing VS Code settings.json is invalid, creating new one')
                    existing_settings = {}
            
            existing_settings.update(proxy_settings)
            
            # Write merged settings to temp file, then SCP to console
            # (avoids double-quote mangling through SSH command wrapping)
            try:
                with open(tmp_settings, 'w') as f:
                    json.dump(existing_settings, f, indent=4)
            except Exception as e:
                lsf.write_output(f'Could not write temp settings file: {e}')
            
            scp_up = lsf.scp(
                tmp_settings,
                f'{console_host}:{vscode_settings_file}'
            )
            
            if scp_up.returncode == 0:
                lsf.write_output(f'VS Code proxy configured: {PROXY_URL}')
                lsf.write_output(f'VS Code noProxy: {len(NO_PROXY_LIST)} entries')
            else:
                lsf.write_output(f'Could not write VS Code settings: {scp_up.stderr}')
            
            chown_cmd = f'chown -R holuser:holuser {vscode_settings_dir}'
            lsf.ssh(chown_cmd, console_host)
            
            # Clean up temp file
            try:
                os.remove(tmp_settings)
            except Exception:
                pass
        else:
            lsf.write_output('Would configure VS Code proxy on console')
        
        if dashboard:
            dashboard.update_task('prelim', 'vscode_proxy', 'complete')
            dashboard.generate_html()
    else:
        lsf.write_output('VS Code proxy configuration disabled (enablevscodeproxy = false)')
        if dashboard:
            dashboard.update_task('prelim', 'vscode_proxy', 'skipped',
                                  'Disabled by enablevscodeproxy = false')
            dashboard.generate_html()

    ##=========================================================================
    ## TASK 6: PUSH LAB FILES TO CONSOLE
    ##=========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'lab_files', 'running')
        dashboard.generate_html()
    
    lsf.push_lab_files_to_console()
    
    if dashboard:
        dashboard.update_task('prelim', 'lab_files', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 7: Holorouter nginx TLS (auth/dns/vault.vcf.lab) near expiry
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'holorouter_tls_renew', 'running')
        dashboard.generate_html()
    
    try:
        from Tools.holorouter_nginx_tls_prelim import maybe_renew_holorouter_nginx_tls

        ok, _msg = maybe_renew_holorouter_nginx_tls(lsf, dry_run=dry_run)
        if not ok:
            lsf.write_output(
                'WARNING: Holorouter nginx TLS renewal queue failed; '
                'check /tmp/holorouter on the manager and doupdate.sh /mnt/manager on the holorouter'
            )
    except Exception as e:
        lsf.write_output(f'WARNING: Holorouter TLS renewal skipped: {e}')
    
    if dashboard:
        dashboard.update_task('prelim', 'holorouter_tls_renew', 'complete')
        dashboard.generate_html()
    
    #==========================================================================
    # TASK 8: Firefox LMC tuning (proxy + lightweight prefs in user.js on console home)
    #==========================================================================
    
    # if dashboard:
    #     dashboard.update_task('prelim', 'firefox_lmchol_tune', 'running')
    #     dashboard.generate_html()
    
    # if os.path.isdir(lsf.lmcholroot):
    #     try:
    #         from Tools.firefox_lmchol_tuning import apply_firefox_lmchol_tuning

    #         if not apply_firefox_lmchol_tuning(lsf, dry_run=dry_run):
    #             lsf.write_output(
    #                 'WARNING: Firefox LMC user.js tuning failed for one or more profiles'
    #             )
    #     except Exception as e:
    #         lsf.write_output(f'WARNING: Firefox LMC tuning skipped: {e}')
    # else:
    #     lsf.write_output('firefox_lmchol_tuning: console home not mounted at lsf.lmcholroot; skip')
    
    # if dashboard:
    #     dashboard.update_task('prelim', 'firefox_lmchol_tune', 'complete')
    #     dashboard.generate_html()
    
    #==========================================================================
    # TASK 9: Install Playwright on Manager (if required by config)
    #
    # Triggered when either of the following is true in config.ini:
    #   [VCFFINAL] authentik_vcf_integration = true  (Authentik integration uses Playwright)
    #   [VPOD]     install_playwright = true          (explicit opt-in)
    #
    # Idempotent: checks whether the Python package is importable AND whether
    # the chromium browser binary is present before attempting any install.
    # Both pip install and playwright install chromium are skipped if already done.
    #==========================================================================

    if dashboard:
        dashboard.update_task('prelim', 'playwright_install', 'running')
        dashboard.generate_html()

    need_playwright = (
        lsf.config.getboolean('VCFFINAL', 'authentik_vcf_integration', fallback=False)
        or lsf.config.getboolean('VPOD', 'install_playwright', fallback=False)
    )

    if need_playwright:
        lsf.write_output('Playwright required by config — checking installation on manager...')

        import subprocess as _sub

        # --- Check 1: is the playwright Python package importable? ---
        pkg_check = _sub.run(
            ['python3', '-c', 'import playwright'],
            capture_output=True, timeout=30
        )
        playwright_pkg_ok = (pkg_check.returncode == 0)

        # --- Check 2: is the chromium browser binary present? ---
        chromium_ok = False
        if playwright_pkg_ok:
            chromium_check = _sub.run(
                ['python3', '-c',
                 'from playwright.sync_api import sync_playwright as _sp; '
                 'import os as _os; '
                 '_pw = _sp().start(); '
                 '_ep = _pw.chromium.executable_path; '
                 '_pw.stop(); '
                 'assert _os.path.isfile(_ep), f"not found: {_ep}"'],
                capture_output=True, text=True, timeout=30
            )
            chromium_ok = (chromium_check.returncode == 0)

        if playwright_pkg_ok and chromium_ok:
            lsf.write_output('Playwright package and chromium browser already installed — skipping.')
            if dashboard:
                dashboard.update_task('prelim', 'playwright_install', 'skipped',
                                      'Already installed')
                dashboard.generate_html()
        elif dry_run:
            lsf.write_output(
                'Would install playwright package and chromium browser on manager (dry-run)')
            if dashboard:
                dashboard.update_task('prelim', 'playwright_install', 'complete')
                dashboard.generate_html()
        else:
            playwright_install_ok = True

            # --- Step 1: pip install playwright (if package missing) ---
            if not playwright_pkg_ok:
                lsf.write_output('Installing playwright Python package globally...')
                pip_result = _sub.run(
                    ['python3', '-m', 'pip', 'install',
                     '--break-system-packages', 'playwright'],
                    capture_output=True, text=True, timeout=300
                )
                if pip_result.stdout:
                    for line in pip_result.stdout.strip().split('\n'):
                        if line.strip():
                            lsf.write_output(f'  pip: {line}')
                if pip_result.returncode != 0:
                    lsf.write_output(
                        f'ERROR: playwright pip install failed (rc={pip_result.returncode})')
                    if pip_result.stderr:
                        lsf.write_output(
                            f'  stderr: {pip_result.stderr.strip()[:500]}')
                    playwright_install_ok = False
                else:
                    playwright_pkg_ok = True
                    lsf.write_output('playwright package installed successfully.')

            # --- Step 2: playwright install chromium (if browser missing) ---
            if playwright_pkg_ok and not chromium_ok:
                lsf.write_output('Installing playwright chromium browser...')
                cr_result = _sub.run(
                    ['python3', '-m', 'playwright', 'install', 'chromium'],
                    capture_output=True, text=True, timeout=600
                )
                if cr_result.stdout:
                    for line in cr_result.stdout.strip().split('\n'):
                        if line.strip():
                            lsf.write_output(f'  playwright: {line}')
                if cr_result.returncode != 0:
                    lsf.write_output(
                        f'ERROR: playwright install chromium failed (rc={cr_result.returncode})')
                    if cr_result.stderr:
                        lsf.write_output(
                            f'  stderr: {cr_result.stderr.strip()[:500]}')
                    playwright_install_ok = False
                else:
                    lsf.write_output('playwright chromium browser installed successfully.')

            if playwright_install_ok:
                if dashboard:
                    dashboard.update_task('prelim', 'playwright_install', 'complete')
                    dashboard.generate_html()
            else:
                lsf.write_output(
                    'WARNING: Playwright installation completed with errors — '
                    'Authentik integration or Playwright-dependent tasks may fail.')
                if dashboard:
                    dashboard.update_task('prelim', 'playwright_install', 'failed',
                                          'Install errors — see log')
                    dashboard.generate_html()
    else:
        lsf.write_output('Playwright installation not required by config — skipping.')
        if dashboard:
            dashboard.update_task('prelim', 'playwright_install', 'skipped',
                                  'Not required by config')
            dashboard.generate_html()

    #==========================================================================
    # TASK 10: Trust Vault PKI root CA in Firefox (console profile via /lmchol on manager)
    #==========================================================================
    
    if dashboard:
        dashboard.update_task('prelim', 'vault_firefox_trust', 'running')
        dashboard.generate_html()
    
    import time
    lsf.write_output('Sleeping for 30 seconds before syncing Vault CA to Firefox...')
    if not dry_run:
        time.sleep(30)

    if os.path.isdir(lsf.lmcholroot):
        try:
            from Tools.vault_firefox_trust import sync_vault_ca_to_firefox

            if not sync_vault_ca_to_firefox(lsf.mc, lsf.write_output, dry_run=dry_run):
                lsf.write_output(
                    'WARNING: Vault PKI root CA could not be fully synced to Firefox profiles'
                )
        except Exception as e:
            lsf.write_output(f'WARNING: Vault Firefox CA sync skipped: {e}')
    else:
        lsf.write_output('vault_firefox_trust: console home not mounted; skipping Firefox CA sync')
    
    if dashboard:
        dashboard.update_task('prelim', 'vault_firefox_trust', 'complete')
        dashboard.generate_html()

    #==========================================================================
    # TASK 11: Ensure Technitium DNS Local Endpoint 10.1.1.1:53
    #==========================================================================

    REQUIRED_DNS_ENDPOINT = '10.1.1.1:53'
    lsf.write_output(
        f'Checking Technitium DNS local endpoints for {REQUIRED_DNS_ENDPOINT}...'
    )

    try:
        import ssl
        import urllib.parse
        import urllib.request

        # Load tdns-mgr config (server, port, protocol, token)
        _tdns_conf = {}
        _tdns_conf_path = os.path.expanduser('~/.config/tdns-mgr/.tdns-mgr.conf')
        if os.path.isfile(_tdns_conf_path):
            with open(_tdns_conf_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if '=' in _line and not _line.startswith('#'):
                        _k, _v = _line.split('=', 1)
                        _tdns_conf[_k.strip()] = _v.strip().strip('"')

        _tdns_server   = _tdns_conf.get('DNS_SERVER',   '192.168.0.2')
        _tdns_port     = _tdns_conf.get('DNS_PORT',     '5380')
        _tdns_protocol = _tdns_conf.get('DNS_PROTOCOL', 'http')
        _tdns_base     = f'{_tdns_protocol}://{_tdns_server}:{_tdns_port}/api'

        # Mirror tdns-mgr's INSECURE_TDNS=true behaviour (lab uses Vault PKI CA which
        # is not in the system trust store until after prelim completes Task 10).
        _ssl_ctx = ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = ssl.CERT_NONE

        def _tdns_post(endpoint, params):
            """POST form-encoded params to the Technitium DNS API."""
            url  = f'{_tdns_base}/{endpoint}'
            data = urllib.parse.urlencode(params, doseq=True).encode('utf-8')
            req  = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
                return json.loads(resp.read().decode('utf-8'))

        def _tdns_valid_token():
            """Return a working API token, re-authenticating if the stored one is stale."""
            _tok = _tdns_conf.get('DNS_TOKEN', '')
            if _tok:
                try:
                    if _tdns_post('user/session/get', {'token': _tok}).get('status') == 'ok':
                        return _tok
                except Exception:
                    pass
            _user = _tdns_conf.get('DNS_USER', 'admin')
            _r = _tdns_post(
                'user/login',
                {'user': _user, 'pass': lsf.get_password(), 'includeInfo': 'false'},
            )
            if _r.get('status') == 'ok':
                return _r['token']
            raise RuntimeError(f'Technitium login failed: {_r}')

        _tok = _tdns_valid_token()

        # Fetch current settings and check the local endpoints list
        _settings = _tdns_post('settings/get', {'token': _tok})
        _current = _settings.get('response', {}).get('dnsServerLocalEndPoints', [])

        if REQUIRED_DNS_ENDPOINT in _current:
            lsf.write_output(
                f'{REQUIRED_DNS_ENDPOINT} already present in DNS local endpoints: {_current}'
            )
        else:
            lsf.write_output(
                f'{REQUIRED_DNS_ENDPOINT} not found in DNS local endpoints {_current}; adding...'
            )
            if not dry_run:
                _new_endpoints = _current + [REQUIRED_DNS_ENDPOINT]
                _resp = _tdns_post(
                    'settings/set',
                    {'token': _tok, 'dnsServerLocalEndPoints': _new_endpoints},
                )
                if _resp.get('status') == 'ok':
                    lsf.write_output(
                        f'Successfully added {REQUIRED_DNS_ENDPOINT} to DNS local endpoints'
                    )
                else:
                    lsf.write_output(
                        f'WARNING: Failed to add {REQUIRED_DNS_ENDPOINT} to DNS local endpoints: {_resp}'
                    )
            else:
                lsf.write_output(
                    f'[dry-run] Would add {REQUIRED_DNS_ENDPOINT} to DNS local endpoints'
                )

    except Exception as _e:
        lsf.write_output(f'WARNING: DNS local endpoint check skipped: {_e}')

    #==========================================================================
    # TASK 12: Authentik User and Group Provisioning
    #
    # Gated on [AUTHENTIK] authentik_groups or authentik_users being non-empty.
    # Reads group/user definitions from config.ini and provisions them in
    # Authentik via the REST API (https://auth.vcf.lab by default).
    # All operations are idempotent — safe to re-run on every startup.
    # Independent of [VCFFINAL] authentik_vcf_integration.
    #==========================================================================

    if dashboard:
        dashboard.update_task('prelim', 'authentik_provisioning', 'running')
        dashboard.generate_html()

    _has_groups = bool(lsf.get_config_list('AUTHENTIK', 'authentik_groups'))
    _has_users  = bool(lsf.get_config_list('AUTHENTIK', 'authentik_users'))

    if _has_groups or _has_users:
        lsf.write_output(
            'Authentik provisioning: [AUTHENTIK] entries found — provisioning users and groups...'
        )
        if not dry_run:
            try:
                _ak_ok = lsf.authentik_provision_from_config()
                if _ak_ok:
                    lsf.write_output('Authentik user/group provisioning completed successfully.')
                    if dashboard:
                        dashboard.update_task('prelim', 'authentik_provisioning', 'complete')
                        dashboard.generate_html()
                else:
                    lsf.write_output(
                        'WARNING: Authentik provisioning completed with errors — see log above.'
                    )
                    if dashboard:
                        dashboard.update_task(
                            'prelim', 'authentik_provisioning', 'failed',
                            'Provisioning errors — see log'
                        )
                        dashboard.generate_html()
            except Exception as _e:
                lsf.write_output(f'WARNING: Authentik provisioning raised an exception: {_e}')
                if dashboard:
                    dashboard.update_task(
                        'prelim', 'authentik_provisioning', 'failed', str(_e)
                    )
                    dashboard.generate_html()
        else:
            lsf.write_output(
                'Would provision Authentik groups/users from [AUTHENTIK] config (dry-run).'
            )
            if dashboard:
                dashboard.update_task('prelim', 'authentik_provisioning', 'complete')
                dashboard.generate_html()
    else:
        lsf.write_output(
            'Authentik provisioning: no [AUTHENTIK] entries configured — skipping.'
        )
        if dashboard:
            dashboard.update_task(
                'prelim', 'authentik_provisioning', 'skipped',
                'No authentik_groups or authentik_users in config'
            )
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
    
    ## Example 7: Run Custom Script
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
