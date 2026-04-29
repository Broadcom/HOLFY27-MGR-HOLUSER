"""
TODO: This script is a work in progress. It is not yet complete.
VERSION: 0.0.1 - 2026-04-27
AUTHOR: Burke Azbill and HOL Core Team

This script is used to configure the Authentik identity provider for VCF SSO (OIDC + SCIM).
It uses the VCF Operations Fleet IAM APIs to create the necessary resources.

Usage:
python authentik_fleet_iam.py

VCF Operations Fleet IAM APIs for VCF SSO (OIDC + SCIM).

Uses public suite-api paths documented in suite-api/doc/openapi/v3/public-api.json
(operationIds configureIDP, generateScimSyncClient, createIamComponentAuthSource, etc.).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None  # type: ignore


def _log(write: Optional[Callable[[str], None]], msg: str) -> None:
    if write:
        write(msg)


def sanitize_fleet_iam_directory_name(label: str) -> str:
    """
    Fleet IAM ``directories[].name`` (validName): 1–128 chars; only alphanumeric,
    space, hyphen, or underscore. Values like ``vcf.lab`` (SCIM default domain) are rejected
    if passed verbatim — normalize invalid characters to underscores.
    """
    if not (label or '').strip():
        return 'Authentik'
    chars: List[str] = []
    for ch in label.strip()[:128]:
        if ch.isalnum() or ch in (' ', '-', '_'):
            chars.append(ch)
        else:
            chars.append('_')
    s = ''.join(chars).strip(' _-') or 'Authentik'
    return s[:128]


def fetch_tls_pem_chain_for_host(
    hostname: str,
    port: int = 443,
    servername: Optional[str] = None,
) -> List[str]:
    """
    Return PEM blocks (leaf first, then chain) for the server's TLS chain,
    using openssl s_client (no extra Python deps).

    When ``hostname`` is a numeric IP but the certificate is issued for a DNS name
    (e.g. Authentik on ``192.168.0.2`` with cert for ``auth.vcf.lab``), pass
    ``servername`` for TLS SNI.
    """
    sni = servername or hostname
    cmd = (
        f'echo | openssl s_client -connect {hostname}:{port} -servername {sni} '
        '-showcerts 2>/dev/null'
    )
    out = subprocess.check_output(cmd, shell=True, text=True, timeout=60)
    blocks = re.findall(
        r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
        out,
        flags=re.DOTALL,
    )
    if not blocks:
        raise RuntimeError(f'No PEM certificates in openssl output for {hostname}:{port} sni={sni!r}')
    # leaf first; include root (last) for trust chain
    if len(blocks) == 1:
        return blocks
    return [blocks[0], blocks[-1]]


def tls_chain_pems_for_oidc_discovery_url(discovery_url: str, logical_host_for_sni: str) -> List[str]:
    """Build ``certificateChain`` PEM list for Fleet IAM from the discovery URL host/scheme."""
    from urllib.parse import urlparse

    u = urlparse(discovery_url)
    scheme = (u.scheme or 'https').lower()
    if scheme == 'http':
        return pem_blocks_to_certificate_chain_field(
            fetch_tls_pem_chain_for_host(logical_host_for_sni),
        )
    host = u.hostname or logical_host_for_sni
    port = u.port or 443
    if re.match(r'^(\d{1,3}\.){3}\d{1,3}$', host or ''):
        return pem_blocks_to_certificate_chain_field(
            fetch_tls_pem_chain_for_host(host, port, servername=logical_host_for_sni),
        )
    return pem_blocks_to_certificate_chain_field(fetch_tls_pem_chain_for_host(host, port))


def pem_blocks_to_certificate_chain_field(pems: List[str]) -> List[str]:
    """
    Build Fleet IAM ``certificateChain``: an array of full PEM certificate strings.

    VIDB treats each JSON array element as one certificate (max three). Passing one
    string per PEM *line* is misread as hundreds of chains and fails with
    ``idp.pem.certificate.chains.max.exceeded``.
    """
    out: List[str] = []
    seen: set = set()
    for pem in pems:
        block = pem.strip()
        if not block or block in seen:
            continue
        seen.add(block)
        out.append(block)
        if len(out) >= 3:
            break
    if not out:
        raise RuntimeError('empty PEM list for Fleet IAM certificateChain')
    return out


class FleetIamClient:
    def __init__(
        self,
        ops_base: str,
        ops_token: str,
        write: Optional[Callable[[str], None]],
        verify_tls: bool,
    ):
        if requests is None:
            raise RuntimeError('requests required for Fleet IAM')
        self.base = ops_base.rstrip('/')
        self.verify = verify_tls
        self.write = write
        self._hdr = {
            'Authorization': f'OpsToken {ops_token}',
            'X-vRealizeOps-API-use-unsupported': 'true',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _url(self, path: str) -> str:
        path = path if path.startswith('/') else '/' + path
        return f'{self.base}{path}'

    def get_json(self, path: str) -> Any:
        r = requests.get(self._url(path), headers=self._hdr, timeout=120, verify=self.verify)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: Optional[Dict]) -> Tuple[int, Any]:
        r = requests.post(
            self._url(path),
            headers=self._hdr,
            data=json.dumps(body) if body is not None else None,
            timeout=300,
            verify=self.verify,
        )
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text[:4000]}
        return r.status_code, data

    def put_json(self, path: str, body: Dict) -> Tuple[int, Any]:
        r = requests.put(
            self._url(path),
            headers=self._hdr,
            data=json.dumps(body),
            timeout=300,
            verify=self.verify,
        )
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text[:4000]}
        return r.status_code, data

    def find_embedded_vidb_for_vc(self, mgmt_vc_fqdn: str) -> Dict[str, Any]:
        data = self.get_json('/suite-api/api/fleet-management/iam/vidbs')
        vidbs = data.get('vidbs') or []
        for v in vidbs:
            if (v.get('deploymentType') or '').upper() == 'EMBEDDED' and (
                (v.get('fqdn') or '').lower() == mgmt_vc_fqdn.lower()
            ):
                return v
        for v in vidbs:
            if (v.get('deploymentType') or '').upper() == 'EMBEDDED':
                return v
        raise RuntimeError(f'No EMBEDDED VIDB found for Fleet IAM (vidbs={len(vidbs)})')

    def ensure_sso_realm(self, vidb_resource_id: str) -> Dict[str, Any]:
        data = self.get_json('/suite-api/api/fleet-management/iam/ssorealms')
        for realm in data.get('ssoRealms') or []:
            if (realm.get('vidbResourceId') or '').lower() == vidb_resource_id.lower():
                return realm
        code, body = self.post_json(
            '/suite-api/api/fleet-management/iam/ssorealms',
            {'vidbResourceId': vidb_resource_id},
        )
        if code not in (200, 201):
            raise RuntimeError(f'createSsoRealm HTTP {code}: {body}')
        return body

    def get_identity_provider(self, idp_id: str) -> Any:
        return self.get_json(f'/suite-api/api/fleet-management/iam/identity-providers/{idp_id}')

    def build_oidc_scim_idp_body(
        self,
        realm_id: str,
        name: str,
        discovery_url: str,
        client_id: str,
        client_secret: str,
        domain: str,
        directory_display_name: str,
        certificate_chain_pems: List[str],
        idp_type: str = 'SYMANTEC_IDSP',
        idp_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            'name': name,
            'ssoRealmId': realm_id,
            'idpType': idp_type,
            'idpProtocol': 'OIDC',
            'provisionType': 'SCIM',
            'idpConfig': {
                'oidcConfiguration': {
                    'discoveryEndpoint': discovery_url,
                    'clientId': client_id,
                    'clientSecret': client_secret,
                    'openIdUserIdentifierAttribute': 'sub',
                    'internalUserIdentifierAttribute': 'ExternalId',
                }
            },
            'directories': [
                {
                    'name': sanitize_fleet_iam_directory_name(directory_display_name),
                    'domains': [domain],
                    'defaultDomain': domain,
                }
            ],
            'certificateChain': certificate_chain_pems,
        }
        if idp_id:
            body['id'] = idp_id
        return body

    def configure_identity_provider(
        self,
        realm: Dict[str, Any],
        idp_body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST new IDP or PUT update if realm already references an idp."""
        idp_id = realm.get('idpId')
        if idp_id:
            idp_body['id'] = idp_id
            idp_body['ssoRealmId'] = realm['id']
            # PUT update requires directory UUIDs; POST create omits them.
            try:
                existing = self.get_identity_provider(idp_id)
                ex_dirs = existing.get('directories') or []
                body_dirs = idp_body.get('directories') or []
                for i, d in enumerate(body_dirs):
                    if d.get('id'):
                        continue
                    if i < len(ex_dirs) and ex_dirs[i].get('id'):
                        d['id'] = ex_dirs[i]['id']
                    else:
                        for ex in ex_dirs:
                            if (ex.get('name') or '') == (d.get('name') or '') and ex.get('id'):
                                d['id'] = ex['id']
                                break
            except Exception:
                pass
            code, body = self.put_json(
                '/suite-api/api/fleet-management/iam/identity-providers',
                idp_body,
            )
            if code not in (200, 201, 204):
                raise RuntimeError(f'updateIDPConfiguration HTTP {code}: {body}')
            if isinstance(body, dict) and body.get('id'):
                return body
            return self.get_identity_provider(idp_id)
        code, body = self.post_json('/suite-api/api/fleet-management/iam/identity-providers', idp_body)
        if code not in (200, 201):
            raise RuntimeError(f'configureIDP HTTP {code}: {body}')
        return body

    def generate_scim_bearer_token(
        self,
        idp_config_id: str,
        directory_id: str,
        token_ttl_minutes: int = 262800,
    ) -> str:
        payload = {'tokenTtl': token_ttl_minutes, 'generateToken': True}
        code, body = self.post_json(
            f'/suite-api/api/fleet-management/iam/identity-providers/{idp_config_id}'
            f'/directories/{directory_id}/sync-client',
            payload,
        )
        if code not in (200, 201):
            raise RuntimeError(f'generateScimSyncClient HTTP {code}: {body}')
        tok = (
            (body.get('accessTokenDetails') or {}).get('accessToken')
            or (body.get('scimClientDetails') or {}).get('clientSecret')
        )
        if not tok:
            raise RuntimeError(f'No SCIM bearer token in response keys={list(body.keys())}')
        return str(tok)

    def get_eligible_vcenter_component_id(self, vidb_resource_id: str) -> str:
        """Pick the eligible VCF component whose resource id matches the embedded VIDB (vCenter)."""
        data = self.get_json('/suite-api/api/fleet-management/iam/components')
        want = (vidb_resource_id or '').lower()
        for c in data.get('eligibleComponents') or []:
            if (c.get('eligibilityStatus') or '').upper() != 'ELIGIBLE':
                continue
            rid = (c.get('vcfComponentResourceId') or c.get('vcfComponentId') or '').lower()
            if rid and rid == want:
                return c['vcfComponentId']
        for c in data.get('eligibleComponents') or []:
            if (c.get('eligibilityStatus') or '').upper() == 'ELIGIBLE':
                return c['vcfComponentId']
        raise RuntimeError('No ELIGIBLE vCenter-like component in fleet-management/iam/components')

    def join_sso_vcf_component(
        self,
        sso_realm_id: str,
        vcf_component_id: str,
    ) -> Tuple[int, Any]:
        return self.post_json(
            '/suite-api/api/fleet-management/iam/components/auth-sources',
            {
                'ssoRealmId': sso_realm_id,
                'authSourceType': 'VCF_COMPONENT',
                'vcfComponentId': vcf_component_id,
            },
        )

    def join_sso_management_component(
        self,
        sso_realm_id: str,
        component_type: str,
    ) -> Tuple[int, Any]:
        return self.post_json(
            '/suite-api/api/fleet-management/iam/components/auth-sources',
            {
                'ssoRealmId': sso_realm_id,
                'authSourceType': 'MANAGEMENT_COMPONENT',
                'componentType': component_type,
            },
        )

    def groups_query(
        self,
        sso_realm_id: str,
        display_name: str,
        page: int = 0,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        body = {
            'searchTerms': {
                'allOf': [],
                'anyOf': [
                    {
                        'searchableField': 'DISPLAY_NAME',
                        'terms': [display_name],
                        'operator': 'EQUALS',
                    }
                ],
            }
        }
        path = (
            f'/suite-api/api/fleet-management/iam/ssorealms/{sso_realm_id}/groups/query'
            f'?page={page}&pageSize={page_size}'
        )
        code, data = self.post_json(path, body)
        if code != 200:
            raise RuntimeError(f'getGroupsList HTTP {code}: {data}')
        return data.get('vidbGroups') or []

    def put_principal_roles(
        self,
        sso_realm_id: str,
        principal_id: str,
        role_assignments: List[Dict[str, Any]],
    ) -> int:
        path = (
            f'/suite-api/api/fleet-management/iam/ssorealms/{sso_realm_id}'
            f'/principals/{principal_id}/roles'
        )
        r = requests.put(
            self._url(path),
            headers=self._hdr,
            json={'vcfRoleAssignments': role_assignments},
            timeout=300,
            verify=self.verify,
        )
        return r.status_code

    def refresh_realm(self, realm_id: str) -> Dict[str, Any]:
        return self.get_json(f'/suite-api/api/fleet-management/iam/ssorealms/{realm_id}')


def wait_for_group(
    client: FleetIamClient,
    sso_realm_id: str,
    group_display_name: str,
    write: Optional[Callable[[str], None]],
    timeout_sec: int = 300,
    interval_sec: int = 10,
) -> Optional[str]:
    """Return groupId (principal id) when the group appears in the realm."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        groups = client.groups_query(sso_realm_id, group_display_name)
        gwant = group_display_name.lower()
        for g in groups:
            dn = (g.get('displayName') or g.get('groupName') or '').lower()
            if dn == gwant or gwant in dn or dn in gwant:
                gid = g.get('groupId')
                if gid:
                    return str(gid)
        _log(write, f'  Waiting for SCIM-provisioned group {group_display_name!r}...')
        time.sleep(interval_sec)
    return None


def assign_vcf_admin_to_groups(
    client: FleetIamClient,
    sso_realm_id: str,
    group_display_names: List[str],
    role_name: str,
    write: Optional[Callable[[str], None]],
) -> bool:
    ok = True
    assignment = [
        {
            'roleName': role_name,
            'roleScope': {'resources': [], 'scopeType': 'SSO_REALM'},
        }
    ]
    for gname in group_display_names:
        gid = wait_for_group(client, sso_realm_id, gname, write)
        if not gid:
            _log(write, f'  Fleet IAM: group {gname!r} not found after SCIM wait — skip role bind.')
            ok = False
            continue
        code = client.put_principal_roles(sso_realm_id, gid, assignment)
        if code not in (200, 204):
            _log(write, f'  Fleet IAM: PUT roles for {gname} HTTP {code}')
            ok = False
        else:
            _log(write, f'  Fleet IAM: assigned {role_name!r} to group {gname!r}.')
    return ok


def join_default_sso_components(
    client: FleetIamClient,
    sso_realm_id: str,
    vidb_resource_id: str,
    write: Optional[Callable[[str], None]],
    join_nsx: bool = False,
) -> bool:
    ok = True
    try:
        vcid = client.get_eligible_vcenter_component_id(vidb_resource_id)
    except Exception as e:
        _log(write, f'  Fleet IAM: could not resolve vCenter component id: {e}')
        return False
    for label, fn, args in [
        ('VCENTER', client.join_sso_vcf_component, (sso_realm_id, vcid)),
        ('VCF_OPERATIONS', client.join_sso_management_component, (sso_realm_id, 'VCF_OPERATIONS')),
        ('VCF_AUTOMATION', client.join_sso_management_component, (sso_realm_id, 'VCF_AUTOMATION')),
    ]:
        code, body = fn(*args)
        if code in (200, 201, 204):
            _log(write, f'  Fleet IAM: Join SSO {label} OK (HTTP {code}).')
        elif code == 409 or (isinstance(body, dict) and 'already' in json.dumps(body).lower()):
            _log(write, f'  Fleet IAM: Join SSO {label} already configured — skip.')
        else:
            _log(write, f'  Fleet IAM: Join SSO {label} HTTP {code}: {str(body)[:500]}')
            ok = False
    if join_nsx:
        data = client.get_json('/suite-api/api/fleet-management/iam/components')
        for c in data.get('eligibleComponents') or []:
            cid = c.get('vcfComponentId')
            if not cid or cid == vcid:
                continue
            code, body = client.join_sso_vcf_component(sso_realm_id, cid)
            if code in (200, 201, 204):
                _log(write, f'  Fleet IAM: Join SSO additional component {cid[:8]}… OK.')
            else:
                _log(write, f'  Fleet IAM: Join SSO component {cid[:8]}… HTTP {code}: {str(body)[:400]}')
    return ok


def log_fleet_sso_realm_summary(
    ops_base: str,
    ops_token: str,
    write: Optional[Callable[[str], None]],
    verify_tls: bool,
) -> None:
    """Log current SSO realm rows (idpId, component counts) for UI/API troubleshooting."""
    try:
        c = FleetIamClient(ops_base, ops_token, write, verify_tls)
        data = c.get_json('/suite-api/api/fleet-management/iam/ssorealms')
        realms = data.get('ssoRealms') or []
        if not realms:
            _log(write, '  Fleet IAM diagnostic: no SSO realms returned by GET ssorealms.')
            return
        for r in realms:
            has_idp = bool(r.get('idpId'))
            _log(
                write,
                '  Fleet IAM diagnostic: '
                f"realm name={r.get('name')!r} id={r.get('id')} "
                f"idpId={r.get('idpId')} "
                f"totalConfiguredComponents={r.get('totalConfiguredComponents')} "
                f"issues={len(r.get('issues') or [])}",
            )
            if not has_idp:
                _log(
                    write,
                    '  Fleet IAM diagnostic: no idpId — SSO Overview will look empty. '
                    'Prerequisites checkboxes alone do not create an IdP; run '
                    'Tools/authentik_vcf_integration.py (Fleet IAM) or POST '
                    '.../identity-providers. If configure failed with '
                    'idp.pem.certificate.chains.max.exceeded, update Tools (PEM array fix). '
                    'OIDC discovery must return JSON from the ops appliance: '
                    'curl -sk https://<issuer>/application/o/<slug>/.well-known/openid-configuration',
                )
    except Exception as e:
        _log(write, f'  Fleet IAM diagnostic: could not read ssorealms: {e}')


def run_fleet_iam_vcf_sso(
    ops_base: str,
    ops_token: str,
    mgmt_vc_fqdn: str,
    issuer_host: str,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    scim_domain: str,
    idp_display_name: str,
    directory_name: str,
    write: Optional[Callable[[str], None]],
    verify_tls: bool,
    dry_run: bool,
    idp_type_try: Tuple[str, ...] = ('SYMANTEC_IDSP', 'OTHER'),
) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Configure Fleet IAM OIDC+SCIM IdP, mint SCIM bearer token.
    Returns (ok, scim_bearer_token, sso_realm_id, vidb_resource_id).
    On dry_run returns (True, None, None, None).
    """
    client = FleetIamClient(ops_base, ops_token, write, verify_tls)
    if dry_run:
        _log(write, '  Fleet IAM: DRY-RUN — skip realm/IdP/SCIM/Join SSO API calls.')
        return True, None, None, None

    vidb = client.find_embedded_vidb_for_vc(mgmt_vc_fqdn)
    vidb_rid = vidb['id']
    _log(write, f'  Fleet IAM: using EMBEDDED VIDB {vidb.get("fqdn")!r} id={vidb_rid}')

    realm = client.ensure_sso_realm(vidb_rid)
    realm_id = realm['id']
    _log(write, f'  Fleet IAM: SSO realm id={realm_id} name={realm.get("name")!r}')

    realm = client.refresh_realm(realm_id)
    chain_pems = tls_chain_pems_for_oidc_discovery_url(discovery_url, issuer_host)

    dir_sanitized = sanitize_fleet_iam_directory_name(directory_name)
    if dir_sanitized != (directory_name or '').strip():
        _log(
            write,
            '  Fleet IAM: directory name for API '
            f'{directory_name!r} → {dir_sanitized!r} '
            '(validName: alphanumeric, space, hyphen, underscore only).',
        )

    last_err: Optional[str] = None
    idp_resp: Optional[Dict[str, Any]] = None
    for idp_type in idp_type_try:
        try:
            body = client.build_oidc_scim_idp_body(
                realm_id=realm_id,
                name=idp_display_name,
                discovery_url=discovery_url,
                client_id=client_id,
                client_secret=client_secret,
                domain=scim_domain,
                directory_display_name=directory_name,
                certificate_chain_pems=chain_pems,
                idp_type=idp_type,
                idp_id=realm.get('idpId'),
            )
            idp_resp = client.configure_identity_provider(realm, body)
            _log(write, f'  Fleet IAM: Identity provider configured (idpType={idp_type}).')
            break
        except Exception as e:
            last_err = str(e)
            _log(write, f'  Fleet IAM: IdP configure attempt {idp_type!r} failed: {e}')
            realm = client.refresh_realm(realm_id)
    if idp_resp is None:
        _log(write, f'  Fleet IAM: all IdP type attempts failed: {last_err}')
        return False, None, None, None

    idp_config_id = idp_resp.get('id')
    dirs = idp_resp.get('directories') or []
    if not idp_config_id or not dirs:
        _log(write, f'  Fleet IAM: unexpected IdP response (missing id/directories): {list(idp_resp.keys())}')
        return False, None, None, None
    directory_id = dirs[0].get('id')
    if not directory_id:
        _log(write, '  Fleet IAM: directory id missing in IdP response')
        return False, None, None, None

    try:
        scim_tok = client.generate_scim_bearer_token(idp_config_id, directory_id)
    except Exception as e:
        _log(write, f'  Fleet IAM: SCIM token generation failed: {e}')
        return False, None, None, None
    _log(write, '  Fleet IAM: SCIM bearer token generated (not logged).')

    return True, scim_tok, realm_id, vidb_rid


def fleet_iam_post_scim_assign_and_join(
    ops_base: str,
    ops_token: str,
    vidb_resource_id: str,
    sso_realm_id: str,
    authentik_scim_sync: Callable[[], bool],
    write: Optional[Callable[[str], None]],
    verify_tls: bool,
    join_nsx: bool,
    group_names: List[str],
    vcf_role: str,
) -> bool:
    """After Authentik SCIM provider exists: run sync, assign roles, join SSO."""
    client = FleetIamClient(ops_base, ops_token, write, verify_tls)
    sync_ok = authentik_scim_sync()
    if not sync_ok:
        _log(write, '  WARNING: Authentik SCIM sync trigger reported failure — continuing with Join SSO.')
    assign_ok = assign_vcf_admin_to_groups(client, sso_realm_id, group_names, vcf_role, write)
    join_ok = join_default_sso_components(client, sso_realm_id, vidb_resource_id, write, join_nsx=join_nsx)
    if not assign_ok:
        _log(
            write,
            '  WARNING: VCF role assignment to SCIM groups incomplete (groups may sync later). '
            'Re-run this script after Authentik→Fleet SCIM sync, or trigger sync in Authentik UI.',
        )
    # Join SSO is what enables federated login on vCenter / Ops / VCFA; role bind is best-effort.
    return join_ok
