#!/usr/bin/env python3
"""
vsp_cert_renewer.py
Version 2.2 - 2026-06-10
Author: Burke Azbill, Kevin Tebear, and HOL Core Team

Proactive Kubernetes certificate check and renewal for VSP and VCFA clusters.

Runs at every lab startup (called by VCFfinal.py Task 2e before component
scale-up). Non-fatal — exceptions are caught per-phase so a failure in one
phase never aborts the others or the boot sequence.

Threshold: THRESHOLD_DAYS (60). Any cert expiring within 60 days is renewed
to 5 years via kubeadm --config certificateValidityPeriod (confirmed valid on
VMware kubeadm v1.34.2). Falls back to default 1-year renewal on older builds.

PHASE EXECUTION ORDER (v2.0):

  All CA-authority work runs FIRST for VSP, before any leaf-cert operations.
  This guarantees that every certificate issued or renewed in the same run is
  signed by the most current CA key pair.

  ┌─ VSP ONLY — CA / pre-check ───────────────────────────────────────────────┐
  │  3.0  Cluster CA extension   extend vcf-cluster-ca + vcf-external-cluster  │
  │                              ca-cert to 10 years if < 1 year remaining.    │
  │                              Returns ca_rotated=True when the CA key pair  │
  │                              is replaced (new Secret issued by cert-mgr).  │
  │                              ca_rotated propagates to Phase 3.1 force_all. │
  │                                                                             │
  │  Pre   CA key consistency    openssl verify registry-certificate against   │
  │        check                 current vcf-cluster-ca.  Detects the cross-   │
  │                              session failure where Phase 3.0 ran in a prior │
  │                              boot, Phase 3.1 then missed some certs, and   │
  │                              the certs appear valid-by-date but carry the  │
  │                              old CA key.  Sets ca_key_mismatch=True, which │
  │                              also propagates to Phase 3.1 force_all.       │
  │                                                                             │
  │  3.2  trust-manager +         When ca_rotated OR ca_key_mismatch:           │
  │       vmsp-operator sync     (a) Restarts trust-manager Deployment, deletes │
  │                              platform-trust ConfigMaps in key namespaces so  │
  │                              trust-manager recreates them with the new CA   │
  │                              cert. Fixes ECDSA verification failures when   │
  │                              the vmsp-operator bundle controller pushes     │
  │                              images to zot-1 (Stage VCF services).          │
  │                              (b) Restarts vmsp-operator Deployment because  │
  │                              its Go binary caches TLS root CAs from startup │
  │                              and does NOT auto-reload updated ConfigMap      │
  │                              volumes — restart forces fresh CA load.        │
  │                              Must run BEFORE Phase 3.1 (leaf cert issuance) │
  │                              so bundles are current before new certs appear.│
  └───────────────────────────────────────────────────────────────────────────┘
  ┌─ ALL CLUSTERS — leaf certs against now-current CA ────────────────────────┐
  │  1.   kubeadm CP certs       API server, etcd, front-proxy certs on CP.   │
  │                              Uses the kubeadm PKI CA (/etc/kubernetes/pki) │
  │                              which is separate from vcf-cluster-ca.        │
  │  2.   Kubelet serving certs  Per-node kubelet.crt via KCM CSR signing.    │
  │                              Also uses kubeadm PKI CA (same CA as above). │
  └───────────────────────────────────────────────────────────────────────────┘
  ┌─ VSP ONLY — cert-manager + containerd ────────────────────────────────────┐
  │  3.1  cert-manager leaf      Renew certs that are not-Ready OR expiring   │
  │                              within threshold_days.  Runs with force_all   │
  │                              when ca_rotated OR ca_key_mismatch is True —  │
  │                              all vcf-cluster-issuer certs are force-signed │
  │                              with the verified current CA key.             │
  │  4.   Antrea controller TLS  Self-signed 5-year cert injected into Secret. │
  │  5.   containerd CA sync     Node CA file sync + pod restarts.            │
  │                              Step 1b runs as a SAFETY NET: re-verifies     │
  │                              registry-cert against CA after Phase 3.1.    │
  │                              Triggers pod restarts when leaf_certs_renewed │
  │                              OR leaf_certs_prerenewed (Phase 3.1 force)   │
  │                              OR any node CA file was updated.              │
  └───────────────────────────────────────────────────────────────────────────┘

Log format per cert:
  CHECK  : <name> — EXPIRES: <date> — RESIDUAL: <Nd>
  ACTION : <description>
  RENEWED: <name> — NEW EXPIRY: <date>
  SKIP   : <name> — valid for ><N>d
  WARN   : <description>
  ERROR  : <description>

v2.2 Changes:
- Phase 3.2 extended: vmsp-operator Deployment is now also restarted (Steps 5-6)
  after trust-manager recreates the platform-trust ConfigMaps.  Root cause: the
  vmsp-operator bundle controller is responsible for downloading component tarballs
  and pushing OCI images to the zot-1 registry.  Its Go binary caches TLS root CAs
  at startup and does NOT auto-reload updated ConfigMap volumes at runtime.  Without
  this restart, even after trust-manager fixes the ConfigMaps, the vmsp-operator
  continues using the stale in-memory CA pool and fails with "ECDSA verification
  failure" when pushing images to zot-1.  The restart ensures it starts with the
  correct CA pool, allowing Stage VCF services runtime to succeed on the first try.

v2.1 Changes:
- New Phase 3.2 (_phase3_sync_trust_manager): after a CA rotation (ca_rotated) OR
  a cross-session CA key mismatch (ca_key_mismatch), trust-manager is restarted and
  all platform-trust ConfigMaps in key namespaces (vmsp-platform, vcf-fleet-depot,
  vcf-fleet-lcm, ops-logs) are deleted so trust-manager recreates them with the new
  CA cert included.  Root cause: trust-manager's Kubernetes informer misses the
  delete+create cycle of vcf-cluster-ca-secret during CA rotation, leaving stale CA
  data in platform-trust ConfigMaps for up to 19h.  The fleet-depot pod uses
  platform-trust to verify TLS connections to the zot-1 internal registry — a stale
  bundle causes every stage-component image push to fail with "ECDSA verification
  failure", blocking Log management and other VCF component installations.  Phase 3.2
  runs BEFORE Phase 3.1 (leaf cert renewal) so bundles are correct before new certs
  are issued.

v2.0 Changes:
- Phase ordering corrected: ALL CA-authority operations now execute BEFORE any
  leaf-cert renewal.  The previous ordering (Phase 1 kubeadm → Phase 2 kubelet
  → Phase 3.0 CA extension → Phase 3.1 leaf certs) left a window where Phases
  1 and 2 could renew certs against a CA that was about to be rotated by Phase
  3.0.  Although Phases 1/2 use the kubeadm PKI CA (separate from vcf-cluster-ca),
  the correct architectural principle is: CAs current before ANY cert issuance.
  New order: Phase 3.0 (CA) → CA pre-check → Phase 1 → Phase 2 → Phase 3.1 →
             Phase 4 → Phase 5.
- New _check_ca_key_consistency() pre-check: runs between Phase 3.0 and Phase 1.
  Calls openssl verify on registry-certificate against vcf-cluster-ca BEFORE
  Phase 3.1 runs.  When a cross-session CA key mismatch is detected (CA rotated
  in a prior boot, leaf certs appear valid-by-date but carry the old key),
  ca_key_mismatch=True is set and propagated to Phase 3.1 as force_all=True,
  so cert-manager re-signs all vcf-cluster-issuer certs in Phase 3.1 rather
  than deferring to Phase 5's post-Phase-3.1 safety net.
- _phase5_casync() gains leaf_certs_prerenewed parameter.  When Phase 3.1
  already force-renewed due to ca_rotated or ca_key_mismatch, Phase 5 carries
  that signal for the pod-restart decision in Step 4.  Phase 5 Step 1b still
  runs as a final safety net; pod restarts now trigger when EITHER Step 1b OR
  leaf_certs_prerenewed is True OR node CA files were updated.

v1.9 Changes:
- Phase 5 (casync): Detects and repairs two independent failure modes that both
  cause x509 ECDSA verification failures, ImagePullBackOff, and 'Stage VCF
  services runtime' errors when installing VCF components.
  Mode A (CA file stale): containerd reads its registry CA trust from a raw
  file (/etc/containerd/certs.d/.../ca.crt) written once at node deploy time
  and never updated by prior startup scripts.  Phase 5 compares that file to
  vcf-cluster-ca-secret ca.crt across all nodes and pushes the current CA
  via inline base64 echo (no SCP), restarting containerd only on stale nodes.
  Mode B (leaf certs signed by old CA key): cert-manager leaf certs may have
  been issued BEFORE the current CA key pair was created — i.e., Phase 3.0
  rotated the CA key in a previous session, but Phase 3.1 force-renewal either
  raced with cert-manager's cache or was skipped in subsequent boots (certs
  appear Ready with >60d remaining).  Phase 5 runs openssl verify on the CP
  node to compare registry-certificate against the current CA cert.  On
  failure, it deletes all vcf-cluster-issuer backed Secrets (30 certs across
  6 namespaces) so cert-manager re-signs them with the current key, then waits
  up to 120s for all certs to be Ready.
  In both modes, zot-1-0, metadata-service, and vmsp-identity are restarted
  after any fix so they reload updated certs.  VSP only.  --skip-casync bypasses.

v1.8 Changes:
- THRESHOLD_DAYS lowered from 365 (1 year) to 60 days.  Some certs cannot be
  issued for more than 1 year; a 365-day threshold caused those certs to be
  renewed on every single VCFfinal.py run.  60-day threshold matches standard
  PKI practice — proactive without excessive churn.  Applies to all phases
  (kubeadm, kubelet, cert-manager, Antrea) for both VSP and VCFA clusters.

v1.7 Changes:
- Phase 3.0: lowered CA_MIN_REMAINING_H from 43830h (5y) to 8760h (1y).
  The VCF operator enforces spec.duration=27740h (~3.17y) on the vcf-cluster-ca
  and continuously reverts our spec.duration patch.  With the 5y threshold, Phase
  3.0 triggered on EVERY boot of a fresh template deployment (CA always ~3y left
  < 5y threshold), generating a new CA key pair each time and breaking all leaf
  certs.  The 1y threshold means Phase 3.0 only fires when the CA is genuinely
  near expiry.
- Phase 3.0: _phase3_extend_ca() now returns True if any CA was actually rotated.
- Phase 3.1: _phase3_certmanager() gains a force_all parameter.  When the CA
  was rotated (return value from Phase 3.0), force_all=True forces immediate
  renewal of ALL leaf certs regardless of their notAfter date — required because
  the new CA has a new key pair and all previously-issued leaf certs are now
  cryptographically broken even if their notAfter is years away.
- _check_cluster(): wires Phase 3.0 rotated flag → Phase 3.1 force_all, with
  a WARN log explaining why all certs are being renewed.

v1.6 Changes:
- Phase 4 (Antrea): instead of deleting the Secret and letting Antrea
  regenerate its hardcoded 1-year cert, now generates a 5-year self-signed
  cert on the CP node (openssl -addext) and pre-injects it into the Secret
  before restarting the controller.  Antrea finds the valid Secret on start-up
  and uses it without regeneration.  Falls back to the original delete+restart
  path if openssl on the node does not support -addext (openssl < 1.1.1).
- Fixed Phase 4 double-warning bug: range(0, 121, 15) caused the "not Ready"
  warning to fire twice (at elapsed=105 and elapsed=120).  Changed to
  range(0, 120, 15) with a post-loop flag check so the warning fires once.

Usage:
  python3 vsp_cert_renewer.py --cluster vsp|vcfa|all
                               [--threshold-days 60]
                               [--dry-run]
                               [--skip-kubeadm] [--skip-kubelet]
                               [--skip-certmanager] [--skip-antrea]
                               [--skip-casync]
"""

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

# ─── Version ──────────────────────────────────────────────────────────────────
VERSION = "2.2"
DATE    = "2026-06-10"

# ─── Global constants ─────────────────────────────────────────────────────────
THRESHOLD_DAYS = 60            # renew if any cert expires within 60 days
THRESHOLD_SEC  = THRESHOLD_DAYS * 86400
CERT_VALIDITY  = "43830h0m0s"  # 5 years via kubeadm certificateValidityPeriod
CREDS_FILE     = "/home/holuser/creds.txt"
VSP_USER       = "vmware-system-user"

# Phase 5: containerd CA trust sync
CONTAINERD_CA_PATH = (
    '/etc/containerd/certs.d/'
    'registry.vmsp-platform.svc.cluster.local:5000/ca.crt'
)
CA_SECRET_NAME = 'vcf-cluster-ca-secret'
CA_SECRET_NS   = 'vmsp-platform'
ZOT_POD        = 'zot-1-0'
ZOT_NS         = 'vmsp-platform'

# ─── Cluster registry ─────────────────────────────────────────────────────────
# worker_fqdn:    DNS name of any VSP worker node — used to discover the CP IP
#                 by reading the kubeconfig from the worker.
# fqdn:           FQDN for VCFA node discovery (DNS → SSH port-22 probe first).
# candidate_ips:  Fallback IPs tried for SSH if DNS resolution fails or the
#                 resolved IP is not yet SSH-reachable at boot time.
#                 NOTE: 10.1.1.70 is the kube-vip *gateway* VIP (HTTP/Envoy
#                 only) — never SSH-reachable.  10.1.1.72 is the K8s API VIP.
#                 10.1.1.73 is the actual VCFA node VM IP used by vcfa-stabilizer.
# phases:         which phases to run for this cluster type.
CLUSTERS = {
    "vsp": {
        "label":          "VSP",
        "worker_fqdn":    "vsp-01a.site-a.vcf.lab",
        "phases":         ["kubeadm", "kubelet", "extendca", "certmanager", "antrea", "casync"],
        # VSP workers use serverTLSBootstrap: true — patch KCM --cluster-signing-duration
        # before Phase 2 so newly-signed kubelet CSRs get CERT_VALIDITY (5 years)
        # instead of the default 1-year duration.
        "fix_kcm_duration": True,
    },
    "vcfa": {
        "label":          "VCFA",
        "fqdn":           "auto-a.site-a.vcf.lab",
        "candidate_ips":  ["10.1.1.71", "10.1.1.72", "10.1.1.73", "10.1.1.74"],
        "phases":         ["kubeadm", "kubelet"],
    },
}

# ─── Logging ──────────────────────────────────────────────────────────────────
_LOG_LABEL      = ""
_SHOW_TIMESTAMPS = True   # set False via --no-timestamps when called by VCFfinal.py


def _log(tag, msg):
    label  = f"[{_LOG_LABEL}] " if _LOG_LABEL else ""
    if _SHOW_TIMESTAMPS:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {label}{tag}: {msg}", flush=True)
    else:
        print(f"{label}{tag}: {msg}", flush=True)


def log_check(msg):    _log("CHECK  ", msg)
def log_action(msg):   _log("ACTION ", msg)
def log_renewed(msg):  _log("RENEWED", msg)
def log_skip(msg):     _log("SKIP   ", msg)
def log_warn(msg):     _log("WARN   ", msg)
def log_error(msg):    _log("ERROR  ", msg)
def log_info(msg):     _log("INFO   ", msg)
def log_sep(width=70): print("=" * width, flush=True)


# ─── Password helper ──────────────────────────────────────────────────────────
_cached_password = None


def _get_password():
    global _cached_password
    if _cached_password is None:
        try:
            with open(CREDS_FILE) as f:
                _cached_password = f.read().strip()
        except OSError as e:
            log_error(f"Cannot read credentials file {CREDS_FILE}: {e}")
            sys.exit(1)
    return _cached_password


# ─── SSH helper ───────────────────────────────────────────────────────────────
def _ssh_exec(ip, password, cmd, timeout=60):
    """Run cmd as root on ip via vmware-system-user + sudo.

    Encodes the inner command in base64 so special characters in the command
    body (pipes, quotes, dollar signs) are never interpreted by intermediate
    shell layers.  Returns (returncode, output_str).  Never raises.
    """
    cmd_b64 = base64.b64encode(cmd.encode()).decode()
    # The outer command is a simple shell snippet: decode cmd_b64 and run as root
    outer = (
        f"echo '{password}' | sudo -S -i "
        f"bash -c \"$(echo {cmd_b64} | base64 -d)\" 2>&1"
    )
    try:
        result = subprocess.run(
            [
                "sshpass", "-p", password,
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=15",
                f"{VSP_USER}@{ip}",
                outer,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        # Filter/strip sudo and SSH noise.
        # NOTE: the sudo password prompt (no trailing newline) is often
        # concatenated onto the same line as the first line of real output,
        # e.g. "[sudo] password for vmware-system-user:     server: https://..."
        # We must strip the prefix rather than drop the whole line.
        _SUDO_PROMPT_RE = re.compile(
            r"(?:\[sudo\] password for [^:]+:\s*|sudo\] password\s*)"
        )
        _NOISE_LINES = ("Welcome to Photon", "Warning: Permanently added",
                        "Connection to ", "Killed by signal")
        filtered = []
        for line in combined.splitlines():
            # Strip sudo prompt prefix if present on this line
            line = _SUDO_PROMPT_RE.sub("", line)
            # Drop lines that are pure noise after stripping
            if any(noise in line for noise in _NOISE_LINES):
                continue
            filtered.append(line)
        return result.returncode, "\n".join(filtered).strip()
    except subprocess.TimeoutExpired:
        return 1, f"SSH timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "sshpass not found — install sshpass"
    except Exception as exc:
        return 1, f"SSH error: {exc}"


# ─── Kubeadm output parser ────────────────────────────────────────────────────
_RESIDUAL_RE = re.compile(r'^>?(\d+)([dyhm])$', re.I)


def _parse_residual_days(residual_str):
    """Convert kubeadm residual-time string to integer days.

    '295d' → 295, '9y' → 3285, '>9y' → 9999 (safe, no renewal needed),
    'MISSING' / 'EXPIRED' → 0 (immediate renewal needed).
    Unknown format → 9999 (treat as safe, don't accidentally renew).
    """
    s = residual_str.strip().lstrip('>')
    if not s or s.upper() in ('MISSING', 'EXPIRED', 'INVALID'):
        return 0
    m = _RESIDUAL_RE.match(s)
    if not m:
        return 9999
    value, unit = int(m.group(1)), m.group(2).lower()
    if residual_str.strip().startswith('>'):
        return 9999  # ">9y" — well beyond threshold
    if unit == 'd': return value
    if unit == 'y': return value * 365
    if unit == 'h': return max(value // 24, 0)
    if unit == 'm': return 0  # minutes — essentially expired
    return 9999


def _parse_kubeadm_expiry(output):
    """Parse the tabular section of 'kubeadm certs check-expiration' output.

    Returns a list of dicts:
      { 'name': str, 'expires': str, 'residual': str,
        'residual_days': int, 'is_ca': bool }

    Only leaf cert rows and CA rows are returned — header and log lines
    are discarded.
    """
    certs       = []
    in_cert_sec = False
    in_ca_sec   = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Section headers.
        # Leaf cert header: "CERTIFICATE                EXPIRES  RESIDUAL TIME   CERTIFICATE AUTHORITY ..."
        #   -> starts with CERTIFICATE then 5+ spaces before EXPIRES.
        # CA cert header:   "CERTIFICATE AUTHORITY   EXPIRES  RESIDUAL TIME   EXTERNALLY MANAGED"
        #   -> starts with "CERTIFICATE AUTHORITY" (single space, then AUTHORITY).
        # We must check the CA header first to avoid misclassifying it.
        if re.match(r'^CERTIFICATE AUTHORITY\s+EXPIRES', line):
            in_cert_sec = False
            in_ca_sec   = True
            continue
        if re.match(r'^CERTIFICATE\s{5,}EXPIRES', line):
            in_cert_sec = True
            in_ca_sec   = False
            continue
        # Skip kubeadm log lines, warnings, errors
        if re.match(r'^\[|^W\d|^E\d|^I\d|^error|^Error', line):
            continue

        if in_cert_sec or in_ca_sec:
            parts = line.split()
            # Row format: name  Month  DD,  YYYY  HH:MM  UTC  residual  [ca  managed]
            # That is: parts[0]=name, parts[1:6]=expires, parts[6]=residual
            if len(parts) < 7:
                continue
            name     = parts[0]
            # Skip separator lines or column headers repeated
            if re.match(r'^[-=]+$', name) or name.upper() == name:
                continue
            expires  = " ".join(parts[1:6])   # "Mar 13, 2027 18:20 UTC"
            residual = parts[6]
            certs.append({
                "name":          name,
                "expires":       expires,
                "residual":      residual,
                "residual_days": _parse_residual_days(residual),
                "is_ca":         in_ca_sec,
            })
    return certs


# ─── Kubeconfig probe ─────────────────────────────────────────────────────────
def _probe_kubeconfig(node_ip, password):
    """Return the best kubeconfig path on node_ip.

    Tries /etc/kubernetes/super-admin.conf first (preferred — unencrypted,
    available on VCF 9.x), falls back to /etc/kubernetes/admin.conf.
    Returns the path string, or None if neither is readable.
    """
    for path in (
        "/etc/kubernetes/super-admin.conf",
        "/etc/kubernetes/admin.conf",
    ):
        rc, out = _ssh_exec(node_ip, password,
                            f"test -r {path} && echo EXISTS || echo MISSING")
        if rc == 0 and "EXISTS" in out:
            return path
    return None


# ─── TCP port probe helper ────────────────────────────────────────────────────
def _test_tcp_port(ip, port=22, timeout=5):
    """Return True if *ip*:*port* accepts a TCP connection within *timeout* s."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


# ─── Cluster CP discovery ─────────────────────────────────────────────────────
def _discover_cp_ip(cluster_cfg, password):
    """Return the SSH-reachable node IP for this cluster.

    For VCFA (fqdn + candidate_ips):
      1. Resolve fqdn via DNS; if the resolved IP answers SSH port 22, use it.
      2. Fall back to probing each candidate_ip in order for SSH port 22.
      This mirrors the pattern used by watchvcfa.sh (DNS → nc -z port 22) and
      VCFfinal.py Task 4b (candidate sweep).  NOTE: 10.1.1.70 is the kube-vip
      *gateway* VIP (HTTP/Envoy) and is never SSH-reachable — it is deliberately
      excluded from candidate_ips.  The actual VM node IP is typically 10.1.1.73
      (VCF 9.1.x) as used by vcfa-stabilizer.sh.

    For VSP (worker_fqdn):
      Resolves the worker FQDN → worker IP → reads the 'server:' line from
      /etc/kubernetes/node-agent.conf (or kubelet.conf as fallback) to extract
      the kube-vip CP VIP.  SSH to the VIP reaches the physical CP node.

    Note: VSP worker nodes have node-agent.conf but NOT super-admin.conf or
    admin.conf — those only exist on the control-plane node.  Kubeconfig is
    probed separately in _check_cluster() once the CP IP is known.
    """
    label = cluster_cfg["label"]

    # ── VCFA: dynamic discovery, never use a hardcoded VIP ───────────────────
    if "fqdn" in cluster_cfg:
        fqdn           = cluster_cfg["fqdn"]
        candidate_ips  = cluster_cfg.get("candidate_ips", [])

        # Step 1: DNS resolution
        resolved_ip = None
        try:
            resolved_ip = socket.gethostbyname(fqdn)
            log_info(f"DNS {fqdn} → {resolved_ip}")
        except socket.gaierror as exc:
            log_warn(f"DNS resolution failed for {fqdn}: {exc} — trying candidates")

        # Step 2: check if DNS-resolved IP has SSH open
        if resolved_ip and _test_tcp_port(resolved_ip, 22, timeout=5):
            log_info(f"SSH reachable at DNS-resolved IP {resolved_ip}")
            return resolved_ip
        elif resolved_ip:
            log_warn(
                f"DNS-resolved {resolved_ip} not SSH-reachable "
                f"(VIP may not be up yet) — probing candidates"
            )

        # Step 3: probe each candidate IP for SSH port 22
        for ip in candidate_ips:
            if _test_tcp_port(ip, 22, timeout=5):
                log_info(f"SSH reachable at candidate IP {ip}")
                return ip
            log_info(f"Candidate {ip}: SSH port 22 not reachable — skipping")

        log_error(
            f"No SSH-reachable IP found for VCFA "
            f"(tried DNS + candidates {candidate_ips})"
        )
        return None

    # ── VSP: resolve worker FQDN, then extract CP VIP from kubeconfig ────────
    worker_fqdn = cluster_cfg.get("worker_fqdn", "")
    try:
        worker_ip = socket.gethostbyname(worker_fqdn)
    except socket.gaierror as exc:
        log_error(f"DNS resolution failed for {worker_fqdn}: {exc}")
        return None

    log_info(f"Worker {worker_fqdn} → {worker_ip}")

    # Read the K8s API server URL from the worker's node-agent config.
    # node-agent.conf is the kubeconfig used by the spherelet agent on VSP nodes.
    # It points to the CP VIP (kube-vip), e.g. "server: https://10.1.1.142:6443".
    for cfg_path in (
        "/etc/kubernetes/node-agent.conf",
        "/etc/kubernetes/kubelet.conf",
    ):
        rc, out = _ssh_exec(
            worker_ip, password,
            f"grep 'server:' {cfg_path} 2>/dev/null | head -1",
            timeout=15,
        )
        if rc == 0 and "server:" in out:
            m = re.search(r'https?://([0-9.]+):', out)
            if m:
                cp_vip = m.group(1)
                log_info(f"CP VIP (from {cfg_path}): {cp_vip}")
                return cp_vip

    log_error(
        f"Cannot find 'server:' in node-agent.conf or kubelet.conf "
        f"on worker {worker_ip}"
    )
    return None


# ─── Phase 1: kubeadm control-plane cert check/renewal ───────────────────────
def _phase1_kubeadm(cluster_cfg, cp_ip, password, kubeconfig, dry_run, threshold_days):
    """Check and renew kubeadm control-plane certs on the CP node.

    Logs CHECK for every cert.  If any leaf cert has residual_days < threshold,
    renews all certs to 5 years (with 1-year fallback), restarts control-plane
    pods, then re-verifies.  CA certs are only checked, never renewed.
    """
    label = cluster_cfg["label"]
    log_sep()
    log_info(f"Phase 1: kubeadm control-plane cert check/renewal")
    log_info(f"  Threshold: {threshold_days}d | Target: {CERT_VALIDITY} (5 years)")

    # ── Step 1: check-expiration ──────────────────────────────────────────────
    rc, out = _ssh_exec(cp_ip, password, "kubeadm certs check-expiration 2>&1", timeout=60)
    if rc != 0 or not out.strip():
        log_error(f"kubeadm certs check-expiration failed (rc={rc}): {out[:300]}")
        return

    certs = _parse_kubeadm_expiry(out)
    if not certs:
        log_warn(f"No certs parsed from check-expiration output")
        log_warn(f"  Raw output: {out[:500]}")
        return

    needs_renewal   = False
    renewal_trigger = []
    for c in certs:
        tag = "CA  " if c["is_ca"] else "CERT"
        log_check(
            f"{tag} {c['name']:40s} — "
            f"EXPIRES: {c['expires']:25s} — RESIDUAL: {c['residual']}"
        )
        if not c["is_ca"] and c["residual_days"] < threshold_days:
            needs_renewal = True
            renewal_trigger.append(c["name"])

    if not needs_renewal:
        log_skip(f"All kubeadm certs valid for >{threshold_days}d — no action needed")
        return

    log_action(
        f"{len(renewal_trigger)} cert(s) expire within {threshold_days}d: "
        f"{', '.join(renewal_trigger)}"
    )
    log_action(f"Renewing all kubeadm certs to 5 years ({CERT_VALIDITY})...")

    if dry_run:
        log_info(f"[dry-run] would run: kubeadm certs renew all --config /tmp/kubeadm-renew.yaml")
        return

    # ── Step 2: write the 5-year config and renew ────────────────────────────
    yaml_content = (
        f"apiVersion: kubeadm.k8s.io/v1beta4\n"
        f"kind: ClusterConfiguration\n"
        f"certificateValidityPeriod: {CERT_VALIDITY}\n"
    )
    yaml_b64 = base64.b64encode(yaml_content.encode()).decode()

    write_rc, write_out = _ssh_exec(
        cp_ip, password,
        f"echo {yaml_b64} | base64 -d > /tmp/kubeadm-renew.yaml && echo OK",
        timeout=15,
    )
    if write_rc != 0 or "OK" not in write_out:
        log_warn(f"Could not write kubeadm config ({write_out[:200]}), using default renewal")
        renew_cmd = "kubeadm certs renew all 2>&1"
    else:
        renew_cmd = "kubeadm certs renew all --config /tmp/kubeadm-renew.yaml 2>&1"

    log_action(f"Running: {renew_cmd.rstrip(' 2>&1')}")
    rc, out = _ssh_exec(cp_ip, password, renew_cmd, timeout=120)

    if rc != 0:
        # Fallback: some older builds don't support certificateValidityPeriod in renew
        log_warn(f"5-year renewal returned rc={rc}: {out[:300]}")
        log_warn(f"Falling back to default 1-year renewal")
        rc, out = _ssh_exec(cp_ip, password, "kubeadm certs renew all 2>&1", timeout=120)
        if rc != 0:
            log_error(f"kubeadm certs renew all failed (rc={rc}): {out[:400]}")
            _ssh_exec(cp_ip, password, "rm -f /tmp/kubeadm-renew.yaml", timeout=10)
            return
        log_action(f"Default 1-year renewal succeeded")
    else:
        log_action(f"5-year renewal command completed")

    _ssh_exec(cp_ip, password, "rm -f /tmp/kubeadm-renew.yaml", timeout=10)

    # ── Step 3: restart control-plane static pods ─────────────────────────────
    # Delete control-plane pods so kubelet reloads them from disk with new certs.
    # The delete itself may briefly lose the API server connection — that's normal.
    log_action(f"Restarting control-plane static pods to load new certs...")
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""

    # Use a short timeout: the command succeeds even if API briefly goes down
    _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} delete pod -n kube-system "
        f"-l tier=control-plane --grace-period=0 2>/dev/null || true",
        timeout=30,
    )

    # Wait up to 120s for control-plane pods to come back
    log_action(f"Waiting up to 120s for control-plane pods to recover...")
    for elapsed in range(0, 121, 10):
        time.sleep(10)
        rc_w, pods_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get pods -n kube-system "
            f"-l tier=control-plane --no-headers 2>/dev/null | "
            f"awk '{{split($2,a,\"/\"); if (a[1]!=a[2]) print $1}}'",
            timeout=20,
        )
        if rc_w == 0 and not pods_out.strip():
            log_info(f"Control-plane pods are Ready ({elapsed + 10}s)")
            break
        if elapsed >= 110:
            log_warn(f"Control-plane pods not fully ready after 120s — continuing")

    # ── Step 4: verify new expiry dates ──────────────────────────────────────
    log_action(f"Verifying renewed cert expiry dates...")
    rc, out = _ssh_exec(cp_ip, password, "kubeadm certs check-expiration 2>&1", timeout=60)
    if rc != 0:
        log_warn(f"Could not re-check cert expiry after renewal: {out[:200]}")
        return

    renewed_certs = _parse_kubeadm_expiry(out)
    for c in renewed_certs:
        if not c["is_ca"]:
            log_renewed(
                f"{c['name']:40s} — NEW EXPIRY: {c['expires']:25s} — "
                f"RESIDUAL: {c['residual']}"
            )


# ─── Phase 2 helper: ensure kube-controller-manager uses 5-year signing duration ──
# Kubelet serving certs on VSP worker nodes are signed via the K8s CSR API
# (serverTLSBootstrap: true).  Duration is set by --cluster-signing-duration on
# kube-controller-manager (default 8760h = 1 year).  We patch the static pod
# manifest on the CP node before triggering kubelet cert renewal so the new
# certs get the full CERT_VALIDITY (5-year) duration.
_KCM_DURATION = "43830h"   # 5 years, matches CERT_VALIDITY
_KCM_MANIFEST = "/etc/kubernetes/manifests/kube-controller-manager.yaml"


def _ensure_kcm_signing_duration(cp_ip, password, kubeconfig, dry_run):
    """Patch kube-controller-manager --cluster-signing-duration to _KCM_DURATION.

    Checks the static pod manifest on the CP node.  If the flag is already set
    to the target duration, returns False immediately.  Otherwise patches the
    manifest and waits up to 60s for the KCM pod to restart and become Running.

    Returns True if the manifest was patched (restart triggered), False if it
    was already correct or patching was skipped (dry-run / error).
    """
    # Check current state with a single grep
    rc_c, cur_out = _ssh_exec(
        cp_ip, password,
        f"grep 'cluster-signing-duration' {_KCM_MANIFEST} 2>/dev/null || echo NOT_SET",
        timeout=15,
    )

    if f"--cluster-signing-duration={_KCM_DURATION}" in cur_out:
        log_info(
            f"kube-controller-manager already uses "
            f"--cluster-signing-duration={_KCM_DURATION} — no patch needed"
        )
        return False

    current_val = "not set"
    _m = re.search(r'--cluster-signing-duration=(\S+)', cur_out)
    if _m:
        current_val = _m.group(1)

    log_action(
        f"Patching kube-controller-manager --cluster-signing-duration: "
        f"{current_val} → {_KCM_DURATION} (so new kubelet CSRs get 5-year certs)"
    )

    if dry_run:
        log_info(f"[dry-run] would patch {_KCM_MANIFEST}")
        return False

    # Build a small Python script that edits the manifest safely.
    # Using Python (not sed) for reliable multi-line YAML editing.
    # This script is base64-encoded and piped to python3 on the remote node.
    py_src = (
        "import re, sys\n"
        f"m = '{_KCM_MANIFEST}'\n"
        f"t = '--cluster-signing-duration={_KCM_DURATION}'\n"
        "c = open(m).read()\n"
        "if t in c:\n"
        "    print('ALREADY_SET'); sys.exit(0)\n"
        "if 'cluster-signing-duration' in c:\n"
        "    c = re.sub(r'--cluster-signing-duration=\\S+', t, c)\n"
        "    open(m, 'w').write(c); print('UPDATED'); sys.exit(0)\n"
        "lines = c.splitlines(keepends=True)\n"
        "for i, ln in enumerate(lines):\n"
        "    if ln.strip() == '- kube-controller-manager':\n"
        "        ind = re.match(r'(\\s*)', ln).group(1)\n"
        "        lines.insert(i+1, ind + '- ' + t + '\\n')\n"
        "        open(m, 'w').write(''.join(lines)); print('INSERTED'); sys.exit(0)\n"
        "print('NOT_FOUND')\n"
    )
    py_b64 = base64.b64encode(py_src.encode()).decode()
    rc_p, patch_out = _ssh_exec(
        cp_ip, password,
        f"echo '{py_b64}' | base64 -d | python3",
        timeout=20,
    )

    if rc_p != 0 or not any(k in patch_out for k in ("ALREADY_SET", "UPDATED", "INSERTED")):
        log_warn(
            f"KCM manifest patch may have failed (rc={rc_p}): {patch_out[:150]} "
            f"— kubelet certs will still be renewed but may only get 1-year validity"
        )
        return False

    if "ALREADY_SET" in patch_out:
        return False

    action = "UPDATED" if "UPDATED" in patch_out else "INSERTED"
    log_info(f"kube-controller-manager manifest {action} — waiting 30s for pod restart")
    time.sleep(30)

    # Verify KCM is back Running (up to 30 more seconds)
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    for _ in range(6):
        rc_v, kcm_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get pods -n kube-system "
            f"-l component=kube-controller-manager "
            f"--no-headers 2>/dev/null | awk '{{print $3}}'",
            timeout=15,
        )
        if rc_v == 0 and "Running" in kcm_out:
            log_info(
                f"kube-controller-manager Running with "
                f"--cluster-signing-duration={_KCM_DURATION}"
            )
            return True
        time.sleep(5)

    log_warn(
        f"kube-controller-manager did not confirm Running after 60s — "
        f"proceeding; kubelet cert CSRs may still use old signing duration"
    )
    return True


# ─── Phase 2: kubelet serving cert check/renewal ──────────────────────────────
def _phase2_kubelet(cluster_cfg, cp_ip, password, kubeconfig, dry_run, threshold_days):
    """Check and renew kubelet serving certs on every node.

    VSP worker nodes use serverTLSBootstrap: true — deleting kubelet.crt
    triggers a new CSR which is signed by kube-controller-manager.  The cert
    duration is controlled by --cluster-signing-duration (default 8760h/1 year).

    To achieve 5-year cert validity, this function first calls
    _ensure_kcm_signing_duration() to patch the KCM static pod manifest if the
    cluster config has fix_kcm_duration: True.  The KCM restarts before any
    kubelet cert is deleted.

    Per-node renewal: SSHes directly to the node, deletes cert+key, restarts
    kubelet, polls for node Ready (up to 120s), then polls for cert file
    existence (up to 30s) before logging the new expiry.  Non-fatal per node.
    """
    label = cluster_cfg["label"]
    log_sep()
    log_info(f"Phase 2: kubelet serving cert check/renewal")
    log_info(f"  Threshold    : {threshold_days}d")
    log_info(f"  Cert duration: {_KCM_DURATION} (via kube-controller-manager CSR signing)")

    # Ensure KCM uses 5-year CSR signing duration before any node renewal
    if cluster_cfg.get("fix_kcm_duration"):
        try:
            _ensure_kcm_signing_duration(cp_ip, password, kubeconfig, dry_run)
        except Exception as exc:
            log_warn(f"_ensure_kcm_signing_duration raised: {exc} — continuing")

    # Discover node names and IPs from the CP node
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    rc, out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get nodes -o json 2>/dev/null",
        timeout=30,
    )
    if rc != 0 or not out.strip():
        log_error(f"Cannot list nodes for kubelet cert check: {out[:200]}")
        return

    json_str = out[out.find("{"):] if "{" in out else out
    try:
        nodes_data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        log_error(f"Cannot parse nodes JSON: {exc} — raw: {out[:200]}")
        return

    nodes = []
    for item in nodes_data.get("items", []):
        node_name = item.get("metadata", {}).get("name", "")
        node_ip   = next(
            (a["address"]
             for a in item.get("status", {}).get("addresses", [])
             if a.get("type") == "InternalIP"),
            None,
        )
        if node_name and node_ip:
            nodes.append({"name": node_name, "ip": node_ip})

    if not nodes:
        log_warn(f"No nodes discovered — skipping kubelet cert phase")
        return

    log_info(f"Checking {len(nodes)} node(s): "
             f"{', '.join(n['name'] for n in nodes)}")

    threshold_sec = threshold_days * 86400

    for node in nodes:
        name = node["name"]
        ip   = node["ip"]
        _phase2_one_node(label, name, ip, password, cp_ip, kubeconfig,
                         dry_run, threshold_days, threshold_sec)


def _phase2_one_node(label, node_name, node_ip, password, cp_ip,
                     kubeconfig, dry_run, threshold_days, threshold_sec):
    """Check and optionally renew the kubelet serving cert on one node."""
    cert_path = "/var/lib/kubelet/pki/kubelet.crt"
    key_path  = "/var/lib/kubelet/pki/kubelet.key"

    # Check if kubelet.crt exists and SSH works
    rc_e, exist_out = _ssh_exec(
        node_ip, password,
        f"test -f {cert_path} && echo EXISTS || echo MISSING",
        timeout=15,
    )
    if rc_e != 0:
        log_warn(f"{node_name} ({node_ip}): SSH failed — skipping")
        return
    if "MISSING" in exist_out:
        # Control-plane nodes on VSP use the kubeadm PKI for kubelet serving
        # and may not have a standalone kubelet.crt.  This is normal.
        log_info(
            f"kubelet.crt not present on {node_name} ({node_ip}) "
            f"— no kubelet serving cert renewal needed for this node"
        )
        return

    # Get expiry date (for logging)
    rc_d, expiry_out = _ssh_exec(
        node_ip, password,
        f"openssl x509 -in {cert_path} -noout -enddate 2>&1",
        timeout=15,
    )
    if rc_d != 0 or "notAfter=" not in expiry_out:
        log_warn(f"{node_name}: cannot read kubelet.crt expiry — "
                 f"rc={rc_d}: {expiry_out[:100]}")
        return

    # "notAfter=Mar 13 18:20:42 2027 GMT"
    expiry_str = expiry_out.split("notAfter=", 1)[-1].strip()

    # Check if within threshold
    rc_c, _ = _ssh_exec(
        node_ip, password,
        f"openssl x509 -in {cert_path} -noout -checkend {threshold_sec} 2>&1",
        timeout=15,
    )
    # rc=0 → cert is valid beyond threshold; rc=1 → expires within threshold
    expiring = (rc_c == 1)

    log_check(
        f"kubelet.crt on {node_name} ({node_ip}) — "
        f"EXPIRES: {expiry_str} — "
        f"{'EXPIRING' if expiring else f'valid for >{threshold_days}d'}"
    )

    if not expiring:
        log_skip(f"kubelet.crt on {node_name} valid for >{threshold_days}d")
        return

    log_action(
        f"kubelet.crt on {node_name} expires within {threshold_days}d — "
        f"deleting cert+key and restarting kubelet"
    )

    if dry_run:
        log_info(f"[dry-run] would delete {cert_path}, {key_path} and restart kubelet")
        return

    # Delete cert+key and restart kubelet
    rc_r, out_r = _ssh_exec(
        node_ip, password,
        f"rm -f {cert_path} {key_path} && systemctl restart kubelet && echo RESTARTED",
        timeout=30,
    )
    if rc_r != 0 or "RESTARTED" not in out_r:
        log_error(f"kubelet restart on {node_name} failed "
                  f"(rc={rc_r}): {out_r[:200]}")
        return

    # Wait for node to become Ready (up to 120s).
    # With CSR-based TLS bootstrapping the kubelet must submit a CSR, have it
    # auto-approved by the controller manager, and receive the signed cert.
    # This typically takes 20-60s after kubelet restart.
    log_action(f"Waiting up to 120s for {node_name} to become Ready...")
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    node_ready = False
    for elapsed in range(0, 130, 10):
        if elapsed > 0:
            time.sleep(10)
        rc_n, node_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get node {node_name} "
            f"--no-headers 2>/dev/null | awk '{{print $2}}'",
            timeout=20,
        )
        if rc_n == 0 and "Ready" in node_out and "NotReady" not in node_out:
            node_ready = True
            break
    if not node_ready:
        log_warn(f"{node_name} not Ready after 120s — continuing")

    # After node-Ready, the new kubelet.crt may not yet be written to disk.
    # The CSR approval cycle (kubelet submits → controller signs → cert written)
    # can lag the node-Ready status by up to ~30s.  Poll for file existence.
    cert_present = False
    for _ in range(15):
        time.sleep(2)
        rc_ce, ce_out = _ssh_exec(
            node_ip, password,
            f"test -f {cert_path} && echo EXISTS",
            timeout=10,
        )
        if rc_ce == 0 and "EXISTS" in ce_out:
            cert_present = True
            break
    if not cert_present:
        log_warn(
            f"{node_name}: new kubelet.crt not present after 30s — "
            f"CSR may still be pending; new expiry cannot be verified"
        )

    # Read new cert expiry
    rc_ne, new_expiry_out = _ssh_exec(
        node_ip, password,
        f"openssl x509 -in {cert_path} -noout -enddate 2>&1",
        timeout=15,
    )
    new_expiry = (new_expiry_out.split("notAfter=", 1)[-1].strip()
                  if rc_ne == 0 and "notAfter=" in new_expiry_out
                  else "unknown")
    log_renewed(
        f"kubelet.crt on {node_name} — NEW EXPIRY: {new_expiry}"
    )


# ─── Phase 3.0: Cluster CA extension (VSP only) ──────────────────────────────
# Both internal CAs use vcf-cluster-issuer (selfSigned) and expire ~3 years
# after template deployment.  Extending them to 10 years ensures subsequent
# leaf-cert reissuances (Phase 3.1) are not capped at the CA's expiry.
# trust-manager Bundles on this cluster are all empty (no sources/targets) so
# there are no CA bundle ConfigMaps to update after rotation.
CA_TARGET_DURATION = "87600h0m0s"   # 10 years (best-effort; VCF operator may revert)
# Threshold: only rotate the CA if it has < 1 year remaining.
# Using 5 years (43830h) caused Phase 3.0 to fire on EVERY boot because the
# VCF operator enforces spec.duration=27740h (~3.17y) and continuously reverts
# our patch.  Each rotation generates a new key pair, breaking all existing
# leaf certs signed by the previous CA.  1 year matches the global THRESHOLD_DAYS
# policy and avoids the destructive rotation cycle on normal deployments.
CA_MIN_REMAINING_H = 8760           # extend if < 1 year (~8760h) remaining

_CA_CERTS = [
    # (namespace, certificate_name, secret_name)
    ("vmsp-platform", "vcf-cluster-ca",           "vcf-cluster-ca-secret"),
    ("vmsp-platform", "vcf-external-cluster-ca-cert", "vcf-external-cluster-ca-cert"),
]


def _phase3_extend_ca(cluster_cfg, cp_ip, password, kubeconfig, dry_run):
    """Extend both cluster CA certs if remaining validity < CA_MIN_REMAINING_H (1 year).

    Returns True if any CA was actually rotated (new Secret issued), False
    otherwise.  The caller uses this to decide whether to force-renew all leaf
    certs in Phase 3.1 — necessary because CA rotation generates a new key pair
    and all existing leaf certs signed by the old key become unverifiable.

    NOTE: The VCF operator enforces spec.duration=27740h and may revert our
    patch shortly after issuance.  CA_MIN_REMAINING_H is set to 8760h (1 year)
    so this phase only fires when the CA is genuinely near expiry, not on every
    boot of a fresh template deployment.
    """
    label   = cluster_cfg["label"]
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    rotated = False   # becomes True if any CA Secret is actually deleted/reissued
    log_sep()
    log_info(f"Phase 3.0: cluster CA extension")
    log_info(f"  Target duration : {CA_TARGET_DURATION} (10 years, best-effort)")
    log_info(f"  Extend threshold: < {CA_MIN_REMAINING_H}h remaining (1 year)")

    for ns, cert_name, secret_name in _CA_CERTS:
        rc, out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get certificate {cert_name} -n {ns} -o json 2>/dev/null",
            timeout=15,
        )
        if rc != 0 or not out.strip() or "{" not in out:
            log_warn(f"Cannot read CA cert {ns}/{cert_name} — skipping")
            continue

        try:
            cert_data = json.loads(out[out.find("{"):])
        except json.JSONDecodeError as exc:
            log_warn(f"Cannot parse CA cert JSON for {ns}/{cert_name}: {exc}")
            continue

        not_after_str = cert_data.get("status", {}).get("notAfter", "")
        cur_duration  = cert_data.get("spec", {}).get("duration", "<none>")

        remaining_h = None
        try:
            not_after_dt = datetime.strptime(not_after_str, "%Y-%m-%dT%H:%M:%SZ")
            remaining_h  = int((not_after_dt - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass

        if remaining_h is None:
            log_warn(f"Cannot parse notAfter '{not_after_str}' for {ns}/{cert_name} — skipping")
            continue

        remaining_y = remaining_h / 8760
        log_check(
            f"CA {ns}/{cert_name} — "
            f"EXPIRES: {not_after_str} — "
            f"REMAINING: {remaining_h}h (~{remaining_y:.1f}y) — "
            f"spec.duration: {cur_duration}"
        )

        if remaining_h >= CA_MIN_REMAINING_H:
            log_skip(
                f"CA {ns}/{cert_name} — {remaining_h}h remaining "
                f">= {CA_MIN_REMAINING_H}h — no extension needed"
            )
            continue

        log_action(
            f"Extending CA {ns}/{cert_name}: {cur_duration} → {CA_TARGET_DURATION}"
        )

        if dry_run:
            log_info(
                f"[dry-run] would patch {ns}/{cert_name} spec.duration "
                f"and delete Secret {secret_name}"
            )
            continue

        # Patch spec.duration
        rc_p, patch_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} patch certificate {cert_name} -n {ns} "
            f"--type=merge -p '{{\"spec\":{{\"duration\":\"{CA_TARGET_DURATION}\"}}}}' "
            f"2>/dev/null && echo PATCHED",
            timeout=15,
        )
        if "PATCHED" not in patch_out:
            log_warn(f"Patch of {ns}/{cert_name} may have failed (rc={rc_p}) — proceeding")

        # Delete the backing Secret to trigger reissuance
        log_action(f"Deleting Secret {ns}/{secret_name} to trigger CA reissuance")
        _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} delete secret {secret_name} -n {ns} "
            f"--ignore-not-found=true 2>/dev/null",
            timeout=15,
        )
        rotated = True  # CA was actually replaced — leaf certs must be force-renewed

        # Wait up to 30s for cert-manager to reissue the CA cert
        new_expiry = "unknown"
        for _ in range(30):
            time.sleep(1)
            rc_w, exp_out = _ssh_exec(
                cp_ip, password,
                f"kubectl {kc_flag} get secret {secret_name} -n {ns} "
                f"-o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null | "
                f"base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null",
                timeout=15,
            )
            if rc_w == 0 and "notAfter=" in exp_out:
                new_expiry = exp_out.split("notAfter=", 1)[-1].strip()
                break

        log_renewed(
            f"CA {ns}/{cert_name} — "
            f"OLD EXPIRY: {not_after_str} → NEW EXPIRY: {new_expiry}"
        )

    return rotated  # True if ≥1 CA was replaced; caller must force-renew leaf certs


# ─── Phase 3.2: trust-manager + vmsp-operator re-sync after CA rotation ──────
# When Phase 3.0 (or a cross-session CA key mismatch) causes new CA key pairs
# to be issued, two platform controllers must be restarted to pick up the new CA:
#
# (A) trust-manager:
#   trust-manager watches labeled Secrets for changes.  When Phase 3.0 deletes and
#   recreates vcf-cluster-ca-secret, trust-manager's watch can miss the creation
#   event (Kubernetes informer race on delete+create within a tight window), leaving
#   ALL platform-trust ConfigMaps with the OLD CA cert.  Pods in the fleet-depot and
#   vmsp-platform namespaces use platform-trust to verify TLS connections to the
#   internal zot-1 registry.
#   Fix: restart trust-manager Deployment, then delete stale platform-trust
#   ConfigMaps so trust-manager recreates them with the new CA cert.
#
# (B) vmsp-operator:
#   The vmsp-operator bundle controller is responsible for downloading component
#   tarballs from fleet-01a and pushing OCI images to the zot-1 registry
#   (registry.vmsp-platform.svc.cluster.local:5000).  Its Go binary loads TLS root
#   CAs at startup and caches them in memory — it does NOT auto-reload updated
#   ConfigMap volumes.  After trust-manager recreates the platform-trust ConfigMaps
#   with the new CA cert, the vmsp-operator is still using the stale in-memory CA
#   pool and will reject zot-1's TLS certificate ("ECDSA verification failure").
#   Fix: restart vmsp-operator Deployment after trust-manager has recreated the
#   ConfigMaps, so the new pod starts with the correct CA pool.
#
# Both restarts must complete BEFORE Phase 3.1 (leaf cert issuance) so all bundles
# are current before new certs are issued for zot-1 and other services.
_TRUST_MANAGER_NS     = "vmsp-platform"
_TRUST_MANAGER_DEPLOY = "trust-manager"
_VMSP_OPERATOR_DEPLOY = "vmsp-operator"
_PLATFORM_TRUST_CM    = "platform-trust"
_PLATFORM_TRUST_NAMESPACES = [
    "vmsp-platform",
    "vcf-fleet-depot",
    "vcf-fleet-lcm",
    "ops-logs",
]


def _phase3_sync_trust_manager(cluster_cfg, cp_ip, password, kubeconfig, dry_run):
    """Restart trust-manager and vmsp-operator; force-refresh platform-trust ConfigMaps.

    Called after Phase 3.0 rotates a CA (ca_rotated=True) OR when a CA key
    mismatch is detected (ca_key_mismatch=True).

    Two controllers are restarted:
      1. trust-manager — forces re-list of CA Secrets and recreates platform-trust
         ConfigMaps in all critical namespaces with the new CA cert.
      2. vmsp-operator — its Go binary caches TLS root CAs at startup and does NOT
         auto-reload updated ConfigMap volumes.  Restarting it after trust-manager
         recreates the ConfigMaps ensures it starts with the correct CA pool, so
         the bundle controller can push images to zot-1 without ECDSA errors.
    """
    label   = cluster_cfg["label"]
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    log_sep()
    log_info(f"Phase 3.2 [{label}]: trust-manager + vmsp-operator re-sync after CA rotation")

    if dry_run:
        log_info("[dry-run] would restart trust-manager and vmsp-operator, delete platform-trust ConfigMaps")
        return

    # Step 1: restart trust-manager Deployment to force a full re-list
    log_action(f"Restarting {_TRUST_MANAGER_DEPLOY} deployment in {_TRUST_MANAGER_NS}")
    _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} rollout restart deployment {_TRUST_MANAGER_DEPLOY} "
        f"-n {_TRUST_MANAGER_NS} 2>/dev/null",
        timeout=20,
    )

    # Step 2: wait up to 60s for trust-manager pod to become Running (1/1)
    log_info("Waiting for trust-manager pod to become Ready...")
    ready = False
    for attempt in range(30):
        time.sleep(2)
        rc, out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get pods -n {_TRUST_MANAGER_NS} "
            f"-l app=trust-manager --no-headers 2>/dev/null | "
            f"awk '{{print $2, $3}}' | grep -c '1/1 Running'",
            timeout=15,
        )
        if rc == 0 and out.strip() == "1":
            ready = True
            break
    if not ready:
        log_warn("trust-manager pod did not reach 1/1 Running within 60s — proceeding anyway")
    else:
        log_info("trust-manager is Running — waiting 5s for initial reconcile")
        time.sleep(5)

    # Step 3: delete stale platform-trust ConfigMaps in critical namespaces so
    # trust-manager recreates them with the current CA cert included.
    log_action(f"Deleting stale platform-trust ConfigMaps in: {_PLATFORM_TRUST_NAMESPACES}")
    for ns in _PLATFORM_TRUST_NAMESPACES:
        rc, out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} delete configmap {_PLATFORM_TRUST_CM} -n {ns} "
            f"--ignore-not-found=true 2>/dev/null && echo OK",
            timeout=15,
        )
        status = "deleted" if "OK" in out else "skip/error"
        log_info(f"  {ns}/{_PLATFORM_TRUST_CM}: {status}")

    # Step 4: wait up to 30s for trust-manager to recreate ConfigMaps
    log_info("Waiting for trust-manager to recreate ConfigMaps...")
    all_ok = False
    for attempt in range(15):
        time.sleep(2)
        missing = []
        for ns in _PLATFORM_TRUST_NAMESPACES:
            rc, out = _ssh_exec(
                cp_ip, password,
                f"kubectl {kc_flag} get configmap {_PLATFORM_TRUST_CM} -n {ns} "
                f"-o jsonpath='{{.data.bundle\\.pem}}' 2>/dev/null | grep -c 'BEGIN CERTIFICATE'",
                timeout=15,
            )
            cert_count = int(out.strip()) if out.strip().isdigit() else 0
            if cert_count < 5:
                missing.append(ns)
        if not missing:
            all_ok = True
            break
    if all_ok:
        log_renewed("Platform-trust ConfigMaps recreated with updated CA bundle")
    else:
        log_warn(f"Some platform-trust ConfigMaps may not be updated yet: {missing}")

    # Step 5: restart vmsp-operator Deployment so it reloads the updated CA pool.
    # The vmsp-operator bundle controller caches TLS root CAs at Go startup — it
    # does not re-read updated ConfigMap volumes at runtime.  Without this restart,
    # the bundle controller continues using the old CA pool and fails with
    # "ECDSA verification failure" when pushing images to zot-1.
    log_action(f"Restarting {_VMSP_OPERATOR_DEPLOY} deployment in {_TRUST_MANAGER_NS}")
    _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} rollout restart deployment {_VMSP_OPERATOR_DEPLOY} "
        f"-n {_TRUST_MANAGER_NS} 2>/dev/null",
        timeout=20,
    )

    # Step 6: wait up to 90s for vmsp-operator to become Running (1/1)
    log_info("Waiting for vmsp-operator pod to become Ready...")
    ready = False
    for attempt in range(45):
        time.sleep(2)
        rc, out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get pods -n {_TRUST_MANAGER_NS} "
            f"-l app={_VMSP_OPERATOR_DEPLOY} --no-headers 2>/dev/null | "
            f"awk '{{print $2, $3}}' | grep -c '1/1 Running'",
            timeout=15,
        )
        if rc == 0 and out.strip() == "1":
            ready = True
            break
    if not ready:
        log_warn("vmsp-operator pod did not reach 1/1 Running within 90s — proceeding anyway")
    else:
        log_info("vmsp-operator is Running with fresh CA bundle")

    log_info(f"Phase 3.2 [{label}]: trust-manager + vmsp-operator re-sync complete")


# ─── Phase 3.1: cert-manager leaf cert renewal (VSP only) ────────────────────
def _phase3_certmanager(cluster_cfg, cp_ip, password, kubeconfig, dry_run,
                        threshold_days, force_all=False):
    """Renew cert-manager leaf certs that are not-Ready OR expiring within threshold.

    If force_all=True, ALL leaf certs are renewed regardless of expiry.  This is
    set automatically when Phase 3.0 rotated a CA: the new CA has a new key pair,
    so every existing leaf cert signed by the old CA is cryptographically broken
    even if its notAfter date is far in the future.

    For every cert that needs renewal:
      1. Patch spec.duration to CERT_VALIDITY (5 years) unless the cert has
         ownerReferences (e.g. the ClickHouse-owned cert).
      2. Delete the backing Secret — cert-manager immediately reissues.
    Uses status.notAfter from the Certificate resource for expiry checks so
    no extra per-cert SSH calls are needed.
    """
    label   = cluster_cfg["label"]
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    log_sep()
    log_info(f"Phase 3.1: cert-manager leaf certificate renewal")
    log_info(f"  Threshold      : {threshold_days}d")
    log_info(f"  Target duration: {CERT_VALIDITY} (5 years)")
    if force_all:
        log_info(f"  Force-all mode : ENABLED — CA was rotated; renewing ALL leaf certs"
                 f" regardless of expiry (old CA key no longer valid)")

    # Check cert-manager health
    rc_h, health_out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get pods -n cert-manager --no-headers 2>/dev/null",
        timeout=20,
    )
    if rc_h != 0:
        log_warn(f"cert-manager namespace not accessible — skipping phase")
        return

    unhealthy = [
        line for line in health_out.splitlines()
        if line.strip() and "Running" not in line and "Completed" not in line
    ]
    if unhealthy:
        log_warn(f"cert-manager pods are not all Running — skipping phase")
        for line in unhealthy:
            log_warn(f"  {line.strip()}")
        return

    # Fetch all Certificate resources in one call — expiry from status.notAfter
    rc_c, certs_out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get certificates -A -o json 2>/dev/null",
        timeout=30,
    )
    if rc_c != 0 or not certs_out.strip():
        log_info(f"No cert-manager Certificate resources found")
        return

    json_str = certs_out[certs_out.find("{"):] if "{" in certs_out else certs_out
    try:
        certs_data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        log_warn(f"Cannot parse Certificate JSON: {exc}")
        return

    threshold_sec = threshold_days * 86400
    to_renew = []

    for item in certs_data.get("items", []):
        ns           = item.get("metadata", {}).get("namespace", "")
        name         = item.get("metadata", {}).get("name", "")
        secret_name  = item.get("spec", {}).get("secretName", "")
        cur_duration = item.get("spec", {}).get("duration") or "<none>"
        has_owners   = bool(item.get("metadata", {}).get("ownerReferences"))
        conditions   = item.get("status", {}).get("conditions", [])
        ready_cond   = next((c for c in conditions if c.get("type") == "Ready"), None)
        is_ready     = bool(ready_cond and ready_cond.get("status") == "True")
        not_after_str = item.get("status", {}).get("notAfter", "")

        # Compute days remaining from status.notAfter (ISO 8601)
        expiring      = False
        days_remaining = None
        try:
            expiry_dt      = datetime.strptime(not_after_str, "%Y-%m-%dT%H:%M:%SZ")
            delta_sec      = (expiry_dt - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
            days_remaining = int(delta_sec / 86400)
            expiring       = delta_sec < threshold_sec
        except (ValueError, TypeError):
            pass

        if days_remaining is not None:
            validity_tag = (
                f"EXPIRING: {days_remaining}d" if expiring
                else f"valid: {days_remaining}d"
            )
        else:
            validity_tag = "expiry unknown"

        log_check(
            f"cert-manager {ns}/{name:45s} — "
            f"Ready: {is_ready!s:5s} — "
            f"EXPIRES: {not_after_str or 'unknown':25s} — {validity_tag}"
        )

        if not is_ready or expiring or force_all:
            to_renew.append({
                "ns":          ns,
                "name":        name,
                "secret":      secret_name,
                "cur_duration": cur_duration,
                "has_owners":  has_owners,
                "old_expiry":  not_after_str,
                "is_ready":    is_ready,
            })

    if not to_renew:
        total = len(certs_data.get("items", []))
        log_skip(
            f"All {total} cert-manager certificates are Ready "
            f"and valid for >{threshold_days}d — no action needed"
        )
        return

    not_ready_n = sum(1 for c in to_renew if not c["is_ready"])
    expiring_n  = sum(1 for c in to_renew if c["is_ready"])  # ready but expiring
    log_action(
        f"{len(to_renew)} cert(s) require renewal "
        f"({not_ready_n} not-Ready, {expiring_n} expiring within {threshold_days}d) — "
        f"patching duration and deleting Secrets"
    )

    if dry_run:
        for c in to_renew:
            should_patch = not c["has_owners"] and c["cur_duration"] != CERT_VALIDITY
            extra = (
                f" + patch {c['cur_duration']} → {CERT_VALIDITY}"
                if should_patch else ""
            )
            log_info(f"[dry-run] would delete Secret {c['ns']}/{c['secret']}{extra}")
        return

    # Patch duration + delete Secret for each cert to renew
    for c in to_renew:
        should_patch = not c["has_owners"] and c["cur_duration"] != CERT_VALIDITY
        if should_patch:
            rc_p, p_out = _ssh_exec(
                cp_ip, password,
                f"kubectl {kc_flag} patch certificate {c['name']} -n {c['ns']} "
                f"--type=merge -p '{{\"spec\":{{\"duration\":\"{CERT_VALIDITY}\"}}}}' "
                f"2>/dev/null && echo PATCHED",
                timeout=15,
            )
            if "PATCHED" in p_out:
                log_action(
                    f"Patched spec.duration {c['cur_duration']} → {CERT_VALIDITY} "
                    f"for {c['ns']}/{c['name']}"
                )
            else:
                log_warn(
                    f"Duration patch may have failed for {c['ns']}/{c['name']} "
                    f"(rc={rc_p}) — proceeding with Secret deletion"
                )

        if c["secret"]:
            rc_del, del_out = _ssh_exec(
                cp_ip, password,
                f"kubectl {kc_flag} delete secret {c['secret']} -n {c['ns']} "
                f"--ignore-not-found=true 2>/dev/null && echo DELETED",
                timeout=20,
            )
            if "DELETED" in del_out:
                log_action(
                    f"Deleted Secret {c['ns']}/{c['secret']} — cert-manager will reissue"
                )
            else:
                log_warn(
                    f"Could not delete Secret {c['ns']}/{c['secret']} "
                    f"(rc={rc_del}): {del_out[:80]}"
                )

    # Wait up to 120s for all renewed certs to become Ready
    wait_max = min(120, max(30, len(to_renew) * 2))
    log_action(f"Waiting up to {wait_max}s for {len(to_renew)} cert(s) to be reissued...")
    all_ready = False
    for elapsed in range(0, wait_max + 1, 15):
        if elapsed > 0:
            time.sleep(15)
        rc_w, pending_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get certificates -A --no-headers 2>/dev/null "
            f"| grep -v ' True '",
            timeout=20,
        )
        if rc_w == 0 and not pending_out.strip():
            log_info(f"All cert-manager certs now Ready after {elapsed}s")
            all_ready = True
            break

    if not all_ready:
        log_warn(f"Some cert-manager certs still not Ready after {wait_max}s — continuing")

    # Log renewed certs with new expiry (one batch JSON call)
    rc_f, final_out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get certificates -A -o json 2>/dev/null",
        timeout=30,
    )
    final_map = {}
    if rc_f == 0 and "{" in final_out:
        try:
            final_data = json.loads(final_out[final_out.find("{"):])
            for fi in final_data.get("items", []):
                fi_ns   = fi.get("metadata", {}).get("namespace", "")
                fi_name = fi.get("metadata", {}).get("name", "")
                fi_exp  = fi.get("status", {}).get("notAfter", "unknown")
                fi_cond = fi.get("status", {}).get("conditions", [])
                fi_rdy  = next((c for c in fi_cond if c.get("type") == "Ready"), None)
                fi_ok   = bool(fi_rdy and fi_rdy.get("status") == "True")
                final_map[(fi_ns, fi_name)] = (fi_exp, fi_ok)
        except (json.JSONDecodeError, KeyError):
            pass

    for c in to_renew:
        new_expiry, new_ready = final_map.get((c["ns"], c["name"]), ("unknown", False))
        status_tag = "Ready" if new_ready else "NOT Ready"
        log_renewed(
            f"cert-manager {c['ns']}/{c['name']} — "
            f"OLD: {c['old_expiry'] or 'unknown'} → NEW: {new_expiry} — {status_tag}"
        )


# ─── Phase 4: Antrea controller TLS check (VSP only) ─────────────────────────
def _phase4_antrea(cluster_cfg, cp_ip, password, kubeconfig, dry_run, threshold_days):
    """Check the antrea-controller-tls secret and renew if expiring.

    Antrea regenerates its own TLS cert when the controller pod is restarted
    and the Secret is absent.  Phase is skipped if the Secret doesn't exist.
    """
    label     = cluster_cfg["label"]
    kc_flag   = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    secret_ns = "kube-system"
    secret_nm = "antrea-controller-tls"
    log_sep()
    log_info(f"Phase 4: Antrea controller TLS check")
    log_info(f"  Threshold: {threshold_days}d")

    threshold_sec = threshold_days * 86400

    # Get and decode the TLS cert from the Secret
    rc, out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get secret {secret_nm} -n {secret_ns} "
        f"-o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null | "
        f"base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null && echo OK",
        timeout=20,
    )
    if rc != 0 or "notAfter=" not in out:
        log_info(f"Secret {secret_nm} not found or TLS cert unreadable — "
                 f"Antrea will regenerate on next restart")
        return

    expiry_str = ""
    for line in out.splitlines():
        if "notAfter=" in line:
            expiry_str = line.split("notAfter=", 1)[-1].strip()
            break

    # Check if within threshold
    rc_c, _ = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get secret {secret_nm} -n {secret_ns} "
        f"-o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null | "
        f"base64 -d 2>/dev/null | openssl x509 -noout -checkend {threshold_sec} 2>/dev/null",
        timeout=20,
    )
    expiring = (rc_c == 1)

    log_check(
        f"{secret_nm} — "
        f"EXPIRES: {expiry_str} — "
        f"{'EXPIRING' if expiring else f'valid for >{threshold_days}d'}"
    )

    if not expiring:
        log_skip(f"{secret_nm} valid for >{threshold_days}d — no action needed")
        return

    # Antrea hardcodes a 1-year validity when it self-generates a TLS cert.
    # To get a 5-year cert we pre-inject our own self-signed cert into the
    # Secret *before* restarting the controller.  When Antrea starts and finds
    # a valid, non-expired Secret it skips regeneration and uses the existing
    # cert.  On restart it also patches the webhook caBundle with the cert's
    # public key, so the API server will trust it.
    #
    # Fall-back: if openssl on this node does not support -addext (openssl
    # < 1.1.1) we remove the Secret so Antrea regenerates normally (1-year).
    _ANTREA_CERT_DAYS = 1826  # 5 years + 1 day
    _ANTREA_SANS = (
        "DNS:antrea,"
        "DNS:antrea.kube-system,"
        "DNS:antrea.kube-system.svc,"
        "DNS:antrea.kube-system.svc.cluster.local"
    )

    log_action(
        f"{secret_nm} expires within {threshold_days}d — "
        f"generating {_ANTREA_CERT_DAYS // 365}y replacement cert and restarting Antrea controller"
    )

    if dry_run:
        log_info(f"[dry-run] would inject {_ANTREA_CERT_DAYS // 365}y cert into {secret_nm} "
                 f"and restart Antrea controller")
        return

    # Step 1: generate a 5-year self-signed cert on the CP node
    rc_gen, gen_out = _ssh_exec(
        cp_ip, password,
        f"openssl req -x509 -newkey rsa:2048 "
        f"-keyout /tmp/antrea_tls.key -out /tmp/antrea_tls.crt "
        f"-days {_ANTREA_CERT_DAYS} -nodes "
        f"-subj '/CN=antrea' "
        f"-addext 'subjectAltName={_ANTREA_SANS}' 2>/dev/null "
        f"&& echo CERT_OK",
        timeout=30,
    )
    five_year_ok = rc_gen == 0 and "CERT_OK" in gen_out

    if five_year_ok:
        # Step 2: replace (or create) the Secret with the generated cert
        rc_apply, _ = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} create secret tls {secret_nm} -n {secret_ns} "
            f"--cert=/tmp/antrea_tls.crt --key=/tmp/antrea_tls.key "
            f"--dry-run=client -o yaml 2>/dev/null | "
            f"kubectl {kc_flag} apply -f - 2>/dev/null; "
            f"rm -f /tmp/antrea_tls.key /tmp/antrea_tls.crt",
            timeout=30,
        )
        if rc_apply != 0:
            log_warn("Failed to apply 5-year cert Secret — falling back to Antrea self-renewal (1-year)")
            five_year_ok = False
        else:
            log_info(f"Pre-injected {_ANTREA_CERT_DAYS // 365}y replacement cert into {secret_nm}")
    else:
        log_warn(
            "openssl -addext not supported on this node — "
            "falling back to Antrea self-renewal (1-year cert)"
        )

    if not five_year_ok:
        # Fall-back: delete Secret so Antrea regenerates (1-year hardcoded)
        _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} delete secret {secret_nm} -n {secret_ns} "
            f"--ignore-not-found=true 2>/dev/null",
            timeout=20,
        )

    # Restart Antrea controller pod so it loads the (new or injected) cert
    # and patches the webhook caBundle accordingly
    _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} delete pod -n {secret_ns} "
        f"-l app=antrea,component=antrea-controller --grace-period=0 2>/dev/null || true",
        timeout=20,
    )

    # Wait for Antrea controller to be Ready (up to 120s, check every 15s)
    # The loop runs 8 × 15s = 120s; the warning fires exactly once after the
    # loop exits without a Ready pod (fixes the previous double-warning bug
    # caused by range(0, 121, 15) firing at both elapsed=105 and elapsed=120).
    log_action(f"Waiting up to 120s for Antrea controller to restart and load cert...")
    _antrea_ready = False
    for elapsed in range(0, 120, 15):
        time.sleep(15)
        rc_w, pod_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get pods -n {secret_ns} "
            f"-l app=antrea,component=antrea-controller "
            f"--no-headers 2>/dev/null | awk '{{print $2}}'",
            timeout=20,
        )
        if rc_w == 0 and pod_out.strip():
            ready_col = pod_out.strip().split("\n")[0]
            parts = ready_col.split("/")
            if len(parts) == 2 and parts[0] == parts[1] and parts[0] != "0":
                log_info(f"Antrea controller Ready ({elapsed + 15}s)")
                _antrea_ready = True
                break
    if not _antrea_ready:
        log_warn(f"Antrea controller not Ready after 120s — continuing")

    # Verify cert in Secret (new or injected)
    rc_n, new_out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get secret {secret_nm} -n {secret_ns} "
        f"-o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null | "
        f"base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null",
        timeout=20,
    )
    new_expiry = (new_out.split("notAfter=", 1)[-1].strip()
                  if rc_n == 0 and "notAfter=" in new_out
                  else "unknown")
    log_renewed(f"{secret_nm} — NEW EXPIRY: {new_expiry}")


# ─── CA key consistency pre-check (VSP only) ─────────────────────────────────
def _check_ca_key_consistency(cp_ip, password, kubeconfig):
    """Pre-check: does registry-certificate verify against the CURRENT vcf-cluster-ca key?

    Called at the top of _check_cluster() for VSP BEFORE Phase 1/2/3.1 so that
    Phase 3.1 can use force_all=True when a cross-session CA key mismatch exists.

    Cross-session failure mode: Phase 3.0 ran in boot N and rotated the CA key
    pair (new Secret issued).  Phase 3.1 in boot N force-renewed leaf certs but
    may have missed some, OR subsequent boots (N+1, N+2…) skip Phase 3.0 (CA
    now has >1y remaining) AND skip Phase 3.1 (leaf certs appear Ready with
    >60d remaining) — but those leaf certs are signed by the OLD CA key and will
    fail x509 ECDSA verification at runtime.

    Returns True  (mismatch detected)  → caller sets force_all=True for Phase 3.1
    Returns False (consistent)         → no corrective action needed from pre-check
    On any SSH / parse failure: logs WARN, returns False (safe default — do not
    accidentally force-renew when the cluster is unreachable).
    """
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    log_sep()
    log_info("CA Key Consistency Pre-Check (runs before Phase 1)")
    log_info(f"  CA source : {CA_SECRET_NS}/{CA_SECRET_NAME}")
    log_info("  Leaf probe: registry-certificate in vmsp-platform")

    verify_cmd = (
        f"kubectl {kc_flag} get secret {CA_SECRET_NAME} -n {CA_SECRET_NS} "
        f"-o jsonpath='{{{{.data.ca\\.crt}}}}' 2>/dev/null | base64 -d > /tmp/precheck_ca.pem && "
        f"kubectl {kc_flag} get secret registry-certificate -n vmsp-platform "
        f"-o jsonpath='{{{{.data.tls\\.crt}}}}' 2>/dev/null | base64 -d > /tmp/precheck_reg.pem && "
        f"openssl verify -CAfile /tmp/precheck_ca.pem /tmp/precheck_reg.pem 2>&1; "
        f"RET=$?; rm -f /tmp/precheck_ca.pem /tmp/precheck_reg.pem; exit $RET"
    )
    rc_v, v_out = _ssh_exec(cp_ip, password, verify_cmd, timeout=25)

    if rc_v == 0 and "OK" in v_out:
        log_info("  Pre-check PASSED — registry-certificate consistent with current CA key")
        return False

    log_warn(
        f"  Pre-check FAILED — registry-certificate does NOT verify against "
        f"vcf-cluster-ca (rc={rc_v}): {v_out[:100]}"
    )
    log_warn(
        "  Root cause: vcf-cluster-ca key pair was rotated in a prior session; "
        "leaf certs appear Ready/valid-by-date but carry the OLD CA key."
    )
    log_warn(
        "  Consequence: Phase 3.1 will run with force_all=True — all "
        "vcf-cluster-issuer certs will be re-signed with the current CA key "
        "BEFORE Phase 1 kubeadm / Phase 2 kubelet renewal begins."
    )
    return True


# ─── Phase 5: containerd CA trust sync (VSP only) ────────────────────────────

def _phase5_renew_leaf_certs(cp_ip, password, kc_flag, dry_run):
    """Force-renew all cert-manager Certificates backed by vcf-cluster-issuer.

    Called when the registry serving cert fails openssl verify against the
    current cluster CA — meaning the CA key pair was rotated after the leaf
    certs were issued and Phase 3.1 did not force-renew them (because the
    rotation was not detected in the current session's Phase 3.0 run).

    Failure mode: Phase 3.0 rotates the CA; Phase 3.1 force-renews leaf certs
    via delete-Secret → cert-manager re-issues.  On a SUBSEQUENT boot, Phase
    3.0 is skipped (CA has >1y remaining), Phase 3.1 is skipped (leaf certs
    have >60d remaining), but the leaf certs are still signed by the old key.

    Returns True if any Secret deletions were submitted.
    """
    log_info("  Step 1b: Identifying all vcf-cluster-issuer backed certs for renewal...")
    rc, certs_out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get certificates -A -o json 2>/dev/null",
        timeout=30,
    )
    if rc != 0 or "{" not in certs_out:
        log_warn("  Could not list Certificates — skipping leaf-cert renewal")
        return False

    try:
        certs_data = json.loads(certs_out[certs_out.find("{"):])
    except Exception as exc:
        log_warn(f"  Could not parse Certificates JSON: {exc} — skipping")
        return False

    to_renew = []
    for item in certs_data.get("items", []):
        issuer_ref = item.get("spec", {}).get("issuerRef", {})
        if issuer_ref.get("name") == "vcf-cluster-issuer":
            ns     = item.get("metadata", {}).get("namespace", "")
            secret = item.get("spec", {}).get("secretName", "")
            name   = item.get("metadata", {}).get("name", "")
            if ns and secret:
                to_renew.append((ns, name, secret))

    if not to_renew:
        log_warn("  No vcf-cluster-issuer backed certs found — skipping")
        return False

    log_action(
        f"  CA rotation detected — force-renewing {len(to_renew)} "
        f"vcf-cluster-issuer cert(s) by deleting their Secrets..."
    )

    if dry_run:
        for ns, name, secret in to_renew:
            log_info(f"  [dry-run] would delete {ns}/{secret} to re-sign {name}")
        return True

    for ns, name, secret in to_renew:
        _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} delete secret {secret} -n {ns} --ignore-not-found 2>/dev/null",
            timeout=15,
        )
        log_info(f"  Deleted {ns}/{secret} (cert: {name})")

    # Wait up to 120s for all certs to be Ready
    log_action(f"  Waiting up to 120s for {len(to_renew)} cert(s) to be re-issued...")
    _all_ready = False
    for elapsed in range(0, 121, 15):
        if elapsed > 0:
            time.sleep(15)
        rc_w, pending_out = _ssh_exec(
            cp_ip, password,
            f"kubectl {kc_flag} get certificates -A --no-headers 2>/dev/null | grep -v ' True '",
            timeout=20,
        )
        if rc_w == 0 and not pending_out.strip():
            log_info(f"  All certs Ready after {elapsed}s")
            _all_ready = True
            break

    if not _all_ready:
        log_warn("  Some certs not Ready after 120s — continuing (pod restarts will still follow)")

    return True


def _phase5_casync(cluster_cfg, cp_ip, password, kubeconfig, dry_run,
                   leaf_certs_prerenewed=False):
    """Sync vcf-cluster-ca-secret ca.crt to every VSP node's containerd trust
    file, and run a final CA key consistency safety-net check.

    v2.0: leaf_certs_prerenewed is True when Phase 3.1 already force-renewed all
    vcf-cluster-issuer certs this session (due to ca_rotated from Phase 3.0 OR
    ca_key_mismatch from the pre-check).  Step 1b still runs as a SAFETY NET
    for any cert that Phase 3.1 may have missed (e.g. --skip-certmanager), but
    pod restarts are triggered in Step 4 based on EITHER:
      • leaf_certs_prerenewed  (Phase 3.1 re-signed certs — pods must reload)
      • leaf_certs_renewed     (Step 1b detected a remaining mismatch and fixed it)
      • updated                (at least one node CA file was stale and synced)

    Failure modes handled:
      A. Node CA file stale — push current CA via inline base64, restart
         containerd on affected nodes only.
      B. Leaf cert CA key mismatch that SURVIVED Phase 3.1 (safety net only
         when leaf_certs_prerenewed=False or --skip-certmanager was used) —
         _phase5_renew_leaf_certs() deletes all vcf-cluster-issuer Secrets and
         waits up to 120s for cert-manager to re-issue.
    """
    kc_flag = f"--kubeconfig={kubeconfig}" if kubeconfig else ""
    log_sep()
    log_info("Phase 5: containerd CA trust sync + leaf cert CA verification (safety net)")
    log_info(f"  CA source            : {CA_SECRET_NS}/{CA_SECRET_NAME}")
    log_info(f"  Node CA path         : {CONTAINERD_CA_PATH}")
    log_info(f"  leaf_certs_prerenewed: {leaf_certs_prerenewed} "
             f"(Phase 3.1 already force-renewed — pods will restart)"
             if leaf_certs_prerenewed else
             f"  leaf_certs_prerenewed: False (Step 1b safety net is active)")

    # ── Step 1: read current cluster CA from vcf-cluster-ca-secret ──────────
    rc, out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get secret {CA_SECRET_NAME} -n {CA_SECRET_NS} -o json",
        timeout=20,
    )
    if rc != 0 or "{" not in out:
        log_warn("Could not read vcf-cluster-ca-secret — skipping Phase 5")
        return

    try:
        secret_data = json.loads(out[out.find("{"):])
        b64_field = secret_data.get("data", {}).get("ca.crt", "")
        if not b64_field:
            log_warn("ca.crt field missing from vcf-cluster-ca-secret — skipping Phase 5")
            return
        current_ca = base64.b64decode(b64_field).decode()
    except Exception as exc:
        log_warn(f"Could not parse vcf-cluster-ca-secret: {exc} — skipping Phase 5")
        return
    log_info(f"  Cluster CA obtained ({len(current_ca)} bytes)")

    # ── Step 1b: CA key consistency SAFETY NET ───────────────────────────────
    # In v2.0, _check_ca_key_consistency() runs BEFORE Phase 1 and propagates
    # any mismatch to Phase 3.1 via force_all.  By the time Phase 5 runs,
    # Phase 3.1 should have already corrected any CA key mismatch.
    # Step 1b is kept as a final safety net for cases where Phase 3.1 was
    # skipped (--skip-certmanager) or failed to renew some certs.
    # When leaf_certs_prerenewed=True, we still run the verify; if it passes
    # (as expected after Phase 3.1 re-signed everything), leaf_certs_renewed
    # stays False — but pod restarts are still triggered via leaf_certs_prerenewed.
    leaf_certs_renewed = False
    log_info("  Step 1b (safety net): verifying registry-certificate against current CA...")
    verify_cmd = (
        f"kubectl {kc_flag} get secret {CA_SECRET_NAME} -n {CA_SECRET_NS} "
        f"-o jsonpath='{{{{.data.ca\\.crt}}}}' 2>/dev/null | base64 -d > /tmp/p5ca.pem && "
        f"kubectl {kc_flag} get secret registry-certificate -n vmsp-platform "
        f"-o jsonpath='{{{{.data.tls\\.crt}}}}' 2>/dev/null | base64 -d > /tmp/p5reg.pem && "
        f"openssl verify -CAfile /tmp/p5ca.pem /tmp/p5reg.pem 2>&1; "
        f"RET=$?; rm -f /tmp/p5ca.pem /tmp/p5reg.pem; exit $RET"
    )
    rc_v, v_out = _ssh_exec(cp_ip, password, verify_cmd, timeout=25)
    if rc_v == 0 and "OK" in v_out:
        log_skip("registry-certificate verifies against current CA — leaf certs OK")
    else:
        log_warn(
            f"registry-certificate FAILED verification against current CA "
            f"(rc={rc_v}): {v_out[:100]}"
        )
        log_warn(
            "Root cause: CA key pair was rotated after leaf certs were issued — "
            "Phase 3.1 did not re-sign them (rotation not detected this session)"
        )
        try:
            leaf_certs_renewed = _phase5_renew_leaf_certs(
                cp_ip, password, kc_flag, dry_run
            )
        except Exception as exc:
            log_error(f"_phase5_renew_leaf_certs raised: {exc}")

    # ── Step 2: discover all VSP node IPs via kubectl get nodes ─────────────
    rc2, nodes_out = _ssh_exec(
        cp_ip, password,
        f"kubectl {kc_flag} get nodes -o json",
        timeout=20,
    )
    node_ips = []
    if rc2 == 0 and "{" in nodes_out:
        try:
            for item in json.loads(nodes_out[nodes_out.find("{"):]).get("items", []):
                for addr in item.get("status", {}).get("addresses", []):
                    if addr.get("type") == "InternalIP":
                        node_ips.append(addr["address"])
                        break
        except Exception as exc:
            log_warn(f"Could not parse node list: {exc}")
    if not node_ips:
        log_warn("No node IPs returned by kubectl — skipping node CA sync")
    else:
        log_info(f"  Nodes ({len(node_ips)}): {', '.join(node_ips)}")

    # ── Step 3: compare and sync CA file on each node ────────────────────────
    updated = []
    for node_ip in node_ips:
        rc_r, node_ca = _ssh_exec(
            node_ip, password,
            f"cat '{CONTAINERD_CA_PATH}' 2>/dev/null || echo MISSING",
            timeout=15,
        )
        if node_ca.strip() == "MISSING":
            log_warn(f"  {node_ip}: containerd CA path not found — skipping node")
            continue
        if node_ca.strip() == current_ca.strip():
            log_skip(f"{node_ip} — containerd CA already current")
            continue

        log_action(f"{node_ip} — containerd CA STALE — syncing...")
        if dry_run:
            log_info(f"  [dry-run] would push CA and restart containerd on {node_ip}")
            updated.append(node_ip)
            continue

        # Write the CA PEM to the node inline: base64-encode it, echo through
        # base64 -d on the remote host.  _ssh_exec base64-encodes the entire
        # command string, so the embedded ca_b64 is safely transported.
        ca_b64 = base64.b64encode(current_ca.encode()).decode()
        target_dir = CONTAINERD_CA_PATH.rsplit("/", 1)[0]
        write_cmd = (
            f"echo '{ca_b64}' | base64 -d > /tmp/vsp-cluster-ca-new.crt && "
            f"mkdir -p '{target_dir}' && "
            f"cp /tmp/vsp-cluster-ca-new.crt '{CONTAINERD_CA_PATH}' && "
            "rm -f /tmp/vsp-cluster-ca-new.crt && "
            "systemctl daemon-reload && "
            "systemctl restart containerd && "
            "sleep 3 && "
            "systemctl is-active containerd && echo CONTAINERD_OK"
        )
        rc_w, w_out = _ssh_exec(node_ip, password, write_cmd, timeout=60)
        if rc_w == 0 and "CONTAINERD_OK" in w_out:
            log_renewed(f"{node_ip} — CA synced, containerd restarted")
            updated.append(node_ip)
        else:
            log_warn(f"{node_ip} — sync failed (rc={rc_w}): {w_out[:120]}")

    # ── Step 4: restart registry + identity pods if ANY fix was applied ──────
    # Trigger pod restarts when:
    #   leaf_certs_prerenewed — Phase 3.1 re-signed certs (CA key sync or rotation)
    #   leaf_certs_renewed    — Step 1b safety net caught a remaining mismatch
    #   updated               — at least one node CA file was stale and synced
    # zot-1-0 must reload its serving cert from the registry-certificate Secret
    # and containerd must already trust the CA that signed that cert.
    need_restart = updated or leaf_certs_renewed or leaf_certs_prerenewed
    if need_restart and not dry_run:
        reason_parts = []
        if leaf_certs_prerenewed:
            reason_parts.append("leaf certs pre-renewed by Phase 3.1")
        if leaf_certs_renewed:
            reason_parts.append("leaf certs re-signed by Phase 5 safety net")
        if updated:
            reason_parts.append(f"node CA file synced: {', '.join(updated)}")
        log_action(
            f"Restarting zot-1-0, metadata-service, vmsp-identity "
            f"({'; '.join(reason_parts)})..."
        )
        _ssh_exec(cp_ip, password,
                  f"kubectl {kc_flag} delete pod {ZOT_POD} -n {ZOT_NS} --ignore-not-found",
                  timeout=20)
        _ssh_exec(cp_ip, password,
                  f"kubectl {kc_flag} rollout restart daemonset/metadata-service -n vmsp-platform",
                  timeout=20)
        _ssh_exec(cp_ip, password,
                  f"kubectl {kc_flag} rollout restart deployment/vmsp-identity -n vmsp-platform",
                  timeout=20)
        log_info("  Waiting 35s for pods to restart...")
        time.sleep(35)
    elif need_restart and dry_run:
        log_info("[dry-run] would restart zot-1-0, metadata-service, vmsp-identity")

    # ── Summary ───────────────────────────────────────────────────────────────
    if not updated and not leaf_certs_renewed and not leaf_certs_prerenewed:
        log_info("Phase 5 complete — all checks passed, no action taken")
    else:
        summary_parts = []
        if leaf_certs_prerenewed:
            summary_parts.append("Phase 3.1 force-renewed vcf-cluster-issuer certs")
        if leaf_certs_renewed:
            summary_parts.append("Phase 5 safety net force-renewed remaining certs")
        if updated:
            summary_parts.append(f"CA file synced on {len(updated)} node(s): {', '.join(updated)}")
        log_info(f"Phase 5 complete — {'; '.join(summary_parts)}")


# ─── Per-cluster runner ───────────────────────────────────────────────────────
def _check_cluster(cluster_name, cluster_cfg, args):
    """Run all configured phases for one cluster.  Never raises."""
    global _LOG_LABEL
    _LOG_LABEL = cluster_cfg["label"]
    label      = cluster_cfg["label"]
    password   = _get_password()

    log_sep()
    log_info(f"══ K8s Certificate Check/Renewal ══")
    log_info(f"Cluster    : {label} ({cluster_name})")
    log_info(f"Threshold  : {args.threshold_days}d")
    log_info(f"Target ttl : {CERT_VALIDITY} (5 years)")
    log_info(f"Dry-run    : {args.dry_run}")

    # ── Discover CP IP ────────────────────────────────────────────────────────
    try:
        cp_ip = _discover_cp_ip(cluster_cfg, password)
    except Exception as exc:
        log_error(f"CP discovery raised: {exc}")
        return

    if not cp_ip:
        log_error(f"Cannot discover control-plane IP — skipping all phases")
        return

    # ── Probe kubeconfig on CP ────────────────────────────────────────────────
    try:
        kubeconfig = _probe_kubeconfig(cp_ip, password)
    except Exception as exc:
        log_error(f"kubeconfig probe raised: {exc}")
        kubeconfig = None

    if kubeconfig:
        log_info(f"kubeconfig: {kubeconfig}")
    else:
        log_warn(f"Could not probe kubeconfig — will run kubectl without --kubeconfig")

    phases_cfg = cluster_cfg.get("phases", [])

    # ════════════════════════════════════════════════════════════════════════
    # CA-AUTHORITY WORK FIRST (VSP only)
    # All CA operations must complete before any leaf-cert renewal so that
    # every cert issued in this run is signed by the most current CA key.
    # ════════════════════════════════════════════════════════════════════════

    # ── Phase 3.0: extend cluster CAs to 10 years (VSP only) ────────────────
    _ca_rotated = False
    if "extendca" in phases_cfg and not args.skip_extend_ca:
        try:
            _ca_rotated = _phase3_extend_ca(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run,
            )
            if _ca_rotated:
                log_warn(
                    "CA was rotated — Phase 3.1 will force-renew ALL leaf certs "
                    "to ensure they are signed by the new CA key"
                )
        except Exception as exc:
            log_error(f"Phase 3.0 (extend-ca) raised unexpected exception: {exc}")
    else:
        log_info("Phase 3.0 (extend-ca): skipped")

    # ── CA key consistency pre-check (VSP only) ──────────────────────────────
    # Detects the cross-session failure: Phase 3.0 rotated the CA in boot N,
    # Phase 3.1 missed some certs or was skipped, and subsequent boots find
    # leaf certs that appear Ready/valid-by-date but carry the OLD CA key.
    # Running this BEFORE Phases 1/2/3.1 ensures Phase 3.1 force-renews ALL
    # leaf certs in the same run, before any kubeadm/kubelet work begins.
    _ca_key_mismatch = False
    if "casync" in phases_cfg:   # casync phase implies VSP with registry infra
        try:
            _ca_key_mismatch = _check_ca_key_consistency(cp_ip, password, kubeconfig)
        except Exception as exc:
            log_error(f"CA key pre-check raised unexpected exception: {exc}")

    # force_all propagates to Phase 3.1: either the CA was rotated THIS session
    # (ca_rotated) or a prior session's rotation left stale-key certs (ca_key_mismatch).
    _force_leaf_renewal = _ca_rotated or _ca_key_mismatch

    # ── Phase 3.2: trust-manager re-sync after CA rotation (VSP only) ────────
    # Runs when EITHER the CA was rotated this session (ca_rotated) OR a cross-
    # session CA key mismatch is detected (ca_key_mismatch).  In both cases the
    # platform-trust ConfigMaps across all namespaces may be stale — they carry
    # the old CA cert but NOT the key pair that cert-manager now uses to sign
    # new leaf certs.  Pods using platform-trust for TLS verification (e.g. the
    # fleet-depot pod pushing images to the zot-1 registry) will fail with
    # "ECDSA verification failure" until the bundles are refreshed.
    #
    # Phase 3.2 must run BEFORE Phase 3.1 so that by the time new leaf certs are
    # issued, the platform-trust bundles already carry the new CA cert.
    if "casync" in phases_cfg and _force_leaf_renewal:
        try:
            _phase3_sync_trust_manager(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run,
            )
        except Exception as exc:
            log_error(f"Phase 3.2 (trust-manager sync) raised unexpected exception: {exc}")
    elif "casync" in phases_cfg:
        log_info("Phase 3.2 (trust-manager sync): skipped — no CA rotation or key mismatch")

    # ════════════════════════════════════════════════════════════════════════
    # LEAF CERT RENEWAL (all clusters — kubeadm PKI is separate from vcf-cluster-ca)
    # ════════════════════════════════════════════════════════════════════════

    # ── Phase 1: kubeadm CP certs ─────────────────────────────────────────────
    if "kubeadm" in phases_cfg and not args.skip_kubeadm:
        try:
            _phase1_kubeadm(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run, args.threshold_days,
            )
        except Exception as exc:
            log_error(f"Phase 1 (kubeadm) raised unexpected exception: {exc}")
    else:
        log_info("Phase 1 (kubeadm): skipped")

    # ── Phase 2: kubelet serving certs ───────────────────────────────────────
    if "kubelet" in phases_cfg and not args.skip_kubelet:
        try:
            _phase2_kubelet(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run, args.threshold_days,
            )
        except Exception as exc:
            log_error(f"Phase 2 (kubelet) raised unexpected exception: {exc}")
    else:
        log_info("Phase 2 (kubelet): skipped")

    # ── Phase 3.1: cert-manager leaf cert renewal (VSP only) ─────────────────
    # force_all=True when ca_rotated OR ca_key_mismatch: guarantees all
    # vcf-cluster-issuer certs are re-signed with the verified current CA key.
    if "certmanager" in phases_cfg and not args.skip_certmanager:
        try:
            _phase3_certmanager(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run, args.threshold_days,
                force_all=_force_leaf_renewal,
            )
        except Exception as exc:
            log_error(f"Phase 3.1 (cert-manager) raised unexpected exception: {exc}")
    else:
        log_info("Phase 3.1 (cert-manager): skipped")

    # ── Phase 4: Antrea TLS (VSP only) ────────────────────────────────────────
    if "antrea" in phases_cfg and not args.skip_antrea:
        try:
            _phase4_antrea(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run, args.threshold_days,
            )
        except Exception as exc:
            log_error(f"Phase 4 (antrea) raised unexpected exception: {exc}")
    else:
        log_info("Phase 4 (antrea): skipped")

    # ── Phase 5: containerd CA trust sync + safety-net verify (VSP only) ─────
    # leaf_certs_prerenewed tells Phase 5 Step 4 that Phase 3.1 already
    # force-renewed leaf certs this session, so pods must restart even if
    # Phase 5's own Step 1b safety-net verify now passes cleanly.
    if "casync" in phases_cfg and not args.skip_casync:
        try:
            _phase5_casync(
                cluster_cfg, cp_ip, password, kubeconfig,
                args.dry_run,
                leaf_certs_prerenewed=_force_leaf_renewal,
            )
        except Exception as exc:
            log_error(f"Phase 5 (casync) raised unexpected exception: {exc}")
    else:
        log_info("Phase 5 (casync): skipped")

    log_sep()
    log_info(f"══ Certificate check/renewal complete ══")
    _LOG_LABEL = ""


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description=(
            f"vsp_cert_renewer.py v{VERSION} — "
            "Proactive K8s certificate check/renewal for VSP and VCFA clusters"
        )
    )
    parser.add_argument(
        "--cluster",
        choices=["vsp", "vcfa", "all"],
        default="all",
        help="Which cluster to process (default: all)",
    )
    parser.add_argument(
        "--threshold-days",
        type=int,
        default=THRESHOLD_DAYS,
        help=f"Renew if cert expires within this many days (default: {THRESHOLD_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be done without making any changes",
    )
    parser.add_argument("--skip-kubeadm",     action="store_true",
                        help="Skip Phase 1 (kubeadm cert renewal)")
    parser.add_argument("--skip-kubelet",     action="store_true",
                        help="Skip Phase 2 (kubelet serving cert renewal)")
    parser.add_argument("--skip-extend-ca",   action="store_true",
                        help="Skip Phase 3.0 (cluster CA extension to 10 years)")
    parser.add_argument("--skip-certmanager", action="store_true",
                        help="Skip Phase 3.1 (cert-manager leaf cert renewal)")
    parser.add_argument("--skip-antrea",      action="store_true",
                        help="Skip Phase 4 (Antrea TLS renewal)")
    parser.add_argument("--skip-casync",      action="store_true",
                        help="Skip Phase 5 (containerd CA trust sync)")
    parser.add_argument(
        "--no-timestamps",
        action="store_true",
        help="Suppress the [timestamp] prefix on log lines (used when called "
             "by VCFfinal.py so lsf.write_output's timestamps are not doubled)",
    )

    args = parser.parse_args()

    global _SHOW_TIMESTAMPS
    if args.no_timestamps:
        _SHOW_TIMESTAMPS = False

    log_sep()
    log_info(f"vsp_cert_renewer.py v{VERSION} ({DATE})")
    log_info(f"Global threshold : {args.threshold_days}d")
    log_info(f"Renewal target   : {CERT_VALIDITY} (5 years via kubeadm certificateValidityPeriod)")
    log_info(f"Dry-run          : {args.dry_run}")
    log_sep()

    target = args.cluster

    if target == "all":
        for cname, ccfg in CLUSTERS.items():
            _check_cluster(cname, ccfg, args)
    elif target in CLUSTERS:
        _check_cluster(target, CLUSTERS[target], args)
    else:
        log_error(f"Unknown cluster '{target}' — valid choices: {list(CLUSTERS.keys())}")
        sys.exit(1)

    log_info("All requested clusters processed.")


if __name__ == "__main__":
    main()
