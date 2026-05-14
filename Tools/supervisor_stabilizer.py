#!/usr/bin/env python3
"""
supervisor_stabilizer.py
Version 1.5 - 2026-05-13
Author - Kevin Tebear, Burke Azbill and HOL Core Team

Unified cert-rotation and control-plane remediation for VCF / vSphere Supervisor environments.

v1.5 Changes:
- Phase 0 vCenter proxy configuration now also calls the vCenter REST API
  (PUT /api/appliance/networking/noproxy) as the SSO user to update the
  no-proxy exclusion list visible in the VAMI UI at :5480.  The OS-level
  /etc/environment and /etc/sysconfig/proxy writes alone did not affect the
  VAMI appliance management layer.  Added _vc_rest_set_noproxy() and
  _vc_rest_delete_session() helpers using stdlib urllib.request + ssl.

v1.4 Changes:
- Proxy constants (HTTP_PROXY, HTTPS_PROXY, NO_PROXY_PARTS) now imported from
  lsfunctions.LAB_PROXY_URL / LAB_NO_PROXY_PARTS (single source of truth).
  Hardcoded fallback values retained for standalone use outside the repo.
- Added Phase 0: vCenter proxy configuration — SSHes to each vCenter as root
  and writes /etc/environment (6 proxy env-vars) and /etc/sysconfig/proxy
  (Photon OS native proxy file) so vCenter knows which subnets and domains
  bypass the proxy. Adds --skip-vcenter-proxy flag to skip this phase.

v1.3 Changes:
- Fixed proxy-cert regeneration commands to be silent no-ops when
  supervisor-management-proxy resources don't exist (newer Supervisor builds):
  cert-manager secret delete now guards on non-empty grep result; kube-system
  secret delete uses --ignore-not-found; rollout restart/status are gated on
  deployment existence.  Eliminates "no name was specified" and NotFound noise.

v1.2 Changes:
- Fixed TCL command-substitution bug: build_expect_script now escapes [ and ]
  (in addition to " and $) so shell test-brackets ([ ! -z "$VAR" ]) no longer
  trigger "invalid command name '!'" TCL errors and abort the recovery phase.
- Fixed proxy containerd drop-in: replaced the here-doc (cat >> file << 'EOF')
  in proxy_commands with a base64-decode single command.  Here-docs put the
  shell into continuation mode (> prompt) which the per-command expect loop
  can never match, causing a timeout that left containerd without proxy
  settings even though /etc/environment was written correctly.
- Fixed workload recovery for-loop: collapsed the multiline for/if block into
  a single compound command for the same reason as above.
- Content library sync: treat connection_to_vcsp_server_failed (HTTP 400) as
  a non-fatal warning (same as HTTP 500).  The trust-store cert is already
  written; vCenter retries sync on its own schedule once the upstream is up.

v1.1 Changes:
- Updated to dynamically read expected vCenters from /tmp/config.ini [RESOURCES] vCenters
  to avoid noisy logs from attempting to connect to statically defined vCenters that
  may not exist in the current lab topology.

Combines functionality from check_fix_wcp.sh and fix_supervisor_certs.py into a single,
idempotent stabilization script that handles:

  1. Content Library trust refresh
       - Fetches the live upstream cert
       - Adds it to each vCenter's Content Library trust store
       - Updates the SHA-1 SSL thumbprint stored on every matching SUBSCRIBED
         library (this is the step the legacy shell script was MISSING -
         `govc library.update <id>` without `-thumbprint` is a no-op for the
         pinned thumbprint, which is why deployments still halt)
       - Triggers a sync and re-reads the thumbprint to verify the update
         actually took effect

  2. Supervisor management-proxy cert refresh
       - SSHes through vCenter to the Supervisor control plane
       - Deletes the expired cert-manager secrets and rolls the
         supervisor-management-proxy deployment so cert-manager regenerates
         fresh mTLS material

Both phases are idempotent. Run --dry-run first to preview.

Requirements on the host running this script:
  - python3 (standard library only)
  - openssl, sshpass, expect on PATH
  - govc on PATH (auto-installed under ~/.local/bin if missing)
  - Network reachability to the target upstream domain AND to each vCenter
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
import configparser

# ---------------------------------------------------------------------------
# Defaults - matched to the lab in supervisor-cert-fix/README.md but every
# value is overridable via CLI flags or a JSON config file (--config).
# ---------------------------------------------------------------------------

DEFAULT_TARGET_DOMAIN = "wp-content.vmware.com"

# Standard VCF lab path - the holuser creds.tst holds the shared vCenter
# password. We auto-load from here if present so an operator can just run
# `python3 fix_supervisor_certs.py` with no flags.
DEFAULT_PASSWORD_FILE = "/home/holuser/creds.txt"

DEFAULT_VCENTERS = [
    {
        "label": "Management vCenter Site A",
        "host": "vc-mgmt-a.site-a.vcf.lab",
        "sso_user": "administrator@vsphere.local",
        "root_user": "root",
    },
    {
        "label": "Workload vCenter Site A",
        "host": "vc-wld01-a.site-a.vcf.lab",
        "sso_user": "administrator@wld.sso",
        "root_user": "root",
    },
    {
        "label": "Management vCenter Site B",
        "host": "vc-mgmt-b.site-b.vcf.lab",
        "sso_user": "administrator@vsphere.local",
        "root_user": "root",
    },
    {
        "label": "Workload vCenter Site B",
        "host": "vc-wld01-b.site-b.vcf.lab",
        "sso_user": "administrator@wld.sso",
        "root_user": "root",
    },
]

# Proxy settings for Supervisor Control Plane and vCenter nodes.
# Imported from lsfunctions (single source of truth) when available; the
# fallback values below are used only when the script is run standalone
# outside the /home/holuser/hol repository tree.
try:
    _hol = "/home/holuser/hol"
    if _hol not in sys.path:
        sys.path.insert(0, _hol)
    from lsfunctions import LAB_PROXY_URL, LAB_NO_PROXY_PARTS
    HTTP_PROXY = LAB_PROXY_URL
    HTTPS_PROXY = LAB_PROXY_URL
    NO_PROXY_PARTS = list(LAB_NO_PROXY_PARTS)
except ImportError:
    HTTP_PROXY = "http://10.1.1.1:3128"
    HTTPS_PROXY = "http://10.1.1.1:3128"
    NO_PROXY_PARTS = [
        "localhost", "127.0.0.1",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "198.18.0.0/16",
        ".vcf.lab", ".svc", ".cluster.local",
    ]
NO_PROXY = ",".join(NO_PROXY_PARTS)

GOVC_DOWNLOAD_URL = (
    "https://github.com/vmware/govmomi/releases/download/"
    "v0.37.1/govc_Linux_x86_64.tar.gz"
)

SSH_OPTS = (
    "-o StrictHostKeyChecking=no "
    "-o UserKnownHostsFile=/dev/null "
    "-o LogLevel=ERROR "
    "-o ConnectTimeout=15"
)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log(msg, level="INFO"):
    # Timestamp is handled by the VCFfinal.py script that runs this script
    #ts = time.strftime("%Y-%m-%d %H:%M:%S")
    #print(f"[{ts}] {level}: {msg}", flush=True)
    print(f"{level}: {msg}", flush=True)


def fail(msg, code=1):
    log(msg, level="ERROR")
    sys.exit(code)


def banner(title):
    log("=" * 70)
    log(title)
    log("=" * 70)


# ---------------------------------------------------------------------------
# Pre-flight: make sure local CLI dependencies are available
# ---------------------------------------------------------------------------

def ensure_tool(tool):
    """Verify a binary exists on PATH; abort if not."""
    if shutil.which(tool) is None:
        fail(f"Required tool '{tool}' not found on PATH. Install it and retry.")


def ensure_govc():
    """Install govc under ~/.local/bin if it's not already on PATH."""
    if shutil.which("govc"):
        return
    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    log(f"govc not found - installing to {bin_dir}")
    archive = os.path.join(bin_dir, "govc.tgz")
    rc = subprocess.run(
        f"curl -fsSL -o {archive} {GOVC_DOWNLOAD_URL}",
        shell=True,
    ).returncode
    if rc != 0:
        fail("Failed to download govc.")
    rc = subprocess.run(
        f"tar -xzf {archive} -C {bin_dir} govc",
        shell=True,
    ).returncode
    if rc != 0:
        fail("Failed to extract govc.")
    os.chmod(os.path.join(bin_dir, "govc"), 0o755)
    os.unlink(archive)
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    if shutil.which("govc") is None:
        fail("govc still not on PATH after install attempt.")


# ---------------------------------------------------------------------------
# Phase 0: vCenter proxy configuration
# ---------------------------------------------------------------------------

def _vc_rest_set_noproxy(host, sso_user, password, noproxy_list):
    """Update the vCenter VAMI no-proxy exclusion list via the REST API.

    The VAMI "Hosts and IP addresses excluded from proxy" field shown at
    :5480 is governed exclusively by the appliance management REST API
    (PUT /api/appliance/networking/noproxy).  It is NOT read from
    /etc/environment or /etc/sysconfig/proxy.

    Authenticates with the vCenter SSO user (POST /api/session), PUTs the
    new list, then deletes the session.  All errors are non-fatal warnings
    because the OS-level proxy files were already written.

    Returns True on success, False on any error.
    """
    label = host.split(".")[0]
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # 1. Create REST API session
    auth_b64 = base64.b64encode(f"{sso_user}:{password}".encode()).decode()
    session_req = urllib.request.Request(
        f"https://{host}/api/session",
        method="POST",
        headers={"Authorization": f"Basic {auth_b64}"},
    )
    try:
        with urllib.request.urlopen(session_req, context=ssl_ctx, timeout=30) as resp:
            token = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        log(f"  [{label}] VAMI REST session failed: {exc}", level="WARN")
        return False

    # 2. PUT the noproxy list — body must be {"servers": [...]} not a raw array
    noproxy_bytes = json.dumps({"servers": noproxy_list}).encode()
    noproxy_req = urllib.request.Request(
        f"https://{host}/api/appliance/networking/noproxy",
        method="PUT",
        data=noproxy_bytes,
        headers={
            "vmware-api-session-id": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(noproxy_req, context=ssl_ctx, timeout=30) as _:
            pass
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        log(f"  [{label}] VAMI noproxy PUT failed HTTP {exc.code}: {body}", level="WARN")
        _vc_rest_delete_session(host, token, ssl_ctx)
        return False
    except urllib.error.URLError as exc:
        log(f"  [{label}] VAMI noproxy PUT failed: {exc}", level="WARN")
        _vc_rest_delete_session(host, token, ssl_ctx)
        return False

    # 3. Delete session (best-effort cleanup)
    _vc_rest_delete_session(host, token, ssl_ctx)
    return True


def _vc_rest_delete_session(host, token, ssl_ctx):
    """DELETE the vCenter REST API session (best-effort; ignores errors)."""
    try:
        del_req = urllib.request.Request(
            f"https://{host}/api/session",
            method="DELETE",
            headers={"vmware-api-session-id": token},
        )
        ssl_ctx2 = ssl.create_default_context()
        ssl_ctx2.check_hostname = False
        ssl_ctx2.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(del_req, context=ssl_ctx2, timeout=15) as _:
            pass
    except Exception:
        pass


def apply_proxy_to_vcenter(vc, password, dry_run):
    """Write HTTP/HTTPS proxy and NO_PROXY settings onto a vCenter appliance.

    Writes two OS-level proxy config files via SSH (root):
      /etc/environment  — 6 lowercase+uppercase env-var forms consumed by
                          most Linux processes launched from PAM sessions.
      /etc/sysconfig/proxy — Photon OS native proxy file read by tdnf and
                             other Photon-aware tools.

    Also calls the vCenter REST API (as the SSO user) to update the VAMI
    no-proxy exclusion list shown at :5480 → Networking → Proxy Settings.
    This is required because the VAMI proxy configuration is stored and
    served by the appliance management layer, completely separate from the
    OS-level environment files.

    Returns True on success, False if SSH is unavailable (non-fatal; vCenter
    may simply not allow root SSH in hardened deployments).
    """
    label = vc["label"]
    log("")
    log(f"--- {label} ({vc['host']}) -- vCenter proxy configuration ---")

    if dry_run:
        log(f"  [{label}] [dry-run] would write /etc/environment and "
            f"/etc/sysconfig/proxy on {vc['host']} and update VAMI noproxy via REST API")
        return True

    # Verify SSH reachability before attempting writes
    probe = ssh_to_vcenter(vc["host"], vc["root_user"], password, "echo VCREACH_OK")
    if "VCREACH_OK" not in probe:
        log(f"  [{label}] SSH to {vc['host']} as {vc['root_user']} not "
            f"available - skipping proxy configuration.", level="WARN")
        return True  # non-fatal: vCenter may have SSH disabled

    # Build /etc/environment additions (sed first to remove any stale lines,
    # then append the current values).
    env_additions = (
        f"http_proxy={HTTP_PROXY}\n"
        f"https_proxy={HTTPS_PROXY}\n"
        f"no_proxy={NO_PROXY}\n"
        f"HTTP_PROXY={HTTP_PROXY}\n"
        f"HTTPS_PROXY={HTTPS_PROXY}\n"
        f"NO_PROXY={NO_PROXY}\n"
    )
    env_b64 = base64.b64encode(env_additions.encode()).decode()

    # Build /etc/sysconfig/proxy (Photon OS native format)
    sysconfig_content = (
        'PROXY_ENABLED="yes"\n'
        f'HTTP_PROXY="{HTTP_PROXY}"\n'
        f'HTTPS_PROXY="{HTTPS_PROXY}"\n'
        'FTP_PROXY=""\n'
        'GOPHER_PROXY=""\n'
        'SOCKS_PROXY=""\n'
        'SOCKS5_SERVER=""\n'
        f'NO_PROXY="{NO_PROXY}"\n'
    )
    sysconfig_b64 = base64.b64encode(sysconfig_content.encode()).decode()

    commands = [
        # Remove any stale proxy lines from /etc/environment (idempotent)
        "sed -i '/^http_proxy=/d;/^https_proxy=/d;/^no_proxy=/d;"
        "/^HTTP_PROXY=/d;/^HTTPS_PROXY=/d;/^NO_PROXY=/d' /etc/environment",
        # Append current values
        f"echo {env_b64} | base64 -d >> /etc/environment",
        # Overwrite /etc/sysconfig/proxy entirely
        f"echo {sysconfig_b64} | base64 -d > /etc/sysconfig/proxy",
    ]

    for cmd in commands:
        ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd)

    log(f"  [{label}] OS-level proxy files written to {vc['host']}.")

    # --- VAMI REST API: update the no-proxy list shown in the :5480 UI ---
    log(f"  [{label}] Updating VAMI no-proxy exclusion list via REST API "
        f"(as {vc['sso_user']})...")
    if _vc_rest_set_noproxy(vc["host"], vc["sso_user"], password, NO_PROXY_PARTS):
        log(f"  [{label}] VAMI no-proxy list updated successfully.")
    else:
        log(f"  [{label}] VAMI no-proxy REST update failed (non-fatal; "
            f"OS-level files were still written).", level="WARN")

    return True


# ---------------------------------------------------------------------------
# Phase 1: Content library trust refresh
# ---------------------------------------------------------------------------

def fetch_upstream_cert(domain, port=443, timeout=30):
    """Pull the live cert from <domain>:<port> and return (pem, sha1_thumbprint).

    sha1_thumbprint is returned in the colon-delimited uppercase hex format
    that vCenter and govc expect (e.g. AA:BB:CC:...).
    """
    log(f"Fetching upstream certificate from {domain}:{port} ...")
    fetch = subprocess.run(
        f"echo | openssl s_client -showcerts -servername {domain} "
        f"-connect {domain}:{port} 2>/dev/null "
        f"| openssl x509 -outform PEM",
        shell=True, capture_output=True, text=True, timeout=timeout,
    )
    pem = fetch.stdout.strip()
    if not pem.startswith("-----BEGIN CERTIFICATE-----"):
        fail(f"Could not retrieve PEM cert from {domain}: "
             f"{fetch.stderr.strip() or 'no output'}")

    # Compute SHA-1 fingerprint in vCenter's expected format.
    fp = subprocess.run(
        "openssl x509 -noout -fingerprint -sha1",
        shell=True, input=pem, capture_output=True, text=True,
    )
    if fp.returncode != 0:
        fail(f"openssl could not compute SHA-1 fingerprint: {fp.stderr.strip()}")
    # Output looks like:  SHA1 Fingerprint=AA:BB:CC:...
    m = re.search(r"=\s*([0-9A-Fa-f:]+)", fp.stdout)
    if not m:
        fail(f"Unexpected fingerprint output: {fp.stdout!r}")
    thumbprint = m.group(1).upper()

    # Audit fields - useful in the log so the operator can sanity-check the
    # cert that's about to be trusted before it lands in production.
    subj = subprocess.run(
        "openssl x509 -noout -subject -issuer -dates",
        shell=True, input=pem, capture_output=True, text=True,
    ).stdout
    log(f"  Thumbprint (SHA-1): {thumbprint}")
    for line in subj.strip().splitlines():
        log(f"  {line.strip()}")

    if domain.lower() not in subj.lower():
        # Don't outright fail - SAN matching can put the domain elsewhere -
        # but warn loudly so someone notices a mismatch.
        log(f"WARNING: target domain '{domain}' not present in cert subject. "
            f"Make sure the cert covers it via SAN.", level="WARN")

    return pem, thumbprint


def govc(args, env_overrides, input_data=None, timeout=60, check=False):
    """Run a govc subcommand with explicit env. Returns CompletedProcess."""
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["govc"] + args,
        env=env,
        input=input_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _sub_block(lib):
    """govc emits the subscription block as either 'subscription' (newer)
    or 'subscription_info' (older). Return whichever exists, or {}."""
    return (lib.get("subscription")
            or lib.get("subscription_info")
            or {})


def _sub_url(sub):
    return sub.get("subscriptionUrl") or sub.get("subscription_url") or ""


def _sub_thumbprint(sub):
    return (sub.get("sslThumbprint")
            or sub.get("ssl_thumbprint")
            or "").upper()


def list_subscribed_libraries(env, target_domain):
    """Return [{id, name, url, host, thumbprint}] for SUBSCRIBED libs.

    If target_domain is None, return every subscribed library on this
    vCenter. If target_domain is set, only return libraries whose
    subscription URL hostname equals target_domain (so we don't accidentally
    rewrite thumbprints for unrelated subscriptions)."""
    res = govc(["library.ls", "-json"], env)
    if res.returncode != 0:
        log(f"  Could not list libraries: "
            f"{(res.stderr or res.stdout).strip()}", level="WARN")
        return []
    raw = (res.stdout or "").strip()
    if not raw or raw == "null":
        # govc emits 'null' when zero libraries exist on the vCenter (common
        # on management vCenters that don't host any content library).
        return []
    try:
        libs = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"  library.ls JSON parse failure: {e}", level="WARN")
        return []
    if libs is None:
        return []
    if not isinstance(libs, list):
        log(f"  library.ls returned unexpected JSON shape: "
            f"{type(libs).__name__}", level="WARN")
        return []

    matches = []
    for lib in libs:
        if (lib.get("type") or "").upper() != "SUBSCRIBED":
            continue
        sub = _sub_block(lib)
        url = _sub_url(sub)
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except ValueError:
            host = ""
        if target_domain and host != target_domain.lower():
            continue
        matches.append({
            "id": lib.get("id"),
            "name": lib.get("name", "<unnamed>"),
            "url": url,
            "host": host,
            "thumbprint": _sub_thumbprint(sub),
        })
    return matches


def add_cert_to_trust_store(env, pem, vc_label):
    """Push the PEM into the vCenter content library trust store. Idempotent
    on the vCenter side - re-adding an existing cert returns non-zero with a
    duplicate-entry message which we treat as success."""
    res = govc(["library.trust.create", "-"], env, input_data=pem)
    out = (res.stdout + res.stderr).strip()
    if res.returncode == 0:
        log(f"  [{vc_label}] Cert added to global trust store.")
        return True
    if "already exists" in out.lower() or "duplicate" in out.lower():
        log(f"  [{vc_label}] Cert already present in trust store (ok).")
        return True
    log(f"  [{vc_label}] library.trust.create returned {res.returncode}: {out}",
        level="WARN")
    return False


def update_subscribed_library(env, lib, new_thumbprint, vc_label, dry_run):
    """Apply the new thumbprint (when one is pinned) and force a sync.
    Returns True on success.

    Some labs / customers configure subscribed libraries with no pinned
    thumbprint (authentication_method=NONE, plain HTTPS trusted via the
    library trust store). For those we skip the thumbprint update entirely
    - touching it would actually start pinning a thumbprint that wasn't
    pinned before, which changes posture in a surprising way."""
    log(f"  [{vc_label}] Library '{lib['name']}' ({lib['id']})")
    log(f"      url:             {lib['url']}")
    log(f"      old thumbprint:  {lib['thumbprint'] or '<not pinned>'}")
    log(f"      new thumbprint:  {new_thumbprint or '<not fetched>'}")

    if not lib["thumbprint"]:
        log(f"      No SSL thumbprint pinned on this library "
            f"(authentication_method likely NONE). Skipping thumbprint "
            f"update; relying on the trust-store cert + sync.")
    elif not new_thumbprint:
        log(f"      No new thumbprint provided. Skipping thumbprint update; "
            f"will still trigger sync.", level="WARN")
    elif lib["thumbprint"] == new_thumbprint:
        log(f"      Thumbprint already current - skipping update, will "
            f"still trigger sync to refresh content.")
    else:
        if dry_run:
            log(f"      [dry-run] would: govc library.update -thumbprint "
                f"{new_thumbprint} {lib['id']}")
        else:
            res = govc(
                ["library.update", "-thumbprint", new_thumbprint, lib["id"]],
                env,
            )
            if res.returncode != 0:
                log(f"      library.update FAILED: "
                    f"{(res.stderr or res.stdout).strip()}", level="ERROR")
                return False

    if dry_run:
        log(f"      [dry-run] would: govc library.sync {lib['id']}")
        return True

    res = govc(["library.sync", lib["id"]], env, timeout=300)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        if "500" in msg or "Internal Server Error" in msg:
            # vCenter's Content Library service occasionally returns a transient
            # 500 on sync even after the cert and thumbprint have been updated
            # successfully. The trust-store write already took effect; vCenter
            # will retry the sync on its next scheduled poll (typically within
            # minutes). Log as a warning so the overall run does not fail.
            log(f"      library.sync returned 500 (transient CL service error). "
                f"The trust-store cert was already written; vCenter will retry "
                f"the sync automatically. Treating as non-fatal.", level="WARN")
            log(f"      raw error: {msg}", level="WARN")
            return True
        if ("connection_to_vcsp_server_failed" in msg
                or ("connection" in msg.lower() and "failed" in msg.lower()
                    and ("400" in msg or "Bad Request" in msg))):
            # The upstream VCSP server (e.g. fleet-01a) was not reachable at
            # sync time — likely still starting up during lab boot.  The
            # trust-store cert was already written successfully; vCenter will
            # retry the sync automatically on its background schedule.
            log(f"      library.sync returned a connectivity error "
                f"(upstream {lib['host']} may not be fully started yet). "
                f"The trust-store cert was already written; vCenter will retry "
                f"the sync automatically. Treating as non-fatal.", level="WARN")
            log(f"      raw error: {msg}", level="WARN")
            return True
        log(f"      library.sync FAILED: {msg}", level="ERROR")
        return False
    log(f"      sync triggered.")
    return True


def verify_library_thumbprint(env, lib_id, expected_thumbprint, vc_label):
    """Re-read the library and confirm the stored thumbprint matches expected.
    Returns True if it matches, False otherwise."""
    res = govc(["library.info", "-json", lib_id], env)
    if res.returncode != 0:
        log(f"  [{vc_label}] Could not re-read library {lib_id}: "
            f"{res.stderr.strip()}", level="WARN")
        return False
    try:
        info = json.loads(res.stdout)
    except json.JSONDecodeError:
        log(f"  [{vc_label}] library.info returned non-JSON for {lib_id}",
            level="WARN")
        return False
    if isinstance(info, list):
        info = info[0] if info else {}
    sub = _sub_block(info)
    actual = _sub_thumbprint(sub)
    if not expected_thumbprint:
        log(f"  [{vc_label}] No expected thumbprint to verify "
            f"(library was not pinning one).")
        return True
    if actual == expected_thumbprint.upper():
        log(f"  [{vc_label}] VERIFIED: library {lib_id} now pins "
            f"{expected_thumbprint}")
        return True
    log(f"  [{vc_label}] MISMATCH after update: library {lib_id} pins "
        f"{actual or '<none>'} (expected {expected_thumbprint})", level="ERROR")
    return False


def fix_content_library_trust(vc, password, target_domain, auto, pem,
                              thumbprint, dry_run):
    """Run the full content-library refresh flow against one vCenter.

    When auto=True, target_domain/pem/thumbprint are ignored and we instead
    iterate every subscribed library, fetching the upstream cert from each
    library's own subscription URL and trusting/syncing per-library. This
    is what you want when you don't know (or care) which upstream rotated
    its cert -- the script will refresh trust for whatever is configured.
    """
    label = vc["label"]
    log("")
    log(f"--- {label} ({vc['host']}) -- content library trust refresh ---")
    env = {
        "GOVC_URL": f"https://{vc['host']}",
        "GOVC_USERNAME": vc["sso_user"],
        "GOVC_PASSWORD": password,
        "GOVC_INSECURE": "true",
    }

    sanity = govc(["about"], env, timeout=20)
    if sanity.returncode != 0:
        log(f"  [{label}] govc about FAILED - check creds / reachability: "
            f"{(sanity.stderr or sanity.stdout).strip()}", level="ERROR")
        return False

    libs = list_subscribed_libraries(env, None if auto else target_domain)
    if not libs:
        if auto:
            log(f"  [{label}] No SUBSCRIBED libraries on this vCenter. "
                f"Nothing to do.")
        else:
            log(f"  [{label}] No SUBSCRIBED libraries point at "
                f"{target_domain}. Nothing to update. (Re-run with --auto "
                f"to refresh whatever upstreams ARE configured.)")
        return True

    log(f"  [{label}] Found {len(libs)} subscribed library/libraries to "
        f"refresh.")

    # Group libraries by upstream host so we only fetch each cert once and
    # only push each cert to the trust store once.
    by_host = {}
    for lib in libs:
        by_host.setdefault(lib["host"], []).append(lib)

    all_ok = True
    for host, host_libs in by_host.items():
        if auto:
            log(f"  [{label}] Processing upstream {host} "
                f"({len(host_libs)} library/libraries)")
            try:
                this_pem, this_thumb = fetch_upstream_cert(host)
            except SystemExit:
                log(f"  [{label}] Could not fetch cert from {host} - "
                    f"skipping its libraries.", level="ERROR")
                all_ok = False
                continue
        else:
            this_pem, this_thumb = pem, thumbprint

        if not dry_run:
            if not add_cert_to_trust_store(env, this_pem, label):
                log(f"  [{label}] Aborting updates for upstream {host} "
                    f"because trust.create failed.", level="ERROR")
                all_ok = False
                continue
        else:
            log(f"  [{label}] [dry-run] would: govc library.trust.create - "
                f"(piping cert for {host})")

        for lib in host_libs:
            if not update_subscribed_library(env, lib, this_thumb, label,
                                             dry_run):
                all_ok = False
                continue
            if dry_run:
                continue
            # Only verify when the library was already pinning a thumbprint
            # AND we had a new one to push. Libraries with no pin (e.g.
            # authentication_method=NONE) intentionally stay unpinned.
            if lib["thumbprint"] and this_thumb:
                if not verify_library_thumbprint(env, lib["id"], this_thumb,
                                                 label):
                    all_ok = False
            else:
                log(f"      Skipping post-update thumbprint verification "
                    f"(library was not pinning one).")
    return all_ok


# ---------------------------------------------------------------------------
# Phase 2: Supervisor management-proxy cert regeneration
# (Lifted from the original fix_proxy_certs.py - same multi-hop ssh+expect
# approach, kept here so the combined script has zero external imports.)
# ---------------------------------------------------------------------------

def ssh_to_vcenter(vcenter_host, vcenter_user, vcenter_pass, command):
    escaped = command.replace("'", "'\"'\"'")
    cmd = (
        f"sshpass -p '{vcenter_pass}' ssh {SSH_OPTS} "
        f"{vcenter_user}@{vcenter_host} '{escaped}'"
    )
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         timeout=60)
    out = res.stdout.strip()
    return "\n".join(
        l for l in out.splitlines() if "Shell access is granted to" not in l
    ).strip()


def build_expect_script(vcenter_host, vcenter_user, vcenter_pass,
                        scp_ip, scp_pass, kubectl_commands, timeout=45):
    kubectl_block = ""
    for cmd in kubectl_commands:
        # Escape TCL metacharacters inside the double-quoted send "..." string:
        #   "  → \"   (string terminator)
        #   $  → \$   (variable substitution)
        #   [  → \[   (command substitution — omitting this causes "invalid
        #               command name '!'" when shell test brackets appear)
        #   ]  → \]   (matching bracket)
        tcl_cmd = (cmd.replace('"', '\\"')
                      .replace('$', '\\$')
                      .replace('[', '\\[')
                      .replace(']', '\\]'))
        kubectl_block += (
            f'send "{tcl_cmd}\\r"\n'
            f'expect {{\n'
            f'    -re "root@.*#" {{}}\n'
            f'    timeout {{ puts "TIMEOUT running kubectl"; exit 1 }}\n'
            f'}}\n\n'
        )

    script = textwrap.dedent(f"""\
        #!/usr/bin/expect -f
        set timeout {timeout}
        log_user 1

        set vcpass {{%VCPASS%}}
        set scppass {{%SCPPASS%}}

        spawn sshpass -p $vcpass ssh {SSH_OPTS} {vcenter_user}@{vcenter_host}

        expect {{
            "Command>" {{
                send "shell\\r"
                expect -re "root@.*#"
            }}
            -re "root@.*#" {{}}
            timeout {{ puts "TIMEOUT waiting for vCenter prompt"; exit 1 }}
        }}

        send "printf '%s' '$scppass' > /tmp/.scppwd && chmod 600 /tmp/.scppwd\\r"
        expect -re "root@.*#"

        send "sshpass -f /tmp/.scppwd ssh {SSH_OPTS} root@{scp_ip}\\r"
        expect {{
            -re "root@.*#" {{}}
            timeout {{ puts "TIMEOUT connecting to Supervisor control plane"; exit 1 }}
        }}

        {kubectl_block}

        send "exit\\r"
        expect {{
            -re "root@.*#" {{}}
            eof {{}}
            timeout {{}}
        }}

        send "rm -f /tmp/.scppwd\\r"
        expect -re "root@.*#"

        send "exit\\r"
        expect eof
    """)

    def tcl_brace_escape(s):
        return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    script = script.replace("%VCPASS%", tcl_brace_escape(vcenter_pass))
    script = script.replace("%SCPPASS%", tcl_brace_escape(scp_pass))
    return script


def run_on_scp(vcenter_host, vcenter_user, vcenter_pass, scp_ip, scp_pass,
               kubectl_commands, description="", timeout=45):
    if description:
        log(description)

    script = build_expect_script(
        vcenter_host, vcenter_user, vcenter_pass,
        scp_ip, scp_pass, kubectl_commands, timeout=timeout,
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".exp", prefix="scp_cmd_", delete=False
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        os.chmod(script_path, 0o700)
        result = subprocess.run(
            f"expect {script_path}",
            shell=True, capture_output=True, text=True,
            timeout=timeout + 30,
        )
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)

        noise = [
            "spawn ", "Command>", "Shell access is granted",
            "Connected to service", "List APIs", "List Plugins",
            "Launch BASH", "Last login:", "tdnf update",
            ".scppwd", "sshpass ", "printf ",
            "load average", "LogLevel=ERROR",
        ]
        clean = []
        for line in output.splitlines():
            s = line.strip()
            if not s or any(p in s for p in noise):
                continue
            if re.match(r"^root@\S+\s*\[.*\]\s*#", s):
                continue
            if s.startswith("<") and len(s) < 80 and "#" not in s:
                continue
            clean.append(s)
            log(f"  {s}")

        if result.returncode != 0:
            log(f"  expect exit code: {result.returncode}", level="WARN")
            if result.stderr.strip():
                log(f"  stderr: {result.stderr.strip()}", level="WARN")
        return "\n".join(clean)
    finally:
        os.unlink(script_path)


def parse_decrypt_k8_pwd(text):
    """Parse the (possibly multi-cluster) output of decryptK8Pwd.py.

    Each Supervisor cluster on a vCenter produces a stanza like:
        Cluster: domain-cN:<uuid>
        IP: <vip>
        PWD: <root password>
        ------------------------------------------------------------

    Returns a list of dicts: [{"cluster": "domain-c9:...", "ip": "...",
                               "pwd": "..."}, ...] in declaration order.

    Also tolerates the single-cluster legacy form and stray blank lines.
    """
    clusters = []
    cur = {}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^Cluster:\s*(.+)$", s, re.IGNORECASE)
        if m:
            if cur.get("ip") and cur.get("pwd"):
                clusters.append(cur)
            cur = {"cluster": m.group(1).strip()}
            continue
        m = re.match(r"^IP:\s*(.+)$", s, re.IGNORECASE)
        if m:
            cur["ip"] = m.group(1).strip()
            continue
        m = re.match(r"^PWD:\s*(.+)$", s, re.IGNORECASE)
        if m:
            cur["pwd"] = m.group(1).strip()
            continue
    if cur.get("ip") and cur.get("pwd"):
        clusters.append(cur)
    return clusters


def ssh_probe_scp(vc, password, scp_ip, scp_pass, timeout=15):
    """Try to SSH from vCenter to a Supervisor node. Returns True on success.

    We use SSH (not ICMP) because:
      - Many lab/customer networks drop ICMP between vCenter and the SCP
        management network even when SSH works fine.
      - SSH is what we actually need to work for the fix, so this is the
        only check that matters.
    """
    pwd_b64 = base64.b64encode(scp_pass.encode()).decode()
    cmd = (
        f"echo {pwd_b64} | base64 -d > /tmp/.scppwd_probe && chmod 600 "
        f"/tmp/.scppwd_probe && "
        f"sshpass -f /tmp/.scppwd_probe ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "
        f"-o ConnectTimeout={timeout} -o BatchMode=no "
        f"root@{scp_ip} 'echo SCPREACH_OK_$(hostname)'; "
        f"rm -f /tmp/.scppwd_probe"
    )
    out = ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd)
    return "SCPREACH_OK_" in out


def discover_scp_node_ips(vc, password):
    """Fall back: ask the VPX DB for any VM whose name looks like a Supervisor
    control plane node, return the candidate IPs in inventory order.

    This is a last-resort discovery for the rare case where decryptK8Pwd.py
    returns a VIP that has no working route from vCenter (broken HAProxy /
    NSX, half-deployed lab, etc.). The script will SSH-probe every candidate
    and use the first one that answers.
    """
    out = ssh_to_vcenter(
        vc["host"], vc["root_user"], password,
        "/opt/vmware/vpostgres/current/bin/psql -U postgres -d VCDB -t -c "
        "\"SELECT ip_address FROM vpx_ip_address WHERE entity_id IN "
        "(SELECT id FROM vpx_vm WHERE file_name LIKE '%Supervisor%') "
        "ORDER BY entity_id, ip_address;\"",
    )
    ips = []
    for line in out.splitlines():
        s = line.strip()
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", s):
            ips.append(s)
    return ips


def fix_supervisor_control_plane(vc, password, dry_run, only_cluster=None,
                        only_ip=None):
    """Regenerate the supervisor-management-proxy mTLS cert on one vCenter.

    A single vCenter can host multiple Supervisor clusters; we discover them
    all via decryptK8Pwd.py and apply the fix to each reachable one.

    Optional overrides:
      only_cluster -- substring to match against the 'domain-cN:<uuid>' id.
                      Use this when you want to target a specific Supervisor
                      (e.g. the user verified the right id with their own
                      `decryptK8Pwd.py` run).
      only_ip      -- exact IP override; bypasses both decryptK8Pwd discovery
                      and the VPX-DB fallback. Useful when the operator
                      already knows which node to talk to.
    """
    label = vc["label"]
    log("")
    log(f"--- {label} ({vc['host']}) -- supervisor control plane stabilization ---")

    log("  Retrieving Supervisor control plane credentials via "
        "decryptK8Pwd.py ...")
    # Disable the vCenter shell pager so multi-cluster output isn't truncated
    # by `--More--`. Without this, decryptK8Pwd.py only shows the first
    # cluster on a vCenter that has more than one Supervisor enabled.
    decrypt = ssh_to_vcenter(
        vc["host"], vc["root_user"], password,
        "PAGER=cat TERM=dumb /usr/lib/vmware-wcp/decryptK8Pwd.py 2>&1 | cat",
    )
    clusters = parse_decrypt_k8_pwd(decrypt)
    if not clusters:
        log(f"  [{label}] Could not parse any Cluster/IP/PWD stanzas from:\n"
            f"{decrypt}", level="INFO")
        log(f"  [{label}] This vCenter likely does not have a Supervisor enabled. Skipping.")
        return True

    log(f"  [{label}] decryptK8Pwd.py reported {len(clusters)} Supervisor "
        f"cluster(s):")
    for c in clusters:
        log(f"      - {c['cluster']}  ip={c['ip']}")

    # Apply --supervisor-cluster / --supervisor-ip filters (operator overrides
    # so they can match their own decryptK8Pwd.py output if state has shifted
    # since this script started).
    if only_cluster:
        before = len(clusters)
        clusters = [c for c in clusters if only_cluster in c["cluster"]]
        log(f"  [{label}] --supervisor-cluster {only_cluster!r} matched "
            f"{len(clusters)}/{before} cluster(s).")
        if not clusters:
            log(f"  [{label}] No cluster matched the --supervisor-cluster "
                f"filter - aborting proxy fix on this vCenter.", level="ERROR")
            return False
    if only_ip:
        for c in clusters:
            c["ip"] = only_ip
        log(f"  [{label}] --supervisor-ip override: forcing IP {only_ip} on "
            f"the selected cluster(s).")

    overall_ok = True
    for c in clusters:
        ok = _stabilize_one_supervisor(vc, password, c, dry_run)
        if not ok:
            overall_ok = False
    return overall_ok


def _stabilize_one_supervisor(vc, password, cluster, dry_run):
    """Apply the stabilization fixes to a single Supervisor cluster stanza."""
    label = vc["label"]
    cid = cluster["cluster"]
    scp_ip = cluster["ip"]
    scp_pass = cluster["pwd"]

    log("")
    log(f"  >>> {label} :: {cid}")
    log(f"      VIP from decryptK8Pwd.py: {scp_ip}")

    if ssh_probe_scp(vc, password, scp_ip, scp_pass):
        log(f"      SSH ok to {scp_ip} - using as the control plane target.")
    else:
        log(f"      SSH to {scp_ip} failed - searching VPX DB for a working "
            f"Supervisor node ...", level="WARN")
        candidates = discover_scp_node_ips(vc, password)
        # Skip ones we already tried; preserve order.
        seen = {scp_ip}
        candidates = [c for c in candidates if c not in seen and not seen.add(c)]
        log(f"      VPX DB returned {len(candidates)} candidate IP(s).")
        chosen = None
        for cand in candidates:
            log(f"      probing {cand} ...")
            if ssh_probe_scp(vc, password, cand, scp_pass):
                chosen = cand
                break
        if not chosen:
            log(f"      [{cid}] No Supervisor node responded to SSH from "
                f"vCenter - cluster control plane appears down. Skipping.",
                level="ERROR")
            log(f"      Hint: log in to {vc['host']}, run "
                f"/usr/lib/vmware-wcp/decryptK8Pwd.py and pass the IP it "
                f"reports via --supervisor-ip <addr> if you know it should "
                f"be reachable.", level="WARN")
            return False
        log(f"      Using fallback node IP {chosen}.")
        scp_ip = chosen

    log(f"      [{cid}] Supervisor control plane: {scp_ip}")

    # --- Phase A: Wait for Services ---
    log(f"      [{cid}] Waiting for hypercrypt and kubelet to become active...")
    max_wait = 1800
    start_time = time.time()
    services_ok = False
    
    while time.time() - start_time < max_wait:
        elapsed = int(time.time() - start_time)
        
        # Check hypercrypt
        cmd_hc = (
            f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_hc && chmod 600 /tmp/.scppwd_hc && "
            f"sshpass -f /tmp/.scppwd_hc ssh {SSH_OPTS} root@{scp_ip} 'systemctl is-active hypercrypt 2>/dev/null'; "
            f"rm -f /tmp/.scppwd_hc"
        )
        hc_status = ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_hc).strip()
        
        # Check kubelet
        cmd_kl = (
            f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_kl && chmod 600 /tmp/.scppwd_kl && "
            f"sshpass -f /tmp/.scppwd_kl ssh {SSH_OPTS} root@{scp_ip} 'systemctl is-active kubelet 2>/dev/null'; "
            f"rm -f /tmp/.scppwd_kl"
        )
        kl_status = ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_kl).strip()
        
        log(f"      [{cid}] hypercrypt: {hc_status or 'unknown'}, kubelet: {kl_status or 'unknown'} ({elapsed}s / {max_wait}s)")
        
        if hc_status == "active" and kl_status == "active":
            log(f"      [{cid}] Both hypercrypt and kubelet are active.")
            services_ok = True
            break
            
        if hc_status == "activating":
            log(f"      [{cid}] hypercrypt is still initializing (encryption keys being delivered)...")
        elif hc_status == "failed":
            log(f"      [{cid}] hypercrypt has failed - attempting restart...", level="WARN")
            cmd_restart_hc = (
                f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_rhc && chmod 600 /tmp/.scppwd_rhc && "
                f"sshpass -f /tmp/.scppwd_rhc ssh {SSH_OPTS} root@{scp_ip} 'systemctl restart hypercrypt'; "
                f"rm -f /tmp/.scppwd_rhc"
            )
            ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_restart_hc)
            
        if kl_status != "active" and hc_status == "active":
            log(f"      [{cid}] hypercrypt is active but kubelet is not - attempting to start kubelet...")
            cmd_start_kl = (
                f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_skl && chmod 600 /tmp/.scppwd_skl && "
                f"sshpass -f /tmp/.scppwd_skl ssh {SSH_OPTS} root@{scp_ip} 'systemctl start kubelet'; "
                f"rm -f /tmp/.scppwd_skl"
            )
            ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_start_kl)
            
        time.sleep(30)
        
    if not services_ok:
        log(f"      [{cid}] SCP services did not become active within timeout.", level="ERROR")
        return False
        
    log(f"      [{cid}] Waiting for Kubernetes API to become available...")
    k8s_ok = False
    while time.time() - start_time < max_wait:
        elapsed = int(time.time() - start_time)
        cmd_k8s = (
            f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_k8s && chmod 600 /tmp/.scppwd_k8s && "
            f"sshpass -f /tmp/.scppwd_k8s ssh {SSH_OPTS} root@{scp_ip} 'kubectl get --raw /healthz 2>&1'; "
            f"rm -f /tmp/.scppwd_k8s"
        )
        k8s_status = ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_k8s).strip()
        
        if k8s_status == "ok":
            log(f"      [{cid}] Kubernetes API is available (healthz: ok).")
            k8s_ok = True
            break
            
        log(f"      [{cid}] K8s API not yet available - waiting... ({elapsed}s / {max_wait}s)")
        time.sleep(30)
        
    if not k8s_ok:
        log(f"      [{cid}] Kubernetes API did not become available within timeout.", level="ERROR")
        return False

    # --- Phase B: Proxy Configuration ---
    log(f"      [{cid}] Configuring PROXY/NO_PROXY settings on SCP node...")
    if dry_run:
        log(f"      [{cid}] [dry-run] would configure /etc/environment and containerd proxy.")
    else:
        # Build the containerd drop-in content and base64-encode it so it
        # can be written with a single echo | base64 -d > command.  A heredoc
        # (cat >> file << 'EOF') cannot be used here because it puts the shell
        # into continuation mode (the '>' prompt) which never matches the
        # expect pattern root@.*# — causing a timeout before systemctl ever
        # runs and leaving containerd without proxy settings.
        _proxy_conf_b64 = base64.b64encode((
            "[Service]\n"
            f'Environment="HTTP_PROXY={HTTP_PROXY}"\n'
            f'Environment="HTTPS_PROXY={HTTPS_PROXY}"\n'
            f'Environment="NO_PROXY={NO_PROXY}"\n'
        ).encode()).decode()
        proxy_commands = [
            f"sed -i '/^http_proxy=/d;/^https_proxy=/d;/^no_proxy=/d;/^HTTP_PROXY=/d;/^HTTPS_PROXY=/d;/^NO_PROXY=/d' /etc/environment",
            f"echo 'http_proxy={HTTP_PROXY}' >> /etc/environment",
            f"echo 'https_proxy={HTTPS_PROXY}' >> /etc/environment",
            f"echo 'no_proxy={NO_PROXY}' >> /etc/environment",
            f"echo 'HTTP_PROXY={HTTP_PROXY}' >> /etc/environment",
            f"echo 'HTTPS_PROXY={HTTPS_PROXY}' >> /etc/environment",
            f"echo 'NO_PROXY={NO_PROXY}' >> /etc/environment",
            f"mkdir -p /etc/systemd/system/containerd.service.d",
            f"echo {_proxy_conf_b64} | base64 -d > /etc/systemd/system/containerd.service.d/http-proxy.conf",
            f"systemctl daemon-reload",
            f"systemctl restart containerd",
        ]
        run_on_scp(
            vc["host"], vc["root_user"], password,
            scp_ip, scp_pass, proxy_commands,
            description=f"      [{cid}] Applying proxy settings and restarting containerd",
            timeout=120,
        )

    # Resolve the best available kubeconfig on this SCP node:
    #
    #   1. /etc/kubernetes/super-admin.conf  (preferred - unencrypted, always
    #      on disk, available from vSphere 8.0 U2 onwards).
    #
    #   2. /etc/kubernetes/admin.conf        (older builds; this is a symlink
    #      to /dev/shm/wcp_decrypted_data/k8s-admin-conf_uid0 which is written
    #      by a PAM module on interactive login.  In our non-interactive expect
    #      session PAM does not run, so we must trigger decryption manually by
    #      running decryptK8Pwd.py equivalent: `wcp-user-keys-gen` or by
    #      sourcing the wcp environment which calls it as a side effect).
    #
    # We probe which path exists *before* we attempt any kubectl commands so
    # we can build the right invocation rather than letting kubectl fail with
    # a confusing "stat: no such file or directory" error.
    kubeconfig_probe = ssh_to_vcenter(
        vc["host"], vc["root_user"], password,
        f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_kc "
        f"&& chmod 600 /tmp/.scppwd_kc "
        f"&& sshpass -f /tmp/.scppwd_kc ssh "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o LogLevel=ERROR -o ConnectTimeout=10 root@{scp_ip} "
        f"'ls /etc/kubernetes/super-admin.conf 2>/dev/null && echo SUPER_ADMIN_EXISTS "
        f"|| ls /etc/kubernetes/admin.conf 2>/dev/null && echo ADMIN_EXISTS "
        f"|| echo NO_KUBECONFIG' "
        f"; rm -f /tmp/.scppwd_kc",
    )
    if "SUPER_ADMIN_EXISTS" in kubeconfig_probe:
        kubeconfig = "/etc/kubernetes/super-admin.conf"
        log(f"      [{cid}] Using super-admin.conf (preferred, unencrypted).")
    elif "ADMIN_EXISTS" in kubeconfig_probe:
        kubeconfig = "/etc/kubernetes/admin.conf"
        log(f"      [{cid}] super-admin.conf not found; using admin.conf "
            f"(PAM-decrypted symlink).", level="WARN")
        log(f"      [{cid}] Note: admin.conf relies on PAM decryption. If "
            f"kubectl commands fail with 'no such file', the PAM symlink may "
            f"not have been populated yet - re-run the script.", level="WARN")
    else:
        log(f"      [{cid}] No kubeconfig found at /etc/kubernetes/ on "
            f"{scp_ip}. kubectl commands will likely fail.", level="WARN")
        kubeconfig = "/etc/kubernetes/super-admin.conf"  # best guess; let kubectl error speak for itself

    K = f"kubectl --kubeconfig={kubeconfig}"

    # --- Phase C: Certificate Management ---
    if dry_run:
        log(f"      [{cid}] [dry-run] would check/delete cert-manager + kube-system "
            f"proxy secrets and roll the deployment on {scp_ip}.")
    else:
        # 1. Check and renew storage-quota certs
        certs_to_check = [
            ("vmware-system-cert-manager", "storage-quota-root-ca-secret"),
            ("kube-system", "storage-quota-webhook-server-internal-cert"),
            ("kube-system", "cns-storage-quota-extension-cert")
        ]
        
        certs_need_renewal = False
        for ns, secret in certs_to_check:
            cmd_cert = (
                f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_cert && chmod 600 /tmp/.scppwd_cert && "
                f"sshpass -f /tmp/.scppwd_cert ssh {SSH_OPTS} root@{scp_ip} "
                f"'{K} -n {ns} get secret {secret} -o jsonpath=\"{{.data.tls\\\\.crt}}\" 2>/dev/null | base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null'; "
                f"rm -f /tmp/.scppwd_cert"
            )
            end_date_out = ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_cert).strip()
            
            if not end_date_out:
                log(f"      [{cid}] {ns}/{secret}: Not found or could not parse (will be created automatically)")
                continue
                
            m = re.search(r"notAfter=(.+)", end_date_out)
            if m:
                expiry_str = m.group(1)
                try:
                    # Parse openssl date format, e.g., "May  7 14:54:44 2026 GMT"
                    expiry_epoch = time.mktime(time.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z"))
                    now_epoch = time.time()
                    remaining = expiry_epoch - now_epoch
                    if remaining <= 604800: # 1 week
                        log(f"      [{cid}] {ns}/{secret}: EXPIRING SOON or EXPIRED. Deleting to trigger regeneration...")
                        certs_need_renewal = True
                        cmd_del = (
                            f"echo {base64.b64encode(scp_pass.encode()).decode()} | base64 -d > /tmp/.scppwd_del && chmod 600 /tmp/.scppwd_del && "
                            f"sshpass -f /tmp/.scppwd_del ssh {SSH_OPTS} root@{scp_ip} "
                            f"'{K} -n {ns} delete secret {secret} --ignore-not-found=true'; "
                            f"rm -f /tmp/.scppwd_del"
                        )
                        ssh_to_vcenter(vc["host"], vc["root_user"], password, cmd_del)
                    else:
                        log(f"      [{cid}] {ns}/{secret}: Valid ({int(remaining/86400)}d remaining)")
                except ValueError:
                    log(f"      [{cid}] Could not parse expiry date: {expiry_str}", level="WARN")
                    
        if certs_need_renewal:
            log(f"      [{cid}] Restarting deployments to regenerate certificates...")
            restart_cmds = [
                f"{K} -n kube-system rollout restart deploy cns-storage-quota-extension || true",
                f"{K} -n kube-system rollout restart deploy storage-quota-webhook || true"
            ]
            run_on_scp(
                vc["host"], vc["root_user"], password,
                scp_ip, scp_pass, restart_cmds,
                description=f"      [{cid}] Restarting storage-quota deployments",
                timeout=120,
            )
            time.sleep(20)

        # 2. Regenerate supervisor-management-proxy certs.
        #    All commands are guarded so they are silent no-ops when the
        #    resources don't exist (e.g. newer Supervisor builds that have
        #    removed or renamed this component).
        commands = [
            # Delete cert-manager proxy secrets only when any actually exist.
            # Without the guard, an empty subshell produces "error: resource(s)
            # were provided, but no name was specified".
            f"PROXY_SC=$({K} get secret -n cert-manager -o name 2>/dev/null"
            f" | grep supervisor-management-proxy);"
            f" if [ -n \"$PROXY_SC\" ]; then {K} delete -n cert-manager $PROXY_SC; fi",
            # --ignore-not-found silences NotFound errors for the well-known
            # kube-system secrets on builds where they don't exist.
            f"{K} -n kube-system delete secret "
            f"supervisor-management-proxy-ca supervisor-management-proxy-tls"
            f" --ignore-not-found",
            # Roll the deployment only if it exists; absent on some builds.
            f"if {K} -n kube-system get deploy supervisor-management-proxy"
            f" >/dev/null 2>&1; then"
            f" {K} -n kube-system rollout restart deploy supervisor-management-proxy"
            f" && {K} -n kube-system rollout status deploy"
            f" supervisor-management-proxy --timeout=180s; fi",
        ]
        run_on_scp(
            vc["host"], vc["root_user"], password,
            scp_ip, scp_pass, commands,
            description=f"      [{cid}] Executing proxy-cert regeneration on SCP",
            timeout=240,
        )
        log(f"      [{cid}] Management proxy regeneration submitted on {scp_ip}.")

    # --- Phase D: Workload Recovery ---
    if dry_run:
        log(f"      [{cid}] [dry-run] would scale up cci, argocd, harbor and clean up stale pods.")
    else:
        log(f"      [{cid}] Scaling up services and cleaning up stale pods...")
        recovery_cmds = [
            # Scale up CCI
            f"CCI_NS=$({K} get ns --no-headers | grep 'svc-cci-ns' | awk '{{print $1}}'); if [ -n \"$CCI_NS\" ]; then {K} -n $CCI_NS scale deployment --all --replicas=1; fi",
            # Scale up ArgoCD
            f"if {K} get ns argocd >/dev/null 2>&1; then {K} -n argocd scale deployment --all --replicas=1; fi",
            # Scale up Harbor
            f"HARBOR_NS=$({K} get ns --no-headers | grep 'svc-harbor' | awk '{{print $1}}'); if [ -n \"$HARBOR_NS\" ]; then {K} -n $HARBOR_NS scale sts --all --replicas=1; {K} -n $HARBOR_NS scale deployment --all --replicas=1; fi",
            # Clean up stale pods in all namespaces.
            # Written as a single compound line so the shell returns to the
            # root@.*# prompt when done.  Multiline for/if blocks put the
            # shell into continuation mode (> prompt) which the expect script
            # can never match, causing a timeout after the first 'do'.
            f"for ns in $({K} get ns --no-headers | awk '{{print $1}}'); do stale=$({K} get pods -n $ns --no-headers 2>/dev/null | grep -E 'NotFound|ProviderFailed|Unknown|ImagePullBackOff|Failed' | awk '{{print $1}}'); if [ -n \"$stale\" ]; then for p in $stale; do {K} delete pod -n $ns $p --force --grace-period=0 2>/dev/null; done; fi; done",
        ]
        run_on_scp(
            vc["host"], vc["root_user"], password,
            scp_ip, scp_pass, recovery_cmds,
            description=f"      [{cid}] Executing workload recovery on SCP",
            timeout=300,
        )

    return True


# ---------------------------------------------------------------------------
# Config loading + CLI
# ---------------------------------------------------------------------------

def read_password_file(path):
    """Read a password from a file. Returns the first non-empty line with all
    whitespace and non-printable bytes stripped (matches the legacy shell
    script's `tr -d "[:space:]" | tr -dc "[:print:]"` behaviour, which is
    important for files written from Windows / GUI tools)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        fail(f"Could not read password file {path}: {e}")
    text = raw.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        cleaned = "".join(c for c in line if c.isprintable() and not c.isspace())
        if cleaned:
            return cleaned
    fail(f"Password file {path} contained no printable, non-blank line.")


def resolve_password(args):
    """Apply the documented resolution order and return the password."""
    if args.password:
        return args.password
    if args.password_file:
        log(f"Reading vCenter password from {args.password_file}")
        return read_password_file(args.password_file)
    if os.path.exists(DEFAULT_PASSWORD_FILE):
        log(f"Reading vCenter password from {DEFAULT_PASSWORD_FILE} "
            f"(default lab path)")
        return read_password_file(DEFAULT_PASSWORD_FILE)
    fail(
        "No password supplied. Provide one of:\n"
        "  --password 'XXX'\n"
        "  --password-file /path/to/file\n"
        "  VC_PASSWORD env var\n"
        f"  or place the password in {DEFAULT_PASSWORD_FILE}"
    )



def load_vcenters_from_ini(ini_path="/tmp/config.ini"):
    if not os.path.exists(ini_path):
        return None
        
    config = configparser.ConfigParser(allow_no_value=True)
    config.optionxform = str  # Preserve case
    
    try:
        config.read(ini_path)
    except Exception as e:
        log(f"Failed to read {ini_path}: {e}", level="WARN")
        return None
        
    if not config.has_section('RESOURCES') or not config.has_option('RESOURCES', 'vCenters'):
        return None
        
    vcenter_entries = config.get('RESOURCES', 'vCenters').split('\n')
    
    vcenters = []
    for entry in vcenter_entries:
        entry = entry.strip()
        if not entry or entry.startswith('#'):
            continue
            
        parts = entry.split(':')
        if len(parts) >= 3:
            hostname = parts[0].strip()
            sso_user = parts[2].strip()
            vcenters.append({
                "label": f"vCenter {hostname}",
                "host": hostname,
                "sso_user": sso_user,
                "root_user": "root"
            })
            
    return vcenters if vcenters else None


def load_vcenters(config_path):
    if config_path:
        with open(config_path) as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            fail(f"Config {config_path} must be a non-empty JSON list.")
        required = {"label", "host", "sso_user", "root_user"}
        for entry in data:
            missing = required - set(entry.keys())
            if missing:
                fail(f"Config entry {entry.get('label', entry)!r} missing keys: "
                     f"{sorted(missing)}")
        return data
        
    # Default behavior: try /tmp/config.ini first
    ini_vcenters = load_vcenters_from_ini()
    if ini_vcenters:
        log(f"Loaded {len(ini_vcenters)} vCenter(s) from /tmp/config.ini")
        return ini_vcenters
        
    log("Falling back to default vCenter list")
    return DEFAULT_VCENTERS


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--password", required=False,
        default=os.environ.get("VC_PASSWORD"),
        help="Shared vCenter password (used for both SSO and root). "
             "May also be supplied via VC_PASSWORD env var or --password-file. "
             "Resolution order: --password > VC_PASSWORD > --password-file > "
             f"{DEFAULT_PASSWORD_FILE} (if it exists).",
    )
    p.add_argument(
        "--password-file",
        help=f"Read the vCenter password from a file (first line, whitespace "
             f"stripped). Defaults to {DEFAULT_PASSWORD_FILE} when no other "
             f"source is provided.",
    )
    p.add_argument(
        "--target-domain", default=DEFAULT_TARGET_DOMAIN,
        help=f"Upstream content-library domain whose cert was rotated "
             f"(default: {DEFAULT_TARGET_DOMAIN}). Ignored when --auto is "
             f"set.",
    )
    p.add_argument(
        "--auto", action="store_true",
        help="Auto-discover every SUBSCRIBED library on each vCenter and "
             "refresh the trust + thumbprint for each one's own upstream. "
             "Use this when you don't know (or don't care) which upstream "
             "rotated its cert -- e.g. internal depots like fleet-01a.",
    )
    p.add_argument(
        "--config",
        help="Optional JSON file with a list of vCenter targets. "
             "See README for schema. Defaults to the lab topology baked in.",
    )
    p.add_argument(
        "--skip-vcenter-proxy", action="store_true",
        help="Don't run the vCenter proxy configuration phase (Phase 0).",
    )
    p.add_argument(
        "--skip-content-lib", action="store_true",
        help="Don't run the content library trust refresh phase.",
    )
    p.add_argument(
        "--skip-proxy", action="store_true",
        help="Don't run the supervisor-management-proxy regeneration phase.",
    )
    p.add_argument(
        "--supervisor-cluster", default=None,
        help="Substring to match against the cluster id reported by "
             "decryptK8Pwd.py (e.g. 'domain-c10'). When a vCenter hosts more "
             "than one Supervisor cluster, this restricts the proxy fix to "
             "the matching cluster. Substring match is case-sensitive.",
    )
    p.add_argument(
        "--supervisor-ip", default=None,
        help="Override the Supervisor control-plane IP. Skips both the "
             "decryptK8Pwd VIP and the VPX-DB fallback. Use when you've "
             "already verified the right node IP yourself.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show every action that would be taken without changing state.",
    )
    return p.parse_args()


def main():
    args = parse_args()
    password = resolve_password(args)

    banner("Supervisor Cert Rotation Remediation")
    log(f"target upstream domain : {args.target_domain}")
    log(f"dry-run                : {args.dry_run}")
    _active = " ".join(filter(None, [
        "" if args.skip_vcenter_proxy else "vcenter-proxy",
        "" if args.skip_content_lib else "content-lib",
        "" if args.skip_proxy else "proxy",
    ]))
    log(f"phases                 : {_active or '(none!)'}")

    if args.skip_vcenter_proxy and args.skip_content_lib and args.skip_proxy:
        fail("All phases skipped - nothing to do.")

    for tool in ("openssl", "sshpass", "expect"):
        ensure_tool(tool)
    if not args.skip_content_lib:
        ensure_govc()

    vcenters = load_vcenters(args.config)
    log(f"vcenters               : "
        f"{', '.join(v['label'] for v in vcenters)}")

    failures = []

    # ---- Phase 0: vCenter proxy configuration ---------------------------
    if not args.skip_vcenter_proxy:
        banner("Phase 0: vCenter proxy configuration")
        for vc in vcenters:
            ok = apply_proxy_to_vcenter(vc, password, args.dry_run)
            if not ok:
                failures.append(f"vcenter-proxy:{vc['label']}")

    # ---- Phase 1: Content Library trust refresh -------------------------
    if not args.skip_content_lib:
        banner("Phase 1: Content Library trust refresh")
        if args.auto:
            log("auto mode: will discover upstreams from each subscribed "
                "library and refresh per-library.")
            pem, thumbprint = None, None
        else:
            pem, thumbprint = fetch_upstream_cert(args.target_domain)
        for vc in vcenters:
            ok = fix_content_library_trust(
                vc, password, args.target_domain, args.auto, pem, thumbprint,
                args.dry_run,
            )
            if not ok:
                failures.append(f"content-lib:{vc['label']}")

    # ---- Phase 2: Supervisor control plane stabilization -----------------------
    if not args.skip_proxy:
        banner("Phase 2: Supervisor control plane stabilization")
        # We try all vCenters, the function will gracefully skip if no Supervisor is found
        proxy_targets = vcenters
        for vc in proxy_targets:
            ok = fix_supervisor_control_plane(
                vc, password, args.dry_run,
                only_cluster=args.supervisor_cluster,
                only_ip=args.supervisor_ip,
            )
            if not ok:
                failures.append(f"proxy:{vc['label']}")

    banner("Summary")
    if failures:
        for f_name in failures:
            log(f"  FAILED: {f_name}", level="ERROR")
        log(f"{len(failures)} step(s) failed - inspect log above.",
            level="ERROR")
        return 2
    log("All requested phases completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
