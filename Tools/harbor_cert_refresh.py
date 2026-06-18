#!/usr/bin/env python3
# harbor_cert_refresh.py - Issue Harbor TLS certificate from Vault PKI
# version 5.0 - 2026-06-18
# Author: Burke Azbill and HOL Core Team
#
# Strategy: preserve the existing cert-manager private key in harbor-tls and
# replace only the certificate (tls.crt) with a Vault-signed cert.
#
# cert-manager v1.18 reconciliation triggers (from cert-manager logs):
#   SecretMismatch    — private key in Secret doesn't match CertificateRequest
#   IncorrectIssuer   — annotation cert-manager.io/issuer-name != Certificate spec issuerRef
#   DNSNamesMismatch  — cert missing SANs from spec (extra SANs are OK in v1.18)
#
# By keeping the existing tls.key (cert-manager generated it; its public key is
# recorded in the current CertificateRequest), the SecretMismatch trigger is
# avoided.  Keeping annotations unchanged avoids IncorrectIssuer.  All spec SANs
# are included in the new cert (superset), avoiding DNSNamesMismatch.
# harbor-01a.site-a.vcf.lab is added as an extra SAN so TLS hostname validation
# works for the actual public FQDN.
#
# The Harbor kapp PackageInstall (vmware-system-supervisor-services) reconciles
# the Certificate spec every ~9 minutes.  This approach touches only the Secret
# data (tls.crt), which kapp does not manage, so the fix is durable.
#
# A cert-manager ClusterIssuer (vault-issuer) backed by Vault is also created in
# case future Certificate CRDs want to use Vault directly.
#
# Usage: python3 harbor_cert_refresh.py [--check-only]
#   --check-only: Print current cert issuer and exit without making changes.
#
# Called from lab-update.py during labstartup.

import subprocess
import json
import re
import base64
import time
import sys
import tempfile
import os
import shutil
import requests
import argparse
import urllib3
urllib3.disable_warnings()

sys.path.append("/home/holuser/hol")
try:
    import lsfunctions as lsf
except ImportError:
    class lsf:
        @staticmethod
        def write_output(msg):
            print(msg)

VAULT_URL = "https://vault.vcf.lab"
VCENTER_HOST = "vc-wld01-a.site-a.vcf.lab"
HARBOR_NS = "svc-harbor-zjx6i"
HARBOR_FQDN = "harbor-01a.site-a.vcf.lab"
VAULT_ROOT_CA_CN = "vcf.lab Root Authority"

# cert-manager Certificate spec values — must be kept exactly
HARBOR_CERT_CN = "harbor"
HARBOR_CERT_DNS_SPEC = ["harbor.yourdomain.com", "depot.kube-system.svc"]
HARBOR_CERT_IP = ["10.1.8.137"]
HARBOR_CERT_TTL = "17520h"

# cert-manager namespace (ClusterIssuer token Secret goes here)
CERT_MANAGER_NS = "vmware-system-cert-manager"
CLUSTER_ISSUER_NAME = "vault-issuer"
VAULT_TOKEN_SECRET_NAME = "vault-token"


def get_vault_token():
    return open("/home/holuser/creds.txt").read().strip()


def get_supervisor_creds(vcenter_host, vcenter_pwd):
    result = subprocess.run(
        ["sshpass", "-p", vcenter_pwd, "ssh",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "UserKnownHostsFile=/dev/null",
         "root@" + vcenter_host,
         "python3 /usr/lib/vmware-wcp/decryptK8Pwd.py"],
        capture_output=True, text=True, timeout=60
    )
    ip_m = re.search(r"IP:\s*([0-9.]+)", result.stdout)
    pwd_m = re.search(r"PWD:\s*(\S+)", result.stdout)
    if not ip_m or not pwd_m:
        lsf.write_output(f"  ERROR: Could not parse Supervisor creds: {result.stdout[:200]}")
        return None, None
    return ip_m.group(1), pwd_m.group(1)


def sup_run(sup_ip, sup_pwd, args_list, timeout=60):
    r = subprocess.run(
        ["sshpass", "-p", sup_pwd, "ssh",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "UserKnownHostsFile=/dev/null",
         "root@" + sup_ip] + args_list,
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def get_vault_root_ca(vault_url):
    resp = requests.get(f"{vault_url}/v1/pki/ca/pem", verify=False, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not fetch Vault root CA: HTTP {resp.status_code}")
    return resp.text.strip()


def harbor_cert_is_from_vault(sup_ip, sup_pwd, ns):
    """Return True if harbor-tls cert is signed by the Vault root CA."""
    out, _, rc = sup_run(sup_ip, sup_pwd,
        ["kubectl", "get", "secret", "harbor-tls", "-n", ns, "-o", "json"])
    if rc != 0 or not out:
        lsf.write_output("  harbor-tls secret not found.")
        return False
    try:
        secret = json.loads(out)
        tls_crt_b64 = secret["data"]["tls.crt"]
        cert_pem = base64.b64decode(tls_crt_b64).decode("utf-8", errors="replace")
    except (json.JSONDecodeError, KeyError) as e:
        lsf.write_output(f"  Could not parse harbor-tls secret: {e}")
        return False

    r = subprocess.run(["openssl", "x509", "-noout", "-issuer"],
                       input=cert_pem, capture_output=True, text=True)
    issuer_line = r.stdout.strip()
    lsf.write_output(f"  Current harbor-tls issuer: {issuer_line}")
    return VAULT_ROOT_CA_CN in issuer_line


def get_harbor_tls_key(sup_ip, sup_pwd, ns):
    """Read the current tls.key PEM from harbor-tls secret."""
    out, _, rc = sup_run(sup_ip, sup_pwd,
        ["kubectl", "get", "secret", "harbor-tls", "-n", ns, "-o", "json"])
    if rc != 0 or not out:
        raise RuntimeError("Could not fetch harbor-tls secret")
    secret = json.loads(out)
    key_b64 = secret["data"].get("tls.key", "")
    if not key_b64:
        raise RuntimeError("harbor-tls secret has no tls.key")
    return base64.b64decode(key_b64).decode("utf-8", errors="replace")


def issue_vault_cert_for_key(vault_url, vault_token, existing_key_pem,
                             common_name, dns_sans, ip_sans, ttl):
    """Generate a CSR from an existing private key and sign it with Vault.

    Uses sign-verbatim (no role domain restrictions) so harbor.yourdomain.com
    and depot.kube-system.svc are accepted even though they're not under vcf.lab.
    Returns cert_pem (the signed certificate PEM).
    """
    tmp = tempfile.mkdtemp()
    try:
        key_file = os.path.join(tmp, "tls.key")
        csr_file = os.path.join(tmp, "tls.csr")
        cfg_file = os.path.join(tmp, "csr.cnf")

        with open(key_file, "w") as f:
            f.write(existing_key_pem)

        san_entries = ",".join(
            [f"DNS:{d}" for d in dns_sans] + [f"IP:{ip}" for ip in ip_sans]
        )
        cfg = (
            "[req]\n"
            "distinguished_name = dn\n"
            "req_extensions = v3_req\n"
            "prompt = no\n"
            "[dn]\n"
            f"CN = {common_name}\n"
            "[v3_req]\n"
            "keyUsage = digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            f"subjectAltName = {san_entries}\n"
        )
        with open(cfg_file, "w") as f:
            f.write(cfg)

        subprocess.run(
            ["openssl", "req", "-new", "-key", key_file, "-out", csr_file,
             "-config", cfg_file],
            check=True, capture_output=True
        )

        with open(csr_file) as f:
            csr_pem = f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    resp = requests.post(
        f"{vault_url}/v1/pki/sign-verbatim/holodeck",
        headers={"X-Vault-Token": vault_token},
        json={"csr": csr_pem, "ttl": ttl},
        verify=False, timeout=30
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Vault sign-verbatim failed ({resp.status_code}): {resp.text[:400]}"
        )
    return resp.json()["data"]["certificate"]


def patch_harbor_tls_cert_only(sup_ip, sup_pwd, ns, cert_pem, ca_pem):
    """Replace only tls.crt and ca.crt in harbor-tls; preserve tls.key and annotations.

    Preserving tls.key avoids the cert-manager SecretMismatch trigger (cert-manager
    checks that harbor-tls private key matches its own CertificateRequest).
    Not modifying annotations avoids the IncorrectIssuer trigger.
    """
    patch = json.dumps({
        "data": {
            "tls.crt": base64.b64encode(cert_pem.encode()).decode(),
            "ca.crt":  base64.b64encode(ca_pem.encode()).decode(),
        }
    })
    r = subprocess.run(
        ["sshpass", "-p", sup_pwd, "ssh",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "UserKnownHostsFile=/dev/null",
         "root@" + sup_ip,
         f"kubectl patch secret harbor-tls -n {ns} --type merge -p '{patch}'"],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def ensure_cluster_issuer(sup_ip, sup_pwd, vault_url, vault_token):
    """Create vault-token Secret + ClusterIssuer for future Vault-backed cert issuance."""
    lsf.write_output(f"Ensuring ClusterIssuer '{CLUSTER_ISSUER_NAME}' (for future use)...")
    vault_root_ca_pem = get_vault_root_ca(vault_url)
    vault_root_ca_b64 = base64.b64encode(vault_root_ca_pem.encode()).decode()
    vault_token_b64 = base64.b64encode(vault_token.encode()).decode()

    token_secret = json.dumps({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": VAULT_TOKEN_SECRET_NAME, "namespace": CERT_MANAGER_NS},
        "type": "Opaque", "data": {"token": vault_token_b64},
    })
    r = subprocess.run(
        ["sshpass", "-p", sup_pwd, "ssh",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null",
         "root@" + sup_ip,
         f"echo '{token_secret}' | kubectl apply -f -"],
        capture_output=True, text=True, timeout=30
    )
    lsf.write_output(f"  vault-token: {r.stdout.strip() or r.stderr.strip()}")

    cluster_issuer = json.dumps({
        "apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer",
        "metadata": {"name": CLUSTER_ISSUER_NAME},
        "spec": {"vault": {
            "server": vault_url,
            "path": "pki/sign-verbatim/holodeck",
            "caBundle": vault_root_ca_b64,
            "auth": {"tokenSecretRef": {"name": VAULT_TOKEN_SECRET_NAME, "key": "token"}},
        }},
    })
    r = subprocess.run(
        ["sshpass", "-p", sup_pwd, "ssh",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null",
         "root@" + sup_ip,
         f"echo '{cluster_issuer}' | kubectl apply -f -"],
        capture_output=True, text=True, timeout=30
    )
    lsf.write_output(f"  ClusterIssuer: {r.stdout.strip() or r.stderr.strip()}")


def refresh_harbor_cert(check_only=False):
    lsf.write_output("=== Harbor Certificate Refresh (Vault PKI, preserve tls.key) ===")
    vault_token = get_vault_token()

    lsf.write_output("Getting Supervisor credentials...")
    sup_ip, sup_pwd = get_supervisor_creds(VCENTER_HOST, vault_token)
    if not sup_ip:
        return False
    lsf.write_output(f"  Supervisor IP: {sup_ip}")

    lsf.write_output("Checking current Harbor TLS certificate chain...")
    if harbor_cert_is_from_vault(sup_ip, sup_pwd, HARBOR_NS):
        lsf.write_output("Harbor TLS cert is already signed by Vault root CA. No update needed.")
        return True

    if check_only:
        lsf.write_output("check-only: cert needs update but no changes made.")
        return False

    lsf.write_output("Harbor cert NOT from Vault root CA — issuing Vault cert...")

    # Set up ClusterIssuer (idempotent; useful for future Certificate CRDs)
    ensure_cluster_issuer(sup_ip, sup_pwd, VAULT_URL, vault_token)

    # Fetch Vault root CA
    lsf.write_output("Fetching Vault root CA...")
    try:
        vault_root_ca_pem = get_vault_root_ca(VAULT_URL)
    except Exception as e:
        lsf.write_output(f"  ERROR: {e}")
        return False
    r = subprocess.run(["openssl", "x509", "-noout", "-subject"],
                       input=vault_root_ca_pem, capture_output=True, text=True)
    lsf.write_output(f"  {r.stdout.strip()}")

    # Read the existing private key from harbor-tls — MUST preserve it to avoid
    # cert-manager SecretMismatch (key in Secret must match key in CertificateRequest)
    lsf.write_output("Reading existing harbor-tls private key...")
    try:
        existing_key_pem = get_harbor_tls_key(sup_ip, sup_pwd, HARBOR_NS)
    except Exception as e:
        lsf.write_output(f"  ERROR: {e}")
        return False

    # Build SAN list: spec-required SANs + FQDN for hostname TLS validation.
    # Extra SANs do NOT trigger cert-manager re-issuance in v1.18 (it checks only
    # that spec SANs are a subset of cert SANs, not for exact equality).
    dns_sans = HARBOR_CERT_DNS_SPEC + [HARBOR_FQDN]
    lsf.write_output(
        f"Signing with Vault for CN={HARBOR_CERT_CN} ..."
        f"\n  DNS SANs: {dns_sans}"
        f"\n  IP SANs:  {HARBOR_CERT_IP}"
        f"\n  TTL:      {HARBOR_CERT_TTL}"
    )
    try:
        cert_pem = issue_vault_cert_for_key(
            VAULT_URL, vault_token, existing_key_pem,
            common_name=HARBOR_CERT_CN,
            dns_sans=dns_sans,
            ip_sans=HARBOR_CERT_IP,
            ttl=HARBOR_CERT_TTL,
        )
    except Exception as e:
        lsf.write_output(f"  ERROR: {e}")
        return False

    r = subprocess.run(
        ["openssl", "x509", "-noout", "-issuer", "-subject", "-dates",
         "-ext", "subjectAltName"],
        input=cert_pem, capture_output=True, text=True)
    lsf.write_output(f"  Issued cert:\n{r.stdout.strip()}")

    # Replace only tls.crt (preserve tls.key and cert-manager annotations)
    lsf.write_output("Patching harbor-tls (tls.crt only, preserving tls.key)...")
    out, err, rc = patch_harbor_tls_cert_only(
        sup_ip, sup_pwd, HARBOR_NS, cert_pem, vault_root_ca_pem)
    if rc != 0:
        lsf.write_output(f"  ERROR: {err}")
        return False
    lsf.write_output(f"  {out}")

    # Restart harbor-nginx to reload the cert
    lsf.write_output("Restarting harbor-nginx...")
    out, err, _ = sup_run(sup_ip, sup_pwd,
        ["kubectl", "rollout", "restart", "deployment/harbor-nginx", "-n", HARBOR_NS])
    lsf.write_output(f"  {out or err}")

    lsf.write_output("Waiting for harbor-nginx rollout (up to 120s)...")
    out, err, _ = sup_run(sup_ip, sup_pwd,
        ["kubectl", "rollout", "status", "deployment/harbor-nginx",
         "-n", HARBOR_NS, "--timeout=120s"],
        timeout=130)
    lsf.write_output(f"  {out or err}")

    lsf.write_output("=== Harbor Certificate Refresh Complete ===")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replace harbor-tls cert with Vault-signed cert (preserves private key)"
    )
    parser.add_argument("--check-only", action="store_true",
                        help="Report current cert state; do not make changes")
    args = parser.parse_args()
    ok = refresh_harbor_cert(check_only=args.check_only)
    sys.exit(0 if ok else 1)
