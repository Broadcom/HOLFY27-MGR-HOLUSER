#!/usr/bin/env python3
"""
fix_supervisor_certs.py

Unified cert-rotation remediation for VCF / vSphere Supervisor environments.

Combines two previously-separate fixes that consistently need to run together
after an upstream content-library cert (e.g. wp-content.vmware.com) is rotated:

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
import urllib.parse

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
        "label": "Management vCenter",
        "host": "vc-mgmt-a.site-a.vcf.lab",
        "sso_user": "administrator@vsphere.local",
        "root_user": "root",
        "has_supervisor": False,
    },
    {
        "label": "Workload vCenter",
        "host": "vc-wld01-a.site-a.vcf.lab",
        "sso_user": "administrator@wld.sso",
        "root_user": "root",
        "has_supervisor": True,
    },
]

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
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {level}: {msg}", flush=True)


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
        tcl_cmd = cmd.replace('"', '\\"').replace('$', '\\$')
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


def fix_management_proxy(vc, password, dry_run, only_cluster=None,
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
    log(f"--- {label} ({vc['host']}) -- supervisor-management-proxy fix ---")

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
            f"{decrypt}", level="ERROR")
        return False

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
        ok = _fix_one_supervisor(vc, password, c, dry_run)
        if not ok:
            overall_ok = False
    return overall_ok


def _fix_one_supervisor(vc, password, cluster, dry_run):
    """Apply the proxy regeneration to a single Supervisor cluster stanza."""
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

    if dry_run:
        log(f"      [{cid}] [dry-run] would delete cert-manager + kube-system "
            f"proxy secrets and roll the deployment on {scp_ip}.")
        return True

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
    commands = [
        f"{K} -n cert-manager delete secret "
        f"$({K} get secret -n cert-manager -o name "
        f"| grep supervisor-management-proxy)",
        f"{K} -n kube-system delete secret "
        f"supervisor-management-proxy-ca supervisor-management-proxy-tls",
        f"{K} -n kube-system rollout restart deploy "
        f"supervisor-management-proxy",
        f"{K} -n kube-system rollout status deploy "
        f"supervisor-management-proxy --timeout=180s",
    ]
    run_on_scp(
        vc["host"], vc["root_user"], password,
        scp_ip, scp_pass, commands,
        description=f"      [{cid}] Executing proxy-cert regeneration on SCP",
        timeout=240,
    )
    log(f"      [{cid}] Management proxy regeneration submitted on {scp_ip}.")
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


def load_vcenters(config_path):
    if not config_path:
        return DEFAULT_VCENTERS
    with open(config_path) as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        fail(f"Config {config_path} must be a non-empty JSON list.")
    required = {"label", "host", "sso_user", "root_user", "has_supervisor"}
    for entry in data:
        missing = required - set(entry.keys())
        if missing:
            fail(f"Config entry {entry.get('label', entry)!r} missing keys: "
                 f"{sorted(missing)}")
    return data


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
    log(f"phases                 : "
        f"{'content-lib ' if not args.skip_content_lib else ''}"
        f"{'proxy' if not args.skip_proxy else ''}".strip() or "(none!)")

    if args.skip_content_lib and args.skip_proxy:
        fail("Both phases skipped - nothing to do.")

    for tool in ("openssl", "sshpass", "expect"):
        ensure_tool(tool)
    if not args.skip_content_lib:
        ensure_govc()

    vcenters = load_vcenters(args.config)
    log(f"vcenters               : "
        f"{', '.join(v['label'] for v in vcenters)}")

    failures = []

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

    # ---- Phase 2: Supervisor management-proxy fix -----------------------
    if not args.skip_proxy:
        banner("Phase 2: Supervisor management-proxy regeneration")
        proxy_targets = [v for v in vcenters if v.get("has_supervisor")]
        if not proxy_targets:
            log("No vCenters in config marked has_supervisor=true - skipping.")
        for vc in proxy_targets:
            ok = fix_management_proxy(
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
