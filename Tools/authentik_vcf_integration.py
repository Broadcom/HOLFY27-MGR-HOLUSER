#!/usr/bin/env python3
"""
TODO: This script is a work in progress. It is not yet complete.
VERSION: 0.0.1 - 2026-04-27
AUTHOR: Burke Azbill and HOL Core Team

Authentik + VCF lab integration (Cycle 7).

End-to-end (default): CoreDNS, Authentik OAuth2 app (idempotent: reuse provider/application by name or slug),
Authentik users/groups, VCF Operations **Fleet IAM**
APIs (SSO realm, OIDC+SCIM IdP, SCIM bearer token), Authentik SCIM back-channel, SCIM sync trigger,
``vcf_administrator`` on ``prod-admins`` / ``dev-admins``, Join SSO for vCenter + VCF Operations + VCF Automation.

Legacy path (``authentik_skip_fleet_iam=true``): vCenter ``/api/vcenter/identity/providers``, VIDB auth source,
optional manual ``vcf_scim_bearer_token``.

Secrets: never printed to stdout/logs (redacted). OAuth client_secret and SCIM token exist only in memory
unless persisted by the admin in config.ini for re-runs.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shlex
import subprocess
import time
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

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


def _vcenter_session(vc_base: str, user: str, password: str, verify_tls: bool) -> str:
    """Return vmware-api-session-id string."""
    if requests is None:
        raise RuntimeError('requests library required for vCenter / VCF Operations calls')
    ctx = verify_tls
    r = requests.post(
        f'{vc_base}/api/session',
        auth=(user, password),
        timeout=60,
        verify=ctx,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f'vCenter session HTTP {r.status_code}: {r.text[:500]}')
    return r.text.strip('"')


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


def _vami_noproxy_entries_to_add(issuer_hostname: str) -> List[str]:
    """Hostnames/IPs Fleet/VIDB must reach without the lab Squid proxy."""
    ih = (issuer_hostname or '').strip().lower().rstrip('.')
    out: List[str] = []
    if ih:
        out.append(ih)
    if ih.endswith('.vcf.lab'):
        out.append('authentik.vcf.lab')
        out.append('.vcf.lab')
    out.append('192.168.0.2')
    return list(dict.fromkeys([x for x in out if x]))


def ensure_mgmt_vcenter_no_proxy_for_oidc(
    mgmt_vc: str,
    issuer_hostname: str,
    root_password: str,
    creds_path: str,
    write: Callable[[str], None],
    dry_run: bool,
    verify_tls: bool,
    restart_vmware_vmon: bool,
) -> bool:
    """
    vCenter **VAMI** (port 5480) owns the no-proxy list that **vmware-vmon** injects into
    the vsphere-ui JVM. Editing ``/etc/sysconfig/proxy`` alone does not update that env;
    use ``PUT /rest/appliance/networking/noproxy`` with body ``{"servers":[...]}``.

    When the list changes, restart **vmware-vmon** so services pick up the new bypass list
    (brief management UI disruption). Skip with ``authentik_skip_mgmt_vc_vmon_restart=true``
    and reboot vCenter manually if needed.
    """
    _log(write, 'Authentik integration: vCenter VAMI no-proxy for OIDC issuer (5480 REST + optional vmon restart)')
    if dry_run:
        _log(write, '  DRY-RUN: would merge VAMI noproxy on ' + mgmt_vc + ' if reachable.')
        return True
    if requests is None:
        _log(write, '  ERROR: requests required for VAMI noproxy merge.')
        return False
    vami_url = f'https://{mgmt_vc}:5480/rest/appliance/networking/noproxy'
    adds = _vami_noproxy_entries_to_add(issuer_hostname)
    try:
        gr = requests.get(
            vami_url,
            auth=('root', root_password),
            timeout=60,
            verify=verify_tls,
        )
    except Exception as e:
        _log(write, f'  VAMI GET noproxy FAILED: {e}')
        return False
    if gr.status_code == 404:
        _log(write, '  VAMI noproxy endpoint not found — skip (non-VCSA?).')
        return True
    if gr.status_code != 200:
        _log(write, f'  VAMI GET noproxy HTTP {gr.status_code}: {gr.text[:400]!r}')
        return False
    try:
        data = gr.json()
    except Exception:
        _log(write, f'  VAMI GET noproxy: non-JSON body {gr.text[:300]!r}')
        return False
    current = [str(x).strip() for x in (data.get('value') or []) if str(x).strip()]
    merged = list(current)
    changed = False
    for a in adds:
        if a not in merged:
            merged.append(a)
            changed = True
    if not changed:
        _log(write, '  VAMI noproxy already includes OIDC issuer / Authentik / holorouter IP.')
        return True
    try:
        pr = requests.put(
            vami_url,
            auth=('root', root_password),
            headers={'Content-Type': 'application/json'},
            json={'servers': merged},
            timeout=120,
            verify=verify_tls,
        )
    except Exception as e:
        _log(write, f'  VAMI PUT noproxy FAILED: {e}')
        return False
    if pr.status_code not in (200, 204):
        _log(write, f'  VAMI PUT noproxy HTTP {pr.status_code}: {pr.text[:500]!r}')
        return False
    _log(write, f'  VAMI noproxy updated (+{", ".join(adds)}).')
    if not restart_vmware_vmon:
        _log(
            write,
            '  NOTE: vmware-vmon not restarted (authentik_skip_mgmt_vc_vmon_restart=true). '
            'VIDB may still use old NO_PROXY until vCenter reboot or: '
            '`systemctl restart vmware-vmon` on the management vCenter.',
        )
        return True
    _log(
        write,
        '  Restarting vmware-vmon so vsphere-ui picks up no-proxy (management UI unavailable ~2–5 min).',
    )
    rr = subprocess.run(
        f'sshpass -f {shlex.quote(creds_path)} ssh -o StrictHostKeyChecking=accept-new '
        f'{shlex.quote(f"root@{mgmt_vc}")} systemctl restart vmware-vmon',
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if rr.returncode != 0:
        _log(
            write,
            f'  vmware-vmon restart FAILED rc={rr.returncode} {(rr.stderr or rr.stdout)[:500]!r}',
        )
        return False
    # Wait for SSH and vsphere-ui to return (Fleet needs VIDB).
    deadline = time.time() + 900.0
    interval = 20.0
    while time.time() < deadline:
        chk = subprocess.run(
            f'sshpass -f {shlex.quote(creds_path)} ssh -o StrictHostKeyChecking=accept-new '
            f'-o ConnectTimeout=10 {shlex.quote(f"root@{mgmt_vc}")} '
            f'/usr/lib/vmware-vmon/vmon-cli --status vsphere-ui 2>/dev/null',
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (chk.stdout or '') + (chk.stderr or '')
        if chk.returncode == 0 and 'RunState: RUNNING' in out:
            _log(write, '  vmware-vmon: vsphere-ui is RUNNING again.')
            return True
        _log(write, '  Waiting for vsphere-ui after vmware-vmon restart…')
        time.sleep(interval)
    _log(write, '  TIMEOUT: vsphere-ui did not reach RUNNING within 15m after vmware-vmon restart.')
    return False


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
            np = pag.get('next')

            if isinstance(np, int) and np > 0 and np != page:
                page = np
                continue
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
            'sub_mode': 'user_username',
            'include_claims_in_id_token': True,
            'issuer_mode': 'per_provider',
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
    ) -> Tuple[bool, Optional[Any]]:
        """Return (success, scim_provider_pk). Idempotent: reuse SCIM provider by name or URL."""
        if dry_run:
            _log(self.write, f'  DRY-RUN would ensure SCIM provider {_redact({"name": scim_name, "url": scim_url})!s}')
            return True, None

        spk: Optional[Any] = None
        for s in self.paginate_list('providers/scim'):
            if s.get('name') == scim_name or (scim_url and (s.get('url') or '') == scim_url):
                spk = s.get('pk')
                _log(self.write, f'  Reusing Authentik SCIM provider name={scim_name!r} pk={spk}')
                break

        if spk is None:
            body = {
                'name': scim_name,
                'url': scim_url,
                'token': bearer_token,
                'verify_certificates': False,
            }
            code, data = self.post_json('providers/scim/', body)
            if code in (200, 201):
                spk = data['pk']
                _log(self.write, f'  Created SCIM provider pk={spk}')
            elif code == 400 and isinstance(data, dict) and 'already' in json.dumps(data).lower():
                for s in self.paginate_list('providers/scim'):
                    if s.get('name') == scim_name or (scim_url and (s.get('url') or '') == scim_url):
                        spk = s.get('pk')
                        _log(self.write, f'  SCIM provider already exists — reusing pk={spk}')
                        break
            if spk is None:
                _log(self.write, f'  SCIM provider create failed HTTP {code}: {_redact(data)!s}')
                return False, None
        else:
            # Refresh token on existing provider (Fleet may mint a new SCIM bearer each run).
            pcode, _ = self.patch_json(f'providers/scim/{int(spk)}/', {'token': bearer_token})
            if pcode not in (200, 201):
                _log(self.write, f'  WARNING: SCIM provider token PATCH HTTP {pcode} — continuing')

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


def ensure_vcenter_oidc(
    vc_base: str,
    session_id: str,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    write: Callable[[str], None],
    dry_run: bool,
    verify_tls: bool,
) -> bool:
    if requests is None:
        raise RuntimeError('requests required')
    hdr = {'vmware-api-session-id': session_id, 'Content-Type': 'application/json'}
    r = requests.get(f'{vc_base}/api/vcenter/identity/providers', headers=hdr, timeout=60, verify=verify_tls)
    if r.status_code != 200:
        raise RuntimeError(f'List identity providers HTTP {r.status_code}: {r.text[:400]}')
    provs = r.json()
    if isinstance(provs, list) and provs:
        for p in provs:
            if p.get('config_tag') == 'Oidc' and p.get('oidc', {}).get('client_id') == client_id:
                _log(write, f'  vCenter already has OIDC provider for client_id={client_id!r} — skip.')
                return True
    body = {
        'config_tag': 'Oidc',
        'oidc': {
            'claim_map': {},
            'client_id': client_id,
            'client_secret': client_secret,
            'discovery_endpoint': discovery_url,
        },
    }
    if dry_run:
        _log(write, f'  DRY-RUN would POST /api/vcenter/identity/providers {_redact(body)!s}')
        return True
    r2 = requests.post(
        f'{vc_base}/api/vcenter/identity/providers',
        headers=hdr,
        json=body,
        timeout=120,
        verify=verify_tls,
    )
    if r2.status_code not in (200, 201):
        _log(write, f'  vCenter OIDC registration FAILED HTTP {r2.status_code}: {r2.text[:800]}')
        return False
    _log(write, '  vCenter OIDC identity provider registered.')
    return True


def ensure_ops_vidb(
    ops_base: str,
    ops_token: str,
    display_name: str,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    write: Callable[[str], None],
    dry_run: bool,
    verify_tls: bool,
) -> bool:
    if requests is None:
        raise RuntimeError('requests required')
    hdr = {
        'Authorization': f'OpsToken {ops_token}',
        'X-vRealizeOps-API-use-unsupported': 'true',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    r = requests.get(f'{ops_base}/suite-api/api/auth/sources', headers=hdr, timeout=60, verify=verify_tls)
    if r.status_code != 200:
        _log(write, f'  List auth sources HTTP {r.status_code}: {r.text[:400]}')
        return False
    for src in r.json().get('sources') or []:
        if src.get('sourceType', {}).get('id') == 'VIDB':
            for prop in src.get('property') or []:
                if prop.get('name') == 'issuer-url' and prop.get('value', '').rstrip('/') == issuer_url.rstrip('/'):
                    _log(write, '  VCF Operations already has matching VIDB issuer-url — skip.')
                    return True
    payload = {
        'name': f'{display_name}-authentik',
        'sourceType': {'id': 'VIDB', 'name': 'VIDB'},
        'property': [
            {'name': 'display-name', 'value': display_name},
            {'name': 'issuer-url', 'value': issuer_url.rstrip('/') + '/'},
            {'name': 'client-id', 'value': client_id},
            {'name': 'client-secret', 'value': client_secret},
        ],
        'certificates': [],
    }
    if dry_run:
        _log(write, f'  DRY-RUN would POST /suite-api/api/auth/sources {_redact(payload)!s}')
        return True
    r2 = requests.post(
        f'{ops_base}/suite-api/api/auth/sources',
        headers=hdr,
        json=payload,
        timeout=120,
        verify=verify_tls,
    )
    if r2.status_code not in (200, 201):
        _log(write, f'  VIDB auth source create HTTP {r2.status_code}: {r2.text[:800]}')
        return False
    _log(write, '  VCF Operations VIDB auth source submitted (validate in UI if 500 during IdP test).')
    return True


def run_authentik_vcf_integration(
    lsf: Any = None,
    dry_run: bool = False,
    config_path: str = '/tmp/config.ini',
) -> bool:
    """
    Main entry for VCFfinal. Returns True on full success, False on partial failure.
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
    if lsf and hasattr(lsf, 'get_password'):
        password = lsf.get_password()
    else:
        password = _read_password(creds_path)

    verify_tls = False
    if cfg.has_option('VCFFINAL', 'authentik_verify_tls'):
        verify_tls = _truthy(cfg.get('VCFFINAL', 'authentik_verify_tls'))

    mgmt_vc, vc_user = _discover_mgmt_vc(cfg)
    ops_fqdn = _discover_ops_fqdn(cfg)
    vc_base = f'https://{mgmt_vc}'
    ops_base = f'https://{ops_fqdn}'

    issuer_base = 'https://auth.vcf.lab'
    if cfg.has_option('VCFFINAL', 'authentik_issuer_base'):
        issuer_base = cfg.get('VCFFINAL', 'authentik_issuer_base').strip().rstrip('/')

    app_slug = 'vcf'
    if cfg.has_option('VCFFINAL', 'authentik_application_slug'):
        app_slug = cfg.get('VCFFINAL', 'authentik_application_slug').strip()
    app_name = 'VCF'
    if cfg.has_option('VCFFINAL', 'authentik_application_name'):
        app_name = cfg.get('VCFFINAL', 'authentik_application_name').strip()

    tenant = 'CUSTOMER'
    if cfg.has_option('VCFFINAL', 'authentik_vcenter_tenant'):
        tenant = cfg.get('VCFFINAL', 'authentik_vcenter_tenant').strip()

    redirect_url = f'{vc_base}/federation/t/{tenant}/auth/response/oauth2'
    discovery_url = f'{issuer_base}/application/o/{app_slug}/.well-known/openid-configuration'
    scim_url = f'{vc_base}/usergroup/t/{tenant}/scim/v2'

    discovery_url_fleet = discovery_url
    if cfg.has_option('VCFFINAL', 'authentik_fleet_oidc_discovery_url'):
        fd = cfg.get('VCFFINAL', 'authentik_fleet_oidc_discovery_url').strip()
        if fd:
            discovery_url_fleet = fd
            _log(write, f'  Fleet IAM: OIDC discovery URL override {discovery_url_fleet!r}.')

    ak_token = os.environ.get('AUTHENTIK_API_TOKEN', 'holodeck')
    if cfg.has_option('VCFFINAL', 'authentik_api_token'):
        v = cfg.get('VCFFINAL', 'authentik_api_token').strip()
        if v:
            ak_token = v

    signing_key = 'e469fc95-d878-4042-abe9-1e46840fd125'
    if cfg.has_option('VCFFINAL', 'authentik_signing_key_pk'):
        signing_key = cfg.get('VCFFINAL', 'authentik_signing_key_pk').strip()
    elif requests:
        try:
            t_hdr = {'Authorization': f'Bearer {ak_token}', 'Accept': 'application/json'}
            kr = requests.get(
                f'{issuer_base}/api/v3/crypto/certificatekeypairs/',
                headers=t_hdr,
                timeout=30,
                verify=verify_tls,
            )
            if kr.status_code == 200:
                keys = kr.json().get('results') or []
                if keys:
                    signing_key = str(keys[0]['pk'])
        except Exception:
            pass

    oauth_provider_name = 'VCF OIDC'
    if cfg.has_option('VCFFINAL', 'authentik_oauth_provider_name'):
        oauth_provider_name = cfg.get('VCFFINAL', 'authentik_oauth_provider_name').strip()

    scim_provider_name = 'VCF SCIM'
    if cfg.has_option('VCFFINAL', 'authentik_scim_provider_name'):
        scim_provider_name = cfg.get('VCFFINAL', 'authentik_scim_provider_name').strip()

    skip_coredns = cfg.has_option('VCFFINAL', 'authentik_skip_coredns') and _truthy(
        cfg.get('VCFFINAL', 'authentik_skip_coredns'))
    skip_vcenter = cfg.has_option('VCFFINAL', 'authentik_skip_vcenter') and _truthy(
        cfg.get('VCFFINAL', 'authentik_skip_vcenter'))
    skip_vidb = cfg.has_option('VCFFINAL', 'authentik_skip_ops_vidb') and _truthy(
        cfg.get('VCFFINAL', 'authentik_skip_ops_vidb'))

    scim_token = ''
    if cfg.has_option('VCFFINAL', 'vcf_scim_bearer_token'):
        scim_token = cfg.get('VCFFINAL', 'vcf_scim_bearer_token').strip()

    skip_fleet_iam = cfg.has_option('VCFFINAL', 'authentik_skip_fleet_iam') and _truthy(
        cfg.get('VCFFINAL', 'authentik_skip_fleet_iam'))
    skip_mgmt_vc_no_proxy = cfg.has_option('VCFFINAL', 'authentik_skip_mgmt_vc_no_proxy') and _truthy(
        cfg.get('VCFFINAL', 'authentik_skip_mgmt_vc_no_proxy'))
    skip_mgmt_vc_vmon_restart = cfg.has_option('VCFFINAL', 'authentik_skip_mgmt_vc_vmon_restart') and _truthy(
        cfg.get('VCFFINAL', 'authentik_skip_mgmt_vc_vmon_restart'))
    use_sso_ui_prereqs = cfg.has_option('VCFFINAL', 'authentik_sso_ui_prerequisites') and _truthy(
        cfg.get('VCFFINAL', 'authentik_sso_ui_prerequisites'))
    join_nsx = cfg.has_option('VCFFINAL', 'authentik_fleet_join_nsx') and _truthy(
        cfg.get('VCFFINAL', 'authentik_fleet_join_nsx'))
    vcf_role = 'vcf_administrator'
    if cfg.has_option('VCFFINAL', 'authentik_fleet_vcf_role'):
        vcf_role = cfg.get('VCFFINAL', 'authentik_fleet_vcf_role').strip() or vcf_role

    group_names = ['prod-admins', 'dev-admins']
    if cfg.has_option('VCFFINAL', 'authentik_scim_group_names'):
        g = cfg.get('VCFFINAL', 'authentik_scim_group_names').strip()
        if g:
            group_names = [x.strip() for x in g.split(',') if x.strip()]

    lab_user_emails = ['prod-admin@vcf.lab', 'dev-admin@vcf.lab']
    if cfg.has_option('VCFFINAL', 'authentik_directory_users'):
        u = cfg.get('VCFFINAL', 'authentik_directory_users').strip()
        if u:
            lab_user_emails = [x.strip() for x in u.split(',') if x.strip()]

    scim_domain = 'vcf.lab'
    if cfg.has_option('VCFFINAL', 'authentik_scim_domain'):
        d = cfg.get('VCFFINAL', 'authentik_scim_domain').strip()
        if d:
            scim_domain = d

    idp_fleet_name = 'VCF Auth'
    if cfg.has_option('VCFFINAL', 'authentik_fleet_idp_name'):
        idp_fleet_name = cfg.get('VCFFINAL', 'authentik_fleet_idp_name').strip() or idp_fleet_name

    directory_fleet_name = scim_domain
    if cfg.has_option('VCFFINAL', 'authentik_fleet_directory_name'):
        directory_fleet_name = cfg.get('VCFFINAL', 'authentik_fleet_directory_name').strip() or directory_fleet_name

    issuer_host = urlparse(issuer_base if '://' in issuer_base else f'https://{issuer_base}').hostname or 'auth.vcf.lab'

    _log(write, '=== Authentik + VCF integration (VCFFINAL.authentik_vcf_integration) ===')
    ok = True

    if not skip_coredns:
        if not run_coredns_patch(creds_path, write, dry_run):
            ok = False
    else:
        _log(write, 'Skipping CoreDNS patch (authentik_skip_coredns).')

    if requests is None:
        _log(write, 'ERROR: Python requests module missing — install requests.')
        return False

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

    if dry_run or not client_secret:
        _log(write, 'Dry-run or reused provider without secret — skipping downstream IdP / SCIM steps.')
        return ok

    # --- Authentik directory: groups + users (for SCIM into vCenter) ---
    group_pk_by_name: Dict[str, Any] = {}
    for gname in group_names:
        gpk = ak.ensure_group(gname, dry_run)
        if gpk is not None:
            group_pk_by_name[gname] = gpk
    for email in lab_user_emails:
        local = email.split('@')[0].lower()
        if local.startswith('prod'):
            gname = 'prod-admins' if 'prod-admins' in group_names else group_names[0]
        else:
            gname = 'dev-admins' if 'dev-admins' in group_names else group_names[-1]
        gpk = group_pk_by_name.get(gname)
        if not gpk:
            _log(write, f'  WARNING: missing group pk for {gname!r} — skip user {email!r}')
            ok = False
            continue
        disp = local.replace('-', ' ').title()
        login_username = email
        if not ak.ensure_user_username(login_username, disp, email, gpk, dry_run):
            ok = False

    use_fleet_iam = not skip_fleet_iam
    if (use_fleet_iam or not skip_vcenter) and not skip_mgmt_vc_no_proxy:
        if not ensure_mgmt_vcenter_no_proxy_for_oidc(
            mgmt_vc,
            issuer_host,
            password,
            creds_path,
            write,
            dry_run,
            verify_tls,
            restart_vmware_vmon=not skip_mgmt_vc_vmon_restart,
        ):
            ok = False
    elif skip_mgmt_vc_no_proxy:
        _log(write, 'Skipping management vCenter NO_PROXY patch (authentik_skip_mgmt_vc_no_proxy).')

    if use_fleet_iam:
        _log(write, 'Fleet IAM path: VCF Operations suite-api (SSO realm, OIDC+SCIM IdP, SCIM token).')
        otok: Optional[str] = None
        try:
            otok = _ops_token(ops_base, password, verify_tls)
            if use_sso_ui_prereqs:
                try:
                    from vcf_sso_ui_prereqs import submit_sso_prerequisites_ui
                except ImportError:
                    submit_sso_prerequisites_ui = None  # type: ignore
                if submit_sso_prerequisites_ui:
                    if not submit_sso_prerequisites_ui(ops_fqdn, password, write, dry_run):
                        _log(
                            write,
                            '  WARNING: SSO UI automation failed — complete Prerequisites, then click '
                            '**Configure SSO** on Get Started (see HOL_Authentik_Config_Cycle_7.md Step 3), '
                            'or install Playwright (Tools/vcf_sso_ui_prereqs.py). '
                            'Continuing with Fleet IAM API calls.',
                        )
                else:
                    _log(write, '  WARNING: vcf_sso_ui_prereqs module not importable — skipping UI prerequisites.')
            else:
                _log(
                    write,
                    '  SSO UI prerequisites automation skipped (set authentik_sso_ui_prerequisites=true '
                    'after: pip install playwright && playwright install chromium).',
                )
            fleet_ok, fleet_scim_tok, realm_id, vidb_rid = run_fleet_iam_vcf_sso(
                ops_base,
                otok,
                mgmt_vc,
                issuer_host,
                discovery_url_fleet,
                client_id,
                client_secret,
                scim_domain,
                idp_fleet_name,
                directory_fleet_name,
                write,
                verify_tls,
                dry_run,
            )
            if not fleet_ok or not fleet_scim_tok or not realm_id or not vidb_rid:
                ok = False
            else:
                scim_linked, scim_pk = ak.ensure_scim_backchannel(
                    app_slug, scim_url, fleet_scim_tok, scim_provider_name, dry_run
                )
                if not scim_linked:
                    ok = False
                else:

                    def _sync() -> bool:
                        if dry_run or scim_pk is None:
                            return True
                        return ak.trigger_scim_provider_sync(scim_pk)

                    if not fleet_iam_post_scim_assign_and_join(
                        ops_base,
                        otok,
                        vidb_rid,
                        realm_id,
                        _sync,
                        write,
                        verify_tls,
                        join_nsx,
                        group_names,
                        vcf_role,
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
    else:
        _log(write, 'Legacy path (authentik_skip_fleet_iam): vCenter OIDC + VIDB auth source.')
        if not skip_vcenter:
            try:
                sess = _vcenter_session(vc_base, vc_user, password, verify_tls)
                if not ensure_vcenter_oidc(
                    vc_base, sess, discovery_url, client_id, client_secret, write, dry_run, verify_tls
                ):
                    ok = False
            except Exception as e:
                _log(write, f'vCenter OIDC step FAILED: {e}')
                ok = False
        else:
            _log(write, 'Skipping vCenter OIDC (authentik_skip_vcenter).')

        if not skip_vidb:
            try:
                otok = _ops_token(ops_base, password, verify_tls)
                issuer_url = f'{issuer_base}/application/o/{app_slug}/'
                if not ensure_ops_vidb(
                    ops_base, otok, 'VCF Auth', issuer_url, client_id, client_secret, write, dry_run, verify_tls
                ):
                    ok = False
            except Exception as e:
                _log(write, f'VCF Operations VIDB step FAILED: {e}')
                ok = False
        else:
            _log(write, 'Skipping VCF Operations VIDB (authentik_skip_ops_vidb).')

        if scim_token:
            sc_ok, _spk = ak.ensure_scim_backchannel(
                app_slug, scim_url, scim_token, scim_provider_name, dry_run
            )
            if not sc_ok:
                ok = False
        else:
            _log(write, 'No [VCFFINAL] vcf_scim_bearer_token — skipping Authentik SCIM provider (legacy path).')

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
