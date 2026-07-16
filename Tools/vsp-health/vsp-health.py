#!/usr/bin/env python3
"""
vsp-health.py
Version 2.5.0 - 2026-07-16
Author: Burke Azbill and HOL Core Team

v2.5.0: CP host resolution now tries candidates in order — explicit --host,
then the hardcoded VSP_VIP, then auto-discovery via --worker — stopping at
the first one that answers SSH, instead of auto-discovery being the only
fallback for a missing --host.

v2.4.0: Added total + per-section elapsed-time reporting (console and the
--json output's "elapsed_seconds" field), so a slow run is visible without
timing it by hand — added while investigating why vsp-health-monitor.py felt
slow; this tool's sections are the same kubectl/SSH calls, so the same
per-check granularity is useful here too.

Comprehensive, read-only health check of the VSP (Supervisor) cluster.
Dynamically discovers and reports the status of EVERY component running
in the cluster — not just Salt.

Sections reported:
  1. CONTROL PLANE     kube-vip VIP, manifest setting, static pods via crictl
  2. KUBERNETES NODES  Ready status, SchedulingDisabled
  3. POD OVERVIEW      All pods across ALL namespaces — one line per namespace
  4. VCF COMPONENTS    All 25 VCF-managed workloads (spec vs readyReplicas),
                       plus components.api.vmsp.vmware.com operational-status
  5. POSTGRESQL        Zalando Spilo instances — readiness + suspended check +
                       numberOfInstances vs. saved original-instances annotation
  6. REDIS & RAAS      Pod readiness, endpoint population, CrashLoopBackOff
  7. SALT STACK        Pod readiness + log tail for known error signatures
  8. TLS CERTIFICATES  cert-manager Certificate resources — readiness + expiry
  9. ARGO WORKFLOWS    Stale system-shutdown workflows + power-off-marker
                       ConfigMap (KB 440862 boot-deadlock signature)
  10. VODAP HEALTH     ClickHouse served-cert-vs-secret mismatch (a cert-manager
                       Certificate check alone can't see this — ClickHouse
                       never hot-reloads) + logging-operator-fluentd buffers
  11. NODE PROXY       VSP node proxy config vs. lsfunctions.LAB_PROXY_URL
  12. KUBEADM CERTS    kubeadm's own cert population (separate from
                       cert-manager's Certificate CRDs in section 8)

No changes are made to the cluster — this is a check-only tool. Every one of
these failure modes IS actively remediated by the companion
vsp-health-monitor.py (checks: kvip_manifest, cp_pod_crash, postgres,
salt_stack, vodap, component_health, argo_cleanup, proxy_config,
cert_renewal) on its 5-min cron cycle — this tool is for point-in-time
diagnosis, not the fix path.
Manual remediation tools (same fixes, run on demand):
  Salt issues:          python3 salt-stabilize.py
  Control plane issues: python3 kube-fix.py
  Vodap/ClickHouse:     python3 vodap-fix.py

Every line printed to the console is also appended (ANSI codes stripped) to
LOG_FILE (/tmp/vsp-health.log) for a persistent, auditable record of the run
— see the print() shadow below.

Exit codes:
  0  All checks passed
  1  One or more checks failed
  2  Cannot connect to VSP cluster
"""
import argparse
import base64
import json
import re
import socket
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

VERSION = "2.5.0"
DATE    = "2026-07-16"

CREDS_FILE = "/home/holuser/creds.txt"
VSP_USER   = "vmware-system-user"
VSP_WORKER = "vsp-01a.site-a.vcf.lab"
VSP_VIP    = "10.1.1.142"
LOG_FILE   = "/tmp/vsp-health.log"
LAB_PROXY_URL = "http://10.1.1.1:3128"  # same value as lsfunctions.LAB_PROXY_URL

# Pod waiting reasons considered "bad" (trigger a FAIL row)
BAD_REASONS = frozenset([
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "OOMKilled", "Error", "CreateContainerConfigError",
    "RunContainerError", "InvalidImageName", "ContainerCannotRun",
    "StartError",
])

# VCF-managed workloads — matches [VCFFINAL] vcfcomponents in config.ini
# Format: (namespace, "kind/name")
VCF_COMPONENTS = [
    ("salt",               "deployment/salt-master"),
    ("salt",               "deployment/salt-minion"),
    ("salt-raas",          "deployment/redis"),
    ("salt-raas",          "deployment/raas"),
    ("telemetry",          "deployment/telemetry-acceptor"),
    ("vcf-fleet-depot",    "deployment/depot-service"),
    ("vcf-fleet-depot",    "deployment/distribution-service"),
    ("vcf-fleet-lcm",      "deployment/vcf-fleet-build-service-fleetbuild"),
    ("vcf-fleet-lcm",      "deployment/vcf-fleet-upgrade-service-fleetupgrade"),
    ("vcf-sddc-lcm",       "deployment/vcf-sddc-build-service-sddcbuild"),
    ("vcf-sddc-lcm",       "deployment/vcf-sddc-upgrade-service-sddcupgrade"),
    ("vidb-external",      "deployment/vidb-service"),
    ("ops-logs",           "statefulset/log-processor"),
    ("ops-logs",           "statefulset/log-store"),
    ("vodap",              "deployment/vcf-obs-collector-controller-service"),
    ("vodap",              "deployment/vcf-obs-data-query-service"),
    ("vodap",              "deployment/vcf-obs-esx-collector-service"),
    ("vodap",              "deployment/vcf-obs-netops-collector-service"),
    ("vodap",              "deployment/vcf-obs-vc-collector-service"),
    ("vodap",              "statefulset/chi-vcf-obs-vcf-obs-0-0"),
    ("vodap",              "statefulset/chk-vcf-obs-keeper-keeper-0-0"),
    ("vodap",              "statefulset/chk-vcf-obs-keeper-keeper-0-1"),
    ("vodap",              "statefulset/chk-vcf-obs-keeper-keeper-0-2"),
    ("vmsp-metrics-store", "deployment/clickhouse-operator-altinity-clickhouse-operator"),
    ("vmsp-metrics-store", "deployment/vsp-metrics-store-operator"),
]

# vodap deployments that depend on ClickHouse (section 10)
CLICKHOUSE_CLIENTS = ["vcf-obs-data-query-service", "vcf-obs-netops-collector-service"]

# Namespaces displayed first in the pod overview (priority order)
_NS_PRIORITY = ["kube-system", "vmsp-platform", "cert-manager", "antrea", "istio-system"]

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


# ─── Logging (mirrors vsp-health-monitor.py's on-disk record) ────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
_stdout_print = print  # keep a handle to the real builtin


def print(*args, **kwargs):
    """Shadow the builtin print(): behaves exactly like print() on the
    console (every existing call site — header/rows/sections/summary/help —
    needs no change), but also appends an ANSI-stripped copy of the same
    text to LOG_FILE, so an interactive run leaves the same kind of
    persistent, auditable record vsp-health-monitor.py already keeps.
    Calls explicitly targeting stderr (file=sys.stderr) are still printed
    normally but are not captured into LOG_FILE."""
    _stdout_print(*args, **kwargs)
    if kwargs.get('file') not in (None, sys.stdout):
        return
    text = kwargs.get('sep', ' ').join(str(a) for a in args)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(_ANSI_RE.sub('', text) + '\n')
    except Exception:
        pass


# ─── Help ─────────────────────────────────────────────────────────────────────
def show_help():
    W = 70
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VSP Cluster Health Check — Full Suite':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    vsp-health.py [--host IP] [--worker FQDN] [--section NAME] [-v] [-j]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--host{_NC} <IP>         VSP control-plane host, tried first if set")
    print(f"    {_GREEN}--worker{_NC} <FQDN>     VSP worker for CP auto-discovery  (default: {VSP_WORKER})")
    print(f"    {_GREEN}--section{_NC} <name>    Run only the named section (see below)")
    print(f"    {_GREEN}-v, --verbose{_NC}       Show raw command output and per-pod details")
    print(f"    {_GREEN}-j, --json{_NC}          Emit final summary as JSON to stdout")
    print(f"    {_GREEN}-h, --help{_NC}          Show this help message\n")
    print(f"{_BOLD}CP HOST RESOLUTION ORDER:{_NC}")
    print(f"    1. {_GREEN}--host{_NC} <IP>, if given")
    print(f"    2. Hardcoded VIP  ({VSP_VIP})")
    print(f"    3. Auto-discovery via {_GREEN}--worker{_NC}")
    print(f"    Each candidate is tried via SSH; the first reachable one wins.\n")
    print(f"{_BOLD}SECTION NAMES:{_NC}  (use with --section)")
    for name, desc in [
        ("cp",       "Control plane (kube-vip, crictl, VIP)"),
        ("nodes",    "Kubernetes node readiness"),
        ("pods",     "Pod health overview across all namespaces"),
        ("vcf",      "VCF managed workloads (vcfcomponents)"),
        ("postgres", "Zalando Spilo PostgreSQL instances"),
        ("redis",    "Redis pod + service endpoints"),
        ("salt",     "Salt master/minion deep check + logs"),
        ("certs",    "cert-manager Certificate resources"),
        ("argo",     "Argo Workflow stale shutdown + power-off-marker check"),
        ("vodap",    "ClickHouse served-cert-vs-secret + fluentd buffers"),
        ("proxy",    "VSP node proxy config drift"),
        ("kubeadm",  "kubeadm cert population expiry"),
    ]:
        print(f"    {_GREEN}{name:<10}{_NC} {desc}")
    print(f"\n{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Full health check (all sections){_NC}")
    print(f"    python3 vsp-health.py\n")
    print(f"    {_GREEN}# Check only Salt and Redis{_NC}")
    print(f"    python3 vsp-health.py --section salt\n")
    print(f"    {_GREEN}# Verbose pod overview with per-pod details{_NC}")
    print(f"    python3 vsp-health.py --section pods --verbose\n")
    print(f"    {_GREEN}# JSON output for scripting/monitoring{_NC}")
    print(f"    python3 vsp-health.py --json 2>/dev/null\n")
    print(f"    {_GREEN}# Specify actual CP node IP when VIP is down{_NC}")
    print(f"    python3 vsp-health.py --host 10.1.1.143\n")
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
                _cached_password = f.read().strip()
        except OSError as e:
            print(f"{_RED}ERROR:{_NC} Cannot read {CREDS_FILE}: {e}", file=sys.stderr)
            sys.exit(2)
    return _cached_password


def ssh_exec(host, password, cmd, timeout=60):
    """Run cmd as root on host via sshpass + sudo -S -i + base64. Returns (rc, output)."""
    cmd_b64 = base64.b64encode(cmd.encode()).decode()
    outer = (
        f"echo '{password}' | sudo -S -i "
        f"bash -c \"$(echo {cmd_b64} | base64 -d)\" 2>&1"
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
             f"{VSP_USER}@{host}", outer],
            capture_output=True, text=True, timeout=timeout,
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


def resolve_cp_host(host_arg, worker_fqdn, password):
    """Pick the VSP control-plane host to use, trying candidates in order:
    1. host_arg (--host), if given
    2. the hardcoded VSP_VIP
    3. auto-discovery via worker_fqdn (--worker)
    Each candidate is SSH-tested; the first one that answers wins. Returns
    (cp_host, tried) — cp_host is None if nothing answered, and tried lists
    every candidate host attempted (for the error message)."""
    tried = []
    candidates = []
    if host_arg:
        candidates.append((host_arg, "--host"))
    if VSP_VIP not in (c[0] for c in candidates):
        candidates.append((VSP_VIP, "hardcoded VIP"))

    for host, label in candidates:
        tried.append(host)
        print(f"{_DIM}  Testing SSH to {host} ({label})...{_NC}", end="", flush=True)
        rc, _ = ssh_exec(host, password, "echo PONG", timeout=20)
        if rc == 0:
            print(f" {_OK}")
            return host, tried
        print(f" {_FAIL}")

    print(f"\n{_DIM}Auto-discovering VSP control plane from {worker_fqdn}...{_NC}")
    discovered = discover_cp(worker_fqdn, password)
    if not discovered:
        print(f"  {_WARN} Discovery failed — no control plane IP found")
        return None, tried
    print(f"  {_DIM}Control plane: {discovered}{_NC}")
    if discovered in tried:
        # Already ruled out above — no point re-testing the same host.
        return None, tried
    tried.append(discovered)
    print(f"{_DIM}  Testing SSH to {discovered} (auto-discovered)...{_NC}", end="", flush=True)
    rc, _ = ssh_exec(discovered, password, "echo PONG", timeout=20)
    if rc == 0:
        print(f" {_OK}")
        return discovered, tried
    print(f" {_FAIL}")
    return None, tried


def discover_cp(worker_fqdn, password):
    """SSH to worker node, read kubeconfig, return CP/VIP IP. Returns None on failure."""
    try:
        worker_ip = socket.gethostbyname(worker_fqdn)
    except socket.gaierror as e:
        print(f"  {_WARN} DNS lookup for {worker_fqdn} failed: {e}")
        return None
    print(f"  {_DIM}Querying {worker_fqdn} ({worker_ip}) for CP IP...{_NC}")
    _, out = ssh_exec(worker_ip, password,
                      "grep server: /etc/kubernetes/node-agent.conf 2>/dev/null || "
                      "grep server: /etc/kubernetes/admin.conf 2>/dev/null")
    m = re.search(r'https?://([0-9.]+):', out)
    return m.group(1) if m else None


def ping_host(ip, timeout=2):
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def parse_json(raw):
    """Find and parse first JSON object in raw string. Returns dict or None."""
    if not raw:
        return None
    start = raw.find('{')
    if start < 0:
        return None
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError:
        return None


def fetch_json(cp_host, password, cmd, timeout=60):
    """SSH + parse JSON. Returns parsed dict or None."""
    _, out = ssh_exec(cp_host, password, cmd, timeout=timeout)
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


def collect(fn, *args):
    """Call a check function and collect its True/False results."""
    return fn(*args)


def _pod_ready(pod_json):
    """Extract (ready_count, total_count, in_crashloop) from a single pod JSON object."""
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
def chk_control_plane(cp_host, password, crictl_out, kvip_out, verbose):
    results = []

    # VIP reachability
    vip_up = ping_host(VSP_VIP)
    results.append(
        row_ok(f"VIP {VSP_VIP}: reachable")
        if vip_up else
        row_fail(f"VIP {VSP_VIP}: reachable", "dropped — run kube-fix.py to restore")
    )

    # kube-vip manifest setting
    kvip_patched = "true" in kvip_out
    if kvip_patched:
        results.append(row_ok("kube-vip: vip_preserve_on_leadership_loss=true"))
    else:
        detail = "set to false — run kube-fix.py to patch" if kvip_out else "manifest unreadable"
        results.append(row_fail("kube-vip: vip_preserve_on_leadership_loss=true", detail))
    if verbose and kvip_out:
        row_verbose(kvip_out.strip())

    # Static pod status from crictl
    if verbose and crictl_out:
        row_verbose("crictl ps output:")
        for line in crictl_out.splitlines()[:20]:
            row_verbose(f"  {line}")

    for kw, label in [
        ("etcd",            "etcd"),
        ("kube-controller", "kube-controller-manager"),
        ("kube-scheduler",  "kube-scheduler"),
        ("kube-vip",        "kube-vip"),
    ]:
        running = any(kw in ln and "Running" in ln for ln in crictl_out.splitlines())
        results.append(
            row_ok(f"{label}: Running")
            if running else
            row_fail(f"{label}: Running", "not found in crictl ps — check CrashLoopBackOff")
        )

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
                                    "cordoned; stale Argo system-shutdown workflows?"))
        else:
            results.append(row_fail(f"Node {name}: Ready"))

    return results


# ─── Section 3: Pod Health Overview ──────────────────────────────────────────
def chk_pod_overview(cp_host, password, verbose):
    """One line per namespace via lightweight --no-headers text output.

    Using -o json for all pods returns megabytes of data that exceeds SSH
    pipe buffers on large clusters.  Text output is ~100x smaller and just
    as informative for a health summary.
    """
    _, out = ssh_exec(cp_host, password,
                      "kubectl get pods -A --no-headers 2>/dev/null", timeout=60)
    if not out or out.strip() == "":
        return [row_fail("kubectl get pods -A: success", "command returned no output")]

    ns_summary = defaultdict(lambda: {"healthy": 0, "total": 0, "bad": []})
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ns, name, ready_str, status = parts[0], parts[1], parts[2], parts[3]
        try:
            cur, tot = (int(x) for x in ready_str.split("/"))
        except (ValueError, AttributeError):
            cur, tot = 0, 1

        # Completed/Succeeded jobs are healthy even when READY shows 0/1 —
        # finished job pods always report 0 ready containers.
        healthy = (
            status in ("Completed", "Succeeded")
            or (status == "Running" and cur == tot)
        )
        ns_summary[ns]["total"] += 1
        if healthy:
            ns_summary[ns]["healthy"] += 1
        else:
            issue = (f"NotReady({cur}/{tot})" if status == "Running"
                     else status)
            ns_summary[ns]["bad"].append((name, issue))

    if not ns_summary:
        return [row_fail("kubectl get pods -A: returned data", "no parseable pod lines")]

    def ns_sort(n):
        return (n not in _NS_PRIORITY,
                _NS_PRIORITY.index(n) if n in _NS_PRIORITY else 999,
                n)

    results = []
    W_NS = 34
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


# ─── Section 4: VCF Managed Components ───────────────────────────────────────
def chk_vcf_components(deps_data, sts_data, comp_data, verbose):
    """Check all VCF-managed workloads — spec.replicas vs status.readyReplicas,
    plus the components.api.vmsp.vmware.com CRDs' operational-status
    annotation (vsp-health-monitor.py's component_health check fixes both of
    these; this section is detect-only)."""
    results = []

    # Build lookup: (namespace, kind, name) -> {spec, ready}
    wl = {}
    for data, kind in [(deps_data, "deployment"), (sts_data, "statefulset")]:
        if not data:
            continue
        for item in data.get("items", []):
            ns    = item.get("metadata", {}).get("namespace", "?")
            name  = item.get("metadata", {}).get("name", "?")
            spec  = item.get("spec", {}).get("replicas", 0) or 0
            ready = item.get("status", {}).get("readyReplicas", 0) or 0
            wl[(ns, kind, name)] = {"spec": spec, "ready": ready}

    W = 62  # label column width
    prev_ns = None
    for ns, resource in VCF_COMPONENTS:
        kind, name = resource.split("/", 1)

        if verbose and ns != prev_ns:
            print(f"  {_DIM}── {ns} ──{_NC}")
            prev_ns = ns

        label = f"{ns}/{resource}"
        key   = (ns, kind, name)

        if key not in wl:
            results.append(row_fail(f"{label:<{W}}", "not found — may not be deployed"))
            continue

        spec  = wl[key]["spec"]
        ready = wl[key]["ready"]

        if spec == 0:
            results.append(row_warn(f"{label:<{W}} 0/0",
                                    "scaled to 0 — vsp-health-monitor.py component_health "
                                    "will scale it back up within 5 min, or run VCFfinal.py Task 2e"))
        elif ready >= spec:
            results.append(row_ok(f"{label:<{W}} {ready}/{spec}"))
        else:
            results.append(row_fail(f"{label:<{W}} {ready}/{spec} ready",
                                    f"{spec - ready} pod(s) not yet ready"))

    # components.api.vmsp.vmware.com operational-status annotation
    if comp_data:
        not_running = [
            item.get("metadata", {}).get("name", "?")
            for item in comp_data.get("items", [])
            if item.get("metadata", {}).get("annotations", {}).get(
                "component.vmsp.vmware.com/operational-status", "") == "NotRunning"
        ]
        if not_running:
            results.append(row_fail("Component CRDs operational-status: all Running",
                                    f"NotRunning: {', '.join(not_running)}"))
        else:
            results.append(row_ok("Component CRDs operational-status: all Running"))
    else:
        results.append(row_warn("Component CRDs (components.api.vmsp.vmware.com): readable",
                                "no response"))

    return results


# ─── Section 5: PostgreSQL ────────────────────────────────────────────────────
def chk_postgres(cp_host, password, verbose):
    """Check all Zalando Spilo postgres instances via CRD + direct pod queries.
    (vsp-health-monitor.py's postgres check fixes both suspension and replica
    mismatches; this section is detect-only.)"""
    results = []

    # Suspended instances via CRD
    pg_inst = fetch_json(cp_host, password,
                         "kubectl get postgresinstances.database.vmsp.vmware.com -A -o json 2>/dev/null",
                         timeout=20)
    if pg_inst:
        suspended = [
            f"{i.get('metadata',{}).get('namespace','?')}/{i.get('metadata',{}).get('name','?')}"
            for i in pg_inst.get("items", [])
            if i.get("metadata", {}).get("labels", {}).get(
                "database.vmsp.vmware.com/suspended", "") == "true"
        ]
        if suspended:
            results.append(row_fail("Suspended postgres instances: none",
                                    f"suspended: {', '.join(suspended)}"))
        else:
            count = len(pg_inst.get("items", []))
            results.append(row_ok(f"Postgres instances: {count} found, none suspended"))

        # Zalando numberOfInstances vs. saved vcf.lab/original-instances annotation
        mismatched = []
        for i in pg_inst.get("items", []):
            ns   = i.get("metadata", {}).get("namespace", "?")
            name = i.get("metadata", {}).get("name", "?")
            zdata = fetch_json(cp_host, password,
                               f"kubectl get postgresqls.acid.zalan.do {name} -n {ns} "
                               f"-o json 2>/dev/null", timeout=15)
            if not zdata:
                continue
            current = zdata.get("spec", {}).get("numberOfInstances", 0) or 0
            anno = zdata.get("metadata", {}).get("annotations", {}).get(
                "vcf.lab/original-instances", "")
            intended = int(anno) if anno.isdigit() and int(anno) > 0 else 1
            if current < intended:
                mismatched.append(f"{ns}/{name}: {current}/{intended}")
        if mismatched:
            results.append(row_fail("Zalando numberOfInstances: all at intended count",
                                    "; ".join(mismatched)))
        else:
            results.append(row_ok("Zalando numberOfInstances: all at intended count"))

    # pgdatabase-0 readiness via targeted pod query
    pg_data = fetch_json(cp_host, password,
                         "kubectl get pod pgdatabase-0 -n salt-raas -o json 2>/dev/null",
                         timeout=20)
    if pg_data and pg_data.get("kind") == "Pod":
        ready, total, _, _ = _pod_ready(pg_data)
        ok    = ready == total and total > 0
        label = f"pgdatabase-0 (salt-raas): {ready}/{total} containers Ready"
        results.append(row_ok(label) if ok
                       else row_fail(label, "pgdata permissions likely wrong — run salt-stabilize.py"))
    else:
        results.append(row_warn("pgdatabase-0 (salt-raas): found",
                                "pod not found or unreadable"))

    return results


# ─── Section 6: Redis & RAAS ─────────────────────────────────────────────────
def chk_redis_raas(cp_host, password, verbose):
    """Check Redis pod, redis-service endpoints, and RAAS pod via direct kubectl queries."""
    results = []

    # Redis pod (targeted label query — avoids large cluster-wide pod JSON)
    redis_data = fetch_json(cp_host, password,
                            "kubectl get pod -n salt-raas -l app=redis -o json 2>/dev/null",
                            timeout=30)
    if redis_data:
        items = redis_data.get("items", [])
        if not items:
            results.append(row_fail("Redis pod (salt-raas): found", "no pod with app=redis"))
        else:
            ready, total, _, _ = _pod_ready(items[0])
            ok = ready == total and total > 0
            results.append(row_ok(f"Redis pod (salt-raas): {ready}/{total} Ready") if ok
                           else row_fail(f"Redis pod (salt-raas): {ready}/{total} Ready",
                                         "run salt-stabilize.py"))
    else:
        results.append(row_fail("Redis pod (salt-raas): found", "no response from kubectl"))

    # Redis service endpoints
    ep_data = fetch_json(cp_host, password,
                         "kubectl get endpoints redis-service -n salt-raas -o json 2>/dev/null",
                         timeout=20)
    if ep_data:
        subsets   = ep_data.get("subsets", [])
        populated = bool(subsets and subsets[0].get("addresses"))
        if populated:
            n_addr = len(subsets[0].get("addresses", []))
            results.append(row_ok(f"redis-service endpoints: {n_addr} address(es)"))
        else:
            results.append(row_fail("redis-service endpoints: populated",
                                    "empty — cert timing race; run salt-stabilize.py"))
    else:
        results.append(row_warn("redis-service endpoints: readable", "no response"))

    # RAAS pod
    raas_data = fetch_json(cp_host, password,
                           "kubectl get pod -n salt-raas -l app=raas -o json 2>/dev/null",
                           timeout=30)
    if raas_data:
        items = raas_data.get("items", [])
        if not items:
            results.append(row_fail("raas pod (salt-raas): found", "no pod with app=raas"))
        else:
            ready, total, in_clbo, restarts = _pod_ready(items[0])
            ok    = ready == total and total > 0 and not in_clbo
            label = f"raas pod (salt-raas): {ready}/{total} Ready"
            if ok and restarts <= 5:
                results.append(row_ok(label))
            elif ok:
                results.append(row_warn(label, f"{restarts} restarts — watch for instability"))
            else:
                detail = "CrashLoopBackOff — run salt-stabilize.py" if in_clbo \
                         else "run salt-stabilize.py"
                results.append(row_fail(label, detail))
    else:
        results.append(row_fail("raas pod (salt-raas): found", "no response from kubectl"))

    return results


# ─── Section 7: Salt Stack ────────────────────────────────────────────────────
def chk_salt(cp_host, password, verbose):
    """Check salt-master and salt-minion pods via targeted per-label kubectl queries."""
    results = []

    for app, ns in [("salt-master", "salt"), ("salt-minion", "salt")]:
        pod_data = fetch_json(cp_host, password,
                              f"kubectl get pod -n {ns} -l app={app} -o json 2>/dev/null",
                              timeout=30)
        if not pod_data:
            results.append(row_fail(f"{app} ({ns}): found", "no response from kubectl"))
            continue
        items = pod_data.get("items", [])
        if not items:
            results.append(row_fail(f"{app} ({ns}): found", "no pod"))
            continue
        ready, total, _, _ = _pod_ready(items[0])
        ok    = ready == total and total > 0
        label = f"{app} ({ns}): {ready}/{total} Ready"
        results.append(row_ok(label) if ok else row_fail(label, "run salt-stabilize.py"))

    # Log scan for known error patterns
    _, log_out = ssh_exec(cp_host, password,
                          "kubectl logs -n salt --selector=app=salt-master --tail=80 2>/dev/null",
                          timeout=30)
    bad = [
        "SSL CERTIFICATE_VERIFY_FAILED", "This Minion was scheduled to stop",
        "530 Unknown", "RAAS is not available", "Connection refused to",
    ]
    found = [p for p in bad if p in log_out]
    if not found:
        results.append(row_ok("salt-master logs: no critical errors in last 80 lines"))
    else:
        err_str = "; ".join(found)
        results.append(row_warn("salt-master logs: errors present",
                                f"{err_str} — run salt-stabilize.py"))
    if verbose and found:
        for ln in log_out.splitlines():
            if any(p in ln for p in found):
                row_verbose(f"  {ln}")

    return results


# ─── Section 8: TLS Certificates ─────────────────────────────────────────────
def chk_certificates(certs_data, verbose):
    """cert-manager Certificate resources — readiness + days to expiry."""
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
    for item in items:
        ns   = item.get("metadata", {}).get("namespace", "?")
        name = item.get("metadata", {}).get("name", "?")
        conds = item.get("status", {}).get("conditions", [])
        ready_cond = next((c for c in conds if c.get("type") == "Ready"), None)
        not_after  = item.get("status", {}).get("notAfter", "")

        cert_ready = bool(ready_cond and ready_cond.get("status") == "True")

        expiry_str   = ""
        warn_expiry  = False
        if not_after:
            try:
                exp_dt = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                days   = (exp_dt - now).days
                if days < 0:
                    expiry_str  = f"EXPIRED {abs(days)}d ago"
                    warn_expiry = True
                elif days < 30:
                    expiry_str  = f"expires in {days}d ⚠"
                    warn_expiry = True
                else:
                    expiry_str = f"expires in {days}d"
            except Exception:
                expiry_str = not_after

        label = f"{name} ({ns})"
        detail = expiry_str

        if cert_ready and not warn_expiry:
            results.append(row_ok(f"{label}: Ready", detail))
        elif cert_ready and warn_expiry:
            results.append(row_warn(f"{label}: Ready — {detail}",
                                    "renew via vsp_cert_renewer.py"))
        else:
            reason = ready_cond.get("reason", "?") if ready_cond else "no Ready condition"
            results.append(row_fail(f"{label}: Ready",
                                    f"{reason}  {detail}".strip()))

    return results


# ─── Section 9: Argo Workflows ────────────────────────────────────────────────
def chk_argo(cp_host, password, verbose):
    """Check for stale system-shutdown Argo Workflows and a leftover
    power-off-marker ConfigMap in vmsp-platform (the KB 440862 boot-deadlock
    signature). vsp-health-monitor.py's argo_cleanup check fixes both plus
    uncordons any non-condemned SchedulingDisabled node; this is detect-only."""
    results = []
    _, out = ssh_exec(cp_host, password,
                      "kubectl get workflows -n vmsp-platform --no-headers 2>/dev/null",
                      timeout=20)

    if not out or "No resources found" in out or "error: the server doesn't have" in out:
        results.append(row_ok("Argo workflows: API available, none found"))
    else:
        all_wf  = [ln for ln in out.splitlines() if ln.strip()]
        bad_wf  = [ln for ln in all_wf if "system-shutdown" in ln]
        other   = len(all_wf) - len(bad_wf)

        if other > 0:
            results.append(row_ok(f"Argo workflows: {other} non-shutdown workflow(s) present"))

        if bad_wf:
            results.append(row_fail(
                f"Stale system-shutdown workflows: 0 found",
                f"{len(bad_wf)} stale — may re-cordon node and scale prelude to 0"
            ))
            if verbose:
                for ln in bad_wf[:10]:
                    row_verbose(f"  {ln.strip()}")
        else:
            results.append(row_ok("Stale system-shutdown Argo workflows: none"))

    pom = fetch_json(cp_host, password,
                     "kubectl get configmap power-off-marker -n vmsp-platform "
                     "-o json 2>/dev/null", timeout=15)
    if pom and "data" in pom:
        results.append(row_fail("power-off-marker ConfigMap: absent",
                                "present — KB 440862 recovery incomplete, saved "
                                "replica counts not yet replayed"))
    else:
        results.append(row_ok("power-off-marker ConfigMap: absent"))

    return results


# ─── Section 10: Vodap (ClickHouse cert + fluentd buffers) ──────────────────
def chk_vodap(cp_host, password, verbose):
    """ClickHouse: compares the cert it's actually SERVING (live openssl
    s_client) against the vcf-obs-clickhouse-cert secret — a cert-manager
    Certificate resource alone can look Ready/valid while ClickHouse still
    serves a stale cert in memory (it never hot-reloads at startup).
    logging-operator-fluentd-0: readiness fails once /buffers disk usage or
    backup-chunk file count crosses a threshold; a plain restart doesn't
    help since the PVC persists. vsp-health-monitor.py's vodap check fixes
    both; this is detect-only."""
    results = []

    sec = fetch_json(cp_host, password,
                     "kubectl get secret vcf-obs-clickhouse-cert -n vodap "
                     "-o json 2>/dev/null", timeout=20)
    if not sec:
        results.append(row_warn("vcf-obs-clickhouse-cert secret: found",
                                "not found — vodap may not be deployed yet"))
    else:
        cert_b64 = sec.get("data", {}).get("tls.crt", "")
        now = datetime.now(timezone.utc)
        na = None
        expiring_soon = False
        if cert_b64:
            secret_pem = base64.b64decode(cert_b64)
            dates = subprocess.run(["openssl", "x509", "-noout", "-dates"],
                                   input=secret_pem, capture_output=True)
            for line in dates.stdout.decode("utf-8", errors="ignore").splitlines():
                if line.startswith("notAfter="):
                    try:
                        na = datetime.strptime(line.split("=", 1)[1].strip(),
                                               "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
            check7 = subprocess.run(["openssl", "x509", "-noout", "-checkend", str(7 * 86400)],
                                    input=secret_pem, capture_output=True)
            expiring_soon = check7.returncode != 0

        _, svc_ip_out = ssh_exec(cp_host, password,
                                 "kubectl get service clickhouse-vcf-obs -n vodap "
                                 "-o jsonpath='{.spec.clusterIP}' 2>/dev/null", timeout=15)
        chi_svc_ip = svc_ip_out.strip().strip('"').strip("'")

        served_mismatch = False
        served_na = None
        if chi_svc_ip:
            _, s_out = ssh_exec(cp_host, password,
                                f"openssl s_client -connect {chi_svc_ip}:8443 -showcerts "
                                f"</dev/null 2>/dev/null | openssl x509 -noout -dates 2>/dev/null",
                                timeout=20)
            served_str = ""
            for ln in s_out.splitlines():
                if ln.startswith("notAfter="):
                    served_str = ln.split("=", 1)[1].strip()
            if served_str:
                try:
                    served_na = datetime.strptime(served_str, "%b %d %H:%M:%S %Y %Z").replace(
                        tzinfo=timezone.utc)
                    if served_na < now:
                        served_mismatch = True
                    elif na and served_na.date() != na.date():
                        served_mismatch = True
                except ValueError:
                    pass

        if served_mismatch:
            results.append(row_fail(
                "ClickHouse served cert matches secret",
                f"serving notAfter={served_na.date() if served_na else '?'}, "
                f"secret notAfter={na.date() if na else '?'} — stale cert in memory, "
                f"restart statefulset/chi-vcf-obs-vcf-obs-0-0"))
        elif expiring_soon:
            results.append(row_warn(f"ClickHouse secret cert expires "
                                    f"{na.date() if na else '?'}", "within 7 days"))
        else:
            results.append(row_ok(f"ClickHouse cert: serving matches secret "
                                  f"(notAfter={na.date() if na else '?'})"))

        for dep in CLICKHOUSE_CLIENTS:
            dep_data = fetch_json(cp_host, password,
                                  f"kubectl get deployment {dep} -n vodap -o json 2>/dev/null",
                                  timeout=20)
            if dep_data:
                ready = dep_data.get("status", {}).get("readyReplicas", 0) or 0
                desired = dep_data.get("spec", {}).get("replicas", 1) or 1
                if desired > 0 and ready < desired:
                    results.append(row_fail(f"{dep} (vodap): {ready}/{desired} ready",
                                            "ClickHouse-dependent client not ready"))
                else:
                    results.append(row_ok(f"{dep} (vodap): {ready}/{desired} ready"))

    fpod = fetch_json(cp_host, password,
                      "kubectl get pod logging-operator-fluentd-0 -n vmsp-platform "
                      "-o json 2>/dev/null", timeout=20)
    if not fpod or fpod.get("kind") != "Pod":
        results.append(row_warn("logging-operator-fluentd-0: found",
                                "pod not found or unreadable"))
        return results

    ready, total, _, _ = _pod_ready(fpod)
    if total > 0 and ready == total:
        results.append(row_ok(f"logging-operator-fluentd-0: {ready}/{total} Ready"))
        return results

    label = f"logging-operator-fluentd-0: {ready}/{total} Ready"
    _, buf_out = ssh_exec(cp_host, password,
                          "kubectl exec -n vmsp-platform logging-operator-fluentd-0 -c fluentd "
                          "-- sh -c \"df -h /buffers | tail -1; "
                          "find /buffers/backup -type f 2>/dev/null | wc -l\" 2>/dev/null",
                          timeout=30)
    lines = [l for l in buf_out.splitlines() if l.strip()]
    disk_pct, backup_files = 0, 0
    if lines:
        parts = lines[0].split()
        if len(parts) >= 5 and "%" in parts[4]:
            try:
                disk_pct = int(parts[4].rstrip("%"))
            except ValueError:
                pass
    if len(lines) >= 2 and lines[1].strip().isdigit():
        backup_files = int(lines[1].strip())
    results.append(row_fail(label, f"buffer disk={disk_pct}% backup_files={backup_files} — "
                            f"run vodap-fix.py or wait for vsp-health-monitor.py"))
    return results


# ─── Section 11: Node Proxy Config ────────────────────────────────────────────
def chk_proxy(cp_host, password, verbose):
    """Compares each VSP node's proxy config against the lab's canonical
    proxy URL (same value as lsfunctions.LAB_PROXY_URL — kept as a local
    constant rather than importing lsfunctions, since this script is
    otherwise fully self-contained). vsp-health-monitor.py's proxy_config
    check repairs drift; this is detect-only."""
    results = []
    proxy_url = LAB_PROXY_URL

    _, node_ips_out = ssh_exec(cp_host, password,
                               "kubectl get nodes -o jsonpath='{range .items[*]}"
                               "{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}' "
                               "2>/dev/null", timeout=20)
    node_ips = [ip for ip in node_ips_out.split() if ip]
    if not node_ips:
        results.append(row_warn("VSP node list: readable", "could not list node IPs"))
        return results

    drifted = []
    for ip in node_ips:
        _, out = ssh_exec(ip, password,
                          f'grep -qF "{proxy_url}" /etc/sysconfig/proxy 2>/dev/null && '
                          f'grep -qF "{proxy_url}" /etc/systemd/system/containerd.service.d/'
                          f'http-proxy.conf 2>/dev/null && echo PROXY_OK || echo PROXY_DRIFT',
                          timeout=15)
        if "PROXY_OK" not in out:
            drifted.append(ip)

    if drifted:
        results.append(row_fail(f"Proxy config: matches canonical on all {len(node_ips)} node(s)",
                                f"drifted: {', '.join(drifted)} — run vsp-health-monitor.py "
                                f"or VCFfinal.py to repair"))
    else:
        results.append(row_ok(f"Proxy config: matches canonical on all {len(node_ips)} node(s)"))
    return results


# ─── Section 12: Kubeadm Certificates ─────────────────────────────────────────
def chk_kubeadm_certs(cp_host, password, verbose):
    """kubeadm's own cert population (apiserver, etcd, front-proxy, etc.) is
    a separate cert population from cert-manager's Certificate CRDs (section
    8) — kubeadm certs never show up there. vsp_cert_renewer.py (invoked by
    vsp-health-monitor.py's cert_renewal check) renews these; this is
    detect-only."""
    results = []
    _, out = ssh_exec(cp_host, password, "kubeadm certs check-expiration 2>/dev/null",
                      timeout=30)
    if not out or "CERTIFICATE" not in out:
        results.append(row_warn("kubeadm certs check-expiration: readable", "no response"))
        return results

    now = datetime.now(timezone.utc)
    expiring = []
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] in ("CERTIFICATE", "CERTIFICATE AUTHORITY"):
            continue
        # "CERTIFICATE  EXPIRES(3 tokens: 'Jun 06, 2031')  RESIDUAL_TIME  ..."
        try:
            exp_dt = datetime.strptime(" ".join(parts[1:4]), "%b %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        total += 1
        days = (exp_dt - now).days
        if days < 30:
            expiring.append(f"{parts[0]}: {days}d")

    if total == 0:
        results.append(row_warn("kubeadm certs check-expiration: parsed", "no cert rows found"))
    elif expiring:
        results.append(row_fail(f"kubeadm certs: all {total} valid 30+ days",
                                f"expiring soon: {'; '.join(expiring)}"))
    else:
        results.append(row_ok(f"kubeadm certs: all {total} valid 30+ days"))
    return results


# ─── Main ─────────────────────────────────────────────────────────────────────
SECTION_MAP = {
    "cp":       "CONTROL PLANE",
    "nodes":    "KUBERNETES NODES",
    "pods":     "POD HEALTH OVERVIEW",
    "vcf":      "VCF MANAGED COMPONENTS",
    "postgres": "POSTGRESQL INSTANCES",
    "redis":    "REDIS & SALT RAAS",
    "salt":     "SALT STACK",
    "certs":    "TLS CERTIFICATES",
    "argo":     "ARGO WORKFLOWS",
    "vodap":    "VODAP HEALTH",
    "proxy":    "NODE PROXY CONFIG",
    "kubeadm":  "KUBEADM CERTIFICATES",
}


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host",    default=None,       metavar="IP")
    parser.add_argument("--worker",  default=VSP_WORKER, metavar="FQDN")
    parser.add_argument("--section", default=None,       metavar="NAME",
                        choices=list(SECTION_MAP.keys()))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-j", "--json",    action="store_true")
    args = parser.parse_args()

    password = get_password()
    run_start = datetime.now()
    ts = run_start.strftime("%Y-%m-%d %H:%M:%S")
    W  = 70

    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VSP Cluster Health Check — Full Suite':^{W}}{_NC}{_CYAN}║{_NC}")
    if args.section:
        print(f"{_CYAN}║{_NC}{f'Section: {SECTION_MAP[args.section]}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ts:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")

    # Determine CP host: --host, then hardcoded VIP, then auto-discovery.
    cp_host, tried = resolve_cp_host(args.host, args.worker, password)
    if not cp_host:
        print(f"\n{_RED}ERROR:{_NC} Cannot SSH to any candidate host as {VSP_USER}.")
        print(f"  Tried: {', '.join(tried)}")
        print(f"  Specify a reachable CP node directly: python3 vsp-health.py --host <IP>")
        sys.exit(2)

    # ── Bulk data fetch ────────────────────────────────────────────────────────
    want = args.section  # None = all sections
    print(f"{_DIM}  Fetching cluster state (this may take ~15-20s)...{_NC}", flush=True)

    needs_crictl = want in (None, "cp")
    needs_kvip   = want in (None, "cp")
    needs_nodes  = want in (None, "nodes")
    needs_deps   = want in (None, "vcf")
    needs_sts    = want in (None, "vcf")
    needs_certs  = want in (None, "certs")
    needs_argo   = want in (None, "argo")
    needs_comp   = want in (None, "vcf")

    crictl_out = ssh_exec(cp_host, password, "crictl ps 2>/dev/null", timeout=20)[1] \
                 if needs_crictl else ""
    kvip_out   = ssh_exec(cp_host, password,
                           "grep -A1 vip_preserve /etc/kubernetes/manifests/kube-vip.yaml 2>/dev/null")[1] \
                 if needs_kvip else ""
    nodes_data = fetch_json(cp_host, password, "kubectl get nodes -o json 2>/dev/null", 30) \
                 if needs_nodes else None
    deps_data  = fetch_json(cp_host, password, "kubectl get deployments -A -o json 2>/dev/null", 45) \
                 if needs_deps else None
    sts_data   = fetch_json(cp_host, password, "kubectl get statefulsets -A -o json 2>/dev/null", 45) \
                 if needs_sts else None
    certs_data = fetch_json(cp_host, password, "kubectl get certificates -A -o json 2>/dev/null", 30) \
                 if needs_certs else None
    comp_data  = fetch_json(cp_host, password,
                            "kubectl get components.api.vmsp.vmware.com -o json 2>/dev/null", 30) \
                 if needs_comp else None

    # ── Run sections ──────────────────────────────────────────────────────────
    all_results: dict = {}

    def run(key, title, fn, *fn_args):
        if want and want != key:
            return
        section(title)
        section_start = datetime.now()
        rows = fn(*fn_args)
        section_elapsed = (datetime.now() - section_start).total_seconds()
        for i, r in enumerate(rows):
            all_results[f"{key}_{i}"] = r
        print(f"  {_DIM}({section_elapsed:.1f}s){_NC}")

    run("cp",       "CONTROL PLANE",
        chk_control_plane, cp_host, password, crictl_out, kvip_out, args.verbose)

    run("nodes",    "KUBERNETES NODES",
        chk_nodes, nodes_data, args.verbose)

    run("pods",     "POD HEALTH OVERVIEW  (all namespaces)",
        chk_pod_overview, cp_host, password, args.verbose)

    run("vcf",      "VCF MANAGED COMPONENTS  (vcfcomponents)",
        chk_vcf_components, deps_data, sts_data, comp_data, args.verbose)

    run("postgres", "POSTGRESQL INSTANCES",
        chk_postgres, cp_host, password, args.verbose)

    run("redis",    "REDIS & SALT RAAS",
        chk_redis_raas, cp_host, password, args.verbose)

    run("salt",     "SALT STACK",
        chk_salt, cp_host, password, args.verbose)

    run("certs",    "TLS CERTIFICATES  (cert-manager)",
        chk_certificates, certs_data, args.verbose)

    run("argo",     "ARGO WORKFLOWS  (vmsp-platform)",
        chk_argo, cp_host, password, args.verbose)

    run("vodap",    "VODAP HEALTH  (ClickHouse cert + fluentd buffers)",
        chk_vodap, cp_host, password, args.verbose)

    run("proxy",    "NODE PROXY CONFIG",
        chk_proxy, cp_host, password, args.verbose)

    run("kubeadm",  "KUBEADM CERTIFICATES",
        chk_kubeadm_certs, cp_host, password, args.verbose)

    # ── Summary ───────────────────────────────────────────────────────────────
    total   = len(all_results)
    failed  = sum(1 for v in all_results.values() if v is False)
    color   = _GREEN if failed == 0 else _RED
    elapsed = (datetime.now() - run_start).total_seconds()

    print(f"\n{_CYAN}{'─' * 64}{_NC}")
    print(f"  {color}{_BOLD}RESULT: {total - failed}/{total} checks passed{_NC}"
          f"  {_DIM}(total: {elapsed:.1f}s){_NC}")
    if failed:
        print(f"  {_RED}  {failed} check(s) require attention — see {_FAIL} rows above{_NC}")
        print(f"  {_DIM}  Remediation: python3 salt-stabilize.py | python3 kube-fix.py | "
              f"python3 vodap-fix.py{_NC}")
        print(f"  {_DIM}  Or wait up to 5 min for vsp-health-monitor.py's next cron cycle{_NC}")
    else:
        print(f"  {_GREEN}  VSP cluster is healthy{_NC}")
    print(f"{_CYAN}{'─' * 64}{_NC}\n")

    if args.json:
        summary = {
            "timestamp": ts,
            "cp_host": cp_host,
            "section_filter": args.section,
            "checks_passed": total - failed,
            "checks_failed": failed,
            "checks_total": total,
            "healthy": failed == 0,
            "elapsed_seconds": round(elapsed, 1),
            "detail": {k: v for k, v in all_results.items()},
        }
        print(json.dumps(summary, indent=2))

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
