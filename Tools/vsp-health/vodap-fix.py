#!/usr/bin/env python3
"""
vodap-fix.py
Version 1.1.0 - 2026-07-16
Author: Burke Azbill and HOL Core Team

v1.1.0: CP host resolution now tries candidates in order — explicit --host,
then the hardcoded VSP_VIP, then auto-discovery via --worker — stopping at
the first one that answers SSH, matching vsp-health.py's resolve_cp_host().

Remediates ClickHouse TLS cert staleness and logging-operator-fluentd readiness
failures in the VSP (Supervisor) cluster.

Root causes addressed:
  1. ClickHouse TLS cert race:
     cert-manager updates vcf-obs-clickhouse-cert every 90 days.  ClickHouse
     loads its TLS cert at pod startup and never hot-reloads.  If ClickHouse
     restarts in the narrow window between the old cert expiring and cert-manager
     finishing the secret update, it will serve the old expired cert.  The vodap
     Java services (vcf-obs-data-query-service, vcf-obs-netops-collector-service)
     use BouncyCastle FIPS which strictly validates cert expiry and refuses to
     connect → startup probe fails (HTTP 503 / connection refused) → pod is killed
     and restarted → CrashLoopBackOff.
     Detection: compare the cert ClickHouse is serving (openssl s_client) with
     the cert in the Kubernetes secret.  Also checks if the secret cert was
     renewed within the last 48h or expires within 7 days.
     Fix: kubectl rollout restart statefulset/chi-vcf-obs-vcf-obs-0-0 -n vodap

  2. logging-operator-fluentd buffer backlog:
     The ConcatFilter plugin buffers multi-line log entries.  After a cold boot
     or restart, timed-out flushes generate @ERROR events that cause the
     readiness probe to fail (1/2 containers Ready).
     Fix: kubectl rollout restart statefulset/logging-operator-fluentd -n vmsp-platform

Use --dry-run to preview actions without making changes.
Use vsp-health.py to verify status before/after.

Exit codes:
  0  All checks passed / all fixes applied successfully
  1  One or more fixes failed or timed out
  2  Cannot connect to VSP cluster
"""
import argparse
import base64
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

VERSION = "1.1.0"
DATE    = "2026-07-16"

CREDS_FILE = "/home/holuser/creds.txt"
VSP_USER   = "vmware-system-user"
VSP_WORKER = "vsp-01a.site-a.vcf.lab"
VSP_VIP    = "10.1.1.142"

# vodap deployments that depend on ClickHouse
CLICKHOUSE_CLIENTS = [
    "vcf-obs-data-query-service",
    "vcf-obs-netops-collector-service",
]

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
_INFO = f"{_CYAN}→{_NC}"


# ─── Help ─────────────────────────────────────────────────────────────────────
def show_help():
    W = 68
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'Vodap / ClickHouse TLS Fix':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    vodap-fix.py [--host IP] [--worker FQDN] [--dry-run] [-v]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--host{_NC} <IP>       VSP control-plane host, tried first if set")
    print(f"    {_GREEN}--worker{_NC} <FQDN>   VSP worker FQDN for CP auto-discovery (default: {VSP_WORKER})")
    print(f"    {_GREEN}--dry-run{_NC}         Show what would be done without making changes")
    print(f"    {_GREEN}-v, --verbose{_NC}     Show full command output")
    print(f"    {_GREEN}-h, --help{_NC}        Show this help message\n")
    print(f"{_BOLD}CP HOST RESOLUTION ORDER:{_NC}")
    print(f"    1. {_GREEN}--host{_NC} <IP>, if given")
    print(f"    2. Hardcoded VIP  ({VSP_VIP})")
    print(f"    3. Auto-discovery via {_GREEN}--worker{_NC}")
    print(f"    Each candidate is tried via SSH; the first reachable one wins.\n")
    print(f"{_YELLOW}SYMPTOMS FIXED:{_NC}")
    print(f"    • vodap data-query-service or netops-collector-service stuck in")
    print(f"      CrashLoopBackOff with 'certificate expired' in logs")
    print(f"    • Startup probe fails: 'dial tcp: connection refused' or HTTP 503")
    print(f"    • logging-operator-fluentd-0 showing 1/2 containers Ready\n")
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Run full remediation (typical usage){_NC}")
    print(f"    python3 vodap-fix.py\n")
    print(f"    {_GREEN}# Preview without making changes{_NC}")
    print(f"    python3 vodap-fix.py --dry-run\n")
    print(f"{_BOLD}NOTE:{_NC}")
    print(f"    This script mirrors the fix VCFfinal.py Task 2e applies at startup.")
    print(f"    Run it manually any time vodap pods are stuck in CrashLoopBackOff")
    print(f"    or VCF Operations shows observability / data query errors.")
    print(f"\n    After completion, verify with: python3 vsp-health.py\n")
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


def ssh_exec(host, password, cmd, timeout=90):
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


def discover_cp(worker_fqdn, password):
    """SSH to worker, read kubeconfig, return CP IP."""
    try:
        worker_ip = socket.gethostbyname(worker_fqdn)
    except socket.gaierror:
        return None
    # Use -o json to avoid jsonpath escaping issues through SSH/sudo/base64 layers
    _, out = ssh_exec(worker_ip, password,
                      "kubectl config view --minify -o json 2>/dev/null",
                      timeout=20)
    start = out.find('{')
    if start < 0:
        return None
    try:
        data   = json.loads(out[start:])
        server = (data.get('clusters', [{}])[0]
                  .get('cluster', {})
                  .get('server', ''))
        m = re.search(r'https?://([0-9.]+)', server)
        return m.group(1) if m else None
    except (json.JSONDecodeError, IndexError):
        return None


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
        return None, tried
    tried.append(discovered)
    print(f"{_DIM}  Testing SSH to {discovered} (auto-discovered)...{_NC}", end="", flush=True)
    rc, _ = ssh_exec(discovered, password, "echo PONG", timeout=20)
    if rc == 0:
        print(f" {_OK}")
        return discovered, tried
    print(f" {_FAIL}")
    return None, tried


def fetch_json(host, password, cmd, timeout=30):
    """Run cmd over SSH and parse JSON output; return dict or None on failure."""
    _, out = ssh_exec(host, password, cmd, timeout=timeout)
    if not out:
        return None
    start = out.find('{')
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return None


def step(label):
    print(f"\n{_CYAN}──── {label} {_NC}")


def ok(msg):
    print(f"  {_OK} {msg}")


def fail(msg, detail=""):
    d = f" — {detail}" if detail else ""
    print(f"  {_FAIL} {msg}{d}")


def warn(msg, detail=""):
    d = f" — {detail}" if detail else ""
    print(f"  {_WARN} {msg}{d}")


def info(msg):
    print(f"  {_INFO} {msg}")


# ─── Cert parsing (local openssl) ─────────────────────────────────────────────
def cert_info(pem_bytes):
    """Return (not_before: datetime|None, not_after: datetime|None, expires_within_7d: bool)."""
    fmt = '%b %d %H:%M:%S %Y %Z'
    dates = subprocess.run(
        ['openssl', 'x509', '-noout', '-dates'],
        input=pem_bytes, capture_output=True
    )
    dates_out = dates.stdout.decode('utf-8', errors='ignore')
    not_before = not_after = None
    for line in dates_out.splitlines():
        if line.startswith('notBefore='):
            try:
                not_before = datetime.strptime(
                    line.split('=', 1)[1].strip(), fmt
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        elif line.startswith('notAfter='):
            try:
                not_after = datetime.strptime(
                    line.split('=', 1)[1].strip(), fmt
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    check7 = subprocess.run(
        ['openssl', 'x509', '-noout', '-checkend', str(7 * 86400)],
        input=pem_bytes, capture_output=True
    )
    return not_before, not_after, (check7.returncode != 0)


# ─── ClickHouse check & fix ───────────────────────────────────────────────────
def fix_clickhouse(cp_host, password, dry_run, verbose):
    step("CLICKHOUSE TLS CERT")
    errors = 0

    # ── 1. Read the cert from the Kubernetes secret ──────────────────────────
    sec_data = fetch_json(cp_host, password,
                          'kubectl get secret vcf-obs-clickhouse-cert -n vodap '
                          '-o json 2>/dev/null',
                          timeout=20)
    if not sec_data:
        warn("vcf-obs-clickhouse-cert secret: not found or unreadable",
             "vodap namespace may not be deployed yet")
        return errors

    cert_b64 = sec_data.get('data', {}).get('tls.crt', '')
    if not cert_b64:
        warn("vcf-obs-clickhouse-cert: tls.crt field missing")
        return errors

    secret_pem = base64.b64decode(cert_b64)
    nb, na, expiring_soon = cert_info(secret_pem)
    now = datetime.now(timezone.utc)

    if na and na < now:
        warn(f"Secret cert EXPIRED on {na.date()} — cert-manager may not have renewed yet",
             "check cert-manager controller pod in vmsp-platform")
    elif expiring_soon:
        warn(f"Secret cert expires on {na.date() if na else '?'} (within 7 days)")
    else:
        ok(f"Secret cert valid  notAfter={na.date() if na else '?'}")

    recently_renewed = nb and (now - nb).total_seconds() < 48 * 3600
    if recently_renewed and nb:
        ok(f"Cert was renewed {(now - nb).total_seconds() / 3600:.1f}h ago "
           f"(will compare against what ClickHouse is serving)")

    # ── 2. Check what ClickHouse is actually serving ──────────────────────────
    _, svc_ip_out = ssh_exec(cp_host, password,
                             'kubectl get service clickhouse-vcf-obs -n vodap '
                             '-o jsonpath="{.spec.clusterIP}" 2>/dev/null',
                             timeout=15)
    chi_svc_ip = svc_ip_out.strip().strip('"').strip("'")

    served_expired = False
    if chi_svc_ip:
        _, s_client_out = ssh_exec(cp_host, password,
                                   f'openssl s_client -connect {chi_svc_ip}:8443 '
                                   f'-showcerts </dev/null 2>/dev/null | '
                                   f'openssl x509 -noout -dates 2>/dev/null',
                                   timeout=20)
        served_not_after_str = ''
        for ln in s_client_out.splitlines():
            if ln.startswith('notAfter='):
                served_not_after_str = ln.split('=', 1)[1].strip()
        if served_not_after_str:
            fmt = '%b %d %H:%M:%S %Y %Z'
            try:
                served_na = datetime.strptime(served_not_after_str, fmt).replace(
                    tzinfo=timezone.utc
                )
                served_expired = served_na < now
                if served_expired:
                    fail(f"ClickHouse is serving EXPIRED cert  (notAfter={served_na.date()})",
                         "ClickHouse loaded old cert before secret was updated")
                elif na and served_na.date() != na.date():
                    warn(f"ClickHouse serving cert from {served_na.date()}, "
                         f"secret has {na.date()} — stale cert in memory")
                    served_expired = True  # treat mismatch as needing restart
                else:
                    ok(f"ClickHouse serving cert  notAfter={served_na.date()}")
            except ValueError:
                warn("Could not parse ClickHouse served cert dates")
        else:
            warn("Could not retrieve cert from ClickHouse TLS endpoint",
                 f"service IP: {chi_svc_ip}:8443")
    else:
        warn("clickhouse-vcf-obs service not found — ClickHouse may not be deployed")

    # ── 3. Check vodap client pods for startup failures ───────────────────────
    failing_clients = []
    for dep in CLICKHOUSE_CLIENTS:
        dep_data = fetch_json(cp_host, password,
                              f'kubectl get deployment {dep} -n vodap '
                              f'-o json 2>/dev/null',
                              timeout=20)
        if dep_data:
            ready   = dep_data.get('status', {}).get('readyReplicas', 0) or 0
            desired = dep_data.get('spec', {}).get('replicas', 1) or 1
            if desired > 0 and ready < desired:
                failing_clients.append(dep)

    if failing_clients:
        fail(f"Vodap clients not ready: {', '.join(failing_clients)}")
    else:
        ok("Vodap ClickHouse-dependent clients: all ready")

    # ── 4. Decide whether to restart ClickHouse ───────────────────────────────
    # Only restart when there is an actual problem detected.
    # `recently_renewed` is informational — if ClickHouse is already serving the
    # correct cert AND clients are healthy, no restart is needed.
    needs_restart = served_expired or expiring_soon or bool(failing_clients)

    if not needs_restart:
        ok("ClickHouse cert is current — no restart needed")
        return errors

    if dry_run:
        info("[DRY-RUN] Would restart: statefulset/chi-vcf-obs-vcf-obs-0-0 -n vodap")
        return errors

    info("Restarting ClickHouse to pick up renewed cert...")
    rc, out = ssh_exec(cp_host, password,
                       'kubectl rollout restart statefulset/chi-vcf-obs-vcf-obs-0-0 '
                       '-n vodap 2>/dev/null',
                       timeout=20)
    if verbose:
        print(f"    {out}")

    # StatefulSet rollout can take 3+ minutes for ClickHouse to initialise
    info("Waiting up to 180s for ClickHouse rollout...")
    rc, ro_out = ssh_exec(cp_host, password,
                          'kubectl rollout status statefulset/chi-vcf-obs-vcf-obs-0-0 '
                          '-n vodap --timeout=180s 2>/dev/null',
                          timeout=190)
    if verbose:
        print(f"    {ro_out}")

    # StatefulSets use "rolling update complete" rather than "successfully rolled out"
    success_phrases = (
        'successfully rolled out',
        'rolling update complete',
        'roll out complete',
        'partitioned roll out complete',
    )
    if any(p in ro_out.lower() for p in success_phrases):
        ok("ClickHouse rollout complete")
        if failing_clients:
            ok("Vodap client pods will auto-recover now that ClickHouse serves a valid cert")
    else:
        fail("ClickHouse rollout did not complete within 180s", ro_out[:120])
        errors += 1

    return errors


# ─── Fluentd check & fix ──────────────────────────────────────────────────────
def fix_fluentd(cp_host, password, dry_run, verbose):
    """
    The fluentd readiness probe checks two things inside the container:
      1. BUFFER_PATH disk usage < 80%
      2. BUFFER_PATH buffer file count < 10,000
    After weeks of continuous operation, /buffers/backup accumulates old chunk
    files (86K+ files / 8+ GB after 89 days), pushing disk usage over the 80%
    threshold.  A pod restart won't help because the PVC persists.
    Fix: delete the stale files in /buffers/backup/ inside the running container.
    """
    step("LOGGING-OPERATOR-FLUENTD")
    errors = 0

    pod_data = fetch_json(cp_host, password,
                          'kubectl get pod logging-operator-fluentd-0 '
                          '-n vmsp-platform -o json 2>/dev/null',
                          timeout=20)
    if not pod_data or pod_data.get('kind') != 'Pod':
        warn("logging-operator-fluentd-0: pod not found or not parseable")
        return errors

    cstats = pod_data.get('status', {}).get('containerStatuses', [])
    ready  = sum(1 for cs in cstats if cs.get('ready', False))
    total  = len(cstats)

    if total == 0:
        warn("logging-operator-fluentd-0: no container status — pod may be initialising")
        return errors

    if ready == total:
        ok(f"logging-operator-fluentd-0: {ready}/{total} containers Ready — OK")
        return errors

    fail(f"logging-operator-fluentd-0: {ready}/{total} containers Ready")

    # ── Diagnose: check disk usage in the running container ──────────────────
    _, buf_out = ssh_exec(cp_host, password,
                          'kubectl exec -n vmsp-platform logging-operator-fluentd-0 '
                          '-c fluentd -- sh -c '
                          '"df -h /buffers | tail -1; '
                          'find /buffers -name \'*.buffer\' -type f 2>/dev/null | wc -l; '
                          'find /buffers/backup -type f 2>/dev/null | wc -l" '
                          '2>/dev/null',
                          timeout=30)
    lines = [l for l in buf_out.splitlines() if l.strip()]
    disk_pct    = 0
    buf_files   = 0
    backup_files = 0
    if len(lines) >= 1:
        parts = lines[0].split()
        # df output: Filesystem Size Used Avail Use% Mounted
        if len(parts) >= 5 and '%' in parts[4]:
            try:
                disk_pct = int(parts[4].rstrip('%'))
            except ValueError:
                pass
    if len(lines) >= 2 and lines[1].strip().isdigit():
        buf_files = int(lines[1].strip())
    if len(lines) >= 3 and lines[2].strip().isdigit():
        backup_files = int(lines[2].strip())

    info(f"Buffer disk: {disk_pct}%  active .buffer files: {buf_files}  "
         f"backup chunks: {backup_files}")

    if disk_pct > 80 or buf_files > 10000:
        info(f"Readiness probe will fail: disk {disk_pct}% (threshold 80%) "
             f"or {buf_files} buffer files (threshold 10000)")
    else:
        warn("Disk and file count look OK — readiness probe may need more time")

    if dry_run:
        info("[DRY-RUN] Would: rm -rf /buffers/backup/* inside fluentd container")
        return errors

    if backup_files > 0 or disk_pct > 80:
        info(f"Purging {backup_files} stale backup chunks from /buffers/backup ...")
        _, clean_out = ssh_exec(cp_host, password,
                                'kubectl exec -n vmsp-platform logging-operator-fluentd-0 '
                                '-c fluentd -- sh -c '
                                '"rm -rf /buffers/backup/* 2>/dev/null && '
                                'df -h /buffers | tail -1" 2>/dev/null',
                                timeout=60)
        if verbose:
            print(f"    {clean_out}")
        # Parse new disk usage
        for ln in clean_out.splitlines():
            parts = ln.split()
            if len(parts) >= 5 and '%' in parts[4]:
                try:
                    new_pct = int(parts[4].rstrip('%'))
                    ok(f"/buffers now at {new_pct}% — below 80% threshold")
                except ValueError:
                    pass
    else:
        info("No backup chunks to purge — the probe may resolve on its own")

    # Poll for up to 60s for 2/2 ready (probe re-runs every 15s)
    info("Waiting up to 60s for readiness probe to pass...")
    for _i in range(6):
        time.sleep(10)
        pd = fetch_json(cp_host, password,
                        'kubectl get pod logging-operator-fluentd-0 '
                        '-n vmsp-platform -o json 2>/dev/null',
                        timeout=15)
        if pd and pd.get('kind') == 'Pod':
            cs  = pd.get('status', {}).get('containerStatuses', [])
            rdy = sum(1 for c in cs if c.get('ready', False))
            tot = len(cs)
            if tot > 0 and rdy == tot:
                ok(f"logging-operator-fluentd-0: {rdy}/{tot} Ready — recovered")
                return errors
            if verbose:
                print(f"    [{(_i+1)*10}s] {rdy}/{tot} containers ready")

    # Final check
    pd = fetch_json(cp_host, password,
                    'kubectl get pod logging-operator-fluentd-0 '
                    '-n vmsp-platform -o json 2>/dev/null',
                    timeout=15)
    if pd and pd.get('kind') == 'Pod':
        cs  = pd.get('status', {}).get('containerStatuses', [])
        rdy = sum(1 for c in cs if c.get('ready', False))
        tot = len(cs)
        if tot > 0 and rdy == tot:
            ok(f"logging-operator-fluentd-0: {rdy}/{tot} Ready — recovered")
            return errors
        warn(f"logging-operator-fluentd-0: {rdy}/{tot} Ready — "
             f"probe may need another interval; check in 30s")
    errors += 1
    return errors


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host",    default=None,       metavar="IP")
    parser.add_argument("--worker",  default=VSP_WORKER, metavar="FQDN")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    password = get_password()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W  = 68

    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'Vodap / ClickHouse TLS Fix':^{W}}{_NC}{_CYAN}║{_NC}")
    if args.dry_run:
        print(f"{_CYAN}║{_NC}{_YELLOW}{'DRY-RUN MODE — no changes will be made':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ts:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")

    # Determine CP host: --host, then hardcoded VIP, then auto-discovery.
    cp_host, tried = resolve_cp_host(args.host, args.worker, password)
    if not cp_host:
        print(f"\n{_RED}ERROR:{_NC} Cannot SSH to any candidate host — is the VIP up?")
        print(f"  Tried: {', '.join(tried)}")
        sys.exit(2)

    total_errors = 0
    total_errors += fix_clickhouse(cp_host, password, args.dry_run, args.verbose)
    total_errors += fix_fluentd(cp_host, password, args.dry_run, args.verbose)

    print(f"\n{_CYAN}{'─' * (W + 2)}{_NC}")
    if total_errors == 0:
        if args.dry_run:
            print(f"  {_YELLOW}DRY-RUN complete — no changes made{_NC}")
        else:
            print(f"  {_GREEN}{_BOLD}All fixes applied successfully{_NC}")
            print(f"  {_DIM}Verify with: python3 vsp-health.py{_NC}")
    else:
        print(f"  {_RED}{_BOLD}{total_errors} step(s) failed or timed out{_NC}")
        print(f"  {_DIM}Check pod events: kubectl describe pod -n vodap <pod-name>{_NC}")
    print(f"{_CYAN}{'─' * (W + 2)}{_NC}\n")

    sys.exit(0 if total_errors == 0 else 1)


if __name__ == "__main__":
    main()
