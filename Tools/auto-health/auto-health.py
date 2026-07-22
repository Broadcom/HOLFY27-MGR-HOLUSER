#!/usr/bin/env python3
"""
auto-health.py
Version 1.3.0 - 2026-07-22
Author: Burke Azbill and HOL Core Team

Comprehensive, read-only health check for VCF Automation (VCFA). VCFA runs as a
single-node Kubernetes cluster (the "auto-platform-a" appliance) hosting two
namespaces of interest: vmsp-platform (platform infra: gateways, kube-vip,
cert-manager, CAPI) and prelude (the ~50-70 VCF Automation microservices:
authentication, resource-manager, account-manager, encryption-manager,
intent-server, vcfa-service-manager, etc). Kyverno policy enforcement lives in
a third namespace, vmsp-policies.

This is the "run this first" sweep before reaching for vcfa-stabilizer.sh.

Sections reported:
  1. CONTROL PLANE       kube-vip VIP pinning (.69/.70/.72 on eth0),
                         vcfa-vip-watchdog.service, API healthz
                         (NOTE v1.1: the plndr-cp-lock lease-duration check was removed —
                         kube-vip v1.0.2 ignores vip_leaseduration for that Lease field
                         regardless of manifest config, so 15s vs 120s there is not a
                         real signal; see vcf-troubleshooting skill / vcfa-stabilizer.sh
                         v2.11 changelog for the full writeup)
  2. KUBERNETES NODES    Ready status, SchedulingDisabled
  3. POD OVERVIEW        All pods across ALL namespaces — one line per namespace
  4. VCFA CORE COMPONENTS  vmsp-platform + vmsp-policies infra (gateways, kube-vip
                         dataplane, CAPI IPAM, kyverno, cert-manager, trust-manager)
  5. AUTHENTICATION SERVICES  prelude auth/identity microservices — readiness
  6. GATEWAY DATAPLANE   kube-vip LoadBalancer Services (vcfa-gateway-configuration,
                         vmsp-gateway) exist and have an assigned VIP ingress IP
  7. HTTP ENDPOINT       /automation probe run from the VCFA node itself, expect 200
  8. TLS CERTIFICATES    cert-manager Certificate resources — readiness + expiry
  9. ARGO WORKFLOWS      Stale system-shutdown-* workflows in vmsp-platform
 10. ETCD                Informational defrag slack % (no action ever taken)

Section "edge" also checks (added v1.3.0, 2026-07-22): rabbitmq-ha-0's "copy-config" init
container is present, and (if present) that the AMQPS(5671) listener is actually up. Root
cause seen live: a prior vcfa-stabilizer.sh RabbitMQ cookie-permission fix used
`kubectl patch --type=merge` on spec.template.spec.initContainers, which under JSON Merge
Patch semantics REPLACES the whole array instead of merging by name -- silently deleting the
init container that copies rabbitmq.conf/enabled_plugins/definitions.json into /etc/rabbitmq.
RabbitMQ then starts with "Config file(s): (none)": no AMQPS listener, no plugins -- but
`rabbitmq-diagnostics ping` (the liveness/startup probe) still passes, so kubectl reports the
pod perfectly healthy while every AMQP client (ebs-app etc) gets "Connection refused", which
cascades into ~15 prelude Deployments stuck Pending/PodInitializing behind ebs-service. This
signal is invisible to sections 3-5 above (pod is Running, deployment is "ready") -- only a
targeted check for the init container + listener catches it.

No changes are made to the cluster — this is a check-only tool.
Remediation: bash vcfa-stabilizer.sh

Exit codes:
  0  All checks passed
  1  One or more checks failed
  2  Cannot connect to VCFA node
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

VERSION = "1.3.0"
DATE    = "2026-07-22"

CREDS_FILE = "/home/holuser/creds.txt"
VCFA_USER  = "vmware-system-user"
VCFA_HOST  = "10.1.1.73"          # VCFA K8s node (single-node appliance) - SSH target

# kube-vip VIPs pinned on eth0 of the VCFA node (see vcfa-stabilizer.sh fix_overload_recovery).
CP_VIP       = "10.1.1.72"        # control-plane VIP (kubernetes.default reachability)
VMSP_GW_VIP  = "10.1.1.69"        # vmsp-gateway Service LB VIP
VCFA_GW_VIP  = "10.1.1.70"        # vcfa-gateway-configuration Service LB VIP (auto-a /automation)

VMSP_NAMESPACE   = "vmsp-platform"
POLICIES_NAMESPACE = "vmsp-policies"
PRELUDE_NAMESPACE = "prelude"

# VCFA core infra components. (namespace, "kind/name"). Pulled from vcfa-stabilizer.sh's
# get_system_status() grep list, cross-checked against the live pod (2026-07-14):
#   - kyverno-admission-controller lives in vmsp-policies, NOT vmsp-platform (deviation from
#     the stabilizer's get_system_status() comment, which greps it alongside vmsp-platform pods).
#   - trust-manager-sds-server was NOT found on this build/pod at all (no such pod/deployment in
#     any namespace) -- kept in the list so the check degrades to a WARN "not found" rather than
#     silently disappearing; some VCFA builds may still ship it.
#   - There is no separate "cert-manager" namespace on this build; cert-manager/cert-manager-
#     cainjector/cert-manager-webhook/trust-manager all run inside vmsp-platform.
CORE_COMPONENTS = [
    (VMSP_NAMESPACE, "deployment/vmsp-gateway"),
    (VMSP_NAMESPACE, "deployment/vcfa-gateway-configuration"),
    (VMSP_NAMESPACE, "deployment/envoy-gateway"),
    (VMSP_NAMESPACE, "deployment/capi-ipam-in-cluster-controller-manager"),
    (VMSP_NAMESPACE, "deployment/synthetic-checker"),
    (VMSP_NAMESPACE, "deployment/hooks-server-synthetic-checker"),
    (VMSP_NAMESPACE, "deployment/cert-manager"),
    (VMSP_NAMESPACE, "deployment/cert-manager-cainjector"),
    (VMSP_NAMESPACE, "deployment/cert-manager-webhook"),
    (VMSP_NAMESPACE, "deployment/trust-manager"),
    (VMSP_NAMESPACE, "deployment/trust-manager-sds-server"),  # may not be deployed - see comment above
    (POLICIES_NAMESPACE, "deployment/kyverno-admission-controller"),
    (POLICIES_NAMESPACE, "deployment/kyverno-background-controller"),
    (POLICIES_NAMESPACE, "deployment/kyverno-cleanup-controller"),
]

# Authentication / identity microservices in prelude (vcfa-stabilizer.sh get_system_status()
# grep list). Live deployment names confirmed on pod: "authentication-server" (not bare
# "authentication"), "resource-manager-server", "account-manager-server".
AUTH_SERVICES = [
    (PRELUDE_NAMESPACE, "deployment/authentication-server"),
    (PRELUDE_NAMESPACE, "deployment/resource-manager-server"),
    (PRELUDE_NAMESPACE, "deployment/account-manager-server"),
    (PRELUDE_NAMESPACE, "deployment/encryption-manager"),
    (PRELUDE_NAMESPACE, "deployment/intent-server"),
    (PRELUDE_NAMESPACE, "deployment/vcfa-service-manager"),
]

# Gateway dataplane kube-vip LoadBalancer Services + the VIP each one should carry.
GATEWAY_SERVICES = [
    ("vcfa-gateway-configuration", VCFA_GW_VIP),
    ("vmsp-gateway", VMSP_GW_VIP),
]

# Pod waiting reasons considered "bad" (trigger a FAIL row) - same set as vsp-health.py.
BAD_REASONS = frozenset([
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "OOMKilled", "Error", "CreateContainerConfigError",
    "RunContainerError", "InvalidImageName", "ContainerCannotRun",
    "StartError",
])

# Namespaces displayed first in the pod overview (priority order)
_NS_PRIORITY = ["kube-system", "vmsp-platform", "vmsp-policies", "prelude"]

# ─── Colors ──────────────────────────────────────────────────────────────────
if sys.stdout.isatty():
    _CYAN, _BLUE, _GREEN, _RED, _YELLOW, _BOLD, _DIM, _NC = (
        '\033[0;36m', '\033[38;2;0;176;255m', '\033[0;32m',
        '\033[0;31m', '\033[1;33m', '\033[1m', '\033[2m', '\033[0m'
    )
else:
    _CYAN = _BLUE = _GREEN = _RED = _YELLOW = _BOLD = _DIM = _NC = ''

_OK   = f"{_GREEN}✓{_NC}"
_FAIL = f"{_RED}✗{_NC}"
_WARN = f"{_YELLOW}⚠{_NC}"


# ─── Help ─────────────────────────────────────────────────────────────────────
def show_help():
    W = 70
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VCF Automation (VCFA) Health Check — Full Suite':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    auto-health.py [--host IP] [--section NAME] [-v] [-j]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--host{_NC} <IP>         VCFA K8s node to SSH into  (default: {VCFA_HOST})")
    print(f"    {_GREEN}--section{_NC} <name>    Run only the named section (see below)")
    print(f"    {_GREEN}-v, --verbose{_NC}       Show raw command output and per-item details")
    print(f"    {_GREEN}-j, --json{_NC}          Emit final summary as JSON to stdout")
    print(f"    {_GREEN}-h, --help{_NC}          Show this help message\n")
    print(f"{_BOLD}SECTION NAMES:{_NC}  (use with --section)")
    for name, desc in [
        ("cp",       "Control plane (VIP pinning, vcfa-vip-watchdog, API healthz)"),
        ("nodes",    "Kubernetes node readiness"),
        ("pods",     "Pod health overview across all namespaces"),
        ("core",     "VCFA core components (vmsp-platform + vmsp-policies)"),
        ("auth",     "Authentication/identity microservices (prelude)"),
        ("gateway",  "Gateway dataplane kube-vip LoadBalancer Services"),
        ("endpoint", "/automation HTTP endpoint probe (expect 200)"),
        ("certs",    "cert-manager Certificate resources"),
        ("argo",     "Argo Workflow stale system-shutdown check"),
        ("edge",     "Known edge cases (runaway jobs, rm deadlock)"),
        ("etcd",     "etcd defrag slack % (informational only)"),
    ]:
        print(f"    {_GREEN}{name:<10}{_NC} {desc}")
    print(f"\n{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Full health check (all sections){_NC}")
    print(f"    python3 auto-health.py\n")
    print(f"    {_GREEN}# Check only the control plane and gateway dataplane{_NC}")
    print(f"    python3 auto-health.py --section cp\n")
    print(f"    {_GREEN}# Verbose pod overview with per-pod details{_NC}")
    print(f"    python3 auto-health.py --section pods --verbose\n")
    print(f"    {_GREEN}# JSON output for scripting/monitoring{_NC}")
    print(f"    python3 auto-health.py --json 2>/dev/null\n")
    print(f"    {_GREEN}# Specify an alternate VCFA node IP{_NC}")
    print(f"    python3 auto-health.py --host 10.1.1.73\n")
    print(f"{_BOLD}EXIT CODES:{_NC}")
    print(f"    0  All checks passed    1  One or more failed    2  Cannot connect")
    sys.exit(0)


# ─── SSH helpers ──────────────────────────────────────────────────────────────
_cached_password = None

def get_password():
    global _cached_password
    if _cached_password is None:
        try:
            with open(CREDS_FILE) as f:
                _cached_password = f.read().strip().splitlines()[0].strip()
        except OSError as e:
            print(f"{_RED}ERROR:{_NC} Cannot read {CREDS_FILE}: {e}", file=sys.stderr)
            sys.exit(2)
    return _cached_password


def ssh_exec(host, password, cmd, timeout=60):
    """Run cmd as root on host via sshpass + sudo -S -i + base64. Returns (rc, output).

    VCFA_USER (vmware-system-user) is not root; on VCF 9.1 sudo requires a password
    (unlike VCF 9.0's NOPASSWD), so we always pipe the password to sudo -S. sudo -S -i
    gives a root login shell, which is required for kubectl to be on PATH. We prepend
    a dynamic KUBECONFIG resolution to handle admin.conf or super-admin.conf.

    v1.3.0: the multi-line `cmd` is base64-decoded into a TEMP FILE first, then executed as
    `bash <file>` under sudo -- NOT inlined as a `bash -c "<multiline string>"` literal.
    Confirmed live (2026-07-22): `sudo -S -i bash -c "<multiline command substitution result>"`
    silently mangles the reconstructed command on this Photon OS sudo build -- it collapses ALL
    embedded newlines (turning a multi-line if/then/else/fi into one unparseable line, a syntax
    error) AND expands `$var` references that were inside single-quotes in the original script
    (e.g. an awk '{print $2}' had its $2 wiped to empty). This silently broke the pre-existing
    resource-manager deadlock check (edge_out["rm"]): RM_LISTENING/RM_DEADLOCK_SUSPECT/
    RM_NOT_FOUND never matched the mangled output, so chk_edge_cases() always fell through to
    its "clear" default -- a false-healthy result that nobody had noticed. Single-line commands
    (the vast majority of ssh_exec() callers) were never affected; only multi-line scripts with
    conditionals were at risk. The temp file is written pre-sudo (plain user, /tmp is writable)
    and removed after execution.
    """
    cmd_b64 = base64.b64encode(cmd.encode()).decode()
    script_path = f"/tmp/.auto-health-{os.getpid()}-{int(time.time() * 1000) % 1000000}.sh"
    outer = (
        f"echo {cmd_b64} | base64 -d > {script_path} && "
        f"echo '{password}' | sudo -S -i "
        f"bash -c \"export KUBECONFIG=\\$(test -f /etc/kubernetes/admin.conf && echo /etc/kubernetes/admin.conf || echo /etc/kubernetes/super-admin.conf); bash {script_path}\"; "
        f"rm -f {script_path} 2>&1"
    )
    _SUDO_RE = re.compile(r"\[sudo\] password for [^:]+:\s*")
    _NOISE   = ("Welcome to Photon", "Warning: Permanently added",
                "Connection to ", "Killed by signal")
    try:
        r = subprocess.run(
            ["sshpass", "-p", password, "ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "LogLevel=ERROR",
             "-o", "ConnectTimeout=15",
             f"{VCFA_USER}@{host}", outer],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        combined = (r.stdout or "") + (r.stderr or "")
        lines = []
        for line in combined.splitlines():
            line = _SUDO_RE.sub("", line)
            if not any(n in line for n in _NOISE):
                lines.append(line)
        return r.returncode, "\n".join(lines).strip()
    except subprocess.TimeoutExpired:
        return 1, f"SSH timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "sshpass not found — install: apt-get install sshpass"
    except Exception as exc:
        return 1, str(exc)


def parse_json(raw):
    """Find and parse first JSON object/array in raw string. Returns parsed value or None."""
    if not raw:
        return None
    start = None
    for i, ch in enumerate(raw):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError:
        return None


def fetch_json(host, password, cmd, timeout=60):
    """SSH + parse JSON. Returns parsed dict/list or None."""
    _, out = ssh_exec(host, password, cmd, timeout=timeout)
    return parse_json(out)


# ─── Output helpers ───────────────────────────────────────────────────────────
def section(title):
    bar = '─' * max(0, 60 - len(title))
    print(f"\n{_BOLD}{_CYAN}──── {title} {bar}{_NC}")


def row_ok(label, detail=""):
    suffix = f"  {_DIM}{detail}{_NC}" if detail else ""
    print(f"  {_OK} {label}{suffix}")
    return True


def row_fail(label, detail=""):
    suffix = f"  {_RED}{detail}{_NC}" if detail else ""
    print(f"  {_FAIL} {label}{suffix}")
    return False


def row_warn(label, detail=""):
    suffix = f"  {_YELLOW}{detail}{_NC}" if detail else ""
    print(f"  {_WARN} {label}{suffix}")
    return True  # warnings are advisory but count as "pass"


def row_verbose(msg, indent=6):
    print(f"{' ' * indent}{_DIM}{msg}{_NC}")


def _pod_ready(pod_json):
    """Extract (ready_count, total_count, in_crashloop, restarts) from a pod JSON object."""
    cstats   = pod_json.get("status", {}).get("containerStatuses", [])
    ready    = sum(1 for cs in cstats if cs.get("ready", False))
    total    = len(cstats)
    in_clbo  = any(
        cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
        for cs in cstats
    )
    restarts = max((cs.get("restartCount", 0) for cs in cstats), default=0)
    return ready, total, in_clbo, restarts


# ─── Section 1: Control Plane ─────────────────────────────────────────────────
def chk_control_plane(ip_out, lease_out, watchdog_out, healthz_out, verbose):
    """VIP pinning, vcfa-vip-watchdog.service, API healthz, plndr-cp-lock death-spiral check."""
    results = []

    # VIP pinning: each VIP must be present on eth0 as /32 with preferred_lft forever (not
    # deprecated). Parsed locally from a single `ip -4 addr show dev eth0` fetch. The kernel
    # emits the address on one line and its lifetime flags on the following line, e.g.:
    #   inet 10.1.1.72/32 scope global eth0
    #       valid_lft forever preferred_lft forever
    ip_lines = ip_out.splitlines()
    for vip, label in [(CP_VIP, "control-plane"), (VMSP_GW_VIP, "vmsp-gateway"),
                        (VCFA_GW_VIP, "vcfa-gateway-configuration")]:
        found, deprecated = False, False
        for i, ln in enumerate(ip_lines):
            if re.search(rf"\binet {re.escape(vip)}/32\b", ln):
                found = True
                lifetime_line = ip_lines[i + 1] if i + 1 < len(ip_lines) else ""
                if "deprecated" in lifetime_line or "preferred_lft forever" not in lifetime_line:
                    deprecated = True
                break
        if not found or deprecated:
            detail = "deprecated (aging out)" if found else "missing from eth0"
            results.append(row_fail(
                f"VIP {vip}/32 ({label}): pinned on eth0",
                f"{detail} — kube-vip may have dropped it; run vcfa-stabilizer.sh"
            ))
        else:
            results.append(row_ok(f"VIP {vip}/32 ({label}): pinned on eth0"))

    if verbose:
        row_verbose("ip -4 addr show dev eth0:")
        for line in ip_out.splitlines():
            row_verbose(f"  {line.strip()}")

    # plndr-cp-lock Lease: only the true death-spiral signature (<10s, originally observed as =1
    # during the Apr 2026 control-plane overload incident) is checked here. v1.0.0 of this tool
    # also flagged "!=120s" as a WARN ("not yet hardened"), but that was removed in v1.1: confirmed
    # live on this pod that kube-vip v1.0.2 ignores vip_leaseduration for this Lease field
    # regardless of manifest config, so it sits at the chart default (15s) permanently and a
    # `kubectl patch` to 120 reverts within one renewal cycle. 15s has no operational consequence on
    # this single-node control plane (nothing else ever contends for the lease) -- it was noise, not
    # a real signal. See vcfa-stabilizer.sh's v2.11 changelog / vcf-troubleshooting skill for detail.
    lease_val = lease_out.strip()
    if lease_val.isdigit():
        v = int(lease_val)
        if v < 10:
            results.append(row_fail(
                f"plndr-cp-lock Lease: leaseDurationSeconds={v}",
                "death-spiral signature (kube-vip can't renew under load) — run vcfa-stabilizer.sh now"
            ))
        elif verbose:
            row_verbose(f"plndr-cp-lock Lease: leaseDurationSeconds={v} "
                        "(kube-vip v1.0.2 quirk, not managed, harmless on single-node CP)")
    elif verbose:
        row_verbose("plndr-cp-lock Lease: leaseDurationSeconds unreadable or lease absent")

    # vcfa-vip-watchdog.service (event-driven VIP re-pinner installed by vcfa-stabilizer.sh v2.10+)
    if watchdog_out.strip() == "active":
        results.append(row_ok("vcfa-vip-watchdog.service: active"))
    elif "not-found" in watchdog_out or not watchdog_out.strip():
        results.append(row_warn("vcfa-vip-watchdog.service: active",
                                "not installed — run vcfa-stabilizer.sh to install the VIP drift watchdog"))
    else:
        results.append(row_warn(f"vcfa-vip-watchdog.service: {watchdog_out.strip()}",
                                "not active — run vcfa-stabilizer.sh"))

    # API server healthz
    if healthz_out.strip() == "ok":
        results.append(row_ok("Kubernetes API server: /healthz ok"))
    else:
        results.append(row_fail("Kubernetes API server: /healthz ok",
                                f"got: {healthz_out.strip()[:80] or '(no response)'}"))

    return results


# ─── Section 2: Kubernetes Nodes ─────────────────────────────────────────────
def chk_nodes(nodes_data, verbose):
    results = []
    if not nodes_data:
        return [row_fail("kubectl get nodes: success")]

    for item in nodes_data.get("items", []):
        name  = item.get("metadata", {}).get("name", "?")
        conds = item.get("status", {}).get("conditions", [])
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)
        unschedulable = item.get("spec", {}).get("unschedulable", False)

        if ready and not unschedulable:
            results.append(row_ok(f"Node {name}: Ready"))
        elif ready and unschedulable:
            results.append(row_warn(f"Node {name}: Ready — SchedulingDisabled",
                                    "cordoned; stale Argo system-shutdown workflow? see --section argo"))
        else:
            results.append(row_fail(f"Node {name}: Ready"))

    return results


# ─── Section 3: Pod Health Overview ──────────────────────────────────────────
def chk_pod_overview(pods_text, verbose):
    """One line per namespace via lightweight --no-headers text output (avoids large -A -o json)."""
    if not pods_text or pods_text.strip() == "":
        return [row_fail("kubectl get pods -A: success", "command returned no output")]

    ns_summary = defaultdict(lambda: {"healthy": 0, "total": 0, "bad": []})
    for line in pods_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ns, name, ready_str, status = parts[0], parts[1], parts[2], parts[3]
        try:
            cur, tot = (int(x) for x in ready_str.split("/"))
        except (ValueError, AttributeError):
            cur, tot = 0, 1

        healthy = (
            status in ("Completed", "Succeeded")
            or (status == "Running" and cur == tot)
        )
        ns_summary[ns]["total"] += 1
        if healthy:
            ns_summary[ns]["healthy"] += 1
        else:
            issue = (f"NotReady({cur}/{tot})" if status == "Running" else status)
            ns_summary[ns]["bad"].append((name, issue))

    if not ns_summary:
        return [row_fail("kubectl get pods -A: returned data", "no parseable pod lines")]

    def ns_sort(n):
        return (n not in _NS_PRIORITY,
                _NS_PRIORITY.index(n) if n in _NS_PRIORITY else 999,
                n)

    results = []
    W_NS = 20
    for ns in sorted(ns_summary.keys(), key=ns_sort):
        d = ns_summary[ns]
        h, t = d["healthy"], d["total"]
        bad  = d["bad"]
        count_str = f"{h}/{t} Running/Completed"
        if not bad:
            results.append(row_ok(f"{ns:<{W_NS}} {count_str}"))
        else:
            inline = ", ".join(f"{nm}: {issue}" for nm, issue in bad[:2])
            if len(bad) > 2:
                inline += f" (+{len(bad)-2} more)"
            results.append(row_fail(f"{ns:<{W_NS}} {count_str}", inline))
            if verbose:
                for nm, issue in bad:
                    row_verbose(f"  {ns}/{nm}: {issue}")
    return results


# ─── Sections 4/5: Deployment readiness (core components + auth services) ────
def chk_deployments(component_list, deps_data, verbose, section_label):
    """Generic spec.replicas vs status.readyReplicas check, shared by core+auth sections."""
    results = []

    wl = {}
    if deps_data:
        for item in deps_data.get("items", []):
            ns    = item.get("metadata", {}).get("namespace", "?")
            name  = item.get("metadata", {}).get("name", "?")
            spec  = item.get("spec", {}).get("replicas", 0) or 0
            ready = item.get("status", {}).get("readyReplicas", 0) or 0
            wl[(ns, "deployment", name)] = {"spec": spec, "ready": ready}

    W = 62
    prev_ns = None
    for ns, resource in component_list:
        kind, name = resource.split("/", 1)

        if verbose and ns != prev_ns:
            print(f"  {_DIM}── {ns} ──{_NC}")
            prev_ns = ns

        label = f"{ns}/{resource}"
        key   = (ns, kind, name)

        if key not in wl:
            results.append(row_warn(f"{label:<{W}}", "not found — may not be deployed on this build"))
            continue

        spec  = wl[key]["spec"]
        ready = wl[key]["ready"]

        if spec == 0:
            results.append(row_warn(f"{label:<{W}} 0/0",
                                    "scaled to 0 — stopped; scale up manually or via VCFfinal.py"))
        elif ready >= spec:
            results.append(row_ok(f"{label:<{W}} {ready}/{spec}"))
        else:
            results.append(row_fail(f"{label:<{W}} {ready}/{spec} ready",
                                    f"{spec - ready} pod(s) not yet ready"))

    return results


# ─── Section 6: Gateway Dataplane ────────────────────────────────────────────
def chk_gateway(svc_data, verbose):
    """Confirm the two kube-vip LoadBalancer Services exist with the expected VIP ingress IP."""
    results = []
    if not svc_data:
        return [row_fail("kubectl get svc -n vmsp-platform: success", "no response from kubectl")]

    svc_by_name = {i.get("metadata", {}).get("name"): i for i in svc_data.get("items", [])}

    for name, expected_vip in GATEWAY_SERVICES:
        svc = svc_by_name.get(name)
        if not svc:
            results.append(row_fail(f"Service {name} (vmsp-platform): exists", "not found"))
            continue
        svc_type = svc.get("spec", {}).get("type", "?")
        ingress  = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        ips      = [i.get("ip") for i in ingress if i.get("ip")]

        if svc_type != "LoadBalancer":
            results.append(row_warn(f"Service {name}: type={svc_type}", "expected LoadBalancer"))
            continue
        if not ips:
            results.append(row_fail(f"Service {name}: LoadBalancer ingress IP assigned",
                                    "no ingress IP — kube-vip has not attached the VIP; run vcfa-stabilizer.sh"))
        elif expected_vip in ips:
            results.append(row_ok(f"Service {name}: LoadBalancer ingress={ips[0]}"))
        else:
            results.append(row_warn(f"Service {name}: LoadBalancer ingress={ips[0]}",
                                    f"expected {expected_vip}"))
    return results


# ─── Section 7: HTTP Endpoint ────────────────────────────────────────────────
def chk_endpoint(http_code, verbose):
    """curl probe run FROM the VCFA node itself, same technique as vcfa_curl_automation_code()."""
    code = http_code.strip()
    if code == "200":
        return [row_ok(f"https://auto-a.site-a.vcf.lab/automation (via {VCFA_GW_VIP}): HTTP {code}")]
    return [row_fail(f"https://auto-a.site-a.vcf.lab/automation (via {VCFA_GW_VIP}): HTTP {code or '000'}",
                     "expected 200 — see check_and_fix_ccs_k3s_cert / recover_gateway_http_503 in vcfa-stabilizer.sh")]


# ─── Section 8: TLS Certificates ─────────────────────────────────────────────
def chk_certificates(certs_data, verbose):
    """cert-manager Certificate resources — readiness + days to expiry.

    VCFA typically has 50-60+ Certificate resources (mostly per-microservice mTLS certs in
    prelude, plus platform certs in vmsp-platform). To keep default output readable, only
    non-Ready or soon-to-expire certs get their own row; a summary row covers the healthy bulk.
    Pass -v/--verbose to see every certificate individually.
    """
    results = []
    if not certs_data:
        results.append(row_warn("cert-manager certificates: API available",
                                "no response — cert-manager may not be installed"))
        return results

    items = certs_data.get("items", [])
    if not items:
        results.append(row_ok("cert-manager Certificate resources: none (not installed or no certs)"))
        return results

    now = datetime.now(timezone.utc)
    not_ready = []   # (label, detail) - always FAIL
    expiring  = []   # (label, detail) - always WARN
    healthy   = 0

    for item in items:
        ns   = item.get("metadata", {}).get("namespace", "?")
        name = item.get("metadata", {}).get("name", "?")
        conds = item.get("status", {}).get("conditions", [])
        ready_cond = next((c for c in conds if c.get("type") == "Ready"), None)
        not_after  = item.get("status", {}).get("notAfter", "")
        cert_ready = bool(ready_cond and ready_cond.get("status") == "True")

        expiry_str  = ""
        warn_expiry = False
        if not_after:
            try:
                exp_dt = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                days   = (exp_dt - now).days
                if days < 0:
                    expiry_str, warn_expiry = f"EXPIRED {abs(days)}d ago", True
                elif days < 30:
                    expiry_str, warn_expiry = f"expires in {days}d", True
                else:
                    expiry_str = f"expires in {days}d"
            except Exception:
                expiry_str = not_after

        label = f"{name} ({ns})"
        if not cert_ready:
            reason = ready_cond.get("reason", "?") if ready_cond else "no Ready condition"
            not_ready.append((label, f"{reason}  {expiry_str}".strip()))
        elif warn_expiry:
            expiring.append((label, expiry_str))
        else:
            healthy += 1
            if verbose:
                results.append(row_ok(f"{label}: Ready", expiry_str))

    # Summary row for the healthy bulk. In non-verbose mode this single row represents all
    # `healthy` certs (kept out of the per-check tally as one aggregate row so 60 certs don't
    # inflate "checks_total" to 60); in verbose mode each healthy cert was already appended
    # above via row_ok, so this is just an additional context line, not counted again.
    if not verbose:
        results.append(row_ok(f"cert-manager Certificates: {healthy}/{len(items)} Ready, not expiring soon"))
    else:
        row_ok(f"cert-manager Certificates: {healthy}/{len(items)} Ready, not expiring soon (see rows above)")

    for label, detail in not_ready:
        results.append(row_fail(f"{label}: Ready", detail))
    for label, detail in expiring:
        results.append(row_warn(f"{label}: Ready — {detail}", "renew via vcfa-stabilizer.sh / cert-manager should auto-renew"))

    return results


# ─── Section 9: Argo Workflows ────────────────────────────────────────────────
def chk_argo(workflows_text, verbose):
    """Stale system-shutdown-* Argo Workflows in vmsp-platform re-cordon the node and scale
    prelude deployments to 0 on the next reconcile after a cold boot (see CLAUDE.md / vcf-
    troubleshooting Section 50)."""
    results = []
    out = workflows_text

    if not out or "No resources found" in out or "error: the server doesn't have" in out.lower():
        results.append(row_ok("Argo workflows: API available, none found"))
        return results

    all_wf = [ln for ln in out.splitlines() if ln.strip()]
    bad_wf = [ln for ln in all_wf if "system-shutdown" in ln]
    other  = len(all_wf) - len(bad_wf)

    if other > 0:
        results.append(row_ok(f"Argo workflows: {other} non-shutdown workflow(s) present"))

    if bad_wf:
        results.append(row_fail(
            "Stale system-shutdown workflows: 0 found",
            f"{len(bad_wf)} stale — may re-cordon node and scale prelude to 0; "
            f"kubectl delete workflow -n {VMSP_NAMESPACE} <name>"
        ))
        if verbose:
            for ln in bad_wf[:10]:
                row_verbose(f"  {ln.strip()}")
    else:
        results.append(row_ok("Stale system-shutdown Argo workflows: none"))

    return results


# ─── Section 9.5: Edge Cases ────────────────────────────────────────────────
def chk_edge_cases(edge_out, pods_text, verbose):
    """Detect known edge cases like support-bundle runaway and RM deadlock."""
    results = []

    # 1. Support Bundle Runaway
    jobs_data = edge_out.get("jobs", [])
    if jobs_data is None:
        results.append(row_fail("Support bundle check", "failed to fetch jobs JSON"))
    else:
        num_jobs = len(jobs_data)
        if num_jobs > 3:
            results.append(row_fail(f"Support bundle runaway: {num_jobs} jobs found", f"threshold is 3, runaway detected!"))
        elif num_jobs > 0:
            results.append(row_ok(f"Support bundle runaway: {num_jobs} jobs found (under threshold)"))
        else:
            results.append(row_ok("Support bundle runaway: 0 jobs found"))

    # 2. Resource Manager Deadlock
    rm_status = edge_out.get("rm", "").strip()
    rm_pod_line = ""
    if pods_text:
        for line in pods_text.splitlines():
            if "resource-manager-server-" in line:
                rm_pod_line = line
                break

    if rm_status == "RM_DEADLOCK_SUSPECT" and "1/1" not in rm_pod_line:
        results.append(row_fail("resource-manager deadlock", "pod is not ready and no listener on 7710/7777 (deadlock!)"))
    elif rm_status == "RM_LISTENING":
        results.append(row_ok("resource-manager deadlock: listening normally"))
    elif rm_status == "RM_NOT_FOUND":
        results.append(row_warn("resource-manager deadlock: process not found", "pod may be missing or restarting"))
    else:
        results.append(row_ok("resource-manager deadlock: clear"))

    # 3. RabbitMQ copy-config init container integrity + AMQPS(5671) listener (added v1.3.0).
    # Root cause seen live: a `kubectl patch --type=merge` on spec.template.spec.initContainers
    # (in an earlier vcfa-stabilizer.sh RabbitMQ cookie-permission fix) replaced the WHOLE
    # initContainers array under JSON Merge Patch semantics, silently deleting the "copy-config"
    # init container that populates /etc/rabbitmq from the rabbitmq-ha ConfigMap + db-credentials
    # Secret. rabbitmq-ha-0 then starts with "Config file(s): (none)" -- no AMQPS(5671) listener,
    # no management plugin -- but `rabbitmq-diagnostics ping` (the liveness/startup probe) still
    # passes, so the pod shows Running/Ready to kubectl while every AMQP client gets "Connection
    # refused". This cascades into ~15 prelude Deployments stuck Pending/PodInitializing behind
    # ebs-service, invisible to the CORE/AUTH deployment-readiness sections above. Read-only:
    # only reports; fix lives in vcfa-stabilizer.sh's fix_vcf_final_edge_cases.
    rmq_status = edge_out.get("rmq", "").strip()
    if rmq_status == "RMQ_OK":
        results.append(row_ok("RabbitMQ copy-config + AMQPS(5671) listener: healthy"))
    elif rmq_status == "RMQ_NO_COPY_CONFIG":
        results.append(row_fail(
            "RabbitMQ copy-config init container: present",
            "MISSING from rabbitmq-ha StatefulSet -- pod reports Ready via rabbitmq-diagnostics "
            "ping but has no real config (no AMQPS listener); AMQP clients (ebs-app etc) will see "
            "Connection refused. Run vcfa-stabilizer.sh (fix_vcf_final_edge_cases restores it)."
        ))
    elif rmq_status == "RMQ_LISTENER_DOWN":
        results.append(row_fail(
            "RabbitMQ AMQPS(5671) listener: up",
            "copy-config init container is present but port 5671 is not listening -- possible "
            "TLS cert/config problem (rabbitmq-tls secret?); not the copy-config bug. Investigate "
            "rabbitmq-ha-0 logs; not auto-fixed."
        ))
    elif rmq_status.startswith("RMQ_NOT_RUNNING"):
        phase = rmq_status.split(":", 1)[1] if ":" in rmq_status else "?"
        results.append(row_warn(f"RabbitMQ pod rabbitmq-ha-0: phase={phase}", "not yet Running — may still be starting"))
    elif rmq_status == "RMQ_NOT_FOUND":
        results.append(row_warn("RabbitMQ pod rabbitmq-ha-0: found", "not found — may not be deployed on this build"))
    else:
        results.append(row_warn("RabbitMQ copy-config / AMQPS listener check", "could not determine status"))

    return results


# ─── Section 10: etcd (informational only — never defrags) ──────────────────
def chk_etcd(etcd_json_out, verbose):
    """Report current etcd defrag slack % (dbSize vs dbSizeInUse). Informational only —
    this tool never triggers a defrag; see vcfa-stabilizer.sh's fix_overload_recovery /
    ETCD_DEFRAG_SLACK_PCT for the actual remediation (default threshold 30%)."""
    results = []
    data = parse_json(etcd_json_out)
    if not data:
        results.append(row_warn("etcd endpoint status: readable", "no response — etcd may be unreachable"))
        return results

    try:
        status = data[0]["Status"]
        db     = int(status.get("dbSize", 0))
        inuse  = int(status.get("dbSizeInUse", 0)) or db
        slack  = 0 if db == 0 else int(100 * (db - inuse) / db)
        db_mb, inuse_mb = db / 1048576, inuse / 1048576
    except (KeyError, IndexError, ValueError, TypeError):
        results.append(row_warn("etcd endpoint status: parseable", "unexpected JSON shape"))
        return results

    detail = f"dbSize={db_mb:.1f}MiB inUse={inuse_mb:.1f}MiB slack={slack}% (informational only, no action taken)"
    if slack >= 30:
        results.append(row_warn(f"etcd defrag slack: {slack}%", detail + " — vcfa-stabilizer.sh default threshold is 30%"))
    else:
        results.append(row_ok(f"etcd defrag slack: {slack}%", detail))
    return results


# ─── Main ─────────────────────────────────────────────────────────────────────
SECTION_MAP = {
    "cp":       "CONTROL PLANE",
    "nodes":    "KUBERNETES NODES",
    "pods":     "POD HEALTH OVERVIEW",
    "core":     "VCFA CORE COMPONENTS",
    "auth":     "AUTHENTICATION SERVICES",
    "gateway":  "GATEWAY DATAPLANE",
    "endpoint": "HTTP ENDPOINT",
    "certs":    "TLS CERTIFICATES",
    "argo":     "ARGO WORKFLOWS",
    "edge":     "KNOWN EDGE CASES",
    "etcd":     "ETCD (informational)",
}


class _QuietArgumentParser(argparse.ArgumentParser):
    """Never print argparse's own usage/error text — always fall back to show_help()."""
    def error(self, message):
        show_help()


def main():
    global VCFA_HOST
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()

    parser = _QuietArgumentParser(add_help=False)
    parser.add_argument("--host",    default=VCFA_HOST, metavar="IP")
    parser.add_argument("--section", default=None,       metavar="NAME",
                        choices=list(SECTION_MAP.keys()))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-j", "--json",    action="store_true")

    try:
        args = parser.parse_args()
    except SystemExit:
        show_help()

    host     = args.host
    if host == "10.1.1.73":
        import socket
        for cand in ["10.1.1.71", "10.1.1.72", "10.1.1.73", "10.1.1.74"]:
            s = socket.socket()
            s.settimeout(2)
            if s.connect_ex((cand, 22)) == 0:
                host = cand
                VCFA_HOST = cand
                break
            s.close()

    password = get_password()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W  = 70

    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VCF Automation (VCFA) Health Check — Full Suite':^{W}}{_NC}{_CYAN}║{_NC}")
    if args.section:
        print(f"{_CYAN}║{_NC}{f'Section: {SECTION_MAP[args.section]}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ts:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")

    # Connectivity test
    print(f"{_DIM}  Testing SSH to {host}...{_NC}", end="", flush=True)
    rc, out = ssh_exec(host, password, "echo PONG", timeout=20)
    if rc != 0 or "PONG" not in out:
        print()
        print(f"\n{_RED}ERROR:{_NC} Cannot SSH+sudo to {host} as {VCFA_USER}.")
        print(f"  {_DIM}{out}{_NC}")
        print(f"  If host is wrong: python3 auto-health.py --host <actual-VCFA-node-IP>")
        sys.exit(2)
    print(f" {_OK}")

    # ── Bulk data fetch ────────────────────────────────────────────────────────
    want = args.section  # None = all sections
    print(f"{_DIM}  Fetching VCFA state (this may take ~10-30s)...{_NC}", flush=True)

    needs_cp       = want in (None, "cp")
    needs_nodes    = want in (None, "nodes")
    needs_pods     = want in (None, "pods", "edge")
    needs_deploys  = want in (None, "core", "auth")
    needs_gateway  = want in (None, "gateway")
    needs_endpoint = want in (None, "endpoint")
    needs_certs    = want in (None, "certs")
    needs_argo     = want in (None, "argo")
    needs_edge     = want in (None, "edge")
    needs_etcd     = want in (None, "etcd")

    ip_out = lease_out = watchdog_out = healthz_out = ""
    if needs_cp:
        ip_out       = ssh_exec(host, password, "ip -4 addr show dev eth0 2>/dev/null", timeout=20)[1]
        lease_out    = ssh_exec(host, password,
                                "kubectl -n kube-system get lease plndr-cp-lock "
                                "-o jsonpath='{.spec.leaseDurationSeconds}' 2>/dev/null", timeout=20)[1]
        watchdog_out = ssh_exec(host, password,
                                "systemctl is-active vcfa-vip-watchdog.service 2>&1", timeout=20)[1]
        healthz_out  = ssh_exec(host, password, "kubectl get --raw /healthz 2>/dev/null", timeout=20)[1]

    nodes_data = fetch_json(host, password, "kubectl get nodes -o json 2>/dev/null", 30) \
                 if needs_nodes else None
    pods_text  = ssh_exec(host, password, "kubectl get pods -A --no-headers 2>/dev/null", timeout=60)[1] \
                 if needs_pods else ""
    # Single cluster-wide fetch feeds both core + auth sections (mirrors vsp-health.py's
    # VCF_COMPONENTS pattern). ~1.6MB JSON on this build; observed ~3s over a double SSH hop.
    deps_data  = fetch_json(host, password, "kubectl get deployments -A -o json 2>/dev/null", 90) \
                 if needs_deploys else None
    svc_data   = fetch_json(host, password,
                            f"kubectl get svc -n {VMSP_NAMESPACE} -o json 2>/dev/null", 30) \
                 if needs_gateway else None
    http_code  = ssh_exec(host, password,
                          "curl -k -s -o /dev/null -w '%{http_code}' --connect-timeout 8 "
                          f"--resolve auto-a.site-a.vcf.lab:443:{VCFA_GW_VIP} "
                          "https://auto-a.site-a.vcf.lab/automation 2>/dev/null || echo 000",
                          timeout=30)[1] if needs_endpoint else ""
    certs_data = fetch_json(host, password, "kubectl get certificates -A -o json 2>/dev/null", 45) \
                 if needs_certs else None
    argo_out   = ssh_exec(host, password,
                          f"kubectl get workflows -n {VMSP_NAMESPACE} --no-headers 2>&1", timeout=30)[1] \
                 if needs_argo else ""
                 
    edge_out   = {}
    if needs_edge:
        edge_out["jobs"] = fetch_json(host, password, "kubectl get jobs -n vmsp-platform -l app.kubernetes.io/name=support-bundle-cluster-info-dump -o json 2>/dev/null", 30)
        edge_out["rm"] = ssh_exec(host, password, """
            rm_pid=$(ps -ef | grep "/bin/resource-manager " | grep -v grep | awk '{print $2}' | head -n1)
            if [ -n "$rm_pid" ]; then
                if nsenter -t "$rm_pid" -n netstat -tlpn 2>/dev/null | grep -qE ":7710|:7777"; then
                    echo "RM_LISTENING"
                else
                    echo "RM_DEADLOCK_SUSPECT"
                fi
            else
                echo "RM_NOT_FOUND"
            fi
        """, timeout=30)[1]
        # RabbitMQ copy-config init container integrity + AMQPS(5671) listener (v1.3.0). Read-only:
        # checks the StatefulSet spec (jsonpath) and, only if the init container is present, runs
        # `rabbitmqctl status` inside the pod to confirm the SSL listener is actually up. See the
        # module docstring / chk_edge_cases() for the full root-cause writeup.
        edge_out["rmq"] = ssh_exec(host, password, """
            rmq_status_line=$(kubectl get pod rabbitmq-ha-0 -n prelude --no-headers 2>/dev/null)
            if [ -z "$rmq_status_line" ]; then
                echo "RMQ_NOT_FOUND"
            else
                rmq_phase=$(kubectl get pod rabbitmq-ha-0 -n prelude -o jsonpath='{.status.phase}' 2>/dev/null)
                if [ "$rmq_phase" != "Running" ]; then
                    echo "RMQ_NOT_RUNNING:$rmq_phase"
                else
                    has_copy_config=$(kubectl get statefulset rabbitmq-ha -n prelude -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="copy-config")].name}' 2>/dev/null)
                    if [ -z "$has_copy_config" ]; then
                        echo "RMQ_NO_COPY_CONFIG"
                    else
                        if kubectl exec rabbitmq-ha-0 -n prelude -c rabbitmq-ha -- rabbitmqctl status 2>/dev/null | grep -q "port: 5671"; then
                            echo "RMQ_OK"
                        else
                            echo "RMQ_LISTENER_DOWN"
                        fi
                    fi
                fi
            fi
        """, timeout=30)[1]

    etcd_out   = ssh_exec(host, password,
                          "etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt "
                          "--cert=/etc/kubernetes/pki/etcd/peer.crt "
                          "--key=/etc/kubernetes/pki/etcd/peer.key "
                          "--endpoints=https://127.0.0.1:2379 endpoint status -w json 2>/dev/null",
                          timeout=30)[1] if needs_etcd else ""

    # ── Run sections ──────────────────────────────────────────────────────────
    all_results: dict = {}

    def run(key, title, fn, *fn_args):
        if want and want != key:
            return
        section(title)
        rows = fn(*fn_args)
        for i, r in enumerate(rows):
            all_results[f"{key}_{i}"] = r

    run("cp",       "CONTROL PLANE",
        chk_control_plane, ip_out, lease_out, watchdog_out, healthz_out, args.verbose)

    run("nodes",    "KUBERNETES NODES",
        chk_nodes, nodes_data, args.verbose)

    run("pods",     "POD HEALTH OVERVIEW  (all namespaces)",
        chk_pod_overview, pods_text, args.verbose)

    run("core",     "VCFA CORE COMPONENTS  (vmsp-platform + vmsp-policies)",
        chk_deployments, CORE_COMPONENTS, deps_data, args.verbose, "core")

    run("auth",     "AUTHENTICATION SERVICES  (prelude)",
        chk_deployments, AUTH_SERVICES, deps_data, args.verbose, "auth")

    run("gateway",  "GATEWAY DATAPLANE  (kube-vip LoadBalancer Services)",
        chk_gateway, svc_data, args.verbose)

    run("endpoint", "HTTP ENDPOINT  (/automation probe)",
        chk_endpoint, http_code, args.verbose)

    run("certs",    "TLS CERTIFICATES  (cert-manager)",
        chk_certificates, certs_data, args.verbose)

    run("argo",     f"ARGO WORKFLOWS  ({VMSP_NAMESPACE})",
        chk_argo, argo_out, args.verbose)

    run("edge",     "KNOWN EDGE CASES  (Support bundle runaway, RM deadlock)",
        chk_edge_cases, edge_out, pods_text, args.verbose)

    run("etcd",     "ETCD  (informational — no action taken)",
        chk_etcd, etcd_out, args.verbose)

    # ── Summary ───────────────────────────────────────────────────────────────
    total  = len(all_results)
    failed = sum(1 for v in all_results.values() if v is False)
    color  = _GREEN if failed == 0 else _RED

    print(f"\n{_CYAN}{'─' * 64}{_NC}")
    print(f"  {color}{_BOLD}RESULT: {total - failed}/{total} checks passed{_NC}")
    if failed:
        print(f"  {_RED}  {failed} check(s) require attention — see {_FAIL} rows above{_NC}")
        print(f"  {_DIM}  Remediation: bash vcfa-stabilizer.sh{_NC}")
    else:
        print(f"  {_GREEN}  VCFA is healthy{_NC}")
    print(f"{_CYAN}{'─' * 64}{_NC}\n")

    if args.json:
        summary = {
            "timestamp": ts,
            "vcfa_host": host,
            "section_filter": args.section,
            "checks_passed": total - failed,
            "checks_failed": failed,
            "checks_total": total,
            "healthy": failed == 0,
            "detail": {k: v for k, v in all_results.items()},
        }
        print(json.dumps(summary, indent=2))

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
