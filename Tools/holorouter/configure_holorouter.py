#!/usr/bin/env python3
"""
configure_holorouter.py - Holorouter Normalization Script

Normalizes a freshly-imported holorouter VM by:
  Phase  1: Vault - switch from dev/inmem to standalone/persistent + PKI
  Phase  2: DNS records (CNAMEs for dns/auth, A for ca)
  Phase  3: Issue Vault CA-signed certs (Vault, GitLab, Technitium, Authentik, ca.vcf.lab)
  Phase 3b: Distribute Vault Root CA to Manager & Console VMs
  Phase  4: Fix GitLab root password + initialization
  Phase  5: Fix nginx config (HTTPS for Technitium/Authentik/GitLab/ca, HTTP preserved for Authentik)
  Phase  6: Deploy certsrv proxy (MSADCS -> Vault PKI)
  Phase  7: Update shutdown.sh (add nginx stop)
  Phase  8: Setup icon serving via nginx
  Phase  9: Authentik users and groups
  Phase 10: Create app tiles with icon URLs
  Phase 11: Verification

Run from manager VM:  python3 configure_holorouter.py
Idempotent: safe to re-run.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import glob as globmod

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_ZIP = os.path.join(SCRIPT_DIR, "images.zip")
IMAGES_DIR = "/tmp/holorouter-images"
CERTSRV_PROXY_SRC = os.path.join(SCRIPT_DIR, "certsrv_proxy.py")
CERTSRV_DOCKERFILE_SRC = os.path.join(SCRIPT_DIR, "Dockerfile.certsrv-proxy")
CREDS_FILE = "/home/holuser/creds.txt"

ROUTER_HOST = "router"
ROUTER_ETH0_IP = "192.168.0.2"
VAULT_NODEPORT = 32000
AUTHENTIK_NODEPORT = 31080
DNS_PORT = 5380


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_password():
    with open(CREDS_FILE) as f:
        return f.read().strip()


def extract_images():
    """Extract password-protected images.zip to IMAGES_DIR if not already present."""
    if os.path.isdir(IMAGES_DIR) and len(os.listdir(IMAGES_DIR)) > 50:
        return
    if not os.path.isfile(IMAGES_ZIP):
        print(f"  [ERROR] {IMAGES_ZIP} not found")
        return
    pw = get_password()
    os.makedirs(IMAGES_DIR, exist_ok=True)
    r = subprocess.run(
        ["unzip", "-o", "-P", pw, "-j", IMAGES_ZIP, "-d", IMAGES_DIR],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        print(f"  [ERROR] Failed to extract images.zip: {r.stderr.strip()[:200]}")
    else:
        count = len([f for f in os.listdir(IMAGES_DIR) if not f.startswith(".")])
        print(f"  [OK] Extracted {count} icons to {IMAGES_DIR}")


def ssh(cmd, timeout=300, check=True):
    pw = get_password()
    full = (
        f'sshpass -p "{pw}" ssh -o StrictHostKeyChecking=accept-new '
        f'-o PubkeyAuthentication=no root@{ROUTER_HOST} "{cmd}"'
    )
    r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        print(f"  [SSH ERROR] rc={r.returncode}")
        if r.stdout.strip():
            print(f"  stdout: {r.stdout.strip()[:500]}")
        if r.stderr.strip():
            print(f"  stderr: {r.stderr.strip()[:500]}")
    return r


def scp_to_router(local_path, remote_path):
    pw = get_password()
    cmd = (
        f'sshpass -p "{pw}" scp -o StrictHostKeyChecking=accept-new '
        f'-o PubkeyAuthentication=no -r "{local_path}" root@{ROUTER_HOST}:"{remote_path}"'
    )
    subprocess.run(cmd, shell=True, check=True, capture_output=True, timeout=120)


def vault_api(path, method="GET", data=None, token=None):
    """Call Vault HTTP API via the holorouter NodePort."""
    import urllib.request
    import urllib.error
    url = f"http://{ROUTER_HOST}:{VAULT_NODEPORT}/v1/{path}"
    headers = {}
    if token:
        headers["X-Vault-Token"] = token
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = ""
        return {"errors": [f"HTTP {e.code}: {err_body[:300]}"]}
    except Exception as e:
        return {"errors": [str(e)]}


def dns_api(action, params_str):
    """Call Technitium DNS API."""
    import urllib.request
    pw = get_password()
    login_url = f"http://{ROUTER_HOST}:{DNS_PORT}/api/user/login?user=admin&pass={pw}"
    with urllib.request.urlopen(login_url, timeout=10) as resp:
        token = json.loads(resp.read().decode())["token"]
    url = f"http://{ROUTER_HOST}:{DNS_PORT}/api/{action}?token={token}&{params_str}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def authentik_api(path, method="GET", data=None, files_data=None):
    """Call Authentik API via NodePort."""
    import urllib.request
    import urllib.error
    url = f"http://{ROUTER_ETH0_IP}:{AUTHENTIK_NODEPORT}/api/v3/{path}"
    headers = {"Authorization": "Bearer holodeck"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = ""
        return {"error": f"HTTP {e.code}", "detail": err_body[:300]}
    except Exception as e:
        return {"error": str(e)}


def wait_for(description, check_fn, timeout=300, interval=10):
    print(f"  Waiting for {description}...")
    elapsed = 0
    while elapsed < timeout:
        if check_fn():
            print(f"  {description} ready ({elapsed}s)")
            return True
        time.sleep(interval)
        elapsed += interval
        if elapsed % 30 == 0:
            print(f"    still waiting... ({elapsed}s)")
    print(f"  TIMEOUT waiting for {description} after {timeout}s")
    return False


# ---------------------------------------------------------------------------
# Phase 1: Vault
# ---------------------------------------------------------------------------

def phase_1_vault():
    print("\n" + "=" * 60)
    print("Phase 1: Vault - Switch to Standalone + PKI")
    print("=" * 60)

    pw = get_password()

    r = ssh("curl -s http://localhost:32000/v1/sys/seal-status", check=False)
    if r.returncode == 0:
        status = json.loads(r.stdout)
        if status.get("storage_type") == "file":
            token_ok = vault_api("auth/token/lookup-self", token=pw)
            if token_ok.get("data"):
                pki = vault_api("pki/cert/ca", token=pw)
                if pki.get("data", {}).get("certificate"):
                    print("  Vault already in standalone mode with PKI. Skipping.")
                    return
                else:
                    print("  Vault standalone but PKI missing - will configure PKI only.")
                    _configure_vault_pki(pw)
                    return
            else:
                print("  Vault standalone but token mismatch - running config_vault.sh")

    print("  Running /root/config_vault.sh on holorouter (this takes ~2 minutes)...")
    r = ssh("bash /root/config_vault.sh", timeout=600, check=False)
    if r.returncode != 0:
        print(f"  config_vault.sh returned rc={r.returncode}")
        if r.stderr.strip():
            print(f"  stderr tail: {r.stderr.strip()[-500:]}")

    wait_for("Vault pod ready", lambda: _vault_ready(), timeout=120, interval=5)

    r = ssh("cat /root/vault-keys/init.json", check=False)
    if r.returncode == 0:
        keys = json.loads(r.stdout)
        print(f"  Root token set to creds.txt password: {'Yes' if keys.get('root_token') == pw else 'No'}")

    pki = vault_api("pki/cert/ca", token=pw)
    if not pki.get("data", {}).get("certificate"):
        print("  PKI not configured by config_vault.sh, configuring manually...")
        _configure_vault_pki(pw)
    else:
        print("  PKI engine and CA already configured.")

    print("  Phase 1 complete.")


def _vault_ready():
    try:
        r = vault_api("sys/seal-status")
        return r.get("sealed") is False
    except Exception:
        return False


def _configure_vault_pki(token):
    print("  Enabling PKI secrets engine...")
    vault_api("sys/mounts/pki", method="POST", data={"type": "pki"}, token=token)

    print("  Tuning PKI max lease TTL (10 years)...")
    vault_api("sys/mounts/pki/tune", method="POST", data={"max_lease_ttl": "87600h"}, token=token)

    print("  Generating Root CA...")
    vault_api("pki/root/generate/internal", method="POST", data={
        "common_name": "vcf.lab Root Authority",
        "ttl": "87600h",
    }, token=token)

    print("  Configuring PKI URLs...")
    vault_api("pki/config/urls", method="POST", data={
        "issuing_certificates": f"http://{ROUTER_ETH0_IP}:{VAULT_NODEPORT}/v1/pki/ca",
        "crl_distribution_points": f"http://{ROUTER_ETH0_IP}:{VAULT_NODEPORT}/v1/pki/crl",
    }, token=token)

    print("  Creating 'holodeck' role...")
    vault_api("pki/roles/holodeck", method="POST", data={
        "allowed_domains": "vcf.lab",
        "allow_subdomains": True,
        "max_ttl": "720h",
    }, token=token)

    pki = vault_api("pki/cert/ca", token=token)
    if pki.get("data", {}).get("certificate"):
        print("  PKI configured successfully.")
    else:
        print("  WARNING: PKI configuration may have failed.")


# ---------------------------------------------------------------------------
# Phase 2: DNS Records
# ---------------------------------------------------------------------------

def phase_2_dns():
    print("\n" + "=" * 60)
    print("Phase 2: DNS Records")
    print("=" * 60)

    records = [
        ("CNAME", "dns.vcf.lab", "technitium.vcf.lab"),
        ("CNAME", "auth.vcf.lab", "authentik.vcf.lab"),
        ("A", "ca.vcf.lab", ROUTER_ETH0_IP),
    ]

    for rtype, domain, value in records:
        try:
            existing = dns_api("zones/records/get", f"domain={domain}&zone=vcf.lab")
            recs = existing.get("response", {}).get("records", [])
            already = any(
                r.get("type") == rtype and (
                    r.get("rData", {}).get("ipAddress") == value if rtype == "A"
                    else r.get("rData", {}).get("cname") == value
                )
                for r in recs
            )
            if already:
                print(f"  [SKIP] {domain} -> {value} ({rtype}) already exists")
                continue
        except Exception:
            pass

        if rtype == "A":
            params = f"domain={domain}&zone=vcf.lab&type=A&ipAddress={value}&ttl=3600"
        else:
            params = f"domain={domain}&zone=vcf.lab&type=CNAME&cname={value}&ttl=3600"
        try:
            dns_api("zones/records/add", params)
            print(f"  [OK] Created {rtype} {domain} -> {value}")
        except Exception as e:
            print(f"  [ERROR] Failed to create {domain}: {e}")

    print("  Phase 2 complete.")


# ---------------------------------------------------------------------------
# Phase 3: Issue Vault CA-signed Certificates
# ---------------------------------------------------------------------------

def phase_3_certs():
    print("\n" + "=" * 60)
    print("Phase 3: Issue Vault CA-signed Certificates")
    print("=" * 60)

    pw = get_password()

    ssh("mkdir -p /root/nginx-certs", check=False)

    certs_to_issue = [
        {
            "cn": "gitlab.vcf.lab", "ttl": "720h",
            "paths": [
                "/holodeck-runtime/gitlab/ssl/gitlab",
                "/holodeck-runtime/gitlab/config/ssl/gitlab",
            ],
        },
        {
            "cn": "gitlab-registry.vcf.lab", "ttl": "720h",
            "paths": [
                "/holodeck-runtime/gitlab/ssl/gitlab-registry",
                "/holodeck-runtime/gitlab/config/ssl/gitlab-registry",
            ],
        },
        {
            "cn": "vault.vcf.lab", "ttl": "720h",
            "ip_sans": f"{ROUTER_ETH0_IP},10.1.1.1",
            "paths": ["/root/nginx-certs/vault"],
        },
        {
            "cn": "ca.vcf.lab", "ttl": "720h",
            "paths": ["/root/certsrv-proxy/ca"],
        },
        {
            "cn": "technitium.vcf.lab", "ttl": "720h",
            "alt_names": "dns.vcf.lab",
            "ip_sans": f"{ROUTER_ETH0_IP},10.1.1.1",
            "paths": ["/root/nginx-certs/technitium"],
        },
        {
            "cn": "auth.vcf.lab", "ttl": "720h",
            "alt_names": "authentik.vcf.lab",
            "ip_sans": ROUTER_ETH0_IP,
            "paths": ["/root/nginx-certs/authentik"],
        },
    ]

    for spec in certs_to_issue:
        cn = spec["cn"]
        print(f"  Issuing cert for {cn}...")
        issue_data = {"common_name": cn, "ttl": spec["ttl"]}
        if "alt_names" in spec:
            issue_data["alt_names"] = spec["alt_names"]
        if "ip_sans" in spec:
            issue_data["ip_sans"] = spec["ip_sans"]

        result = vault_api("pki/issue/holodeck", method="POST", data=issue_data, token=pw)

        if "errors" in result:
            print(f"  [ERROR] Vault cert issue failed for {cn}: {result['errors']}")
            continue

        cert_data = result.get("data", {})
        cert_pem = cert_data.get("certificate", "") + "\n" + cert_data.get("issuing_ca", "") + "\n"
        key_pem = cert_data.get("private_key", "") + "\n"

        for path in spec["paths"]:
            ssh(f"mkdir -p $(dirname {path}.crt)", check=False)
            _write_remote_file(f"{path}.crt", cert_pem)
            _write_remote_file(f"{path}.key", key_pem)

        sans = spec.get("alt_names", "")
        ip_sans = spec.get("ip_sans", "")
        extra = ""
        if sans:
            extra += f" SANs={sans}"
        if ip_sans:
            extra += f" IPs={ip_sans}"
        print(f"  [OK] {cn} cert written{extra}")

    print("  Restarting GitLab pod to pick up new certs...")
    ssh("kubectl delete pod -l app=gitlab -n default --grace-period=15 --wait=false", check=False)

    print("  Phase 3 complete.")


def _write_remote_file(remote_path, content):
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        scp_to_router(tmp, remote_path)
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Phase 3b: Distribute Vault Root CA to Manager & Console VMs
# ---------------------------------------------------------------------------

CA_CERT_NAME = "vcf-lab-root-ca.crt"
CA_TRUST_DIR = "/usr/local/share/ca-certificates"

def _ssh_host(host, user, cmd, timeout=60):
    """SSH to an arbitrary host as a given user."""
    pw = get_password()
    full = (
        f'sshpass -p "{pw}" ssh -o StrictHostKeyChecking=accept-new '
        f'-o PubkeyAuthentication=no {user}@{host} "{cmd}"'
    )
    return subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)


def _scp_to_host(host, user, local_path, remote_path, timeout=60):
    """SCP a file to an arbitrary host as a given user."""
    pw = get_password()
    cmd = (
        f'sshpass -p "{pw}" scp -o StrictHostKeyChecking=accept-new '
        f'-o PubkeyAuthentication=no "{local_path}" {user}@{host}:"{remote_path}"'
    )
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def phase_3b_distribute_ca():
    print("\n" + "=" * 60)
    print("Phase 3b: Distribute Vault Root CA to Manager & Console VMs")
    print("=" * 60)

    pw = get_password()
    pki = vault_api("pki/cert/ca", token=pw)
    ca_pem = pki.get("data", {}).get("certificate", "")
    if not ca_pem:
        print("  [ERROR] Could not retrieve Vault Root CA - skipping")
        return

    import tempfile
    ca_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False)
    ca_tmp.write(ca_pem + "\n")
    ca_tmp.close()

    targets = [
        ("localhost", "root", "Manager VM"),
        ("console", "root", "Console VM"),
    ]

    for host, user, label in targets:
        print(f"\n  --- {label} ({user}@{host}) ---")

        r = _ssh_host(host, user, "cat /etc/os-release 2>/dev/null | head -1")
        if r.returncode != 0:
            print(f"  [WARN] Cannot SSH to {user}@{host} - skipping")
            continue

        remote_ca_path = f"{CA_TRUST_DIR}/{CA_CERT_NAME}"

        r = _ssh_host(host, user, f"openssl x509 -fingerprint -noout -in {remote_ca_path} 2>/dev/null")
        existing_fp = r.stdout.strip()
        import hashlib
        ca_der = subprocess.run(
            ["openssl", "x509", "-outform", "DER"],
            input=ca_pem.encode(), capture_output=True
        ).stdout
        local_fp = hashlib.sha1(ca_der).hexdigest().upper() if ca_der else ""

        if existing_fp and local_fp and local_fp in existing_fp.replace(":", "").upper():
            print(f"  [SKIP] CA already installed and matches")
        else:
            print(f"  Installing Vault Root CA...")
            _ssh_host(host, user, f"mkdir -p {CA_TRUST_DIR}")
            r = _scp_to_host(host, user, ca_tmp.name, remote_ca_path)
            if r.returncode != 0:
                print(f"  [ERROR] Failed to copy CA cert: {r.stderr.strip()}")
                continue

            r = _ssh_host(host, user, "update-ca-certificates", timeout=30)
            if r.returncode == 0:
                count = ""
                for line in (r.stdout + r.stderr).split("\n"):
                    if "added" in line.lower() or "certificate" in line.lower():
                        count = line.strip()
                        break
                print(f"  [OK] CA installed and trust store updated ({count})")
            else:
                print(f"  [WARN] update-ca-certificates failed: {r.stderr.strip()[:200]}")

        r = _ssh_host(
            host, user,
            "curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://vault.vcf.lab/ 2>/dev/null",
        )
        code = r.stdout.strip().strip("'")
        if code in ("200", "307"):
            print(f"  [OK] HTTPS verification passed (HTTP {code})")
        else:
            print(f"  [INFO] HTTPS test returned HTTP {code} (may need curl restart)")

    os.unlink(ca_tmp.name)
    print("\n  Phase 3b complete.")


# ---------------------------------------------------------------------------
# Phase 4: Fix GitLab Root Password
# ---------------------------------------------------------------------------

def phase_4_gitlab():
    print("\n" + "=" * 60)
    print("Phase 4: Fix GitLab Root Password")
    print("=" * 60)

    pw = get_password()

    print("  Waiting for GitLab to be ready...")
    wait_for("GitLab HTTPS", lambda: _gitlab_responds(), timeout=300, interval=15)

    r = ssh(
        f"curl -sk -X POST 'https://localhost:30443/oauth/token' "
        f"-F grant_type=password -F username=root -F 'password={pw}'",
        check=False
    )
    try:
        token = json.loads(r.stdout).get("access_token")
    except Exception:
        token = None

    if token:
        print("  [SKIP] Root password already matches creds.txt")
        return

    print("  Attempting API password reset via initial_root_password...")
    _gitlab_api_password_reset(pw)

    print("  Phase 4 complete.")


def _gitlab_responds():
    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://localhost:30443/", check=False)
    code = r.stdout.strip().strip("'")
    return code in ("200", "302", "301")


def _gitlab_api_password_reset(pw):
    pod_r = ssh("kubectl get pod -n default -l app=gitlab -o jsonpath='{.items[0].metadata.name}'", check=False)
    pod = pod_r.stdout.strip().strip("'") if pod_r.returncode == 0 else ""

    init_pw = None
    if pod:
        pw_r = ssh(f"kubectl exec -n default {pod} -- cat /etc/gitlab/initial_root_password 2>/dev/null", check=False)
        for line in pw_r.stdout.splitlines():
            if line.startswith("Password:"):
                init_pw = line.split(":", 1)[1].strip()
                break

    passwords_to_try = []
    if init_pw:
        passwords_to_try.append(init_pw)
    passwords_to_try.append("uQKBP2ZHIaTfZBWCJ7fXCB1a+avFB0jHcbu+pGkf+VE=")

    token = None
    for candidate_pw in passwords_to_try:
        r = ssh(
            f"curl -sk -X POST 'https://localhost:30443/oauth/token' "
            f"-F grant_type=password -F username=root -F 'password={candidate_pw}'",
            check=False
        )
        try:
            token = json.loads(r.stdout).get("access_token")
        except Exception:
            token = None
        if token:
            print(f"  Authenticated with {'initial_root_password' if candidate_pw == init_pw else 'fallback password'}")
            break

    if not token:
        print("  API auth failed, falling back to gitlab-rails runner...")
        if pod:
            rails_cmd = (
                f"user = User.find_by(username: %q(root)); "
                f"user.password = %q({pw}); "
                f"user.password_confirmation = %q({pw}); "
                f"user.email = %q(root@vcf.lab); "
                f"user.save!"
            )
            r = ssh(
                f"kubectl exec -n default {pod} -- gitlab-rails runner \\\"{rails_cmd}\\\"",
                timeout=180, check=False
            )
            if r.returncode == 0:
                print("  [OK] Password reset via rails runner")
            else:
                print(f"  [ERROR] rails runner returned rc={r.returncode} - manual password reset needed")
        else:
            print("  [ERROR] Could not find GitLab pod - manual password reset needed")
        return

    ssh(
        f"curl -sk -X PUT 'https://localhost:30443/api/v4/users/1' "
        f"-H 'Authorization: Bearer {token}' "
        f"-d 'password={pw}' -d 'email=root@vcf.lab'",
        check=False
    )
    r = ssh(
        f"curl -sk -X POST 'https://localhost:30443/oauth/token' "
        f"-F grant_type=password -F username=root -F 'password={pw}'",
        check=False
    )
    try:
        verify_token = json.loads(r.stdout).get("access_token")
    except Exception:
        verify_token = None
    if verify_token:
        print("  [OK] Root password reset and verified")
    else:
        print("  [WARN] Password reset sent but verification failed")


# ---------------------------------------------------------------------------
# Phase 5: Fix nginx Configuration
# ---------------------------------------------------------------------------

def phase_5_nginx():
    print("\n" + "=" * 60)
    print("Phase 5: Fix nginx Configuration")
    print("=" * 60)

    r = ssh("cat /etc/nginx/nginx.conf", check=False)
    if r.returncode != 0:
        print("  [ERROR] Could not read nginx.conf")
        return

    conf = r.stdout
    modified = False

    # --- Vault: add HTTPS block (keep HTTP working too) ---
    if not _find_full_server_block(conf, "vault.vcf.lab", "443"):
        vault_http = _find_full_server_block(conf, "vault.vcf.lab", "80")
        if vault_http:
            vault_ssl = (
                "\n    server {\n"
                "        listen 443 ssl;\n"
                "        server_name vault.vcf.lab;\n"
                "\n"
                "        ssl_certificate     /root/nginx-certs/vault.crt;\n"
                "        ssl_certificate_key /root/nginx-certs/vault.key;\n"
                "\n"
                "        location / {\n"
                "            proxy_pass http://localhost:32000;\n"
                "            proxy_set_header Host $host;\n"
                "            proxy_set_header X-Real-IP $remote_addr;\n"
                "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "            proxy_set_header X-Forwarded-Proto https;\n"
                "        }\n"
                "    }"
            )
            idx = conf.find(vault_http) + len(vault_http)
            conf = conf[:idx] + vault_ssl + conf[idx:]
            modified = True
            print("  [ADD] Vault HTTPS block")
        else:
            print("  [WARN] Could not locate Vault HTTP block")
    else:
        print("  [SKIP] Vault SSL already configured")

    # --- Technitium: convert HTTP-only to HTTP redirect + HTTPS ---
    tech_http_only = _find_full_server_block(conf, "technitium.vcf.lab", "80")
    if tech_http_only and "return 301" not in tech_http_only:
        tech_redirect = (
            "    server {\n"
            "        listen 80;\n"
            "        server_name technitium.vcf.lab dns.vcf.lab;\n"
            "        return 301 https://$host$request_uri;\n"
            "    }"
        )
        tech_ssl = (
            "    server {\n"
            "        listen 443 ssl;\n"
            "        server_name technitium.vcf.lab dns.vcf.lab;\n"
            "\n"
            "        ssl_certificate     /root/nginx-certs/technitium.crt;\n"
            "        ssl_certificate_key /root/nginx-certs/technitium.key;\n"
            "\n"
            "        location / {\n"
            "            proxy_pass http://localhost:5380;\n"
            "            proxy_set_header Host $host;\n"
            "            proxy_set_header X-Real-IP $remote_addr;\n"
            "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "            proxy_set_header X-Forwarded-Proto https;\n"
            "        }\n"
            "    }"
        )
        conf = conf.replace(tech_http_only, tech_redirect + "\n\n" + tech_ssl)
        modified = True
        print("  [UPD] Technitium: HTTP->HTTPS redirect + SSL proxy")
    elif _find_full_server_block(conf, "technitium.vcf.lab", "443"):
        if "dns.vcf.lab" not in _find_full_server_block(conf, "technitium.vcf.lab", "443"):
            conf = conf.replace(
                "server_name technitium.vcf.lab;",
                "server_name technitium.vcf.lab dns.vcf.lab;",
            )
            modified = True
        print("  [SKIP] Technitium SSL already configured")
    else:
        print("  [WARN] Could not locate Technitium HTTP block")

    # --- Authentik: ensure HTTP proxy (no redirect) + HTTPS proxy, auth.vcf.lab preferred ---
    auth_http_block = _find_full_server_block(conf, "authentik.vcf.lab", "80")
    if not auth_http_block:
        auth_http_block = _find_full_server_block(conf, "auth.vcf.lab", "80")

    needs_auth_http_update = False
    if auth_http_block:
        if "return 301" in auth_http_block:
            needs_auth_http_update = True
        elif "auth.vcf.lab" not in auth_http_block:
            needs_auth_http_update = True

    if needs_auth_http_update and auth_http_block:
        auth_http = (
            "    server {\n"
            "        listen 80;\n"
            "        server_name auth.vcf.lab authentik.vcf.lab;\n"
            "\n"
            "        location /icons/ {\n"
            "            alias /var/www/icons/;\n"
            "            expires 30d;\n"
            '            add_header Cache-Control "public, immutable";\n'
            "        }\n"
            "\n"
            "        location / {\n"
            "            proxy_pass http://localhost:31080;\n"
            "            proxy_set_header Host $host;\n"
            "            proxy_set_header X-Real-IP $remote_addr;\n"
            "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "            proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        }\n"
            "    }"
        )
        conf = conf.replace(auth_http_block, auth_http)
        modified = True
        print("  [UPD] Authentik HTTP: proxy without redirect")

    if not _find_full_server_block(conf, "auth.vcf.lab", "443"):
        auth_ssl = (
            "\n    server {\n"
            "        listen 443 ssl;\n"
            "        server_name auth.vcf.lab authentik.vcf.lab;\n"
            "\n"
            "        ssl_certificate     /root/nginx-certs/authentik.crt;\n"
            "        ssl_certificate_key /root/nginx-certs/authentik.key;\n"
            "\n"
            "        location /icons/ {\n"
            "            alias /var/www/icons/;\n"
            "            expires 30d;\n"
            '            add_header Cache-Control "public, immutable";\n'
            "        }\n"
            "\n"
            "        location / {\n"
            "            proxy_pass http://localhost:31080;\n"
            "            proxy_set_header Host $host;\n"
            "            proxy_set_header X-Real-IP $remote_addr;\n"
            "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "            proxy_set_header X-Forwarded-Proto https;\n"
            "            proxy_set_header X-Forwarded-Host $host;\n"
            "            proxy_http_version 1.1;\n"
            '            proxy_set_header Upgrade $http_upgrade;\n'
            '            proxy_set_header Connection "upgrade";\n'
            "        }\n"
            "    }\n"
        )
        auth_http_end = _find_full_server_block(conf, "auth.vcf.lab", "80")
        if auth_http_end:
            idx = conf.find(auth_http_end) + len(auth_http_end)
            conf = conf[:idx] + auth_ssl + conf[idx:]
        else:
            conf = _insert_before_last_closing_brace(conf, auth_ssl)
        modified = True
        print("  [ADD] Authentik HTTPS block (auth.vcf.lab preferred)")
    else:
        print("  [SKIP] Authentik SSL already configured")

    # --- GitLab HTTP redirect ---
    if not _find_full_server_block(conf, "gitlab.vcf.lab", "80"):
        gitlab_80 = (
            "\n    server {\n"
            "        listen 80;\n"
            "        server_name gitlab.vcf.lab;\n"
            "        return 301 https://$host$request_uri;\n"
            "    }\n"
        )
        conf = _insert_before_last_closing_brace(conf, gitlab_80)
        modified = True
        print("  [ADD] HTTP redirect block for gitlab.vcf.lab")

    # --- GitLab Registry HTTP redirect ---
    if not _find_full_server_block(conf, "gitlab-registry.vcf.lab", "80"):
        registry_80 = (
            "\n    server {\n"
            "        listen 80;\n"
            "        server_name gitlab-registry.vcf.lab;\n"
            "        return 301 https://$host$request_uri;\n"
            "    }\n"
        )
        conf = _insert_before_last_closing_brace(conf, registry_80)
        modified = True
        print("  [ADD] HTTP redirect block for gitlab-registry.vcf.lab")

    # --- ca.vcf.lab ---
    if "server_name ca.vcf.lab" not in conf:
        ca_blocks = (
            "\n    server {\n"
            "        listen 80;\n"
            "        server_name ca.vcf.lab;\n"
            "        return 301 https://$host$request_uri;\n"
            "    }\n"
            "\n"
            "    server {\n"
            "        listen 443 ssl;\n"
            "        server_name ca.vcf.lab;\n"
            "\n"
            "        ssl_certificate     /root/certsrv-proxy/ca.crt;\n"
            "        ssl_certificate_key /root/certsrv-proxy/ca.key;\n"
            "\n"
            "        location / {\n"
            "            proxy_pass http://localhost:8900;\n"
            "            proxy_set_header Host $host;\n"
            "            proxy_set_header X-Real-IP $remote_addr;\n"
            "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "            proxy_set_header X-Forwarded-Proto https;\n"
            "        }\n"
            "    }\n"
        )
        conf = _insert_before_last_closing_brace(conf, ca_blocks)
        modified = True
        print("  [ADD] HTTP + HTTPS blocks for ca.vcf.lab")

    if modified:
        _write_remote_file("/etc/nginx/nginx.conf", conf)
        r = ssh("nginx -t 2>&1", check=False)
        if r.returncode == 0:
            ssh("nginx -s reload", check=False)
            print("  [OK] nginx.conf updated and reloaded")
        else:
            print(f"  [ERROR] nginx config test failed: {r.stdout} {r.stderr}")
    else:
        print("  [SKIP] nginx.conf already has all required blocks")

    print("  Phase 5 complete.")


def _find_full_server_block(conf, server_name, port):
    """Find the full 'server { ... }' block matching server_name and listen port."""
    import re
    idx = 0
    while idx < len(conf):
        start = conf.find("server {", idx)
        if start == -1:
            break
        depth = 0
        end = start
        for i in range(start, len(conf)):
            if conf[i] == "{":
                depth += 1
            elif conf[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        block = conf[start:end]
        if server_name in block and f"listen {port}" in block:
            ws_start = conf.rfind("\n", 0, start)
            if ws_start == -1:
                ws_start = start
            else:
                ws_start += 1
            return conf[ws_start:end]
        idx = end
    return ""


def _insert_before_last_closing_brace(conf, new_block):
    last_brace = conf.rfind("}")
    if last_brace == -1:
        return conf + new_block
    return conf[:last_brace] + new_block + "\n" + conf[last_brace:]


# ---------------------------------------------------------------------------
# Phase 6: Deploy Certsrv Proxy
# ---------------------------------------------------------------------------

def phase_6_certsrv():
    print("\n" + "=" * 60)
    print("Phase 6: Deploy Certsrv Proxy")
    print("=" * 60)

    r = ssh("kubectl get pods -l app=certsrv-proxy -n default -o jsonpath='{.items[0].status.phase}' 2>/dev/null", check=False)
    if r.stdout.strip().strip("'") == "Running":
        print("  [SKIP] certsrv-proxy pod already running")
        return

    if not os.path.isfile(CERTSRV_PROXY_SRC):
        print(f"  [ERROR] certsrv_proxy.py not found at {CERTSRV_PROXY_SRC}")
        return

    print("  Creating /root/certsrv-proxy/ directory on holorouter...")
    ssh("mkdir -p /root/certsrv-proxy", check=False)

    print("  Copying certsrv_proxy.py to holorouter...")
    scp_to_router(CERTSRV_PROXY_SRC, "/root/certsrv-proxy/certsrv_proxy.py")

    ssh("cp /root/creds.txt /root/certsrv-proxy/creds.txt 2>/dev/null", check=False)

    print("  Copying Dockerfile to holorouter...")
    if os.path.isfile(CERTSRV_DOCKERFILE_SRC):
        scp_to_router(CERTSRV_DOCKERFILE_SRC, "/root/certsrv-proxy/Dockerfile")
    else:
        dockerfile = (
            "FROM python:3.11-slim\n"
            "RUN pip install --no-cache-dir cryptography requests urllib3\n"
            "COPY certsrv_proxy.py /app/certsrv_proxy.py\n"
            "WORKDIR /app\n"
            'ENTRYPOINT ["python3", "/app/certsrv_proxy.py"]\n'
        )
        _write_remote_file("/root/certsrv-proxy/Dockerfile", dockerfile)

    print("  Starting docker daemon for image build...")
    ssh("systemctl start docker", timeout=30, check=False)
    time.sleep(3)

    print("  Building certsrv-proxy container image (this may take a minute)...")
    r = ssh(
        "cd /root/certsrv-proxy && "
        "docker build -t certsrv-proxy:latest -f Dockerfile . && "
        "docker save certsrv-proxy:latest -o /root/certsrv-proxy/certsrv-proxy.tar",
        timeout=300, check=False
    )
    if r.returncode != 0:
        print(f"  [WARN] Docker build may have issues: {r.stderr.strip()[-300:]}")
    else:
        print("  [OK] Docker image built and saved")

    print("  Stopping docker daemon...")
    ssh("systemctl stop docker 2>/dev/null; systemctl stop docker.socket 2>/dev/null", timeout=15, check=False)

    print("  Importing image into containerd...")
    ssh("ctr -n k8s.io images import /root/certsrv-proxy/certsrv-proxy.tar", timeout=60, check=False)

    print("  Deploying certsrv-proxy DaemonSet...")
    daemonset_yaml = r"""
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: certsrv-proxy
  namespace: default
  labels:
    app: certsrv-proxy
spec:
  selector:
    matchLabels:
      app: certsrv-proxy
  template:
    metadata:
      labels:
        app: certsrv-proxy
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
      - name: certsrv-proxy
        image: certsrv-proxy:latest
        imagePullPolicy: IfNotPresent
        command:
        - python3
        - /app/certsrv_proxy.py
        - --port
        - "8900"
        - --vault-url
        - http://127.0.0.1:32000
        - --creds-file
        - /app/creds.txt
        ports:
        - containerPort: 8900
          hostPort: 8900
          protocol: TCP
        volumeMounts:
        - name: creds-file
          mountPath: /app/creds.txt
          readOnly: true
      volumes:
      - name: creds-file
        hostPath:
          path: /root/creds.txt
          type: File
"""
    _write_remote_file("/tmp/certsrv-daemonset.yaml", daemonset_yaml)
    ssh("kubectl apply -f /tmp/certsrv-daemonset.yaml", check=False)

    svc_yaml = r"""
apiVersion: v1
kind: Service
metadata:
  name: certsrv-proxy
  namespace: default
spec:
  selector:
    app: certsrv-proxy
  ports:
  - port: 8900
    targetPort: 8900
    protocol: TCP
  type: ClusterIP
"""
    _write_remote_file("/tmp/certsrv-svc.yaml", svc_yaml)
    ssh("kubectl apply -f /tmp/certsrv-svc.yaml", check=False)

    wait_for("certsrv-proxy pod", lambda: _certsrv_running(), timeout=120, interval=5)

    print("  Phase 6 complete.")


def _certsrv_running():
    r = ssh("kubectl get pods -l app=certsrv-proxy -n default -o jsonpath='{.items[0].status.phase}'", check=False)
    return r.stdout.strip().strip("'") == "Running"


# ---------------------------------------------------------------------------
# Phase 7: Update shutdown.sh
# ---------------------------------------------------------------------------

def phase_7_shutdown():
    print("\n" + "=" * 60)
    print("Phase 7: Update shutdown.sh")
    print("=" * 60)

    r = ssh("cat /root/shutdown.sh", check=False)
    if r.returncode != 0:
        print("  [ERROR] Could not read shutdown.sh")
        return

    content = r.stdout

    if "stop_nginx" in content:
        print("  [SKIP] shutdown.sh already has stop_nginx")
        return

    nginx_func = '''
stop_nginx() {
    if systemctl is-active nginx &>/dev/null; then
        log "Stopping nginx reverse proxy..."
        systemctl stop nginx 2>/dev/null
        log "  nginx stopped"
    else
        log "Nginx is not running"
    fi
}
'''

    content = content.replace(
        "# Step 6: Stop squid\nstop_squid",
        "# Step 6: Stop nginx\nstop_nginx\n\n    # Step 6b: Stop squid\n    stop_squid"
    )

    if "stop_nginx" not in content:
        content = content.replace(
            "stop_squid",
            "stop_nginx\n\n    stop_squid",
            1
        )

    if "stop_nginx()" not in content:
        content = content.replace(
            "stop_squid() {",
            nginx_func + "\nstop_squid() {"
        )

    _write_remote_file("/root/shutdown.sh", content)
    ssh("chmod +x /root/shutdown.sh", check=False)
    print("  [OK] shutdown.sh updated with stop_nginx")
    print("  Phase 7 complete.")


# ---------------------------------------------------------------------------
# Phase 8: Fix Authentik Media Storage
# ---------------------------------------------------------------------------

def phase_8_authentik_storage():
    print("\n" + "=" * 60)
    print("Phase 8: Setup Icon Serving via nginx")
    print("=" * 60)

    extract_images()

    r = ssh("ls /var/www/icons/*.svg 2>/dev/null | wc -l", check=False)
    icon_count = int(r.stdout.strip()) if r.returncode == 0 else 0

    if icon_count > 50:
        print(f"  [SKIP] /var/www/icons already has {icon_count} icons")
    else:
        print("  Creating /var/www/icons on holorouter...")
        ssh("mkdir -p /var/www/icons && chmod 755 /var/www /var/www/icons", check=False)

        print("  Copying images to holorouter...")
        ssh("mkdir -p /tmp/authentik-icons", check=False)
        pw = get_password()
        subprocess.run(
            f'sshpass -p "{pw}" scp -o StrictHostKeyChecking=accept-new '
            f'-o PubkeyAuthentication=no {IMAGES_DIR}/* root@{ROUTER_HOST}:/tmp/authentik-icons/',
            shell=True, check=False, capture_output=True, timeout=120
        )

        ssh("cp /tmp/authentik-icons/* /var/www/icons/ && chmod 644 /var/www/icons/*", check=False)
        r = ssh("ls /var/www/icons/ | wc -l", check=False)
        print(f"  [OK] {r.stdout.strip()} icons staged in /var/www/icons/")

    r = ssh("curl -sk -o /dev/null -w '%{http_code}' https://localhost/icons/gitlab.svg -H 'Host: auth.vcf.lab'", check=False)
    code = r.stdout.strip().strip("'")
    if code == "200":
        print(f"  [OK] Icon serving verified via HTTPS (HTTP {code})")
    else:
        print(f"  [WARN] Icon serving returned HTTP {code} - nginx /icons/ may need Phase 5 first")

    print("  Phase 8 complete.")


def _authentik_ready():
    try:
        r = authentik_api("core/users/?username=akadmin")
        return "results" in r
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Phase 9: Authentik Users and Groups
# ---------------------------------------------------------------------------

def phase_9_authentik_users():
    print("\n" + "=" * 60)
    print("Phase 9: Authentik Users and Groups")
    print("=" * 60)

    pw = get_password()

    if not _authentik_ready():
        print("  Waiting for Authentik API...")
        wait_for("Authentik API", _authentik_ready, timeout=120, interval=10)

    users = [
        ("dev-user", "dev-user@vcf.lab", "Dev User"),
        ("dev-admin", "dev-admin@vcf.lab", "Dev Admin"),
        ("dev-readonly", "dev-readonly@vcf.lab", "Dev ReadOnly"),
        ("approver", "approver@vcf.lab", "Approver"),
        ("requestor", "requestor@vcf.lab", "Requestor"),
        ("prod-user", "prod-user@vcf.lab", "Prod User"),
        ("prod-admin", "prod-admin@vcf.lab", "Prod Admin"),
        ("prod-readonly", "prod-readonly@vcf.lab", "Prod ReadOnly"),
        ("vcadmin", "vcadmin@vcf.lab", "VC Admin"),
        ("demouser", "demouser@vcf.lab", "Demo User"),
        ("backup", "backup@vcf.lab", "Backup"),
        ("audit", "audit@vcf.lab", "Audit"),
        ("configadmin", "configadmin@vcf.lab", "Config Admin"),
    ]

    groups = {
        "dev-users": ["dev-user"],
        "dev-admins": ["dev-admin"],
        "dev-readonly": ["dev-readonly"],
        "approvers": ["approver"],
        "prod-users": ["prod-user"],
        "prod-admins": ["prod-admin"],
        "prod-readonly": ["prod-readonly"],
    }

    print("  --- Creating Users ---")
    user_pks = {}
    for username, email, name in users:
        pk = _create_authentik_user(username, email, name, pw)
        if pk:
            user_pks[username] = pk

    print(f"  Created/found {len(user_pks)} users")

    print("\n  --- Creating Groups ---")
    group_pks = {}
    for group_name in groups:
        pk = _create_authentik_group(group_name)
        if pk:
            group_pks[group_name] = pk

    print(f"  Created/found {len(group_pks)} groups")

    print("\n  --- Adding Users to Groups ---")
    for group_name, members in groups.items():
        gpk = group_pks.get(group_name)
        if not gpk:
            continue
        for member in members:
            upk = user_pks.get(member)
            if upk:
                authentik_api(f"core/groups/{gpk}/add_user/", method="POST", data={"pk": upk})
                print(f"    {member} -> {group_name}")

    print("\n  --- Removing Password Expiry Policies ---")
    policies = authentik_api("policies/password_expiry/")
    if "results" in policies:
        for p in policies["results"]:
            ppk = p.get("pk")
            if ppk:
                authentik_api(f"policies/password_expiry/{ppk}/", method="DELETE")
        print(f"  Removed {len(policies['results'])} password expiry policies")
    else:
        print("  No password expiry policies found")

    print("  Phase 9 complete.")


def _create_authentik_user(username, email, name, password):
    existing = authentik_api(f"core/users/?username={username}")
    results = existing.get("results", [])
    if results:
        pk = results[0]["pk"]
        print(f"    [EXISTS] {username} (pk={pk})")
    else:
        resp = authentik_api("core/users/", method="POST", data={
            "username": username,
            "email": email,
            "name": name,
            "is_active": True,
            "attributes": {"goauthentik.io/user/password-change-date": "2099-12-31T00:00:00Z"},
        })
        pk = resp.get("pk")
        if not pk:
            print(f"    [ERROR] Failed to create {username}: {resp}")
            return None
        print(f"    [OK] {username} (pk={pk})")

    authentik_api(f"core/users/{pk}/set_password/", method="POST", data={"password": password})
    authentik_api(f"core/users/{pk}/", method="PATCH", data={
        "attributes": {"goauthentik.io/user/password-change-date": "2099-12-31T00:00:00Z"},
    })
    return pk


def _create_authentik_group(name):
    existing = authentik_api(f"core/groups/?name={name}")
    results = existing.get("results", [])
    if results:
        pk = results[0]["pk"]
        print(f"    [EXISTS] {name} (pk={pk})")
        return pk

    resp = authentik_api("core/groups/", method="POST", data={"name": name})
    pk = resp.get("pk")
    if pk:
        print(f"    [OK] {name} (pk={pk})")
    else:
        print(f"    [ERROR] Failed to create group {name}: {resp}")
    return pk


# ---------------------------------------------------------------------------
# Phase 10: Upload Icons + Create Placeholder App Tiles
# ---------------------------------------------------------------------------

SLUG_TO_NAME = {
    "gitlab": "GitLab", "appflowy": "AppFlowy", "artifactory": "JFrog Artifactory",
    "argo-cd": "Argo CD", "baserow": "Baserow", "jenkins": "Jenkins",
    "git": "Git", "gitea": "Gitea", "grafana": "Grafana", "forgejo": "Forgejo",
    "1password": "1Password", "bentopdf": "BentoPDF", "zulip": "Zulip",
    "xcp-ng": "XCP-ng", "windmill": "Windmill", "vscode": "VS Code",
    "vaultwarden": "Vaultwarden", "uptime-kuma": "Uptime Kuma",
    "unifi-controller": "UniFi Controller", "unbound": "Unbound",
    "truenas-scale": "TrueNAS Scale", "teleport": "Teleport",
    "standard-notes": "Standard Notes", "sftpgo": "SFTPGo",
    "selfhosted": "Self-Hosted", "semaphore": "Semaphore", "seafile": "Seafile",
    "rustdesk": "RustDesk", "roundcube": "Roundcube", "redis": "Redis",
    "rabbitmq": "RabbitMQ", "proxmox": "Proxmox", "poste": "Poste",
    "portainer": "Portainer", "pocket-id": "Pocket ID", "pocketbase": "PocketBase",
    "pi-hole": "Pi-hole", "phpmyadmin": "phpMyAdmin", "pfsense": "pfSense",
    "pgadmin": "pgAdmin", "passbolt": "Passbolt", "paperless-ng": "Paperless-ngx",
    "pangolin": "Pangolin", "owncloud": "ownCloud", "openstack": "OpenStack",
    "open-webui": "Open WebUI", "ntfy": "ntfy", "notesnook": "Notesnook",
    "node-red": "Node-RED", "nocodb": "NocoDB",
    "nginx-proxy-manager": "Nginx Proxy Manager", "nextcloud-white": "Nextcloud",
    "netbox": "NetBox", "netbird": "NetBird", "n8n": "n8n",
    "mail-in-a-box": "Mail-in-a-Box", "mailcow": "Mailcow",
    "lubelogger": "LubeLogger", "linkwarden": "Linkwarden", "kestra": "Kestra",
    "keycloak": "Keycloak", "karakeep": "Karakeep", "kasm": "Kasm",
    "joplin": "Joplin", "jfrog": "JFrog", "itop": "iTop",
    "influxdb": "InfluxDB", "immich": "Immich", "homebox": "Homebox",
    "homarr": "Homarr", "hedgedoc": "HedgeDoc", "heimdall": "Heimdall",
    "hashicorp-boundary": "HashiCorp Boundary", "guacamole": "Guacamole",
    "grist": "Grist", "gitbook": "GitBook", "freeipa": "FreeIPA",
    "draw-io": "draw.io", "dokploy": "Dokploy", "docmost": "Docmost",
    "dockhand": "Dockhand", "dockge": "Dockge", "ddns-updater": "DDNS Updater",
    "ddclient": "DDClient", "couchdb": "CouchDB", "coolify": "Coolify",
    "comfy-ui": "ComfyUI", "code": "Code", "cloudflare": "Cloudflare",
    "budibase": "Budibase", "bookstack": "BookStack", "bitwarden": "Bitwarden",
    "traefik-proxy": "Traefik Proxy", "traefik": "Traefik",
    "traefik-logo": "Traefik Logo", "vault": "HashiCorp Vault",
    "vault-light": "HashiCorp Vault", "technitium": "Technitium DNS",
}

PREFERRED_ICON = {
    "vaultwarden": "vaultwarden-light.svg", "proxmox": "proxmox-light.svg",
    "pocket-id": "pocket-id-light.svg", "pfsense": "pfsense-light.svg",
    "open-webui": "open-webui-light.svg", "notesnook": "notesnook-light.svg",
    "heimdall": "heimdall-light.svg", "guacamole": "guacamole-light.svg",
    "selfhosted": "selfhosted-light.png", "vault": "vault-light.svg",
}

SKIP_SLUGS = {
    "1password-dark", "vault-light", "vaultwarden-light", "proxmox-light",
    "pocket-id-light", "pocketbase-dark", "pfsense-light", "standard-notes-light",
    "notesnook-light", "netbox-dark", "heimdall-light", "guacamole-light",
    "dokploy-dark", "karakeep-dark", "open-webui-light", "semaphore-dark",
    "portainer-dark", "selfhosted-light", "traefik-logo",
}


def phase_10_icons():
    print("\n" + "=" * 60)
    print("Phase 10: Create App Tiles with Icons")
    print("=" * 60)

    extract_images()

    if not os.path.isdir(IMAGES_DIR):
        print(f"  [ERROR] Images directory not found: {IMAGES_DIR}")
        return

    if not _authentik_ready():
        wait_for("Authentik API", _authentik_ready, timeout=120, interval=10)

    image_files = sorted(os.listdir(IMAGES_DIR))
    print(f"  Found {len(image_files)} image files")

    print("\n  --- Creating/Updating Placeholder Application Tiles ---")
    created = 0
    updated = 0

    for filename in image_files:
        slug = os.path.splitext(filename)[0]

        if slug in SKIP_SLUGS:
            continue

        name = SLUG_TO_NAME.get(slug)
        if not name:
            name = slug.replace("-", " ").replace("_", " ").title()

        icon_file = PREFERRED_ICON.get(slug, filename)
        icon_url = f"https://auth.vcf.lab/icons/{icon_file}"

        existing = authentik_api(f"core/applications/{slug}/")
        if existing.get("slug") == slug:
            if existing.get("meta_icon") != icon_url:
                authentik_api(f"core/applications/{slug}/", method="PATCH", data={"meta_icon": icon_url})
            updated += 1
            continue

        authentik_api("core/applications/", method="POST", data={
            "name": name,
            "slug": slug,
            "meta_launch_url": "",
            "open_in_new_tab": True,
            "policy_engine_mode": "any",
            "meta_icon": icon_url,
        })
        created += 1

    print(f"  App tiles: {created} created, {updated} updated")
    print("  Phase 10 complete.")


# ---------------------------------------------------------------------------
# Phase 11: Verification
# ---------------------------------------------------------------------------

def phase_11_verify():
    print("\n" + "=" * 60)
    print("Phase 11: Verification")
    print("=" * 60)

    pw = get_password()
    results = []

    print("\n  --- Vault ---")
    status = vault_api("sys/seal-status")
    sealed = status.get("sealed", True)
    storage = status.get("storage_type", "unknown")
    results.append(("Vault unsealed", not sealed))
    results.append(("Vault storage=file", storage == "file"))
    print(f"  Sealed: {sealed}, Storage: {storage}")

    token_check = vault_api("auth/token/lookup-self", token=pw)
    token_ok = bool(token_check.get("data"))
    results.append(("Vault token=creds.txt", token_ok))
    print(f"  Token matches creds.txt: {token_ok}")

    pki = vault_api("pki/cert/ca", token=pw)
    pki_ok = bool(pki.get("data", {}).get("certificate"))
    results.append(("Vault PKI CA exists", pki_ok))
    print(f"  PKI CA: {'OK' if pki_ok else 'MISSING'}")

    role = vault_api("pki/roles/holodeck", token=pw)
    role_ok = bool(role.get("data"))
    results.append(("Vault holodeck role", role_ok))
    print(f"  Holodeck role: {'OK' if role_ok else 'MISSING'}")

    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: vault.vcf.lab' https://localhost/", check=False)
    vault_ssl_code = r.stdout.strip().strip("'")
    vault_ssl_ok = vault_ssl_code in ("200", "307")
    results.append(("Vault HTTPS", vault_ssl_ok))
    print(f"  HTTPS status: {vault_ssl_code}")

    print("\n  --- CA Trust (Manager & Console) ---")
    for host, label in [("localhost", "Manager"), ("console", "Console")]:
        r = _ssh_host(host, "root",
                      f"test -f {CA_TRUST_DIR}/{CA_CERT_NAME} && echo yes || echo no")
        installed = r.stdout.strip() == "yes"
        results.append((f"{label} VM CA installed", installed))
        print(f"  {label} CA cert: {'installed' if installed else 'MISSING'}")

        r = _ssh_host(host, "root",
                      "curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://vault.vcf.lab/ 2>/dev/null")
        code = r.stdout.strip().strip("'")
        trusted = code in ("200", "307")
        results.append((f"{label} VM HTTPS trusted", trusted))
        print(f"  {label} curl HTTPS (no -k): HTTP {code} ({'trusted' if trusted else 'NOT trusted'})")

    print("\n  --- GitLab ---")
    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://localhost:30443/", check=False)
    gl_code = r.stdout.strip().strip("'")
    gl_ok = gl_code in ("200", "302")
    results.append(("GitLab HTTPS responds", gl_ok))
    print(f"  HTTPS status: {gl_code}")

    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: gitlab.vcf.lab' http://localhost/", check=False)
    redir_code = r.stdout.strip().strip("'")
    redir_ok = redir_code == "301"
    results.append(("GitLab HTTP->HTTPS redirect", redir_ok))
    print(f"  HTTP redirect: {redir_code}")

    r = ssh(
        f"curl -sk -X POST 'https://localhost:30443/oauth/token' "
        f"-F grant_type=password -F username=root -F 'password={pw}' 2>/dev/null",
        check=False
    )
    try:
        gl_token = json.loads(r.stdout).get("access_token")
    except Exception:
        gl_token = None
    results.append(("GitLab root login", bool(gl_token)))
    print(f"  Root login with creds.txt: {'OK' if gl_token else 'FAILED'}")

    r = ssh("openssl s_client -connect localhost:30443 -servername gitlab.vcf.lab </dev/null 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null", check=False)
    issuer = r.stdout.strip()
    ca_signed = "vcf.lab Root Authority" in issuer
    results.append(("GitLab cert CA-signed", ca_signed))
    print(f"  Cert issuer: {issuer}")

    print("\n  --- Technitium SSL ---")
    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: technitium.vcf.lab' https://localhost/", check=False)
    tech_code = r.stdout.strip().strip("'")
    tech_ok = tech_code == "200"
    results.append(("Technitium HTTPS", tech_ok))
    print(f"  HTTPS status: {tech_code}")

    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: dns.vcf.lab' https://localhost/", check=False)
    dns_ssl_code = r.stdout.strip().strip("'")
    dns_ssl_ok = dns_ssl_code == "200"
    results.append(("dns.vcf.lab HTTPS", dns_ssl_ok))
    print(f"  dns.vcf.lab HTTPS: {dns_ssl_code}")

    r = ssh("openssl s_client -connect localhost:443 -servername technitium.vcf.lab </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName 2>/dev/null", check=False)
    tech_sans = r.stdout.strip()
    tech_san_ok = "dns.vcf.lab" in tech_sans and "192.168.0.2" in tech_sans
    results.append(("Technitium cert SANs", tech_san_ok))
    print(f"  Cert SANs: {tech_sans}")

    print("\n  --- Certsrv Proxy ---")
    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8900/certsrv/", check=False)
    certsrv_code = r.stdout.strip().strip("'")
    certsrv_ok = certsrv_code in ("200", "401")
    results.append(("Certsrv proxy responds", certsrv_ok))
    print(f"  HTTP status: {certsrv_code}")

    print("\n  --- DNS ---")
    r = ssh("nslookup dns.vcf.lab 127.0.0.1 2>/dev/null | grep -i 'canonical name\\|cname' || echo 'no cname'", check=False)
    dns_cname = "technitium" in r.stdout.lower() or "canonical" in r.stdout.lower()
    results.append(("dns.vcf.lab CNAME", dns_cname))
    print(f"  dns.vcf.lab: {r.stdout.strip()}")

    r = ssh("nslookup auth.vcf.lab 127.0.0.1 2>/dev/null | grep -i 'canonical name\\|cname' || echo 'no cname'", check=False)
    auth_cname = "authentik" in r.stdout.lower() or "canonical" in r.stdout.lower()
    results.append(("auth.vcf.lab CNAME", auth_cname))
    print(f"  auth.vcf.lab: {r.stdout.strip()}")

    print("\n  --- Authentik SSL ---")
    r = ssh("curl -sk -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: auth.vcf.lab' https://localhost/", check=False)
    auth_ssl_code = r.stdout.strip().strip("'")
    auth_ssl_ok = auth_ssl_code in ("200", "302")
    results.append(("auth.vcf.lab HTTPS", auth_ssl_ok))
    print(f"  auth.vcf.lab HTTPS: {auth_ssl_code}")

    r = ssh("curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: auth.vcf.lab' http://localhost/", check=False)
    auth_http_code = r.stdout.strip().strip("'")
    auth_http_ok = auth_http_code in ("200", "302")
    results.append(("auth.vcf.lab HTTP (no redirect)", auth_http_ok))
    print(f"  auth.vcf.lab HTTP: {auth_http_code}")

    print("\n  --- Authentik ---")
    user_check = authentik_api("core/users/?username=dev-admin")
    user_count = user_check.get("pagination", {}).get("count", 0)
    results.append(("Authentik dev-admin user", user_count > 0))
    print(f"  dev-admin user: {'found' if user_count > 0 else 'NOT FOUND'}")

    apps = authentik_api("core/applications/?page_size=200")
    app_count = apps.get("pagination", {}).get("count", 0)
    results.append(("Authentik app tiles", app_count >= 50))
    print(f"  Application tiles: {app_count}")

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    all_ok = True
    for desc, ok in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  [{status}] {desc}")

    print("\n  Endpoints:")
    print("    http://vault.vcf.lab           -> Vault UI (HTTP)")
    print("    https://vault.vcf.lab          -> Vault UI (HTTPS)")
    print("    https://technitium.vcf.lab    -> Technitium DNS (SSL)")
    print("    https://dns.vcf.lab           -> Technitium DNS (CNAME alias)")
    print("    http://auth.vcf.lab            -> Authentik IdP (HTTP)")
    print("    https://auth.vcf.lab          -> Authentik IdP (HTTPS, preferred)")
    print("    http://authentik.vcf.lab      -> Authentik IdP (HTTP)")
    print("    https://authentik.vcf.lab     -> Authentik IdP (HTTPS)")
    print("    https://gitlab.vcf.lab        -> GitLab EE")
    print("    https://ca.vcf.lab/certsrv/   -> MSADCS Proxy (Vault PKI)")
    print("\n  All passwords: contents of /home/holuser/creds.txt")
    print("  GitLab user: root")
    print("  Authentik admin: akadmin")

    if all_ok:
        print("\n  All checks PASSED.")
    else:
        print("\n  Some checks FAILED - review output above.")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Holorouter Normalization Script")
    print("=" * 60)

    if not os.path.isfile(CREDS_FILE):
        print(f"ERROR: {CREDS_FILE} not found")
        sys.exit(1)

    r = ssh("hostname", check=False)
    if r.returncode != 0:
        print("ERROR: Cannot SSH to holorouter")
        sys.exit(1)
    print(f"  Connected to: {r.stdout.strip()}")

    phase_1_vault()
    phase_2_dns()
    phase_3_certs()
    phase_3b_distribute_ca()
    phase_4_gitlab()
    phase_5_nginx()
    phase_6_certsrv()
    phase_7_shutdown()
    phase_8_authentik_storage()
    phase_9_authentik_users()
    phase_10_icons()
    phase_11_verify()

    if os.path.isdir(IMAGES_DIR) and IMAGES_DIR.startswith("/tmp/"):
        shutil.rmtree(IMAGES_DIR, ignore_errors=True)
        print(f"\n  Cleaned up {IMAGES_DIR}")


if __name__ == "__main__":
    main()
