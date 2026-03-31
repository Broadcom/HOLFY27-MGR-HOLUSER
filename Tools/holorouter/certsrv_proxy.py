#!/usr/bin/env python3
"""
VCF CA Proxy - Production

This software is not developed, endorsed, or affiliated with Microsoft Corporation. 
It provides a certsrv-compatible interface for interoperability with products that 
expect a Microsoft ADCS Web Enrollment endpoint, and translates requests to 
HashiCorp Vault PKI API calls.

This production version runs plain HTTP on port 8900 (configurable).
TLS termination is handled by Reverse Proxy via an IngressRoute for ca.vcf.lab.

Implements the certsrv protocol endpoints:
  GET  /certsrv/              - Home page (Welcome)
  GET  /certsrv/certrqus.asp  - Request a Certificate
  GET  /certsrv/certrqad.asp  - Advanced Certificate Request
  GET  /certsrv/certrqma.asp  - Advanced Request Form
  GET  /certsrv/certrqxt.asp  - Paste CSR (base-64 encoded)
  GET  /certsrv/certckpn.asp  - Pending request status
  GET  /certsrv/certcarc.asp  - CA download + certificate management
  POST /certsrv/certfnsh.asp  - CSR submission / User cert auto-issue
  GET  /certsrv/certnew.cer   - Certificate retrieval (issued or CA)
  GET  /certsrv/certnew.p7b   - CA chain (PKCS#7)
  GET  /certsrv/api/certs     - JSON list of all issued certificates
  GET  /certsrv/api/cert/<serial>?fmt=pem|der|p7b - Download issued cert
  POST /certsrv/api/revoke    - Revoke certificates by serial number

Usage:
  python3 certsrv_proxy.py \\
    --vault-url http://127.0.0.1:32000 \\
    --vault-token <token> \\
    --creds-file /root/creds.txt
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import threading
import time
import secrets
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote_plus

import requests
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-7s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('certsrv-proxy')


class CertStore:
    """Thread-safe in-memory certificate store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._certs = {}
        self._next_id = 1

    def store(self, cert_pem: str) -> int:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._certs[req_id] = cert_pem
            return req_id

    def get(self, req_id: int) -> str | None:
        with self._lock:
            return self._certs.get(req_id)


class KeyStore:
    """Thread-safe in-memory store for generated Private Keys and CSRs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def store(self, serial: str, key_pem: str, csr_pem: str):
        with self._lock:
            self._data[serial] = {
                'key': key_pem,
                'csr': csr_pem
            }

    def get(self, serial: str) -> dict | None:
        with self._lock:
            return self._data.get(serial)


class SessionManager:
    """Thread-safe in-memory session store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}

    def create_session(self, username: str) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = {
                'username': username,
                'created': time.time()
            }
        return session_id

    def get_username(self, session_id: str) -> str | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            if time.time() - session['created'] > 86400:  # 24 hour expiry
                del self._sessions[session_id]
                return None
            return session['username']


class VaultPKIClient:
    """Vault PKI client for CSR signing, CA cert retrieval, listing, and revocation."""

    def __init__(self, vault_url: str, vault_token: str,
                 pki_mount: str = 'pki', default_pki_role: str = 'holodeck',
                 cert_ttl: str = '17520h', skip_verify: bool = True,
                 role_mapping: dict = None):
        self.vault_url = vault_url.rstrip('/')
        self.vault_token = vault_token
        self.pki_mount = pki_mount
        self.default_pki_role = default_pki_role
        self.cert_ttl = cert_ttl
        self.verify = not skip_verify
        self._cert_cache = None
        self._cert_cache_time = 0
        self.role_mapping = role_mapping or {}

    def _headers(self):
        return {'X-Vault-Token': self.vault_token}

    def _get_role_for_template(self, template_name: str) -> str:
        """Resolve the Vault PKI role based on the requested AD CS template."""
        if not template_name or template_name == 'Unknown':
            return self.default_pki_role
        # Try direct match
        if template_name in self.role_mapping:
            return self.role_mapping[template_name]
        # Try case-insensitive match
        template_lower = template_name.lower()
        for k, v in self.role_mapping.items():
            if k.lower() == template_lower:
                return v
        # Fallback
        return self.default_pki_role

    def sign_csr(self, csr_pem: str, common_name: str, template_name: str = None) -> str | None:
        """Sign a CSR and return the PEM certificate bundle (cert + CA chain).

        Uses the sign-verbatim endpoint to preserve the full CSR subject DN
        (O, OU, C, ST, L) which SDDC Manager validates during installation.
        """
        role = self._get_role_for_template(template_name)
        url = f'{self.vault_url}/v1/{self.pki_mount}/sign-verbatim/{role}'
        payload = {
            'csr': csr_pem,
            'format': 'pem_bundle',
            'ttl': self.cert_ttl,
            'key_usage': ['DigitalSignature', 'KeyAgreement', 'KeyEncipherment'],
            'ext_key_usage': ['ServerAuth', 'ClientAuth'],
        }
        try:
            resp = requests.post(url, json=payload, headers=self._headers(),
                                 timeout=30, verify=self.verify)
            if resp.status_code == 200:
                data = resp.json().get('data', {})
                cert = data.get('certificate', '')
                ca_chain = data.get('ca_chain', [])
                issuing_ca = data.get('issuing_ca', '')
                bundle = cert
                if ca_chain:
                    bundle += '\n' + '\n'.join(ca_chain)
                elif issuing_ca:
                    bundle += '\n' + issuing_ca
                return bundle
            else:
                logger.error('Vault sign failed (%d): %s', resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.error('Vault sign error: %s', e)
            return None

    def issue_certificate(self, common_name: str, template_name: str = None) -> dict | None:
        """Issue a certificate via Vault (generates key + cert). Returns full Vault response data."""
        role = self._get_role_for_template(template_name)
        url = f'{self.vault_url}/v1/{self.pki_mount}/issue/{role}'
        payload = {
            'common_name': common_name,
            'format': 'pem_bundle',
            'ttl': self.cert_ttl,
        }
        try:
            resp = requests.post(url, json=payload, headers=self._headers(),
                                 timeout=30, verify=self.verify)
            if resp.status_code == 200:
                return resp.json().get('data', {})
            logger.error('Vault issue failed (%d): %s', resp.status_code, resp.text[:200])
            return None
        except Exception as e:
            logger.error('Vault issue error: %s', e)
            return None

    def get_ca_cert_pem(self) -> str | None:
        """Retrieve the CA certificate in PEM format."""
        url = f'{self.vault_url}/v1/{self.pki_mount}/ca/pem'
        try:
            resp = requests.get(url, headers=self._headers(),
                                timeout=10, verify=self.verify)
            if resp.status_code == 200:
                return resp.text
            logger.error('Vault CA cert fetch failed (%d)', resp.status_code)
            return None
        except Exception as e:
            logger.error('Vault CA cert error: %s', e)
            return None

    def get_crl(self, encoding: str = 'der') -> bytes | None:
        """Retrieve the CRL from Vault in the specified format ('der' or 'pem')."""
        url = f'{self.vault_url}/v1/{self.pki_mount}/crl/{encoding}'
        try:
            resp = requests.get(url, headers=self._headers(),
                                timeout=10, verify=self.verify)
            if resp.status_code == 200:
                return resp.content
            logger.error('Vault CRL fetch failed (%d): %s', resp.status_code, resp.text[:200])
            return None
        except Exception as e:
            logger.error('Vault CRL fetch error: %s', e)
            return None

    def health_check(self) -> bool:
        """Verify Vault connectivity and token validity."""
        url = f'{self.vault_url}/v1/auth/token/lookup-self'
        try:
            resp = requests.get(url, headers=self._headers(),
                                timeout=10, verify=self.verify)
            return resp.status_code == 200
        except Exception:
            return False

    def list_certificates(self, force_refresh: bool = False) -> list[dict]:
        """List all issued certificates with parsed details. Cached for 30 seconds."""
        now = time.time()
        if not force_refresh and self._cert_cache is not None and (now - self._cert_cache_time) < 30:
            return self._cert_cache

        url = f'{self.vault_url}/v1/{self.pki_mount}/certs'
        try:
            resp = requests.request('LIST', url, headers=self._headers(),
                                    timeout=15, verify=self.verify)
            if resp.status_code != 200:
                logger.error('Vault list certs failed (%d)', resp.status_code)
                return self._cert_cache or []
            serials = resp.json().get('data', {}).get('keys', [])
        except Exception as e:
            logger.error('Vault list certs error: %s', e)
            return self._cert_cache or []

        certs = []
        for serial in serials:
            try:
                cert_url = f'{self.vault_url}/v1/{self.pki_mount}/cert/{serial}'
                r = requests.get(cert_url, headers=self._headers(),
                                 timeout=5, verify=self.verify)
                if r.status_code != 200:
                    continue
                data = r.json().get('data', {})
                pem = data.get('certificate', '')
                revocation_time = data.get('revocation_time', 0)
                if not pem:
                    continue

                cert_obj = x509.load_pem_x509_certificate(pem.encode())
                cn = ''
                for attr in cert_obj.subject:
                    if attr.oid == x509.oid.NameOID.COMMON_NAME:
                        cn = attr.value
                        break

                dns_sans = []
                ip_sans = []
                try:
                    san_ext = cert_obj.extensions.get_extension_for_class(
                        x509.SubjectAlternativeName)
                    dns_sans = san_ext.value.get_values_for_type(x509.DNSName)
                    ip_sans = [str(ip) for ip in
                               san_ext.value.get_values_for_type(x509.IPAddress)]
                except x509.ExtensionNotFound:
                    pass

                key_usage_parts = []
                try:
                    ku = cert_obj.extensions.get_extension_for_class(x509.KeyUsage).value
                    _ku_map = [
                        ('digital_signature', 'DigitalSignature'),
                        ('content_commitment', 'ContentCommitment'),
                        ('key_encipherment', 'KeyEncipherment'),
                        ('data_encipherment', 'DataEncipherment'),
                        ('key_agreement', 'KeyAgreement'),
                        ('key_cert_sign', 'KeyCertSign'),
                        ('crl_sign', 'CRLSign'),
                    ]
                    for attr_name, label in _ku_map:
                        try:
                            if getattr(ku, attr_name):
                                key_usage_parts.append(label)
                        except ValueError:
                            pass
                except x509.ExtensionNotFound:
                    pass

                _eku_oid_map = {
                    '1.3.6.1.5.5.7.3.1': 'ServerAuth',
                    '1.3.6.1.5.5.7.3.2': 'ClientAuth',
                    '1.3.6.1.5.5.7.3.3': 'CodeSigning',
                    '1.3.6.1.5.5.7.3.4': 'EmailProtection',
                    '1.3.6.1.5.5.7.3.8': 'TimeStamping',
                    '1.3.6.1.5.5.7.3.9': 'OCSPSigning',
                }
                try:
                    eku = cert_obj.extensions.get_extension_for_class(
                        x509.ExtendedKeyUsage).value
                    for usage in eku:
                        label = _eku_oid_map.get(usage.dotted_string, usage.dotted_string)
                        key_usage_parts.append(label)
                except x509.ExtensionNotFound:
                    pass

                certs.append({
                    'serial': serial,
                    'cn': cn,
                    'dns_sans': dns_sans,
                    'ip_sans': ip_sans,
                    'not_before': cert_obj.not_valid_before_utc.strftime('%Y-%m-%d %H:%M'),
                    'not_after': cert_obj.not_valid_after_utc.strftime('%Y-%m-%d %H:%M'),
                    'revoked': revocation_time > 0,
                    'key_usage': key_usage_parts,
                })
            except Exception as e:
                logger.debug('Failed to parse cert %s: %s', serial, e)
                continue

        certs.sort(key=lambda c: c.get('not_before', ''), reverse=True)
        self._cert_cache = certs
        self._cert_cache_time = now
        logger.info('Loaded %d certificates from Vault', len(certs))
        return certs

    def get_certificate_pem(self, serial: str) -> str | None:
        """Retrieve a single certificate PEM by serial number."""
        url = f'{self.vault_url}/v1/{self.pki_mount}/cert/{serial}'
        try:
            resp = requests.get(url, headers=self._headers(),
                                timeout=10, verify=self.verify)
            if resp.status_code == 200:
                return resp.json().get('data', {}).get('certificate', '')
            logger.error('Vault cert fetch failed (%d): %s', resp.status_code, resp.text[:200])
            return None
        except Exception as e:
            logger.error('Vault cert fetch error: %s', e)
            return None

    def revoke_certificate(self, serial_number: str) -> bool:
        """Revoke a certificate by serial number."""
        url = f'{self.vault_url}/v1/{self.pki_mount}/revoke'
        try:
            resp = requests.post(url, json={'serial_number': serial_number},
                                 headers=self._headers(), timeout=10, verify=self.verify)
            if resp.status_code == 200:
                self._cert_cache = None
                logger.info('Revoked certificate: %s', serial_number)
                return True
            logger.error('Revoke failed (%d): %s', resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            logger.error('Revoke error: %s', e)
            return False


def normalize_csr_pem(raw: str) -> str:
    """Normalize a CSR into valid PEM format.

    Handles CSRs that arrive with missing newlines (e.g., SDDC Manager sends
    the PEM header glued to the base64 body with no line break).
    """
    raw = raw.strip()
    if not raw:
        return ''

    header = '-----BEGIN CERTIFICATE REQUEST-----'
    footer = '-----END CERTIFICATE REQUEST-----'
    alt_header = '-----BEGIN NEW CERTIFICATE REQUEST-----'
    alt_footer = '-----END NEW CERTIFICATE REQUEST-----'

    b64 = raw
    for h in (header, alt_header):
        if h in b64:
            b64 = b64.split(h, 1)[1]
            break
    for f in (footer, alt_footer):
        if f in b64:
            b64 = b64.split(f, 1)[0]
            break

    b64 = re.sub(r'\s+', '', b64)

    if not b64:
        return ''

    lines = [b64[i:i+64] for i in range(0, len(b64), 64)]
    return header + '\n' + '\n'.join(lines) + '\n' + footer


def extract_cn_from_csr(csr_pem: str) -> str | None:
    """Extract the Common Name from a PEM-encoded CSR."""
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        for attr in csr.subject:
            if attr.oid == x509.oid.NameOID.COMMON_NAME:
                return attr.value
    except Exception as e:
        logger.error('Failed to parse CSR: %s', e)
    return None


def _der_length(length: int) -> bytes:
    """Encode an ASN.1 DER length field."""
    if length < 0x80:
        return bytes([length])
    elif length < 0x100:
        return bytes([0x81, length])
    elif length < 0x10000:
        return bytes([0x82, length >> 8, length & 0xff])
    else:
        return bytes([0x83, length >> 16, (length >> 8) & 0xff, length & 0xff])


def build_ordered_pkcs7(cert_der_list: list[bytes]) -> bytes:
    """Build a PKCS#7 SignedData structure preserving certificate order.

    Python's ``cryptography`` library uses DER encoding which sorts SET OF
    elements by encoded value, destroying the certificate ordering that SDDC
    Manager relies on (``certs[0]`` = signed cert, ``certs[1..]`` = CA chain).
    This function constructs the ASN.1 manually so the caller controls order.
    """
    oid_signed_data = bytes([
        0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x07, 0x02])
    oid_data = bytes([
        0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x07, 0x01])

    version = bytes([0x02, 0x01, 0x01])
    digest_algs = bytes([0x31, 0x00])
    content_info = bytes([0x30]) + _der_length(len(oid_data)) + oid_data

    certs_content = b''.join(cert_der_list)
    certs_field = bytes([0xa0]) + _der_length(len(certs_content)) + certs_content

    signer_infos = bytes([0x31, 0x00])

    signed_data_inner = (version + digest_algs + content_info
                         + certs_field + signer_infos)
    signed_data = (bytes([0x30]) + _der_length(len(signed_data_inner))
                   + signed_data_inner)

    explicit0 = bytes([0xa0]) + _der_length(len(signed_data)) + signed_data
    outer_inner = oid_signed_data + explicit0
    return bytes([0x30]) + _der_length(len(outer_inner)) + outer_inner


def build_pkcs7_from_pem(ca_pem: str) -> bytes:
    """Wrap a PEM CA certificate in a PKCS#7 (DER) structure."""
    ca_cert = x509.load_pem_x509_certificate(ca_pem.encode())
    return build_ordered_pkcs7([ca_cert.public_bytes(serialization.Encoding.DER)])


# ---------------------------------------------------------------------------
# Shared CSS used across all pages
# ---------------------------------------------------------------------------
ADCS_CSS = """
<style>
:root {
  --bg: #fcfdfd; --bg-alt: #f4f6f8; --text: #1b2b32; --text-muted: #57707a;
  --accent: #0079b8; --accent-light: #48a0d9; --accent-hover: #005a8c; --accent-dark: #003b5c;
  --banner-bg: #002538;
  --border: #cccccc; --border-light: #e0e0e0;
  --input-bg: #ffffff; --input-border: #9baeb8;
  --dl-bg: #eeeeee; --dl-hover: #e1e1e1;
  --table-hover: #e8f3fa; --table-stripe: #f4f6f8;
  --th-bg: #002538; --th-text: #ffffff; --th-hover: #003b5c;
  --status-ok: #2f8400; --status-err: #c92100; --status-err-hover: #a11a00;
  --revoked-text: #738f9c;
  --spinner-border: #cccccc;
}
[data-theme="dark"] {
  --bg: #1b2b32; --bg-alt: #17242b; --text: #f1f6f8; --text-muted: #9baeb8;
  --accent: #2eabff; --accent-light: #73c8ff; --accent-hover: #008fee; --accent-dark: #006bb3;
  --banner-bg: #111d24;
  --border: #3b5360; --border-light: #2c404b;
  --input-bg: #111d24; --input-border: #57707a;
  --dl-bg: #22343c; --dl-hover: #2c404b;
  --table-hover: #22343c; --table-stripe: #17242b;
  --th-bg: #111d24; --th-text: #f1f6f8; --th-hover: #17242b;
  --status-ok: #60b515; --status-err: #e82c00; --status-err-hover: #ff4a21;
  --revoked-text: #57707a;
  --spinner-border: #3b5360;
}
body { font-family: 'Metropolis', 'Avenir Next', 'Helvetica Neue', Arial, sans-serif; margin: 0; padding: 0; font-size: 14px; color: var(--text); background: var(--bg); transition: background .2s, color .2s; }
.banner { background: var(--banner-bg); color: #fff; padding: 10px 24px; font-size: 14px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.banner a { color: #fff; text-decoration: none; font-size: 13px; }
.banner-right { display: flex; align-items: center; gap: 16px; }
.theme-toggle { background: none; border: none; cursor: pointer; color: #fff; font-size: 14px; line-height: 1; display: flex; align-items: center; padding: 4px 8px; border-radius: 4px; }
.theme-toggle:hover { background: rgba(255,255,255,.1); }
.content { padding: 24px 30px; max-width: 960px; }
h2 { color: var(--text); font-size: 18px; margin-top: 0; font-weight: 400; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
a { color: var(--accent); }
a:hover { color: var(--accent-hover); }
hr { border: none; border-top: 1px solid var(--border); margin: 24px 0; }
.task-list { margin: 12px 0 12px 24px; padding-left: 0; }
.task-list li { margin: 8px 0; }
input[type=submit], button.btn-primary { background: var(--accent); color: #fff; border: 1px solid var(--accent-dark); padding: 8px 24px; cursor: pointer; font-size: 14px; border-radius: 4px; font-weight: 500; }
input[type=submit]:hover, button.btn-primary:hover { background: var(--accent-hover); }
select, textarea, input[type=text], input[type=number] { border: 1px solid var(--input-border); padding: 6px 10px; font-family: Consolas, monospace; font-size: 13px; background: var(--input-bg); color: var(--text); border-radius: 4px; }
select { font-family: 'Metropolis', 'Avenir Next', 'Helvetica Neue', Arial, sans-serif; }
table.form-table td { padding: 6px 10px; vertical-align: top; }
table.form-table td:first-child { font-weight: 500; white-space: nowrap; text-align: right; }
.cert-table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; table-layout: auto; }
.cert-table th { background: var(--th-bg); color: var(--th-text); padding: 10px 12px; text-align: left; position: sticky; top: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
.cert-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-light); overflow: hidden; text-overflow: ellipsis; }
.cert-table th.date-col, .cert-table td.date-col { width: 90px; min-width: 90px; max-width: 100px; white-space: normal; }
.cert-table tr:nth-child(even) { background: var(--table-stripe); }
.cert-table tr:hover { background: var(--table-hover); }
.cert-table tr.revoked td { color: var(--revoked-text); text-decoration: line-through; }
.cert-table input[type=checkbox] { transform: scale(1.2); }
.san-list { font-size: 12px; color: var(--text-muted); }
.btn-revoke { background: var(--status-err); color: #fff; border: 1px solid var(--status-err-hover); margin-top: 12px; padding: 8px 24px; cursor: pointer; font-size: 14px; border-radius: 4px; font-weight: 500; }
.btn-revoke:hover { background: var(--status-err-hover); }
.section { margin-top: 32px; }
.section h3 { color: var(--accent); font-size: 16px; font-weight: 500; margin-bottom: 12px; }
.dl-links a { display: inline-block; margin: 4px 12px 4px 0; padding: 6px 16px; background: var(--dl-bg); border: 1px solid var(--border); border-radius: 4px; text-decoration: none; color: var(--text); font-size: 13px; font-weight: 500; }
.dl-links a:hover { background: var(--dl-hover); border-color: var(--accent-light); }
.spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--spinner-border); border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; vertical-align: middle; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
.msg-ok { color: var(--status-ok); font-weight: 600; }
.msg-err { color: var(--status-err); font-weight: 600; }
</style>
<script>
(function(){var t=localStorage.getItem('clarity-theme');if(t==='dark')document.documentElement.setAttribute('data-theme','dark');})();
</script>
"""

def page_wrap(title: str, body: str, wide: bool = False) -> str:
    content_style = ' style="max-width:95%"' if wide else ''
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>{ADCS_CSS}</head>
<body>
<div class="banner">
<span>VCF CA Proxy &mdash; Vault-PKI-CA</span>
<span class="banner-right">
<button class="theme-toggle" onclick="toggleTheme()" title="Toggle light/dark mode" id="theme-btn">
<svg id="theme-icon" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="width:16px;height:16px;fill:currentColor;vertical-align:middle;margin-right:6px;"><path d="M18.11 32.0003C10.33 32.0003 4 25.7203 4 17.9903C4 10.2603 10.03 4.2003 17.73 4.0003C18.15 3.9903 18.52 4.2303 18.68 4.6103C18.84 4.9903 18.75 5.4303 18.46 5.7203C16.69 7.4503 15.71 9.7603 15.71 12.2103C15.71 17.2403 19.83 21.3303 24.91 21.3303C26.9 21.3303 28.8 20.7003 30.41 19.5103C30.74 19.2703 31.19 19.2503 31.53 19.4603C31.88 19.6803 32.06 20.0803 31.99 20.4903C30.78 27.1603 24.94 32.0003 18.11 32.0003ZM15.43 6.2903C9.99 7.4803 6 12.2403 6 17.9903C6 24.6103 11.43 30.0003 18.11 30.0003C23.16 30.0003 27.58 26.9203 29.37 22.4003C27.97 23.0103 26.46 23.3203 24.91 23.3203C18.74 23.3203 13.71 18.3303 13.71 12.2003C13.71 10.0703 14.31 8.0303 15.43 6.2803V6.2903Z"></path></svg>
<span id="theme-text">Dark</span>
</button>
<a href="/certsrv/">Home</a>
</span>
</div>
<div class="content"{content_style}>
{body}
</div>
<script>
var pathMoon = "M18.11 32.0003C10.33 32.0003 4 25.7203 4 17.9903C4 10.2603 10.03 4.2003 17.73 4.0003C18.15 3.9903 18.52 4.2303 18.68 4.6103C18.84 4.9903 18.75 5.4303 18.46 5.7203C16.69 7.4503 15.71 9.7603 15.71 12.2103C15.71 17.2403 19.83 21.3303 24.91 21.3303C26.9 21.3303 28.8 20.7003 30.41 19.5103C30.74 19.2703 31.19 19.2503 31.53 19.4603C31.88 19.6803 32.06 20.0803 31.99 20.4903C30.78 27.1603 24.94 32.0003 18.11 32.0003ZM15.43 6.2903C9.99 7.4803 6 12.2403 6 17.9903C6 24.6103 11.43 30.0003 18.11 30.0003C23.16 30.0003 27.58 26.9203 29.37 22.4003C27.97 23.0103 26.46 23.3203 24.91 23.3203C18.74 23.3203 13.71 18.3303 13.71 12.2003C13.71 10.0703 14.31 8.0303 15.43 6.2803V6.2903Z";
var pathSun = "M8.81 10.22C9.01 10.42 9.26 10.51 9.52 10.51C9.78 10.51 10.03 10.41 10.23 10.22C10.62 9.83 10.62 9.2 10.23 8.81L8.11 6.69C7.72 6.3 7.09 6.3 6.7 6.69C6.31 7.08 6.31 7.71 6.7 8.1L8.82 10.22H8.81ZM7 18C7 17.45 6.55 17 6 17H3C2.45 17 2 17.45 2 18C2 18.55 2.45 19 3 19H6C6.55 19 7 18.55 7 18ZM18 7C18.55 7 19 6.55 19 6V3C19 2.45 18.55 2 18 2C17.45 2 17 2.45 17 3V6C17 6.55 17.45 7 18 7ZM26.49 10.51C26.75 10.51 27 10.41 27.2 10.22L29.32 8.1C29.71 7.71 29.71 7.08 29.32 6.69C28.93 6.3 28.3 6.3 27.91 6.69L25.79 8.81C25.4 9.2 25.4 9.83 25.79 10.22C25.99 10.42 26.24 10.51 26.5 10.51H26.49ZM8.81 25.78L6.69 27.9C6.3 28.29 6.3 28.92 6.69 29.31C6.89 29.51 7.14 29.6 7.4 29.6C7.66 29.6 7.91 29.5 8.11 29.31L10.23 27.19C10.62 26.8 10.62 26.17 10.23 25.78C9.84 25.39 9.21 25.39 8.82 25.78H8.81ZM33 17H30C29.45 17 29 17.45 29 18C29 18.55 29.45 19 30 19H33C33.55 19 34 18.55 34 18C34 17.45 33.55 17 33 17ZM18 9C13.04 9 9 13.04 9 18C9 22.96 13.04 27 18 27C22.96 27 27 22.96 27 18C27 13.04 22.96 9 18 9ZM18 25C14.14 25 11 21.86 11 18C11 14.14 14.14 11 18 11C21.86 11 25 14.14 25 18C25 21.86 21.86 25 18 25ZM27.19 25.78C26.8 25.39 26.17 25.39 25.78 25.78C25.39 26.17 25.39 26.8 25.78 27.19L27.9 29.31C28.1 29.51 28.35 29.6 28.61 29.6C28.87 29.6 29.12 29.5 29.32 29.31C29.71 28.92 29.71 28.29 29.32 27.9L27.2 25.78H27.19ZM18 29C17.45 29 17 29.45 17 30V33C17 33.55 17.45 34 18 34C18.55 34 19 33.55 19 33V30C19 29.45 18.55 29 18 29Z";
function toggleTheme(){{
  var d=document.documentElement, i=document.getElementById('theme-icon'), t=document.getElementById('theme-text');
  if(d.getAttribute('data-theme')==='dark'){{
    d.removeAttribute('data-theme');
    localStorage.setItem('clarity-theme','light');
    i.innerHTML='<path d="' + pathMoon + '"></path>';
    t.innerText='Dark';
  }}else{{
    d.setAttribute('data-theme','dark');
    localStorage.setItem('clarity-theme','dark');
    i.innerHTML='<path d="' + pathSun + '"></path>';
    t.innerText='Light';
  }}
}}
(function(){{
  var i=document.getElementById('theme-icon'), t=document.getElementById('theme-text');
  if(document.documentElement.getAttribute('data-theme')==='dark'){{
    i.innerHTML='<path d="' + pathSun + '"></path>';
    t.innerText='Light';
  }} else {{
    i.innerHTML='<path d="' + pathMoon + '"></path>';
    t.innerText='Dark';
  }}
}})();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# HTML page templates
# ---------------------------------------------------------------------------

CERTSRV_HOME_HTML = page_wrap('VCF CA Proxy', """
<h2>Welcome</h2>
<p>Use this Web site to request a certificate for your Web browser, e-mail client, or other program.
By using a certificate, you can verify your identity to people you communicate with over the Web,
sign and encrypt messages, and, depending upon the type of certificate you request, perform other security tasks.</p>
<p>You can also use this Web site to download a certificate authority (CA) certificate, certificate chain,
or certificate revocation list (CRL), or to view the status of a pending request.</p>
<p><b>Select a task:</b></p>
<ul class="task-list">
<li><a href="/certsrv/certrqus.asp">Request a certificate</a></li>
<li><a href="/certsrv/certgen.asp">CSR Generator (New Certificate)</a></li>
<li><a href="/certsrv/certckpn.asp">View the status of a pending certificate request</a></li>
<li><a href="/certsrv/certcarc.asp">Download a CA certificate, certificate chain, or CRL</a></li>
</ul>
<hr>
""")

CERTRQUS_HTML = page_wrap('VCF CA Proxy - Request a Certificate', """
<h2>Request a Certificate</h2>
<p>Select the certificate type:</p>
<ul class="task-list">
<li>
<form method="POST" action="certfnsh.asp" style="display:inline">
<input type="hidden" name="Mode" value="userreq" />
<a href="#" onclick="this.closest('form').submit();return false">User Certificate</a>
</form>
</li>
</ul>
<p>Or, submit an <a href="certrqad.asp">advanced certificate request</a>.</p>
<hr>
""")

CERTRQAD_HTML = page_wrap('VCF CA Proxy - Advanced Certificate Request', """
<h2>Advanced Certificate Request</h2>
<p>The policy of the CA determines the types of certificates you can request.
Click one of the following options to:</p>
<ul class="task-list">
<li><a href="certrqma.asp">Create and submit a request to this CA.</a></li>
<li><a href="certrqxt.asp">Submit a certificate request by using a base-64-encoded CMC or PKCS #10 file,
or submit a renewal request by using a base-64-encoded PKCS #7 file.</a></li>
</ul>
<hr>
""")

CERTCKPN_HTML = page_wrap('VCF CA Proxy - Pending Request Status', """
<h2>View the Status of a Pending Certificate Request</h2>
<p>All certificate requests issued by this CA are processed immediately.
There are no pending requests.</p>
<p>Certificates issued through this proxy are signed instantly by the backing PKI engine (HashiCorp Vault).</p>
<p><a href="/certsrv/">&laquo; Back to home</a></p>
<hr>
""")

def build_certfnsh_success(req_id: int) -> str:
    return page_wrap('VCF CA Proxy - Certificate Issued', f"""
<h2>Certificate Issued</h2>
<p>The certificate you requested was issued to you.</p>
<p><a href="certnew.cer?ReqID={req_id}&amp;Enc=b64"><b>Install this certificate</b></a></p>
<div class="dl-links" style="margin-top:14px">
<a href="certnew.cer?ReqID={req_id}&amp;Enc=b64">Download certificate (PEM)</a>
<a href="certnew.cer?ReqID={req_id}&amp;Enc=bin">Download certificate (DER)</a>
<a href="certnew.p7b?ReqID={req_id}&amp;Enc=b64">Download certificate chain (PKCS#7)</a>
<a href="certnew.p7b?ReqID={req_id}&amp;Enc=bin">Download certificate chain (PKCS#7 DER)</a>
</div>
<hr>
<p><a href="/certsrv/">&laquo; Back to home</a></p>
""")

CERTFNSH_DENIED_TEMPLATE = page_wrap('VCF CA Proxy - Certificate Request Denied', """
<h2>Certificate Request Denied</h2>
<p>Your certificate request was denied.</p>
<p>The disposition message is: <b>{{ERROR}}</b></p>
<hr>
<p><a href="/certsrv/">&laquo; Back to home</a></p>
""")

def build_certrqma(template_options: str) -> str:
    return page_wrap('VCF CA Proxy - Advanced Certificate Request', f"""
<h2>Advanced Certificate Request</h2>
<form method="POST" action="certfnsh.asp" name="SubmitForm">
<table class="form-table">
<tr><td>Certificate Template:</td><td>
<select name="lbCertTemplateID" id="lbCertTemplateID" style="width:260px">
{template_options}
</select>
</td></tr>
<tr><td colspan="2"><hr></td></tr>
<tr><td>Key Size:</td><td><input type="number" name="KeySize" value="2048" min="1024" max="4096" step="1024" style="width:80px" /></td></tr>
<tr><td colspan="2"><hr></td></tr>
<tr><td>Attributes:</td><td><textarea name="CertAttrib" rows="3" cols="60" placeholder="CertificateTemplate:VCFWebServer"></textarea></td></tr>
<tr><td>Friendly Name:</td><td><input type="text" name="FriendlyName" value="" style="width:260px" /></td></tr>
<tr><td colspan="2"><hr></td></tr>
<tr><td>CMC / PKCS #10<br>Certificate Request:</td><td>
<textarea name="CertRequest" rows="16" cols="64" placeholder="-----BEGIN CERTIFICATE REQUEST-----&#10;...&#10;-----END CERTIFICATE REQUEST-----"></textarea>
</td></tr>
</table>
<input type="hidden" name="Mode" value="newreq" />
<input type="hidden" name="SaveCert" value="yes" />
<input type="hidden" name="TargetStoreFlags" value="0" />
<br>
<input type="submit" value="Submit &gt;" />
</form>
""")

def build_certrqxt(template_options: str) -> str:
    """The paste-CSR page. Also used by SDDC Manager for template scraping."""
    return page_wrap('VCF CA Proxy - Submit a Certificate Request or Renewal Request', f"""
<h2>Submit a Certificate Request or Renewal Request</h2>
<p>To submit a saved request to the CA, paste a base-64-encoded CMC or PKCS #10
certificate request or PKCS #7 renewal request in the box below.</p>
<form method="POST" action="certfnsh.asp" name="SubmitForm">
<table class="form-table">
<tr><td>Certificate Template:</td><td>
<select name="lbCertTemplateID" id="lbCertTemplateID" style="width:260px">
{template_options}
</select>
</td></tr>
<tr><td>Saved Request:</td><td>
<textarea name="CertRequest" rows="20" cols="64" placeholder="-----BEGIN CERTIFICATE REQUEST-----&#10;...&#10;-----END CERTIFICATE REQUEST-----"></textarea>
</td></tr>
</table>
<input type="hidden" name="Mode" value="newreq" />
<input type="hidden" name="SaveCert" value="yes" />
<input type="hidden" name="TargetStoreFlags" value="0" />
<br>
<input type="submit" value="Submit &gt;" />
</form>
""")

def build_certgen(template_options: str) -> str:
    return page_wrap('VCF CA Proxy - CSR Generator', f"""
<h2>CSR Generator</h2>
<p>Complete this form to generate a new CSR and private key, and automatically issue a certificate.</p>
<form method="POST" action="certgen_process.asp" name="SubmitForm">
<table class="form-table">
<tr><td>Certificate Template:</td><td>
<select name="lbCertTemplateID" id="lbCertTemplateID" style="width:260px">
{template_options}
</select>
</td></tr>
<tr><td colspan="2"><hr></td></tr>
<tr><td>Country (C):</td><td><input type="text" name="Country" value="US" style="width:60px" /></td></tr>
<tr><td>State (ST):</td><td><input type="text" name="State" value="California" style="width:260px" /></td></tr>
<tr><td>Locality (L):</td><td><input type="text" name="Locality" value="Palo Alto" style="width:260px" /></td></tr>
<tr><td>Organization (O):</td><td><input type="text" name="Organization" value="VMware" style="width:260px" /></td></tr>
<tr><td>Organizational Unit (OU):</td><td><input type="text" name="OrganizationalUnit" value="Cloud Foundation" style="width:260px" /></td></tr>
<tr><td>Common Name (CN):</td><td><input type="text" name="CommonName" value="" placeholder="app.vcf.lab" style="width:260px" required /></td></tr>
<tr><td>Subject Alternative Names (SANs):</td><td><textarea name="SANs" rows="3" cols="60" placeholder="DNS:app.vcf.lab&#10;IP:192.168.1.10"></textarea><br><span style="font-size:11px;color:var(--text-muted)">One per line, e.g., DNS:name or IP:1.2.3.4</span></td></tr>
<tr><td colspan="2"><hr></td></tr>
<tr><td>Key Size:</td><td>
<select name="KeySize" style="width:100px">
<option value="2048" selected>2048</option>
<option value="4096">4096</option>
</select>
</td></tr>
</table>
<br>
<input type="submit" value="Generate &amp; Issue &gt;" />
</form>
""")


CERTCARC_COMPAT_BLOCK = '<script language="VBScript">\nvar nRenewals=0;\n</script>\n'

def build_certcarc() -> str:
    return page_wrap('VCF CA Proxy - CA Certificate and Certificate Management', """
<h2>Download a CA Certificate, Certificate Chain, or CRL</h2>
""" + CERTCARC_COMPAT_BLOCK + """
<div class="section">
<h3>CA Certificate</h3>
<div class="dl-links">
<a href="certnew.cer?ReqID=CACert&amp;Enc=b64">Download CA certificate (PEM)</a>
<a href="certnew.cer?ReqID=CACert&amp;Enc=bin">Download CA certificate (DER)</a>
<a href="certnew.p7b?ReqID=CACert&amp;Renewal=0&amp;Enc=bin">Download certificate chain (PKCS#7)</a>
<a href="certnew.crl?ReqID=CACert&amp;Enc=b64">Download Base CRL (PEM)</a>
<a href="certnew.crl?ReqID=CACert&amp;Enc=bin">Download Base CRL (DER)</a>
</div>
</div>

<div class="section">
<h3>Issued Certificates</h3>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap">
<input type="text" id="cert-filter" placeholder="Filter by CN, SAN, serial, key usage..." style="width:320px;padding:5px 8px;font-size:12px" oninput="applyFilter()">
<span id="cert-count" style="font-size:12px;color:var(--text-muted)"></span>
</div>
<p id="cert-status"><span class="spinner"></span> Loading certificates&hellip;</p>
<div style="overflow-x:auto;width:90%">
<table class="cert-table" id="cert-table" style="display:none">
<thead><tr>
<th style="width:30px;min-width:30px"><input type="checkbox" id="select-all" title="Select all"></th>
<th class="sortable resizable" data-col="cn" style="width:14%">Common Name <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="sortable resizable" data-col="sans" style="width:20%">Subject Alternative Names <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="sortable resizable date-col" data-col="issued">Issued <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="sortable resizable date-col" data-col="expires">Expires <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="sortable resizable" data-col="serial" style="width:12%;min-width:150px">Serial Number <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="sortable resizable" data-col="key_usage" style="width:14%">Key Usage <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="sortable resizable" data-col="status" style="width:6%">Status <span class="sort-arrow"></span><div class="col-resize-handle"></div></th>
<th class="resizable" style="width:12%;min-width:130px">Download<div class="col-resize-handle"></div></th>
</tr></thead>
<tbody id="cert-body"></tbody>
</table>
</div>
<button class="btn-revoke" id="btn-revoke" style="display:none" onclick="revokeSelected()">Revoke Selected</button>
</div>
<hr>

<style>
.sortable { cursor: pointer; user-select: none; position: relative; }
.resizable { position: relative; }
.sortable:hover { background: var(--th-hover); }
.sort-arrow { font-size: 10px; margin-left: 3px; }
.cert-table td.dl-td { overflow: visible; }
.cert-table .dl-cell { white-space: nowrap; }
.cert-table .dl-cell a { display: inline-block; margin: 1px 3px; padding: 2px 7px; background: var(--dl-bg); border: 1px solid var(--border); border-radius: 2px; text-decoration: none; color: var(--text); font-size: 10px; }
.cert-table .dl-cell a:hover { background: var(--dl-hover); border-color: var(--accent-light); }
.col-resize-handle { position: absolute; right: 0; top: 0; bottom: 0; width: 5px; cursor: col-resize; background: transparent; }
.col-resize-handle:hover, .col-resize-handle.active { background: rgba(255,255,255,.35); }
.cert-table td.serial-cell { font-family: Consolas,monospace; font-size: 11px; white-space: nowrap; overflow: visible; }
.copy-btn { background: none; border: none; cursor: pointer; padding: 1px 4px; color: var(--text-muted); font-size: 13px; vertical-align: middle; border-radius: 3px; position: relative; }
.copy-btn:hover { color: var(--accent); background: var(--dl-bg); }
.copy-tooltip { position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); background: var(--accent); color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 10px; white-space: nowrap; pointer-events: none; opacity: 0; transition: opacity .2s; }
.copy-tooltip.show { opacity: 1; }
.ku-cell { font-size: 11px; color: var(--text-muted); }
</style>

<script>
var _allCerts = [];
var _sortCol = 'issued';
var _sortAsc = false;

function xhrGet(url, cb) {
    var x = new XMLHttpRequest();
    x.open('GET', url, true);
    x.withCredentials = true;
    x.onreadystatechange = function() {
        if (x.readyState === 4) cb(x.status, x.responseText);
    };
    x.send();
}
function xhrPost(url, data, cb) {
    var x = new XMLHttpRequest();
    x.open('POST', url, true);
    x.withCredentials = true;
    x.setRequestHeader('Content-Type', 'application/json');
    x.onreadystatechange = function() {
        if (x.readyState === 4) cb(x.status, x.responseText);
    };
    x.send(JSON.stringify(data));
}

function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

function sansText(c) {
    var parts = [];
    if (c.dns_sans) c.dns_sans.forEach(function(s) { parts.push('DNS: ' + s); });
    if (c.ip_sans) c.ip_sans.forEach(function(s) { parts.push('IP: ' + s); });
    return parts.join(', ') || '-';
}

function kuText(c) {
    return (c.key_usage || []).join(', ') || '-';
}

function fmtDate(d) {
    if (!d) return '-';
    var p = d.split(' ');
    if (p.length === 2) return esc(p[0]) + '<br>' + esc(p[1]);
    return esc(d);
}

function dlLinks(c) {
    var s = encodeURIComponent(c.serial);
    return '<span class="dl-cell">'
        + '<a href="/certsrv/api/cert/' + s + '?fmt=pem" title="Download PEM">PEM</a>'
        + '<a href="/certsrv/api/cert/' + s + '?fmt=der" title="Download DER">DER</a>'
        + '<a href="/certsrv/api/cert/' + s + '?fmt=p7b" title="Download PKCS#7">P7B</a>'
        + '<a href="/certsrv/api/cert/' + s + '?fmt=csr" title="Download CSR">CSR</a>'
        + '<a href="/certsrv/api/cert/' + s + '?fmt=key" title="Download Private Key">KEY</a>'
        + '</span>';
}

function truncSerial(serial) {
    if (!serial) return '';
    if (serial.length <= 12) return esc(serial);
    return esc(serial.substring(0, 12)) + '&hellip;';
}

function copySerial(btn, serial) {
    navigator.clipboard.writeText(serial).then(function() {
        var tip = btn.querySelector('.copy-tooltip');
        tip.classList.add('show');
        setTimeout(function() { tip.classList.remove('show'); }, 1200);
    });
}

function renderTable(certs) {
    var tbody = document.getElementById('cert-body');
    tbody.innerHTML = '';
    certs.forEach(function(c) {
        var tr = document.createElement('tr');
        if (c.revoked) tr.className = 'revoked';
        var serialDisp = truncSerial(c.serial);
        tr.innerHTML = '<td><input type="checkbox" name="sel" value="' + esc(c.serial) + '"' + (c.revoked ? ' disabled' : '') + '></td>'
            + '<td>' + esc(c.cn) + '</td>'
            + '<td class="san-list">' + esc(sansText(c)) + '</td>'
            + '<td class="date-col">' + fmtDate(c.not_before) + '</td>'
            + '<td class="date-col">' + fmtDate(c.not_after) + '</td>'
            + '<td class="serial-cell" title="' + esc(c.serial) + '" data-serial="' + esc(c.serial) + '">'
            + serialDisp
            + ' <button class="copy-btn" onclick="copySerial(this,\\'' + esc(c.serial).replace(/'/g, "\\\\'") + '\\')" title="Copy full serial">'
            + '&#128203;<span class="copy-tooltip">Copied!</span></button></td>'
            + '<td class="ku-cell">' + esc(kuText(c)) + '</td>'
            + '<td>' + (c.revoked ? '<span style="color:var(--status-err)">Revoked</span>' : '<span style="color:var(--status-ok)">Active</span>') + '</td>'
            + '<td class="dl-td">' + dlLinks(c) + '</td>';
        tbody.appendChild(tr);
    });
    document.getElementById('cert-count').textContent = certs.length + ' certificate(s)';
}

function getVal(c, col) {
    if (col === 'cn') return (c.cn || '').toLowerCase();
    if (col === 'sans') return sansText(c).toLowerCase();
    if (col === 'issued') return c.not_before || '';
    if (col === 'expires') return c.not_after || '';
    if (col === 'serial') return c.serial || '';
    if (col === 'key_usage') return kuText(c).toLowerCase();
    if (col === 'status') return c.revoked ? 'revoked' : 'active';
    return '';
}

function sortCerts(certs) {
    var col = _sortCol, asc = _sortAsc;
    return certs.slice().sort(function(a, b) {
        var va = getVal(a, col), vb = getVal(b, col);
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
        return 0;
    });
}

function applyFilter() {
    var q = (document.getElementById('cert-filter').value || '').toLowerCase().trim();
    var filtered = _allCerts;
    if (q) {
        filtered = _allCerts.filter(function(c) {
            return (c.cn || '').toLowerCase().indexOf(q) >= 0
                || sansText(c).toLowerCase().indexOf(q) >= 0
                || (c.serial || '').toLowerCase().indexOf(q) >= 0
                || (c.not_before || '').indexOf(q) >= 0
                || (c.not_after || '').indexOf(q) >= 0
                || kuText(c).toLowerCase().indexOf(q) >= 0
                || (c.revoked ? 'revoked' : 'active').indexOf(q) >= 0;
        });
    }
    renderTable(sortCerts(filtered));
}

function updateSortArrows() {
    document.querySelectorAll('.sortable .sort-arrow').forEach(function(el) { el.textContent = ''; });
    var active = document.querySelector('.sortable[data-col="' + _sortCol + '"] .sort-arrow');
    if (active) active.textContent = _sortAsc ? ' \\u25B2' : ' \\u25BC';
}

document.addEventListener('click', function(e) {
    if (e.target.closest('.col-resize-handle')) return;
    var th = e.target.closest('.sortable');
    if (!th) return;
    var col = th.getAttribute('data-col');
    if (_sortCol === col) { _sortAsc = !_sortAsc; }
    else { _sortCol = col; _sortAsc = true; }
    updateSortArrows();
    applyFilter();
});

/* Column resize */
(function() {
    var resizing = null;
    document.addEventListener('mousedown', function(e) {
        var handle = e.target.closest('.col-resize-handle');
        if (!handle) return;
        e.preventDefault();
        var th = handle.parentElement;
        var startX = e.pageX;
        var startW = th.offsetWidth;
        handle.classList.add('active');
        resizing = {th: th, startX: startX, startW: startW, handle: handle};
    });
    document.addEventListener('mousemove', function(e) {
        if (!resizing) return;
        e.preventDefault();
        var newW = Math.max(40, resizing.startW + (e.pageX - resizing.startX));
        resizing.th.style.width = newW + 'px';
    });
    document.addEventListener('mouseup', function() {
        if (resizing) { resizing.handle.classList.remove('active'); resizing = null; }
    });
})();

function loadCerts() {
    xhrGet('/certsrv/api/certs', function(status, body) {
        if (status !== 200) {
            document.getElementById('cert-status').innerHTML = '<span class="msg-err">Failed to load certificates (HTTP ' + status + ')</span>';
            return;
        }
        try { var certs = JSON.parse(body); } catch(e) {
            document.getElementById('cert-status').innerHTML = '<span class="msg-err">Invalid response from server</span>';
            return;
        }
        _allCerts = certs;
        if (certs.length === 0) {
            document.getElementById('cert-status').innerHTML = 'No certificates found.';
            return;
        }
        document.getElementById('cert-status').style.display = 'none';
        document.getElementById('cert-table').style.display = '';
        document.getElementById('btn-revoke').style.display = '';
        updateSortArrows();
        applyFilter();
    });
}

document.getElementById('select-all').addEventListener('change', function() {
    document.querySelectorAll('#cert-body input[type=checkbox]:not([disabled])').forEach(function(cb) { cb.checked = document.getElementById('select-all').checked; });
});

function revokeSelected() {
    var boxes = document.querySelectorAll('#cert-body input[type=checkbox]:checked');
    if (boxes.length === 0) { alert('No certificates selected.'); return; }
    var serials = [];
    boxes.forEach(function(cb) { serials.push(cb.value); });
    if (!confirm('Revoke ' + serials.length + ' certificate(s)?\\n\\nThis action cannot be undone.')) return;
    xhrPost('/certsrv/api/revoke', {serials: serials}, function(status, body) {
        try { var result = JSON.parse(body); } catch(e) { alert('Revocation failed'); return; }
        if (result.error) { alert('Error: ' + result.error); }
        else { alert('Revoked ' + (result.revoked || 0) + ' certificate(s).'); }
        loadCerts();
    });
}

loadCerts();
</script>
""", wide=True)




def make_handler(vault_client: VaultPKIClient, cert_store: CertStore, key_store: KeyStore,
                 auth_password: str, session_manager: SessionManager,
                 oidc_config: dict = None):
    """Create a request handler class with the given configuration."""

    class CertsrvHandler(BaseHTTPRequestHandler):

        def log_message(self, format, *args):
            logger.info('%s %s', self.address_string(), format % args)

        def _get_session_id(self) -> str | None:
            cookie_header = self.headers.get('Cookie', '')
            for cookie in cookie_header.split(';'):
                cookie = cookie.strip()
                if cookie.startswith('certsrv_session='):
                    return cookie[len('certsrv_session='):]
            return None

        def _get_auth_username(self) -> str | None:
            """Extract username from Basic Auth header or session cookie."""
            auth_header = self.headers.get('Authorization', '')
            if auth_header.startswith('Basic '):
                try:
                    decoded = base64.b64decode(auth_header[6:]).decode('utf-8')
                    username, _ = decoded.split(':', 1)
                    return username
                except Exception:
                    pass
            
            session_id = self._get_session_id()
            if session_id:
                return session_manager.get_username(session_id)
                
            return None

        def _check_auth(self) -> bool:
            """Validate Basic Auth credentials or valid session."""
            # 1. Basic Auth check
            auth_header = self.headers.get('Authorization', '')
            if auth_header.startswith('Basic '):
                try:
                    decoded = base64.b64decode(auth_header[6:]).decode('utf-8')
                    _, password = decoded.split(':', 1)
                    if password == auth_password:
                        return True
                except Exception:
                    pass
            
            # 2. Session cookie check
            if session_manager.get_username(self._get_session_id()):
                return True
                
            return False

        def _send_auth_required(self):
            if oidc_config:
                import urllib.parse
                state = secrets.token_urlsafe(16)
                params = {
                    'client_id': oidc_config['client_id'],
                    'response_type': 'code',
                    'scope': 'openid profile email',
                    'redirect_uri': oidc_config['redirect_uri'],
                    'state': state
                }
                url = f"{oidc_config['auth_endpoint']}?{urllib.parse.urlencode(params)}"
                self.send_response(302)
                self.send_header('Location', url)
                self.send_header('Set-Cookie', f'certsrv_oidc_state={state}; HttpOnly; Path=/')
                self.end_headers()
                return

            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="CertSrv"')
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body>401 Unauthorized</body></html>')

        def _send_html(self, code: int, html: str):
            body = html.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, data):
            body = json.dumps(data).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_cert(self, cert_data: bytes, content_type: str = 'application/pkix-cert'):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(cert_data)))
            self.end_headers()
            self.wfile.write(cert_data)

        def _template_options(self) -> str:
            if not vault_client.role_mapping:
                known_templates = [
                    ('1.3.6.1.4.1.311.21.8.1;WebServer', 'Web Server'),
                    ('1.3.6.1.4.1.311.21.8.2;VCFWebServer', 'VCF Web Server'),
                    ('1.3.6.1.4.1.311.21.8.3;VMwareWebServer', 'VMware Web Server'),
                    ('1.3.6.1.4.1.311.21.8.4;Machine', 'Machine'),
                    ('1.3.6.1.4.1.311.21.8.5;SubCA', 'Subordinate CA'),
                ]
                return '\n'.join(
                    f'<Option Value="{val}">{label}</Option>'
                    for val, label in known_templates
                )
            
            options = []
            for idx, template_name in enumerate(vault_client.role_mapping.keys(), start=1):
                val = f'1.3.6.1.4.1.311.21.8.{idx};{template_name}'
                options.append(f'<Option Value="{val}">{template_name}</Option>')
            return '\n'.join(options)

        def _send_redirect(self, location: str):
            self.send_response(301)
            self.send_header('Location', location)
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip('/')
            params = parse_qs(parsed.query)

            if not self._check_auth():
                self._send_auth_required()
                return

            if path in ('/certsrv', '/certsrv/default.asp', ''):
                self._send_html(200, CERTSRV_HOME_HTML)

            elif path == '/certsrv/certrqus.asp':
                self._send_html(200, CERTRQUS_HTML)

            elif path == '/certsrv/certrqad.asp':
                self._send_html(200, CERTRQAD_HTML)

            elif path == '/certsrv/certgen.asp':
                self._send_html(200, build_certgen(self._template_options()))

            elif path == '/certsrv/certrqma.asp':
                self._send_html(200, build_certrqma(self._template_options()))

            elif path in ('/certsrv/certrqxt.asp', '/certsrv/certrmpn.asp'):
                self._send_html(200, build_certrqxt(self._template_options()))

            elif path == '/certsrv/certckpn.asp':
                self._send_html(200, CERTCKPN_HTML)

            elif path == '/certsrv/certcarc.asp':
                self._send_html(200, build_certcarc())

            elif path == '/certsrv/certnew.cer':
                self._handle_certnew_cer(params)

            elif path == '/certsrv/certnew.p7b':
                self._handle_certnew_p7b(params)

            elif path == '/certsrv/certnew.crl':
                self._handle_certnew_crl(params)

            elif path == '/certsrv/api/certs':
                self._handle_api_certs()

            elif path.startswith('/certsrv/api/cert/'):
                serial = path[len('/certsrv/api/cert/'):]
                self._handle_api_cert_download(serial, params)

            elif path == '/certsrv/oidc/callback':
                self._handle_oidc_callback(params)

            else:
                self._send_html(404, page_wrap('Not Found', '<h2>Not Found</h2><p>The requested page does not exist.</p>'))

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip('/')

            if not self._check_auth():
                self._send_auth_required()
                return

            if path == '/certsrv/certfnsh.asp':
                self._handle_certfnsh()
            elif path == '/certsrv/certgen_process.asp':
                self._handle_certgen_process()
            elif path == '/certsrv/api/revoke':
                self._handle_api_revoke()
            else:
                self._send_html(404, page_wrap('Not Found', '<h2>Not Found</h2>'))

        def _send_denied(self, error_msg: str):
            """Send the certificate-denied page with the given error message."""
            html = CERTFNSH_DENIED_TEMPLATE.replace('{{ERROR}}', error_msg)
            self._send_html(200, html)

        def _handle_certgen_process(self):
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ''
            form_data = parse_qs(body, keep_blank_values=True)
            
            try:
                country = form_data.get('Country', [''])[0].strip()
                state = form_data.get('State', [''])[0].strip()
                locality = form_data.get('Locality', [''])[0].strip()
                org = form_data.get('Organization', [''])[0].strip()
                ou = form_data.get('OrganizationalUnit', [''])[0].strip()
                cn = form_data.get('CommonName', [''])[0].strip()
                sans_raw = form_data.get('SANs', [''])[0].strip()
                key_size = int(form_data.get('KeySize', ['2048'])[0])
                
                lb_template = form_data.get('lbCertTemplateID', [''])[0]
                template = 'Unknown'
                if lb_template:
                    if ';' in lb_template:
                        template = lb_template.split(';', 1)[1]
                    else:
                        template = lb_template
                        
                if not cn:
                    self._send_denied('Common Name is required')
                    return

                x509_sans = []
                for line in sans_raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.upper().startswith('DNS:'):
                        x509_sans.append(x509.DNSName(line[4:].strip()))
                    elif line.upper().startswith('IP:'):
                        import ipaddress
                        x509_sans.append(x509.IPAddress(ipaddress.ip_address(line[3:].strip())))

                private_key = rsa.generate_private_key(
                    public_exponent=65537,
                    key_size=key_size,
                )
                key_pem = private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()
                ).decode('utf-8')

                subject_attrs = []
                if country: subject_attrs.append(x509.NameAttribute(x509.oid.NameOID.COUNTRY_NAME, country))
                if state: subject_attrs.append(x509.NameAttribute(x509.oid.NameOID.STATE_OR_PROVINCE_NAME, state))
                if locality: subject_attrs.append(x509.NameAttribute(x509.oid.NameOID.LOCALITY_NAME, locality))
                if org: subject_attrs.append(x509.NameAttribute(x509.oid.NameOID.ORGANIZATION_NAME, org))
                if ou: subject_attrs.append(x509.NameAttribute(x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME, ou))
                subject_attrs.append(x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, cn))

                builder = x509.CertificateSigningRequestBuilder().subject_name(
                    x509.Name(subject_attrs)
                )

                if x509_sans:
                    builder = builder.add_extension(
                        x509.SubjectAlternativeName(x509_sans),
                        critical=False,
                    )

                csr = builder.sign(private_key, hashes.SHA256())
                csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode('utf-8')

                logger.info('Generated CSR for CN=%s, Template=%s', cn, template)

                cert_bundle = vault_client.sign_csr(csr_pem, cn, template_name=template)
                if not cert_bundle:
                    self._send_denied('The generated CSR could not be signed by the CA')
                    return
                
                req_id = cert_store.store(cert_bundle)
                
                try:
                    pem_blocks = re.findall(
                        r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                        cert_bundle, re.DOTALL)
                    if pem_blocks:
                        cert_obj = x509.load_pem_x509_certificate(pem_blocks[0].encode())
                        serial_int = cert_obj.serial_number
                        serial_hex = format(serial_int, 'x')
                        if len(serial_hex) % 2 != 0:
                            serial_hex = '0' + serial_hex
                        vault_serial = '-'.join(serial_hex[i:i+2] for i in range(0, len(serial_hex), 2))
                        key_store.store(vault_serial, key_pem, csr_pem)
                        logger.info('Stored keys for serial %s', vault_serial)
                except Exception as e:
                    logger.error('Failed to extract serial for key storage: %s', e)

                self._send_html(200, build_certfnsh_success(req_id))

            except Exception as e:
                logger.error('Error generating CSR: %s', e)
                self._send_denied(f'Error generating CSR: {e}')

        def _handle_certfnsh(self):
            """Process CSR submission or User Certificate auto-issue."""
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ''
            form_data = parse_qs(body, keep_blank_values=True)

            mode = form_data.get('Mode', ['newreq'])[0]

            if mode == 'userreq':
                username = self._get_auth_username() or 'user'
                cn = f'{username}.vcf.lab'
                logger.info('User certificate requested for: %s', cn)
                issue_data = vault_client.issue_certificate(cn)
                if not issue_data:
                    self._send_denied('Failed to issue user certificate')
                    return
                cert = issue_data.get('certificate', '')
                ca_chain = issue_data.get('ca_chain', [])
                issuing_ca = issue_data.get('issuing_ca', '')
                bundle = cert
                if ca_chain:
                    bundle += '\n' + '\n'.join(ca_chain)
                elif issuing_ca:
                    bundle += '\n' + issuing_ca
                req_id = cert_store.store(bundle)
                logger.info('User certificate issued: ReqID=%d, CN=%s', req_id, cn)
                self._send_html(200, build_certfnsh_success(req_id))
                return

            csr_raw = form_data.get('CertRequest', [''])[0]
            cert_attrib = form_data.get('CertAttrib', [''])[0]

            template = 'Unknown'
            if cert_attrib:
                match = re.search(r'CertificateTemplate:(\S+)', cert_attrib)
                if match:
                    template = match.group(1)
            else:
                lb_template = form_data.get('lbCertTemplateID', [''])[0]
                if lb_template:
                    if ';' in lb_template:
                        template = lb_template.split(';', 1)[1]
                    else:
                        template = lb_template

            logger.info('Raw CSR length: %d, first 80 chars: %s',
                        len(csr_raw), repr(csr_raw[:80]))

            csr_pem = normalize_csr_pem(csr_raw)

            if not csr_pem:
                logger.warning('Empty CSR received')
                self._send_denied('The request contains no certificate request')
                return

            cn = extract_cn_from_csr(csr_pem)
            logger.info('CSR received: CN=%s, Template=%s', cn or '(unknown)', template)

            cert_bundle = vault_client.sign_csr(csr_pem, cn or 'unknown.vcf.lab', template_name=template)
            if not cert_bundle:
                self._send_denied('The certificate request could not be signed by the CA')
                return

            req_id = cert_store.store(cert_bundle)
            logger.info('Certificate issued: ReqID=%d, CN=%s', req_id, cn)
            self._send_html(200, build_certfnsh_success(req_id))

        def _handle_certnew_cer(self, params: dict):
            """Handle certificate retrieval (issued cert or CA cert)."""
            req_id_raw = params.get('ReqID', [''])[0]
            encoding = params.get('Enc', ['b64'])[0]

            if req_id_raw == 'CACert':
                ca_pem = vault_client.get_ca_cert_pem()
                if not ca_pem:
                    self._send_html(500, page_wrap('Error', '<p>CA certificate unavailable.</p>'))
                    return
                if encoding == 'bin':
                    ca_cert = x509.load_pem_x509_certificate(ca_pem.encode())
                    der = ca_cert.public_bytes(serialization.Encoding.DER)
                    self._send_cert(der, 'text/html')
                else:
                    self._send_cert(ca_pem.encode(), 'text/html')
                return

            try:
                req_id = int(req_id_raw)
            except (ValueError, TypeError):
                self._send_html(400, page_wrap('Error', '<p>Invalid ReqID.</p>'))
                return

            cert_pem = cert_store.get(req_id)
            if not cert_pem:
                error_html = (
                    '<html><body>'
                    'Disposition message:\t\tThe request ID is invalid or '
                    'the certificate has not been issued.\r\n'
                    '</body></html>'
                )
                self._send_html(404, error_html)
                return

            if encoding == 'bin':
                cert = x509.load_pem_x509_certificate(cert_pem.encode())
                der = cert.public_bytes(serialization.Encoding.DER)
                self._send_cert(der, 'text/html')
            else:
                pem_blocks = re.findall(
                    r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                    cert_pem, re.DOTALL)
                if pem_blocks:
                    self._send_cert(pem_blocks[0].encode(), 'text/html')
                else:
                    self._send_cert(cert_pem.encode(), 'text/html')

        def _handle_certnew_p7b(self, params: dict):
            """Handle PKCS#7 retrieval — CA chain or issued cert chain."""
            req_id_raw = params.get('ReqID', ['CACert'])[0]
            encoding = params.get('Enc', ['bin'])[0]

            der_list = []

            if req_id_raw != 'CACert':
                try:
                    req_id = int(req_id_raw)
                except (ValueError, TypeError):
                    self._send_html(400, page_wrap('Error', '<p>Invalid ReqID.</p>'))
                    return
                cert_pem = cert_store.get(req_id)
                if not cert_pem:
                    self._send_html(404, page_wrap('Error',
                        '<p>The request ID is invalid or the certificate has not been issued.</p>'))
                    return
                pem_blocks = re.findall(
                    r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                    cert_pem, re.DOTALL)
                for block in pem_blocks:
                    try:
                        cert_obj = x509.load_pem_x509_certificate(block.encode())
                        der_list.append(cert_obj.public_bytes(serialization.Encoding.DER))
                    except Exception:
                        pass
                
                # Fallback: if Vault didn't return a chain (only 1 block), append CA explicitly
                if len(pem_blocks) == 1:
                    ca_pem = vault_client.get_ca_cert_pem()
                    if ca_pem:
                        try:
                            ca_obj = x509.load_pem_x509_certificate(ca_pem.encode())
                            der_list.append(ca_obj.public_bytes(serialization.Encoding.DER))
                        except Exception:
                            pass
            else:
                ca_pem = vault_client.get_ca_cert_pem()
                if not ca_pem:
                    self._send_html(500, page_wrap('Error', '<p>CA chain unavailable.</p>'))
                    return
                ca_obj = x509.load_pem_x509_certificate(ca_pem.encode())
                der_list.append(ca_obj.public_bytes(serialization.Encoding.DER))

            if not der_list:
                self._send_html(500, page_wrap('Error', '<p>No certificates to wrap.</p>'))
                return

            p7_der = build_ordered_pkcs7(der_list)
            if encoding == 'b64':
                b64_raw = base64.b64encode(p7_der).decode()
                lines = [b64_raw[i:i+64] for i in range(0, len(b64_raw), 64)]
                pem_p7b = '-----BEGIN CERTIFICATE-----\n' + '\n'.join(lines) + '\n-----END CERTIFICATE-----\n'
                self._send_cert(pem_p7b.encode(), 'application/x-pkcs7-certificates')
            else:
                self._send_cert(p7_der, 'application/x-pkcs7-certificates')

        def _handle_certnew_crl(self, params: dict):
            """Handle CRL retrieval."""
            encoding = params.get('Enc', ['bin'])[0]
            
            vault_format = 'der' if encoding == 'bin' else 'pem'
            crl_data = vault_client.get_crl(encoding=vault_format)
            
            if not crl_data:
                self._send_html(500, page_wrap('Error', '<p>CRL unavailable.</p>'))
                return
                
            self._send_cert(crl_data, 'application/pkix-crl')

        def _handle_api_certs(self):
            """JSON API: list all issued certificates."""
            certs = vault_client.list_certificates()
            self._send_json(200, certs)

        def _handle_api_cert_download(self, serial: str, params: dict):
            """Download a single issued certificate by serial in PEM, DER, or PKCS#7 format."""
            fmt = params.get('fmt', ['pem'])[0].lower()
            pem = vault_client.get_certificate_pem(serial)
            if not pem:
                self._send_json(404, {'error': 'Certificate not found'})
                return

            try:
                cert_obj = x509.load_pem_x509_certificate(pem.encode())
                cn = ''
                for attr in cert_obj.subject:
                    if attr.oid == x509.oid.NameOID.COMMON_NAME:
                        cn = attr.value
                        break
            except Exception:
                cn = serial

            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', cn) if cn else serial

            if fmt == 'der':
                der_bytes = cert_obj.public_bytes(serialization.Encoding.DER)
                self.send_response(200)
                self.send_header('Content-Type', 'application/pkix-cert')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}.cer"')
                self.send_header('Content-Length', str(len(der_bytes)))
                self.end_headers()
                self.wfile.write(der_bytes)
            elif fmt == 'p7b':
                p7_der = build_ordered_pkcs7([cert_obj.public_bytes(serialization.Encoding.DER)])
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-pkcs7-certificates')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}.p7b"')
                self.send_header('Content-Length', str(len(p7_der)))
                self.end_headers()
                self.wfile.write(p7_der)
            elif fmt in ('csr', 'key'):
                keys = key_store.get(serial)
                if not keys:
                    self._send_json(404, {'error': f'{fmt.upper()} not available for this certificate. It may have been generated externally or the proxy was restarted.'})
                    return
                content = keys[fmt].encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-pem-file')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}.{fmt}"')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                pem_bytes = pem.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-pem-file')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}.pem"')
                self.send_header('Content-Length', str(len(pem_bytes)))
                self.end_headers()
                self.wfile.write(pem_bytes)

        def _handle_oidc_callback(self, params: dict):
            """Handle OIDC authorization code exchange."""
            if not oidc_config:
                self._send_html(404, page_wrap('Not Found', '<h2>OIDC Not Configured</h2>'))
                return

            code = params.get('code', [''])[0]
            state = params.get('state', [''])[0]
            
            cookie_state = None
            cookie_header = self.headers.get('Cookie', '')
            for cookie in cookie_header.split(';'):
                cookie = cookie.strip()
                if cookie.startswith('certsrv_oidc_state='):
                    cookie_state = cookie[len('certsrv_oidc_state='):]

            if not code or not state or state != cookie_state:
                self._send_html(400, page_wrap('Error', '<h2>OIDC Error</h2><p>Invalid state or missing code.</p>'))
                return

            token_url = oidc_config['token_endpoint']
            data = {
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': oidc_config['redirect_uri'],
                'client_id': oidc_config['client_id'],
                'client_secret': oidc_config['client_secret']
            }
            try:
                resp = requests.post(token_url, data=data, timeout=10, verify=oidc_config.get('verify_tls', True))
                if resp.status_code == 200:
                    token_data = resp.json()
                    access_token = token_data.get('access_token')
                    
                    username = 'oidc_user'
                    if 'userinfo_endpoint' in oidc_config:
                        ui_resp = requests.get(oidc_config['userinfo_endpoint'], 
                                               headers={'Authorization': f'Bearer {access_token}'}, 
                                               timeout=10, verify=oidc_config.get('verify_tls', True))
                        if ui_resp.status_code == 200:
                            ui_data = ui_resp.json()
                            username = ui_data.get('preferred_username') or ui_data.get('email') or ui_data.get('sub') or 'oidc_user'
                    
                    session_id = session_manager.create_session(username)
                    self.send_response(302)
                    self.send_header('Location', '/certsrv/')
                    self.send_header('Set-Cookie', f'certsrv_session={session_id}; HttpOnly; Path=/')
                    self.end_headers()
                    return
                else:
                    self._send_html(400, page_wrap('Error', f'<h2>OIDC Error</h2><p>Failed to exchange code.</p>'))
                    return
            except Exception as e:
                self._send_html(500, page_wrap('Error', f'<h2>OIDC Error</h2><p>{e}</p>'))
                return

        def _handle_api_revoke(self):
            """JSON API: revoke certificates by serial number."""
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'error': 'Empty request'})
                return
            try:
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
            except json.JSONDecodeError:
                self._send_json(400, {'error': 'Invalid JSON'})
                return

            serials = body.get('serials', [])
            if not serials:
                self._send_json(400, {'error': 'No serial numbers provided'})
                return

            revoked = 0
            failed = []
            for serial in serials:
                if vault_client.revoke_certificate(serial):
                    revoked += 1
                else:
                    failed.append(serial)

            self._send_json(200, {'revoked': revoked, 'failed': failed})

    return CertsrvHandler


def main():
    parser = argparse.ArgumentParser(
        description='VCF CA Proxy (Production - Behind Reverse Proxy)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This version runs plain HTTP. TLS is terminated by Reverse Proxy.

Examples:
  # Start with defaults (reads password from /root/creds.txt):
  python3 certsrv_proxy.py

  # Specify all options:
  python3 certsrv_proxy.py \\
    --port 8900 \\
    --vault-url http://127.0.0.1:32000 \\
    --vault-token 'mytoken' \\
    --vault-role holodeck \\
    --password 'YOUR_PASSWORD_HERE'
        """
    )
    parser.add_argument('--port', type=int, default=int(os.environ.get('PROXY_PORT', 8900)),
                        help='HTTP listen port (default: $PROXY_PORT or 8900)')
    parser.add_argument('--bind', default=os.environ.get('PROXY_BIND', '0.0.0.0'),
                        help='Bind address (default: $PROXY_BIND or 0.0.0.0)')
    parser.add_argument('--vault-url', default=os.environ.get('VAULT_ADDR', 'http://127.0.0.1:32000'),
                        help='Vault API URL (default: $VAULT_ADDR or http://127.0.0.1:32000)')
    parser.add_argument('--vault-token', default=os.environ.get('VAULT_TOKEN'),
                        help='Vault token (default: $VAULT_TOKEN or read from --creds-file)')
    parser.add_argument('--vault-mount', default=os.environ.get('VAULT_PKI_MOUNT', 'pki'),
                        help='Vault PKI mount path (default: $VAULT_PKI_MOUNT or pki)')
    parser.add_argument('--vault-role', default=os.environ.get('VAULT_PKI_ROLE', 'holodeck'),
                        help='Vault PKI role name (default: $VAULT_PKI_ROLE or holodeck)')
    parser.add_argument('--vault-skip-verify', action='store_true', default=os.environ.get('VAULT_SKIP_VERIFY', 'true').lower() == 'true',
                        help='Skip TLS verification for Vault (default: true)')
    parser.add_argument('--cert-ttl', default=os.environ.get('CERT_TTL', '17520h'),
                        help='Certificate TTL (default: $CERT_TTL or 17520h / 2 years)')
    parser.add_argument('--password', default=os.environ.get('PROXY_PASSWORD'),
                        help='Auth password (default: $PROXY_PASSWORD or read from --creds-file)')
    parser.add_argument('--creds-file', default=os.environ.get('CREDS_FILE', '/root/creds.txt'),
                        help='Path to credentials file (default: $CREDS_FILE or /root/creds.txt)')
    parser.add_argument('--debug', action='store_true', default=os.environ.get('PROXY_DEBUG', 'false').lower() == 'true',
                        help='Enable debug logging')
    parser.add_argument('--role-mapping', default=os.environ.get('ROLE_MAPPING'),
                        help='Path to JSON file mapping AD CS template names to Vault PKI roles (default: $ROLE_MAPPING)')
    parser.add_argument('--oidc-issuer', default=os.environ.get('OIDC_ISSUER'),
                        help='OIDC issuer URL (default: $OIDC_ISSUER)')
    parser.add_argument('--oidc-client-id', default=os.environ.get('OIDC_CLIENT_ID'),
                        help='OIDC Client ID (default: $OIDC_CLIENT_ID)')
    parser.add_argument('--oidc-client-secret', default=os.environ.get('OIDC_CLIENT_SECRET'),
                        help='OIDC Client Secret (default: $OIDC_CLIENT_SECRET)')
    parser.add_argument('--oidc-redirect-uri', default=os.environ.get('OIDC_REDIRECT_URI', 'https://ca.vcf.lab/certsrv/oidc/callback'),
                        help='OIDC Redirect URI (default: $OIDC_REDIRECT_URI or https://ca.vcf.lab/certsrv/oidc/callback)')

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    password = args.password
    vault_token = args.vault_token

    if not password or not vault_token:
        try:
            with open(args.creds_file) as f:
                creds_password = f.read().strip()
            if not password:
                password = creds_password
            if not vault_token:
                vault_token = creds_password
            logger.info('Loaded credentials from %s', args.creds_file)
        except FileNotFoundError:
            logger.error('Credentials file not found: %s', args.creds_file)
            if not password:
                logger.error('No password available. Use --password or --creds-file.')
                sys.exit(1)
            if not vault_token:
                logger.error('No Vault token available. Use --vault-token or --creds-file.')
                sys.exit(1)

    role_mapping = {}
    if args.role_mapping:
        try:
            with open(args.role_mapping, 'r') as f:
                role_mapping = json.load(f)
            logger.info('Loaded role mapping from %s: %s', args.role_mapping, role_mapping)
        except Exception as e:
            logger.error('Failed to load role mapping: %s', e)

    oidc_config = None
    if args.oidc_issuer and args.oidc_client_id and args.oidc_client_secret:
        oidc_config = {
            'client_id': args.oidc_client_id,
            'client_secret': args.oidc_client_secret,
            'redirect_uri': args.oidc_redirect_uri,
            'auth_endpoint': f"{args.oidc_issuer.rstrip('/')}/protocol/openid-connect/auth" if 'authentik' in args.oidc_issuer else f"{args.oidc_issuer.rstrip('/')}/authorize",
            'token_endpoint': f"{args.oidc_issuer.rstrip('/')}/protocol/openid-connect/token" if 'authentik' in args.oidc_issuer else f"{args.oidc_issuer.rstrip('/')}/token",
            'userinfo_endpoint': f"{args.oidc_issuer.rstrip('/')}/protocol/openid-connect/userinfo" if 'authentik' in args.oidc_issuer else f"{args.oidc_issuer.rstrip('/')}/userinfo",
            'verify_tls': True
        }
        logger.info('OIDC configuration enabled for issuer %s', args.oidc_issuer)

    vault_client = VaultPKIClient(
        vault_url=args.vault_url,
        vault_token=vault_token,
        pki_mount=args.vault_mount,
        default_pki_role=args.vault_role,
        cert_ttl=args.cert_ttl,
        skip_verify=args.vault_skip_verify,
        role_mapping=role_mapping
    )

    if not vault_client.health_check():
        logger.warning('Vault health check failed -- proxy will start but signing may fail')
    else:
        logger.info('Vault connection verified')

    cert_store = CertStore()
    key_store = KeyStore()
    session_manager = SessionManager()
    handler_class = make_handler(vault_client, cert_store, key_store, password, session_manager, oidc_config)
    server = HTTPServer((args.bind, args.port), handler_class)

    logger.info('=' * 60)
    logger.info('VCF CA Proxy (certsrv) - Production')
    logger.info('  Listening:  http://%s:%d/certsrv/', args.bind, args.port)
    logger.info('  Vault URL:  %s', args.vault_url)
    logger.info('  PKI Role:   %s/%s', args.vault_mount, args.vault_role)
    logger.info('  Cert TTL:   %s', args.cert_ttl)
    logger.info('  TLS:        Disabled (handled by Reverse Proxy)')
    logger.info('=' * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Shutting down...')
        server.shutdown()


if __name__ == '__main__':
    main()
