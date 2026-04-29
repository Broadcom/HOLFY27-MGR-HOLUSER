#!/usr/bin/env python3
"""
TODO: This script is a work in progress. It is not yet complete.
VERSION: 0.0.1 - 2026-04-27
AUTHOR: Burke Azbill and HOL Core Team

Automate VCF Operations UI for HOL Authentik Cycle 7 Step 3 (VCF SSO Overview).

1) **Prerequisites** tab — check all acknowledgement boxes and **Submit** (HOL Step 3).
2) **Configure SSO** on the Get Started tab — opens the **Configure VCF SSO** wizard
   (`…/sso-overview/initial-setup`). Fleet IAM REST calls do not perform this UI step; without
   it, operators only see marketing copy and think nothing is configured.

Requires: pip install playwright && playwright install chromium
Optional: set HOL_PLAYWRIGHT_PYTHON to a venv interpreter that has playwright if the system
Python is PEP-668 locked.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional

OPS_LOGIN = '/ui/login.action'
PREREQ_PATH = (
    '/vcf-operations/ui/manage/fleet/identity-and-access/sso-overview/prerequisites'
)
FALLBACK_PATH = (
    '/vcf-operations/ui/manage/fleet/identity-and-access/sso-overview/get-started'
)


def _log(write: Optional[Callable[[str], None]], msg: str) -> None:
    if write:
        write(msg)
    else:
        print(msg)


def _playwright_python() -> str:
    return os.environ.get('HOL_PLAYWRIGHT_PYTHON', sys.executable)


def submit_sso_prerequisites_ui(
    ops_fqdn: str,
    password: str,
    write: Optional[Callable[[str], None]] = None,
    dry_run: bool = False,
    username: str = 'admin',
) -> bool:
    """
    Log into VCF Operations, complete SSO **Prerequisites**, then click **Configure SSO**
    (opens the deployment-mode wizard per HOL_Authentik_Config_Cycle_7.md Step 3).

    ops_fqdn: e.g. ops-a.site-a.vcf.lab (no scheme)
    """
    if dry_run:
        _log(
            write,
            '  SSO UI: DRY-RUN would complete prerequisites + Configure SSO on '
            f'https://{ops_fqdn}',
        )
        return True

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log(
            write,
            '  SSO UI: playwright is not installed. Example: '
            'python3 -m venv /tmp/pw-venv && /tmp/pw-venv/bin/pip install playwright && '
            '/tmp/pw-venv/bin/playwright install chromium && '
            'export HOL_PLAYWRIGHT_PYTHON=/tmp/pw-venv/bin/python3',
        )
        return False

    base = f'https://{ops_fqdn.rstrip("/")}'
    shot = '/tmp/vcf-sso-prereqs-failure.png'

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--ignore-certificate-errors', '--no-sandbox'],
        )
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            _log(write, f'  SSO UI: logging in to {base}{OPS_LOGIN} …')
            page.goto(base + OPS_LOGIN, wait_until='domcontentloaded', timeout=120000)
            page.wait_for_timeout(2000)
            # Auth source (e.g. Local / vsphere.local) when shown as a select
            try:
                sel = page.locator('select').first
                if sel.count() and sel.is_visible():
                    opts = sel.locator('option').all_text_contents()
                    for label in ('local', 'Local', 'LOCAL', 'vsphere'):
                        for i, txt in enumerate(opts):
                            if label.lower() in txt.lower():
                                sel.select_option(index=i)
                                break
            except Exception:
                pass
            # vROps / Aria: username + password fields vary by skin — prefer role-based fills
            user_filled = False
            for sel in (
                'input[name="j_username"]',
                'input#username',
                'input[formcontrolname="username"]',
                'input[type="text"]',
            ):
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.fill(username)
                    user_filled = True
                    break
            if not user_filled:
                gl = page.get_by_label(re.compile('user|login', re.I))
                if gl.count():
                    gl.first.fill(username)
                else:
                    raise RuntimeError('Could not find username field on login page')

            for sel in ('input[name="j_password"]', 'input#password', 'input[type="password"]'):
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.fill(password)
                    break
            else:
                gl = page.get_by_label(re.compile('password', re.I))
                if gl.count():
                    gl.first.fill(password)
                else:
                    raise RuntimeError('Could not find password field on login page')

            clicked = False
            for name_pat in (r'Log\s*In', r'Sign\s*In', r'Login', r'Submit'):
                btn = page.get_by_role('button', name=re.compile(name_pat, re.I))
                if btn.count():
                    btn.first.click()
                    clicked = True
                    break
            if not clicked:
                page.locator('button[type="submit"]').first.click()

            page.wait_for_load_state('networkidle', timeout=180000)
            # Prefer Get Started URL + "Prerequisites" tab (matches HOL Step 3 wording).
            _log(write, f'  SSO UI: opening VCF SSO Overview {FALLBACK_PATH} …')
            page.goto(base + FALLBACK_PATH, wait_until='domcontentloaded', timeout=120000)
            page.wait_for_timeout(2000)
            pre_tab = page.get_by_role('tab', name=re.compile('prerequisite', re.I))
            if pre_tab.count():
                pre_tab.first.click()
                page.wait_for_timeout(1500)
            else:
                _log(write, f'  SSO UI: no Prerequisites tab — trying direct URL {PREREQ_PATH} …')
                r = page.goto(base + PREREQ_PATH, wait_until='domcontentloaded', timeout=120000)
                if r and r.status >= 400:
                    _log(write, f'  SSO UI: prerequisites URL HTTP {r.status}.')
                page.wait_for_timeout(2000)
                link = page.get_by_role('link', name=re.compile('prerequisite', re.I))
                if link.count():
                    link.first.click()
                    page.wait_for_timeout(1500)

            page.wait_for_timeout(2000)
            boxes = page.locator('input[type="checkbox"]:visible')
            n = boxes.count()
            if n == 0:
                _log(write, '  SSO UI: no visible checkboxes (prerequisites may already be done).')
            else:
                for i in range(n):
                    boxes.nth(i).check(force=True)
                _log(write, f'  SSO UI: checked {n} prerequisite checkbox(es).')

                submitted = False
                for name_pat in (r'Submit', r'Continue', r'Next', r'Save'):
                    btn = page.get_by_role('button', name=re.compile(name_pat, re.I))
                    if btn.count():
                        btn.first.click()
                        submitted = True
                        break
                if not submitted:
                    st = page.locator('button:has-text("SUBMIT"), button:has-text("Submit")')
                    if st.count():
                        st.first.click()
                    else:
                        _log(write, '  SSO UI: WARNING: no Submit button for prerequisites.')
                try:
                    page.wait_for_load_state('networkidle', timeout=120000)
                except Exception:
                    pass
                _log(write, '  SSO UI: prerequisites submit completed.')

            # HOL Step 3: return to Get Started and launch the Configure VCF SSO wizard.
            gs_tab = page.get_by_role('tab', name=re.compile(r'Get Started with SSO', re.I))
            if gs_tab.count():
                gs_tab.first.click()
                page.wait_for_timeout(1500)
            configure = page.get_by_role('button', name=re.compile(r'Configure SSO', re.I))
            if not configure.count():
                _log(
                    write,
                    '  SSO UI: Configure SSO button not visible — '
                    'already in wizard, SSO disabled, or UI variant; stopping after prerequisites.',
                )
                return True
            configure.first.click()
            page.wait_for_timeout(3000)
            try:
                page.wait_for_url('**/sso-overview/initial-setup**', timeout=120000)
            except Exception:
                pass
            url = page.url
            if 'initial-setup' in url:
                _log(write, '  SSO UI: Configure SSO wizard opened (…/initial-setup).')
            else:
                _log(write, f'  SSO UI: WARNING: expected initial-setup in URL after Configure SSO; got {url!r}')
            return True
        except Exception as e:
            _log(write, f'  SSO UI: FAILED: {e}')
            try:
                page.screenshot(path=shot)
                _log(write, f'  SSO UI: screenshot saved to {shot}')
            except Exception:
                pass
            return False
        finally:
            context.close()
            browser.close()


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description='VCF SSO UI: Prerequisites + Configure SSO wizard entry (Playwright)',
    )
    p.add_argument('--ops-fqdn', default='ops-a.site-a.vcf.lab')
    p.add_argument('--password-file', default='/home/holuser/creds.txt')
    args = p.parse_args()
    pw = open(args.password_file).read().strip()
    ok = submit_sso_prerequisites_ui(args.ops_fqdn, pw, print, dry_run=False)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
