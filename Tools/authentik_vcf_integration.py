#!/usr/bin/env python3
"""
VERSION: 0.1.0 - 2026-05-05
AUTHOR: Burke Azbill and HOL Core Team

Authentik + VCF lab integration (Cycle 7).

Enabled by a single config.ini toggle:  [VCFFINAL] authentik_vcf_integration = true

Steps performed (all idempotent/re-runnable):
  1. CoreDNS forwarder patch on holorouter
  2. Vault CA trust check on VCF Operations (ops-a)
  3. Authentik OAuth2 provider + application (VCF OIDC / VCF)
  4. Authentik OIDC scope mappings (email, openid, profile)
  5. Authentik groups + lab users (prod-admins, dev-admins, etc.)
  6. VCF SSO UI Prerequisites (via Playwright)
  7. Fleet IAM: SSO realm, OIDC+SCIM IdP, SCIM bearer token
  8. Authentik SCIM provider + backchannel (VCF SCIM)
  9. Fleet IAM role assignments:
       prod-admins  -> vcf_administrator
       dev-admins   -> sddc_admin
       prod-readonly -> vcf_viewer
  10. Fleet IAM: Join SSO (vCenter, VCF Operations, VCF Automation)

Secrets: never printed to stdout/logs (redacted).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import time
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_tools_dir = str(Path(__file__).resolve().parent)
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)
from authentik_fleet_iam import (
    fleet_iam_post_scim_assign_and_join,
    log_fleet_sso_realm_summary,
    run_fleet_iam_vcf_sso,
)

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

try:
    import requests
except ImportError:
    requests = None  # type: ignore

CREDS_DEFAULT = '/home/holuser/creds.txt'
ROUTER_SSH = 'root@router'
COREDNS_CMD = (
    "sed -i 's/10.1.1.1/10.244.0.1/g' /holodeck-runtime/k8s/coredns_configmap.yaml "
    "&& kubectl delete -f /holodeck-runtime/k8s/coredns_configmap.yaml "
    "&& kubectl apply -f /holodeck-runtime/k8s/coredns_configmap.yaml"
)


def _log(write: Callable[[str], None], msg: str) -> None:
    if write:
        write(msg)


def _truthy(val: str) -> bool:
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in ('password', 'client_secret', 'client-secret') or lk == 'token':
                out[k] = '***REDACTED***'
            elif 'secret' in lk and 'signing' not in lk:
                out[k] = '***REDACTED***'
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _read_password(creds_path: str) -> str:
    with open(creds_path, encoding='utf-8') as f:
        return f.read().strip()


def _load_ini(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    cfg.read(path)
    return cfg




def _discover_mgmt_vc(cfg: configparser.ConfigParser) -> Tuple[str, str]:
    """Return (fqdn, sso_user) for management vCenter."""
    opt = 'authentik_mgmt_vc_fqdn'
    if cfg.has_option('VCFFINAL', opt):
        fqdn = cfg.get('VCFFINAL', opt).strip()
        if fqdn:
            user = 'administrator@vsphere.local'
            if cfg.has_option('VCFFINAL', 'authentik_mgmt_vc_user'):
                user = cfg.get('VCFFINAL', 'authentik_mgmt_vc_user').strip()
            return fqdn, user
    if cfg.has_section('RESOURCES') and cfg.has_option('RESOURCES', 'vCenters'):
        for line in cfg.get('RESOURCES', 'vCenters').splitlines():
            line = line.split('#', 1)[0].strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(':')]
            if len(parts) >= 3:
                host, typ, user = parts[0], parts[1].upper(), parts[2]
                if typ == 'MGMT' or 'mgmt' in host.lower() or 'vc-mgmt' in host.lower():
                    return host, user
        for line in cfg.get('RESOURCES', 'vCenters').splitlines():
            line = line.split('#', 1)[0].strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(':')]
            if len(parts) >= 3:
                return parts[0], parts[2]
            if len(parts) >= 1 and parts[0]:
                return parts[0], 'administrator@vsphere.local'
    return 'vc-mgmt-a.site-a.vcf.lab', 'administrator@vsphere.local'


def _discover_ops_fqdn(cfg: configparser.ConfigParser) -> str:
    if cfg.has_option('VCFFINAL', 'authentik_ops_fqdn'):
        v = cfg.get('VCFFINAL', 'authentik_ops_fqdn').strip()
        if v:
            return v
    if cfg.has_section('VCF') and cfg.has_option('VCF', 'urls'):
        for line in cfg.get('VCF', 'urls').splitlines():
            line = line.split('#', 1)[0].strip()
            if 'ops-' in line.lower():
                m = re.search(r'https?://([^/,;\s]+)', line)
                if m:
                    return m.group(1)
    return 'ops-a.site-a.vcf.lab'


def _ops_token(ops_base: str, password: str, verify_tls: bool) -> str:
    if requests is None:
        raise RuntimeError('requests library required')
    url = f'{ops_base}/suite-api/api/auth/token/acquire'
    for auth_source in ('local', 'localItem'):
        r = requests.post(
            url,
            json={'username': 'admin', 'password': password, 'authSource': auth_source},
            headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                     'X-vRealizeOps-API-use-unsupported': 'true'},
            timeout=60,
            verify=verify_tls,
        )
        if r.status_code == 200:
            tok = r.json().get('token')
            if tok:
                return tok
    raise RuntimeError(f'Failed to acquire OpsToken from {url}')


def run_coredns_patch(creds_path: str, write: Callable[[str], None], dry_run: bool) -> bool:
    ssh_cmd = (
        f'sshpass -f {creds_path} ssh -o StrictHostKeyChecking=accept-new '
        f'{ROUTER_SSH} {COREDNS_CMD!r}'
    )
    _log(write, 'Authentik integration: Step 1 — CoreDNS forwarder (router kubectl)')
    if dry_run:
        _log(write, f'  DRY-RUN would run: {ssh_cmd[:120]}...')
        return True
    r = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        _log(write, f'  CoreDNS patch FAILED rc={r.returncode} stderr={r.stderr[:500]}')
        return False
    _log(write, '  CoreDNS patch applied.')
    return True


def ensure_ops_vault_ca_trust(
    ops_fqdn: str,
    creds_path: str,
    write: Callable[[str], None],
    dry_run: bool,
    vault_url: str = 'https://vault.vcf.lab',
) -> bool:
    """
    Ensure the Vault root CA is trusted by VCF Operations so the Fleet IAM Java service
    can verify TLS when fetching the Authentik OIDC discovery URL.

    VCF Operations runs on Photon OS with a VMware JRE whose cacerts uses a proprietary
    format that standard keytool cannot modify. The fix is to:
      1. Check if Vault CA is already in /etc/pki/tls/certs/ca-bundle.crt.
      2. If not, append /etc/pki/tls/certs/vault-ca.pem (placed by confighol-9.1.py) to
         ca-bundle.crt. If vault-ca.pem is missing, fetch from Vault.
      3. Restart vmware-vcops-web (Tomcat/suite-api) so the updated bundle is loaded.
      4. Wait up to 120 s for the suite-api to answer.
    """
    import urllib.request, ssl, tempfile, os as _os
    _log(write, 'Authentik integration: Step 1b — Vault CA trust in VCF Operations (ca-bundle.crt)')
    if dry_run:
        _log(write, '  DRY-RUN: would append Vault CA to ops-a ca-bundle.crt and restart vmware-vcops-web.')
        return True

    ssh = f'sshpass -f {creds_path} ssh -o StrictHostKeyChecking=accept-new root@{ops_fqdn}'
    ca_bundle = '/etc/pki/tls/certs/ca-bundle.crt'
    vault_pem_remote = '/etc/pki/tls/certs/vault-ca.pem'

    # Test if auth.vcf.lab is already TLS-trusted by curl (proxy for the full CA trust state)
    check_tls_cmd = (
        f'{ssh} "curl -s -o /dev/null -w \'%{{http_code}}\' '
        f'https://auth.vcf.lab/application/o/vcf/.well-known/openid-configuration 2>/dev/null"'
    )
    rc_check = subprocess.run(check_tls_cmd, shell=True, capture_output=True, text=True, timeout=30)
    if rc_check.stdout.strip().startswith('2') or rc_check.stdout.strip().startswith('3'):
        _log(write, '  Vault CA already trusted by ops-a (auth.vcf.lab TLS verified) — skip.')
        return True

    # Determine CA PEM source: prefer the pre-placed vault-ca.pem, else fetch from Vault
    check_pem_cmd = f'{ssh} "test -f {vault_pem_remote} && echo PRESENT || echo ABSENT"'
    rc_pem = subprocess.run(check_pem_cmd, shell=True, capture_output=True, text=True, timeout=15)
    if 'PRESENT' in rc_pem.stdout:
        # Append the pre-placed file to ca-bundle.crt
        append_cmd = (
            f'{ssh} "grep -qF \\"$(openssl x509 -in {vault_pem_remote} -noout -fingerprint 2>/dev/null)\\" {ca_bundle} 2>/dev/null || '
            f'cat {vault_pem_remote} >> {ca_bundle} && echo APPENDED || echo SKIPPED"'
        )
        # Simpler: just append unconditionally and deduplicate by BEGIN count
        append_cmd = f'{ssh} "cat {vault_pem_remote} >> {ca_bundle} && echo APPENDED"'
        ra = subprocess.run(append_cmd, shell=True, capture_output=True, text=True, timeout=30)
        if 'APPENDED' in ra.stdout:
            _log(write, f'  Vault CA from {vault_pem_remote} appended to ca-bundle.crt.')
        else:
            _log(write, f'  WARNING: append of vault-ca.pem failed: {ra.stderr[:200]}')
    else:
        # Fetch from Vault and SCP to ops-a, then append
        try:
            with urllib.request.urlopen(f'{vault_url}/v1/pki/ca/pem', timeout=15) as resp:
                ca_pem = resp.read().decode()
            if '-----BEGIN CERTIFICATE-----' not in ca_pem:
                _log(write, f'  WARNING: Vault CA from {vault_url} is not a PEM — skipping.')
                return True
        except Exception as e:
            _log(write, f'  WARNING: could not fetch Vault CA from {vault_url}: {e} — skipping.')
            return True
        with tempfile.NamedTemporaryFile(suffix='.pem', delete=False, mode='w') as tf:
            tf.write(ca_pem)
            local_pem = tf.name
        try:
            scp_cmd = (
                f'sshpass -f {creds_path} scp -o StrictHostKeyChecking=accept-new '
                f'{local_pem} root@{ops_fqdn}:{vault_pem_remote}'
            )
            r_scp = subprocess.run(scp_cmd, shell=True, capture_output=True, text=True, timeout=30)
            if r_scp.returncode != 0:
                _log(write, f'  WARNING: scp to ops-a failed: {r_scp.stderr[:200]} — skipping.')
                return True
        finally:
            try:
                _os.unlink(local_pem)
            except Exception:
                pass
        append_cmd = f'{ssh} "cat {vault_pem_remote} >> {ca_bundle} && echo APPENDED"'
        ra = subprocess.run(append_cmd, shell=True, capture_output=True, text=True, timeout=30)
        if 'APPENDED' in ra.stdout:
            _log(write, f'  Vault CA fetched from Vault and appended to ca-bundle.crt.')
        else:
            _log(write, f'  WARNING: append of fetched vault CA failed: {ra.stderr[:200]}')

    # Restart vmware-vcops-web so Tomcat loads the updated CA bundle
    restart_cmd = f'{ssh} "systemctl restart vmware-vcops-web"'
    _log(write, '  Restarting vmware-vcops-web (suite-api Tomcat) to apply updated CA bundle...')
    r2 = subprocess.run(restart_cmd, shell=True, capture_output=True, text=True, timeout=60)
    if r2.returncode != 0:
        _log(write, f'  WARNING: vmware-vcops-web restart rc={r2.returncode}: {r2.stderr[:200]}')

    # Wait for suite-api (up to 120 s)
    suite_api_url = f'https://{ops_fqdn}/suite-api/api/version'
    _log(write, '  Waiting for suite-api to become available after restart (up to 120 s)...')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(24):
        time.sleep(5)
        try:
            with urllib.request.urlopen(suite_api_url, context=ctx, timeout=10) as resp:
                if resp.status < 500:
                    _log(write, f'  suite-api is up (attempt {attempt + 1}).')
                    break
        except Exception:
            pass
    else:
        _log(write, '  WARNING: suite-api did not respond within 120 s — Fleet IAM may still fail.')

    return True


class AuthentikApi:
    def __init__(self, base: str, token: str, write: Callable[[str], None], verify_tls: bool = False):
        self.base = base.rstrip('/')
        self.token = token
        self.write = write
        self.verify_tls = verify_tls
        self._sess = requests.Session() if requests else None
        if self._sess:
            self._sess.headers.update({
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            })
            self._sess.verify = verify_tls

    def _url(self, path: str) -> str:
        path = path.lstrip('/')
        return f'{self.base}/{path}'

    def get_json(self, path: str) -> Any:
        r = self._sess.get(self._url(path), timeout=60)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: Dict) -> Tuple[int, Any]:
        r = self._sess.post(self._url(path), data=json.dumps(body), timeout=120)
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text[:2000]}
        return r.status_code, data

    def patch_json(self, path: str, body: Dict) -> Tuple[int, Any]:
        r = self._sess.patch(self._url(path), data=json.dumps(body), timeout=120)
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text[:2000]}
        return r.status_code, data

    def _get_oauth2_scope_mapping_pks(self, scope_names: List[str]) -> List[str]:
        """Look up Authentik OAuth2 scope mapping UUIDs by name (propertymappings/provider/scope/)."""
        if not scope_names:
            return []
        all_scopes: List[Dict] = []
        try:
            all_scopes = self.paginate_list('propertymappings/provider/scope/')
        except Exception as e:
            _log(self.write, f'  WARNING: could not fetch OAuth2 scope mappings: {e}')
            return []
        by_name: Dict[str, str] = {
            m.get('name', ''): str(m['pk']) for m in all_scopes if m.get('pk') and m.get('name')
        }
        pks: List[str] = []
        for name in scope_names:
            pk = by_name.get(name)
            if pk:
                pks.append(pk)
            else:
                _log(self.write, f'  WARNING: OAuth2 scope mapping {name!r} not found — skipping')
        return pks

    def ensure_oauth2_provider_scopes(
        self,
        provider_pk: Any,
        scope_names: List[str],
        dry_run: bool,
    ) -> bool:
        """Ensure the OAuth2/OIDC provider has the required scope mappings.

        Additive: any scopes already present are preserved. Only missing scopes are added.
        Idempotent: re-running with the same scope list is a no-op.
        """
        if dry_run or not scope_names or not self._sess:
            return True
        try:
            detail = self._oauth2_provider_detail(provider_pk)
        except Exception as e:
            _log(self.write, f'  WARNING: could not read OAuth2 provider pk={provider_pk} for scope check: {e}')
            return False
        current_pks: set = {str(x) for x in (detail.get('property_mappings') or [])}
        desired_pks: set = set(self._get_oauth2_scope_mapping_pks(scope_names))
        missing = desired_pks - current_pks
        if not missing:
            _log(self.write, '  Authentik OAuth2: required OIDC scopes already present.')
            return True
        merged = list(current_pks | desired_pks)
        code, body = self.patch_json(f'providers/oauth2/{int(provider_pk)}/', {'property_mappings': merged})
        if code in (200, 201):
            _log(
                self.write,
                f'  Authentik OAuth2: added missing OIDC scope mappings to provider pk={provider_pk} '
                f'({len(missing)} added, {len(merged)} total).',
            )
            return True
        _log(self.write, f'  WARNING: OAuth2 scope PATCH HTTP {code}: {_redact(body)!s}')
        return False

    def align_oauth2_sub_mode_for_fleet_iam(
        self,
        provider_pk: Any,
        cfg: configparser.ConfigParser,
        dry_run: bool,
    ) -> None:
        """
        Fleet IAM IdP maps OIDC ``sub`` to VIDB ``ExternalId`` (see ``authentik_fleet_iam``).
        Authentik's default SCIM ``externalId`` pairs with the default (hashed) OAuth subject, not
        ``user_username``. Misaligned modes cause SCIM-visible users who cannot complete SSO login.
        """
        if dry_run or not self._sess:
            return
        try:
            detail = self._oauth2_provider_detail(provider_pk)
        except Exception as e:
            _log(self.write, f'  WARNING: could not read OAuth2 provider pk={provider_pk} for sub_mode: {e}')
            return
        current = (detail.get('sub_mode') or '').strip()
        explicit = ''
        if cfg.has_option('VCFFINAL', 'authentik_oauth_sub_mode'):
            explicit = (cfg.get('VCFFINAL', 'authentik_oauth_sub_mode') or '').strip()
        if explicit:
            if current == explicit:
                return
            code, body = self.patch_json(f'providers/oauth2/{int(provider_pk)}/', {'sub_mode': explicit})
            if code in (200, 201):
                _log(self.write, f'  Authentik OAuth2: patched sub_mode → {explicit!r} (VCFFINAL.authentik_oauth_sub_mode).')
            else:
                _log(
                    self.write,
                    f'  WARNING: OAuth2 sub_mode patch to {explicit!r} HTTP {code}: {_redact(body)!s}',
                )
            return
        if current != 'user_username':
            return
        code, body = self.patch_json(
            f'providers/oauth2/{int(provider_pk)}/',
            {'sub_mode': 'hashed_user_id'},
        )
        if code in (200, 201):
            _log(
                self.write,
                '  Authentik OAuth2: patched sub_mode user_username → hashed_user_id '
                '(Fleet IAM OIDC sub must match SCIM ExternalId; see vcf-troubleshooting §44).',
            )
            return
        _log(
            self.write,
            f'  WARNING: could not patch OAuth2 sub_mode from user_username (HTTP {code}: {_redact(body)!s}). '
            'Set OAuth2 subject in Authentik to the default/hashed mode, or set VCFFINAL.authentik_oauth_sub_mode.',
        )

    def _resolve_pagination_url(self, nxt: Optional[str]) -> Optional[str]:
        """Authentik may return ``next`` as absolute URL or a path-only URL."""
        if not nxt:
            return None
        if nxt.startswith('http://') or nxt.startswith('https://'):
            return nxt
        base = urlparse(self.base)
        origin = f'{base.scheme}://{base.netloc}'
        if nxt.startswith('/'):
            return origin + nxt
        return urljoin(self.base + '/', nxt.lstrip('/'))

    @staticmethod
    def _app_provider_pk(app: Dict[str, Any]) -> Optional[int]:
        """OAuth2 provider pk from a ``core/applications`` list or detail row."""
        ap = app.get('provider')
        if ap is None:
            return None
        if isinstance(ap, dict):
            v = ap.get('pk')
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        if isinstance(ap, str):
            s = ap.strip()
            if s.isdigit():
                return int(s)
            m = re.search(r'/providers/oauth2/(\d+)/?', s)
            if m:
                return int(m.group(1))
            return None
        try:
            return int(ap)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _app_slug(app: Dict[str, Any]) -> Optional[str]:
        s = app.get('slug')
        if s:
            return str(s)
        meta = app.get('meta') or {}
        if isinstance(meta, dict) and meta.get('slug'):
            return str(meta['slug'])
        return None

    def paginate_list(self, path: str) -> List[Dict]:
        """
        Walk Authentik pagination for a list endpoint (path without leading slash).

        Authentik uses ``pagination.next`` as the next **page number** (int). Some builds also
        cap each response (~20 rows) while ``pagination.count`` is larger and ``next`` is 0 —
        then we advance ``page`` until ``len(results)`` across pages reaches ``count``.
        """
        all_items: List[Dict] = []
        p = path.lstrip('/').rstrip('/')
        page_size = 100
        page = 1
        while True:
            url = self._url(f'{p}/?page_size={page_size}&page={page}')
            r = self._sess.get(url, timeout=120)
            r.raise_for_status()
            data = r.json()

            legacy = data.get('next')
            if isinstance(legacy, str) and legacy.strip():
                all_items.extend(data.get('results') or [])
                next_url = self._resolve_pagination_url(legacy)
                while next_url:
                    r2 = self._sess.get(next_url, timeout=120)
                    r2.raise_for_status()
                    d2 = r2.json()
                    all_items.extend(d2.get('results') or [])
                    next_url = self._resolve_pagination_url(d2.get('next'))
                return all_items

            chunk = data.get('results') or []
            if not chunk:
                break
            all_items.extend(chunk)
            pag = data.get('pagination') or {}
            total_count = int(pag.get('count') or 0)
            total_pages = int(pag.get('total_pages') or 0)
            np = pag.get('next')

            # np==0 (int) means Authentik signals "no next page"
            if isinstance(np, int) and np == 0:
                break
            if isinstance(np, int) and np > 0 and np != page:
                page = np
                continue
            # Respect total_pages when provided
            if total_pages > 0 and page >= total_pages:
                break
            if total_count and len(all_items) >= total_count:
                break
            if total_count and len(all_items) < total_count:
                page += 1
                if page > 1000:
                    break
                continue
            break
        return all_items

    def _oauth2_provider_detail(self, pk: Any) -> Dict[str, Any]:
        return self.get_json(f'providers/oauth2/{int(pk)}/')

    def _reuse_oauth2_provider_and_ensure_app(
        self,
        prov_pk: Any,
        app_name: str,
        app_slug: str,
        dry_run: bool,
    ) -> Tuple[int, str, str]:
        """Load provider credentials and ensure ``core/applications`` row exists for ``app_slug``."""
        prov_obj = self._oauth2_provider_detail(prov_pk)
        cid = prov_obj.get('client_id')
        csec = prov_obj.get('client_secret')
        if not cid:
            raise RuntimeError(f'OAuth2 provider pk={prov_pk} has no client_id in API response')

        def _return_reuse(msg: str) -> Tuple[int, str, str]:
            _log(self.write, msg)
            return int(prov_pk), str(cid), str(csec or '')

        try:
            detail = self.get_json(f'core/applications/{app_slug}/')
            ap_pk = self._app_provider_pk(detail)
            if ap_pk == int(prov_pk):
                return _return_reuse(
                    f'  Reusing Authentik application (GET by slug) {app_slug!r} provider pk={prov_pk}',
                )
        except Exception:
            pass

        # Any application already bound to this OAuth2 provider (list views may omit slug).
        try:
            filtered = self.get_json(f'core/applications/?slug={app_slug}')
            for app in filtered.get('results') or []:
                if self._app_provider_pk(app) == int(prov_pk):
                    return _return_reuse(
                        f'  Reusing Authentik application slug={app_slug!r} provider pk={prov_pk} (slug filter)',
                    )
        except Exception:
            pass

        apps = self.paginate_list('core/applications')
        for app in apps:
            if self._app_provider_pk(app) == int(prov_pk):
                return _return_reuse(
                    f'  Reusing Authentik application (matched by provider pk={prov_pk}) '
                    f"slug={self._app_slug(app)!r} name={app.get('name')!r}",
                )

        for app in apps:
            aslug = self._app_slug(app)
            if aslug == app_slug or app.get('name') == app_name:
                ap = self._app_provider_pk(app)
                if ap is not None and ap == int(prov_pk):
                    return _return_reuse(f'  Reusing Authentik application {app_slug!r} provider pk={prov_pk}')
                if dry_run:
                    _log(self.write, f'  DRY-RUN would PATCH application {app_slug!r} to provider pk={prov_pk}')
                    return int(prov_pk), str(cid), str(csec or '')
                code, body = self.patch_json(
                    f'core/applications/{app_slug}/',
                    {'provider': int(prov_pk), 'name': app_name},
                )
                if code not in (200, 201):
                    raise RuntimeError(f'Application patch (link provider) HTTP {code}: {body}')
                _log(self.write, f'  Linked Authentik application {app_slug!r} to existing provider pk={prov_pk}')
                return int(prov_pk), str(cid), str(csec or '')

        if dry_run:
            _log(self.write, f'  DRY-RUN would POST core/applications slug={app_slug!r} provider={prov_pk}')
            return int(prov_pk), str(cid), str(csec or '')

        app_body = {'name': app_name, 'slug': app_slug, 'provider': int(prov_pk)}
        code2, app_resp = self.post_json('core/applications/', app_body)
        if code2 in (200, 201):
            _log(self.write, f'  Created application {app_slug!r} pk={app_resp.get("pk")} (existing provider pk={prov_pk})')
            return int(prov_pk), str(cid), str(csec or '')

        if code2 == 400 and isinstance(app_resp, dict):
            flat = json.dumps(app_resp).lower()
            if 'unique' in flat or 'already' in flat or 'slug' in app_resp or 'provider' in app_resp:
                apps2 = self.paginate_list('core/applications')
                for app in apps2:
                    if self._app_provider_pk(app) == int(prov_pk):
                        return _return_reuse(
                            f'  Application already linked to provider pk={prov_pk} '
                            f"(slug={self._app_slug(app)!r}) — treating as success",
                        )
                for app in apps2:
                    if self._app_slug(app) == app_slug:
                        return _return_reuse(
                            f'  Application slug={app_slug!r} already exists (provider pk={self._app_provider_pk(app)})',
                        )
                for app in apps2:
                    if app.get('name') == app_name:
                        return _return_reuse(
                            f'  Application name={app_name!r} already exists (provider pk={self._app_provider_pk(app)})',
                        )
        raise RuntimeError(f'Application create failed HTTP {code2}: {app_resp}')

    def _get_scim_property_mapping_pks(self, names: List[str]) -> List[str]:
        """Look up Authentik SCIM property mapping UUIDs by name.

        Covers both user and group SCIM mappings; the endpoint returns all of them.
        Returns only the PKs for names that were found; logs a warning for any missing.
        """
        if not names:
            return []
        all_mappings: List[Dict] = []
        try:
            all_mappings = self.paginate_list('propertymappings/provider/scim')
        except Exception as e:
            _log(self.write, f'  WARNING: could not fetch SCIM property mappings: {e}')
            return []
        by_name: Dict[str, str] = {
            m.get('name', ''): str(m['pk']) for m in all_mappings if m.get('pk') and m.get('name')
        }
        pks: List[str] = []
        for name in names:
            pk = by_name.get(name)
            if pk:
                pks.append(pk)
            else:
                _log(self.write, f'  WARNING: SCIM property mapping {name!r} not found — skipping')
        return pks

    def _get_group_pks(self, names: List[str]) -> List[str]:
        """Look up Authentik group UUIDs by name, for use in SCIM group_filters."""
        if not names:
            return []
        pks: List[str] = []
        for name in names:
            try:
                r = self._sess.get(
                    self._url('core/groups/'),
                    params={'name': name},
                    timeout=30,
                )
                if r.status_code == 200:
                    matched = [g for g in r.json().get('results', []) if g.get('name') == name]
                    if matched:
                        pks.append(str(matched[0]['pk']))
                    else:
                        _log(self.write, f'  WARNING: Authentik group {name!r} not found for SCIM filter')
            except Exception as e:
                _log(self.write, f'  WARNING: error looking up group {name!r}: {e}')
        return pks

    def ensure_oauth_application(
        self,
        app_name: str,
        app_slug: str,
        redirect_url: str,
        oauth_name: str,
        signing_key_pk: str,
        auth_flow_slug: str,
        invalidation_flow_slug: str,
        dry_run: bool,
    ) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Return (provider_pk, client_id, client_secret)."""
        flows = self.get_json('flows/instances/?slug=' + auth_flow_slug)
        results = flows.get('results') or []
        if not results:
            raise RuntimeError(f'Authentik authorization flow {auth_flow_slug!r} not found')
        auth_flow = results[0]['pk']
        inv = self.get_json('flows/instances/?slug=' + invalidation_flow_slug)
        inv_results = inv.get('results') or []
        if not inv_results:
            raise RuntimeError(f'Authentik invalidation flow {invalidation_flow_slug!r} not found')
        inv_flow = inv_results[0]['pk']

        for app in self.paginate_list('core/applications'):
            aslug = self._app_slug(app)
            if aslug == app_slug or app.get('name') == app_name:
                prov = self._app_provider_pk(app)
                if prov is not None:
                    prov_obj = self.get_json(f'providers/oauth2/{int(prov)}/')
                    cid = prov_obj.get('client_id')
                    csec = prov_obj.get('client_secret')
                    _log(self.write, f'  Reusing Authentik application {app_slug!r} provider pk={prov}')
                    return int(prov), str(cid), str(csec or '')
                # Matching app row without provider — try to attach an existing OAuth2 provider by name.
                for p in self.paginate_list('providers/oauth2'):
                    if p.get('name') == oauth_name:
                        return self._reuse_oauth2_provider_and_ensure_app(
                            p['pk'], app_name, app_slug, dry_run,
                        )
                break

        for p in self.paginate_list('providers/oauth2'):
            if p.get('name') == oauth_name:
                _log(
                    self.write,
                    f'  Reusing existing OAuth2 provider name={oauth_name!r} pk={p.get("pk")} (no matching application yet)',
                )
                return self._reuse_oauth2_provider_and_ensure_app(p['pk'], app_name, app_slug, dry_run)

        body = {
            'name': oauth_name,
            'authorization_flow': auth_flow,
            'invalidation_flow': inv_flow,
            'client_type': 'confidential',
            'redirect_uris': [{'url': redirect_url, 'matching_mode': 'strict'}],
            'signing_key': signing_key_pk,
            'sub_mode': 'hashed_user_id',
            'include_claims_in_id_token': True,
            'issuer_mode': 'per_provider',
            'encryption_key': None,
        }
        if dry_run:
            _log(self.write, f'  DRY-RUN would POST providers/oauth2/ {_redact(body)!s}')
            return None, None, None
        code, data = self.post_json('providers/oauth2/', body)
        if code in (200, 201):
            prov_pk = data['pk']
            client_id = data['client_id']
            client_secret = data['client_secret']
            _log(self.write, f'  Created OAuth2 provider pk={prov_pk} client_id={client_id} (secret not logged)')

            app_body = {'name': app_name, 'slug': app_slug, 'provider': int(prov_pk)}
            code2, app_resp = self.post_json('core/applications/', app_body)
            if code2 in (200, 201):
                _log(self.write, f'  Created application {app_slug!r} pk={app_resp.get("pk")}')
                return int(prov_pk), str(client_id), str(client_secret)
            if code2 == 400 and isinstance(app_resp, dict):
                flat = json.dumps(app_resp).lower()
                if 'unique' in flat or 'already' in flat or 'slug' in app_resp or 'provider' in app_resp:
                    return self._reuse_oauth2_provider_and_ensure_app(prov_pk, app_name, app_slug, dry_run)
            raise RuntimeError(f'Application create failed HTTP {code2}: {app_resp}')

        if code == 400 and isinstance(data, dict):
            err_txt = json.dumps(data).lower()
            if 'already exists' in err_txt or (data.get('name') and 'already' in str(data.get('name')).lower()):
                for p in self.paginate_list('providers/oauth2'):
                    if p.get('name') == oauth_name:
                        _log(self.write, f'  OAuth2 provider {oauth_name!r} already exists — reusing pk={p.get("pk")}')
                        return self._reuse_oauth2_provider_and_ensure_app(p['pk'], app_name, app_slug, dry_run)
        raise RuntimeError(f'OAuth2 provider create failed HTTP {code}: {data}')

    def ensure_scim_backchannel(
        self,
        app_slug: str,
        scim_url: str,
        bearer_token: str,
        scim_name: str,
        dry_run: bool,
        filter_group_names: Optional[List[str]] = None,
        user_mapping_names: Optional[List[str]] = None,
        group_mapping_names: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[Any]]:
        """Return (success, scim_provider_pk). Idempotent: reuse SCIM provider by name or URL.

        When ``filter_group_names`` is provided, sets ``group_filters`` on the SCIM provider so
        Authentik only syncs users/groups that belong to those groups.
        ``user_mapping_names`` / ``group_mapping_names`` set ``property_mappings`` /
        ``property_mappings_group`` respectively (looked up by name from propertymappings/provider/scim/).
        Pass ``None`` to leave an existing value unchanged; pass ``[]`` to clear it.
        """
        if dry_run:
            _log(self.write, f'  DRY-RUN would ensure SCIM provider {_redact({"name": scim_name, "url": scim_url})!s}')
            return True, None

        # Resolve PKs only when the caller specified a value (None = "don't touch").
        user_pm_pks: Optional[List[str]] = (
            self._get_scim_property_mapping_pks(user_mapping_names)
            if user_mapping_names is not None else None
        )
        group_pm_pks: Optional[List[str]] = (
            self._get_scim_property_mapping_pks(group_mapping_names)
            if group_mapping_names is not None else None
        )
        filter_gp_pks: Optional[List[str]] = (
            self._get_group_pks(filter_group_names)
            if filter_group_names is not None else None
        )

        spk: Optional[Any] = None
        existing: Optional[Dict[str, Any]] = None
        for s in self.paginate_list('providers/scim'):
            if s.get('name') == scim_name or (scim_url and (s.get('url') or '') == scim_url):
                spk = s.get('pk')
                existing = s
                _log(self.write, f'  Reusing Authentik SCIM provider name={scim_name!r} pk={spk}')
                break

        if spk is None:
            body: Dict[str, Any] = {
                'name': scim_name,
                'url': scim_url,
                'token': bearer_token,
                'verify_certificates': False,
            }
            if user_pm_pks:
                body['property_mappings'] = user_pm_pks
            if group_pm_pks:
                body['property_mappings_group'] = group_pm_pks
            if filter_gp_pks:
                body['group_filters'] = filter_gp_pks
            code, data = self.post_json('providers/scim/', body)
            if code in (200, 201):
                spk = data['pk']
                _log(self.write, f'  Created SCIM provider pk={spk}')
            elif code == 400 and isinstance(data, dict) and 'already' in json.dumps(data).lower():
                for s in self.paginate_list('providers/scim'):
                    if s.get('name') == scim_name or (scim_url and (s.get('url') or '') == scim_url):
                        spk = s.get('pk')
                        existing = s
                        _log(self.write, f'  SCIM provider already exists — reusing pk={spk}')
                        break
            if spk is None:
                _log(self.write, f'  SCIM provider create failed HTTP {code}: {_redact(data)!s}')
                return False, None

        if spk is not None:
            # Always sync: token (Fleet mints new each run) + URL + property mappings + group filters.
            patch: Dict[str, Any] = {'token': bearer_token}
            if (existing or {}).get('url', '') != scim_url:
                patch['url'] = scim_url
                _log(self.write, f'  SCIM provider pk={spk}: correcting URL → {scim_url!r}')
            if user_pm_pks is not None:
                current = sorted(str(x) for x in (existing or {}).get('property_mappings', []))
                if current != sorted(user_pm_pks):
                    patch['property_mappings'] = user_pm_pks
            if group_pm_pks is not None:
                current = sorted(str(x) for x in (existing or {}).get('property_mappings_group', []))
                if current != sorted(group_pm_pks):
                    patch['property_mappings_group'] = group_pm_pks
            if filter_gp_pks is not None:
                current = sorted(str(x) for x in (existing or {}).get('group_filters', []))
                if current != sorted(filter_gp_pks):
                    patch['group_filters'] = filter_gp_pks
            changed = [k for k in patch if k != 'token']
            if changed:
                _log(self.write, f'  SCIM provider pk={spk}: updating {", ".join(changed)}')
            pcode, _ = self.patch_json(f'providers/scim/{int(spk)}/', patch)
            if pcode not in (200, 201):
                _log(self.write, f'  WARNING: SCIM provider PATCH HTTP {pcode} — continuing')

        app: Optional[Dict[str, Any]] = None
        for a in self.paginate_list('core/applications'):
            if self._app_slug(a) == app_slug:
                app = self.get_json(f'core/applications/{app_slug}/')
                break
        if not app:
            _log(self.write, f'  ERROR: Authentik application slug={app_slug!r} not found — cannot attach SCIM')
            return False, spk

        bcp = [int(x) for x in (app.get('backchannel_providers') or [])]
        sid = int(spk)
        if sid in bcp:
            _log(self.write, f'  Application {app_slug!r} already lists SCIM backchannel pk={sid}')
            return True, spk
        bcp.append(sid)
        code2, patched = self.patch_json(f'core/applications/{app_slug}/', {'backchannel_providers': bcp})
        if code2 not in (200, 201):
            _log(self.write, f'  Link SCIM to application failed HTTP {code2}: {patched}')
            return False, None
        _log(self.write, f'  SCIM provider pk={spk} linked to application {app_slug!r}.')
        return True, spk

    def trigger_scim_provider_sync(self, provider_pk: Any) -> bool:
        """Best-effort full SCIM push; Authentik also syncs periodically."""
        pk = int(provider_pk)
        paths = [
            f'providers/scim/{pk}/sync/',
            f'providers/scim/{pk}/sync/full/',
            # Newer Authentik builds expose object sync; full push may still be async.
            f'providers/scim/{pk}/sync/object/',
        ]
        for path in paths:
            code, data = self.post_json(path, {})
            if code in (200, 201, 204):
                _log(self.write, f'  Authentik SCIM sync triggered via {path!r}.')
                return True
            _log(self.write, f'  Authentik SCIM sync {path!r} HTTP {code}: {_redact(data)!s}')
        _log(self.write, '  WARNING: SCIM sync API not accepted; groups may appear after worker/hourly sync.')
        return True

    def ensure_group(self, name: str, dry_run: bool) -> Optional[int]:
        r = self._sess.get(self._url('core/groups/'), params={'search': name}, timeout=60)
        if r.status_code == 200:
            res = r.json().get('results') or []
            for g in res:
                if g.get('name') == name:
                    return g['pk']
        if dry_run:
            _log(self.write, f'  DRY-RUN would create Authentik group {name!r}')
            return None
        code, data = self.post_json('core/groups/', {'name': name})
        if code not in (200, 201):
            _log(self.write, f'  Authentik group create {name!r} HTTP {code}: {data}')
            return None
        return data['pk']

    def ensure_user_username(
        self,
        username: str,
        display_name: str,
        email: str,
        group_pk: Any,
        dry_run: bool,
    ) -> bool:
        r = self._sess.get(self._url('core/users/'), params={'email': email}, timeout=60)
        if r.status_code == 200:
            for u in r.json().get('results') or []:
                if (u.get('email') or '').lower() == email.lower():
                    _log(self.write, f'  Authentik user with email {email!r} already exists — skip.')
                    return True
        if dry_run:
            _log(self.write, f'  DRY-RUN would create Authentik user {username!r}')
            return True
        body = {
            'username': username,
            'name': display_name,
            'email': email,
            'groups': [group_pk],
            'path': 'users',
        }
        code, data = self.post_json('core/users/', body)
        if code not in (200, 201):
            _log(self.write, f'  Authentik user create {username!r} HTTP {code}: {_redact(data)!s}')
            return False
        _log(self.write, f'  Authentik user {username!r} created (pk={data.get("pk")}).')
        return True


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
            '  SSO UI: playwright is not installed. This lab environment requires playwright '
            'to be available in the python environment to complete the VCF SSO Prerequisites. '
            'Please ensure the lab template is built with playwright installed.'
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



def run_authentik_vcf_integration(
    lsf: Any = None,
    dry_run: bool = False,
    config_path: str = '/tmp/config.ini',
) -> bool:
    """
    Main entry point. Enabled by [VCFFINAL] authentik_vcf_integration = true.
    Returns True on full success, False on any partial failure.
    All steps are idempotent — safe to re-run on an already-configured environment.
    """
    write: Callable[[str], None] = (lambda m: lsf.write_output(m)) if lsf else print

    cfg = _load_ini(config_path)
    if not cfg.has_section('VCFFINAL'):
        _log(write, 'authentik_vcf_integration: no [VCFFINAL] section — skip.')
        return True
    if not cfg.has_option('VCFFINAL', 'authentik_vcf_integration'):
        _log(write, 'authentik_vcf_integration: option not set — skip.')
        return True
    if not _truthy(cfg.get('VCFFINAL', 'authentik_vcf_integration')):
        _log(write, 'authentik_vcf_integration: false — skip.')
        return True

    creds_path = os.environ.get('HOL_CREDS_PATH', CREDS_DEFAULT)
    password = lsf.get_password() if (lsf and hasattr(lsf, 'get_password')) else _read_password(creds_path)

    # ── Environment discovery ────────────────────────────────────────────────
    mgmt_vc, _vc_user = _discover_mgmt_vc(cfg)
    ops_fqdn = _discover_ops_fqdn(cfg)
    vc_base = f'https://{mgmt_vc}'
    ops_base = f'https://{ops_fqdn}'
    verify_tls = False

    issuer_base = 'https://auth.vcf.lab'
    app_slug = 'vcf'
    app_name = 'VCF'
    tenant = 'CUSTOMER'
    scim_domain = 'vcf.lab'

    redirect_url = f'{vc_base}/federation/t/{tenant}/auth/response/oauth2'
    discovery_url = f'{issuer_base}/application/o/{app_slug}/.well-known/openid-configuration'
    scim_url = f'{vc_base}/usergroup/scim/v2'
    issuer_host = urlparse(issuer_base).hostname or 'auth.vcf.lab'

    # ── Authentik API token + signing key (auto-discovered) ──────────────────
    ak_token = os.environ.get('AUTHENTIK_API_TOKEN', 'holodeck')
    signing_key = 'e469fc95-d878-4042-abe9-1e46840fd125'
    if requests:
        try:
            kr = requests.get(
                f'{issuer_base}/api/v3/crypto/certificatekeypairs/',
                headers={'Authorization': f'Bearer {ak_token}', 'Accept': 'application/json'},
                timeout=30,
                verify=verify_tls,
            )
            if kr.status_code == 200:
                keys = kr.json().get('results') or []
                if keys:
                    signing_key = str(keys[0]['pk'])
        except Exception:
            pass

    # ── Fixed provider / role configuration ─────────────────────────────────
    oauth_provider_name = 'VCF OIDC'
    scim_provider_name = 'VCF SCIM'
    idp_fleet_name = 'VCF Auth'
    directory_fleet_name = scim_domain

    oauth_scope_names: List[str] = [
        "authentik default OAuth Mapping: OpenID 'email'",
        "authentik default OAuth Mapping: OpenID 'openid'",
        "authentik default OAuth Mapping: OpenID 'profile'",
    ]
    scim_user_mapping_names: List[str] = ['authentik default SCIM Mapping: User']
    scim_group_mapping_names: List[str] = ['authentik default SCIM Mapping: Group']

    # Groups and their Fleet IAM role assignments
    vcf_admin_groups: List[str] = ['prod-admins']      # vcf_administrator
    sddc_admin_groups: List[str] = ['dev-admins']       # sddc_admin
    viewer_groups: List[str] = ['prod-readonly']        # vcf_viewer

    # SCIM filter: all groups whose users and memberships sync into vCenter VIDB
    scim_filter_groups: List[str] = [
        'approvers', 'dev-admins', 'dev-readonly', 'dev-users',
        'prod-admins', 'prod-readonly', 'prod-users',
    ]

    # Lab users to create in Authentik (idempotent)
    lab_user_emails: List[str] = ['prod-admin@vcf.lab', 'dev-admin@vcf.lab']

    _log(write, '=== Authentik + VCF integration ===')
    ok = True

    # ── Step 1: CoreDNS forwarder patch on holorouter ────────────────────────
    if not run_coredns_patch(creds_path, write, dry_run):
        ok = False

    # ── Step 2: Vault CA trust on VCF Operations ─────────────────────────────
    ensure_ops_vault_ca_trust(ops_fqdn, creds_path, write, dry_run)

    if requests is None:
        _log(write, 'ERROR: Python requests module missing — install requests.')
        return False

    # ── Step 3-4: Authentik OAuth2 provider + application + OIDC scopes ──────
    ak = AuthentikApi(f'{issuer_base}/api/v3', ak_token, write, verify_tls=verify_tls)
    try:
        prov_pk, client_id, client_secret = ak.ensure_oauth_application(
            app_name=app_name,
            app_slug=app_slug,
            redirect_url=redirect_url,
            oauth_name=oauth_provider_name,
            signing_key_pk=signing_key,
            auth_flow_slug='default-provider-authorization-explicit-consent',
            invalidation_flow_slug='default-provider-invalidation-flow',
            dry_run=dry_run,
        )
    except Exception as e:
        _log(write, f'Authentik OAuth/Application FAILED: {e}')
        return False

    if prov_pk is not None and not dry_run:
        ak.align_oauth2_sub_mode_for_fleet_iam(prov_pk, cfg, dry_run)
        if not ak.ensure_oauth2_provider_scopes(prov_pk, oauth_scope_names, dry_run):
            ok = False

    if dry_run or not client_secret:
        _log(write, 'Dry-run or reused provider without secret — skipping downstream steps.')
        return ok

    # ── Step 5: Authentik groups + lab users ─────────────────────────────────
    all_groups = list(dict.fromkeys(
        scim_filter_groups + vcf_admin_groups + sddc_admin_groups + viewer_groups
    ))
    group_pk_by_name: Dict[str, Any] = {}
    for gname in all_groups:
        gpk = ak.ensure_group(gname, dry_run)
        if gpk is not None:
            group_pk_by_name[gname] = gpk

    for email in lab_user_emails:
        local = email.split('@')[0].lower()
        gname = 'prod-admins' if local.startswith('prod') else 'dev-admins'
        gpk = group_pk_by_name.get(gname)
        if not gpk:
            _log(write, f'  WARNING: missing group pk for {gname!r} — skip user {email!r}')
            ok = False
            continue
        if not ak.ensure_user_username(email, local.replace('-', ' ').title(), email, gpk, dry_run):
            ok = False

    # ── Steps 6-9: Fleet IAM SSO realm, IdP, SCIM, role assignment, Join SSO ─
    _log(write, 'Fleet IAM: VCF Operations suite-api (SSO realm, OIDC+SCIM IdP, SCIM token).')
    otok: Optional[str] = None
    
    # Run UI Prerequisites via Playwright if available
    submit_sso_prerequisites_ui(ops_fqdn, password, write, dry_run)

    try:
        otok = _ops_token(ops_base, password, verify_tls)
        fleet_ok, fleet_scim_tok, realm_id, vidb_rid = run_fleet_iam_vcf_sso(
            ops_base, otok, mgmt_vc, issuer_host, discovery_url,
            client_id, client_secret, scim_domain, idp_fleet_name,
            directory_fleet_name, write, verify_tls, dry_run,
        )
        if not fleet_ok or not fleet_scim_tok or not realm_id or not vidb_rid:
            ok = False
        else:
            scim_linked, scim_pk = ak.ensure_scim_backchannel(
                app_slug, scim_url, fleet_scim_tok, scim_provider_name, dry_run,
                filter_group_names=scim_filter_groups,
                user_mapping_names=scim_user_mapping_names,
                group_mapping_names=scim_group_mapping_names,
            )
            if not scim_linked:
                ok = False
            else:
                def _sync() -> bool:
                    if dry_run or scim_pk is None:
                        return True
                    return ak.trigger_scim_provider_sync(scim_pk)

                if not fleet_iam_post_scim_assign_and_join(
                    ops_base, otok, vidb_rid, realm_id, _sync, write, verify_tls,
                    join_nsx=False,
                    group_names=vcf_admin_groups,
                    vcf_role='vcf_administrator',
                    viewer_group_names=viewer_groups,
                    vcf_viewer_role='vcf_viewer',
                    sddc_admin_group_names=sddc_admin_groups,
                    vcf_sddc_role='sddc_admin',
                ):
                    ok = False
    except Exception as e:
        _log(write, f'Fleet IAM / SCIM / Join SSO FAILED: {e}')
        ok = False
    finally:
        if otok:
            try:
                log_fleet_sso_realm_summary(ops_base, otok, write, verify_tls)
            except Exception:
                pass

    _log(write, '=== Authentik + VCF integration finished ===')
    return ok


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description='Authentik + VCF integration (Cycle 7)')
    p.add_argument('--config', default='/tmp/config.ini', help='Path to config.ini')
    p.add_argument('--dry-run', action='store_true', help='Log actions only')
    args = p.parse_args(argv)
    try:
        ok = run_authentik_vcf_integration(lsf=None, dry_run=args.dry_run, config_path=args.config)
        return 0 if ok else 1
    except FileNotFoundError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
