#!/usr/bin/env python3
"""
salt-stabilize.py
Version 1.0.0 - 2026-06-17
Author: Burke Azbill and HOL Core Team

Remediates the Salt infrastructure (salt-raas + salt namespaces) on the VSP
(Supervisor) cluster after a cold boot or an unexpected failure.

Root causes addressed:
  1. pgdatabase-0: Postgres data directory permissions != 0700 (Spilo refuses
     to start → FATAL: data directory has invalid permissions).  Fixed via
     the walg sidecar.
  2. Redis TLS cert race: Redis loads its in-memory TLS cert at pod start.
     If vsp_cert_renewer runs ~18s later the cert in memory is stale/expired.
     RAAS Celery worker sees SSL CERTIFICATE_VERIFY_FAILED and crash-loops.
  3. salt-master receives 500/530 from the unhealthy RAAS SSE API and
     eventually marks its event bus broken — does not auto-recover.
  4. salt-minion permanently stops ("This Minion was scheduled to stop")
     when it cannot authenticate with the broken master.

Fix strategy:
  Step 1: Check pgdatabase-0 readiness; fix pgdata permissions via walg if < 3/3.
  Step 2: rollout restart redis  (wait up to 30s)
  Step 3: rollout restart raas   (wait up to 60s)
  Step 4: rollout restart salt-master (wait up to 45s)
  Step 5: rollout restart salt-minion

Use --dry-run to preview actions without making changes.
Use vsp-health.py to verify status before/after.

Exit codes:
  0  All steps completed (or dry-run succeeded)
  1  One or more steps failed
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
from datetime import datetime

VERSION = "1.0.0"
DATE    = "2026-06-17"

CREDS_FILE = "/home/holuser/creds.txt"
VSP_USER   = "vmware-system-user"
VSP_WORKER = "vsp-01a.site-a.vcf.lab"
VSP_VIP    = "10.1.1.142"

# Rollout steps: (label, namespace, k8s-resource, wait-timeout-seconds)
SALT_STEPS = [
    ("redis",       "salt-raas", "deployment/redis",       30),
    ("raas",        "salt-raas", "deployment/raas",        60),
    ("salt-master", "salt",      "deployment/salt-master", 45),
    ("salt-minion", "salt",      "deployment/salt-minion", 0),
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
    print(f"{_CYAN}║{_NC}{_BLUE}{'Salt Infrastructure Stabilizer':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    salt-stabilize.py [--host IP] [--worker FQDN] [--dry-run] [-v]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--host{_NC} <IP>       VSP control-plane host/VIP  (default: {VSP_VIP})")
    print(f"    {_GREEN}--worker{_NC} <FQDN>   VSP worker FQDN for CP discovery (default: {VSP_WORKER})")
    print(f"    {_GREEN}--dry-run{_NC}         Show what would be done without making changes")
    print(f"    {_GREEN}-v, --verbose{_NC}     Show full command output for each step")
    print(f"    {_GREEN}-h, --help{_NC}        Show this help message\n")
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Run full remediation (typical usage){_NC}")
    print(f"    python3 salt-stabilize.py\n")
    print(f"    {_GREEN}# Preview without making changes{_NC}")
    print(f"    python3 salt-stabilize.py --dry-run\n")
    print(f"    {_GREEN}# Remediate with verbose output{_NC}")
    print(f"    python3 salt-stabilize.py --verbose\n")
    print(f"{_BOLD}NOTE:{_NC}")
    print(f"    This script runs the same fix that VCFfinal.py Task 2e applies")
    print(f"    automatically at startup. Run it manually any time the VCF")
    print(f"    Operations Security Posture shows 'Salt infrastructure is down'.")
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
    _NOISE   = ("Welcome to Photon", "Warning: Permanently added", "Connection to ", "Killed by signal")
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
    except socket.gaierror as e:
        print(f"  {_WARN} DNS lookup for {worker_fqdn} failed: {e}")
        return None
    print(f"  {_DIM}Querying worker {worker_fqdn} ({worker_ip}) for CP IP...{_NC}")
    _, out = ssh_exec(worker_ip, password,
                      "grep server: /etc/kubernetes/node-agent.conf 2>/dev/null || "
                      "grep server: /etc/kubernetes/admin.conf 2>/dev/null")
    m = re.search(r'https?://([0-9.]+):', out)
    return m.group(1) if m else None


def parse_json(raw):
    """Find and parse first JSON object in raw string."""
    start = raw.find('{')
    if start < 0:
        return None
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError:
        return None


# ─── Output helpers ───────────────────────────────────────────────────────────
_step_num = [0]

def step(title):
    _step_num[0] += 1
    print(f"\n{_CYAN}[STEP {_step_num[0]}]{_NC} {_BOLD}{title}{_NC}")


def info(msg):
    print(f"  {_INFO} {msg}")


def success(msg):
    print(f"  {_OK} {msg}")


def failure(msg):
    print(f"  {_FAIL} {_RED}{msg}{_NC}")


def warn(msg):
    print(f"  {_WARN} {_YELLOW}{msg}{_NC}")


def verbose_out(out, indent=6):
    if not out:
        return
    pad = " " * indent
    for line in out.splitlines()[:40]:
        print(f"{pad}{_DIM}{line}{_NC}")


# ─── Remediation steps ────────────────────────────────────────────────────────

def fix_pgdata_permissions(cp_host, password, dry_run, verbose):
    """Check pgdatabase-0 readiness and fix pgdata dir permissions via walg sidecar."""
    step("Check pgdatabase-0 / Fix pgdata permissions")

    _, out = ssh_exec(cp_host, password,
                      "kubectl get pod -n salt-raas pgdatabase-0 -o json 2>/dev/null",
                      timeout=30)
    data = parse_json(out)
    if not data:
        warn("pgdatabase-0 not found or not parseable — skipping permission fix")
        return True

    cstats = data.get("status", {}).get("containerStatuses", [])
    ready  = sum(1 for cs in cstats if cs.get("ready", False))
    total  = len(cstats)
    info(f"pgdatabase-0: {ready}/{total} containers ready")

    if ready == total and total > 0:
        success(f"pgdatabase-0 is {ready}/{total} — permissions OK, no fix needed")
        return True

    info(f"pgdatabase-0 is {ready}/{total} — attempting pgdata permission fix via walg sidecar")

    if dry_run:
        info(f"{_DIM}[dry-run] Would exec: chmod 700 /home/postgres/pgdata/pgroot/data in pgdatabase-0:walg{_NC}")
        info(f"{_DIM}[dry-run] Would delete pod pgdatabase-0 and wait for 3/3{_NC}")
        return True

    _, chmod_out = ssh_exec(cp_host, password,
                            "kubectl exec -n salt-raas pgdatabase-0 -c walg -- "
                            "chmod 700 /home/postgres/pgdata/pgroot/data 2>/dev/null && "
                            "echo CHMOD_OK",
                            timeout=30)
    if verbose:
        verbose_out(chmod_out)

    if "CHMOD_OK" not in chmod_out:
        warn("chmod did not confirm — pgdata exec may have failed; continuing anyway")
    else:
        success("pgdata permissions fixed (chmod 700)")

    info("Deleting pgdatabase-0 to trigger restart with corrected permissions...")
    ssh_exec(cp_host, password,
             "kubectl delete pod -n salt-raas pgdatabase-0 --grace-period=0 2>/dev/null",
             timeout=30)

    info("Waiting up to 90s for pgdatabase-0 to reach 3/3 ready...")
    for attempt in range(18):
        time.sleep(5)
        _, ready_out = ssh_exec(cp_host, password,
                                "kubectl get pod -n salt-raas pgdatabase-0 "
                                "-o jsonpath=\"{.status.containerStatuses[*].ready}\" 2>/dev/null",
                                timeout=20)
        ready_out = ready_out.strip()
        if ready_out.count("true") == 3:
            success("pgdatabase-0 reached 3/3 ready")
            return True
        print(f"  {_DIM}  [{attempt + 1}/18] pgdatabase-0 status: {ready_out or '(pending)'}{_NC}",
              flush=True)

    warn("pgdatabase-0 did not reach 3/3 within 90s — continuing (RAAS may be slow to start)")
    return True


def rollout_restart_salt_stack(cp_host, password, dry_run, verbose):
    """Rollout-restart Redis → RAAS → salt-master → salt-minion in order."""
    step("Rollout-restart Salt stack (Redis → RAAS → salt-master → salt-minion)")
    info("This ensures all components load fresh post-rotation TLS certificates")
    info("and connect to healthy dependencies in the correct order.")

    all_ok = True
    for label, ns, resource, wait_secs in SALT_STEPS:
        print(f"\n  {_CYAN}──{_NC} {label} ({ns}/{resource})")

        if dry_run:
            info(f"[dry-run] Would: kubectl rollout restart {resource} -n {ns}")
            if wait_secs:
                info(f"[dry-run] Would wait up to {wait_secs}s for rollout to complete")
            continue

        info(f"kubectl rollout restart {resource} -n {ns}")
        _, rr_out = ssh_exec(cp_host, password,
                             f"kubectl rollout restart {resource} -n {ns} 2>/dev/null",
                             timeout=30)
        if verbose:
            verbose_out(rr_out)

        if not wait_secs:
            info(f"Rollout triggered (no wait requested for {label})")
            continue

        info(f"Waiting up to {wait_secs}s for rollout to complete...")
        _, status_out = ssh_exec(cp_host, password,
                                 f"kubectl rollout status {resource} -n {ns} "
                                 f"--timeout={wait_secs}s 2>/dev/null",
                                 timeout=wait_secs + 15)
        if verbose:
            verbose_out(status_out)

        if "successfully rolled out" in status_out:
            success(f"{label}: rollout complete")
        else:
            warn(f"{label}: did not confirm 'successfully rolled out' within {wait_secs}s")
            warn(f"         Continuing — pod may still be starting up")
            all_ok = False

    return all_ok


def verify_salt_status(cp_host, password, verbose):
    """Quick readiness check for all salt components after remediation."""
    step("Verify Salt component status")

    checks = {
        "redis (salt-raas)":       ("kubectl get pod -n salt-raas -l app=redis -o json", "salt-raas"),
        "raas (salt-raas)":        ("kubectl get pod -n salt-raas -l app=raas -o json",  "salt-raas"),
        "salt-master (salt)":      ("kubectl get pod -n salt -l app=salt-master -o json", "salt"),
        "salt-minion (salt)":      ("kubectl get pod -n salt -l app=salt-minion -o json",  "salt"),
    }

    all_ok = True
    for label, (cmd, _) in checks.items():
        _, out = ssh_exec(cp_host, password, f"{cmd} 2>/dev/null", timeout=30)
        data = parse_json(out)
        if not data:
            failure(f"{label}: no data")
            all_ok = False
            continue
        items  = data.get("items", [])
        cstats = items[0].get("status", {}).get("containerStatuses", []) if items else []
        ready  = sum(1 for cs in cstats if cs.get("ready", False))
        total  = len(cstats)
        ok     = ready == total and total > 0
        if ok:
            success(f"{label}: {ready}/{total} Ready")
        else:
            failure(f"{label}: {ready}/{total} Ready")
            all_ok = False

    # Log tail
    info("Checking salt-master logs for errors...")
    _, log_out = ssh_exec(cp_host, password,
                          "kubectl logs -n salt --selector=app=salt-master --tail=30 2>/dev/null",
                          timeout=30)
    bad = ["SSL CERTIFICATE_VERIFY_FAILED", "This Minion was scheduled to stop",
           "530 Unknown", "RAAS is not available"]
    found = [p for p in bad if p in log_out]
    if not found:
        success("salt-master logs: no critical error patterns in last 30 lines")
    else:
        warn(f"salt-master logs still contain: {'; '.join(found)}")
        warn("  Pods may need additional time. Re-run this script if needed.")
        all_ok = False

    if verbose:
        verbose_out(log_out)

    return all_ok


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
    ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W        = 68

    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'Salt Infrastructure Stabilizer':^{W}}{_NC}{_CYAN}║{_NC}")
    if args.dry_run:
        print(f"{_CYAN}║{_NC}{_YELLOW}{'*** DRY-RUN MODE — no changes will be made ***':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ts:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")

    # Discover CP host
    cp_host = args.host
    if not cp_host:
        print(f"\n{_DIM}Auto-discovering VSP control plane from {args.worker}...{_NC}")
        cp_host = discover_cp(args.worker, password)
        if cp_host:
            print(f"  {_DIM}Control plane IP: {cp_host}{_NC}")
        else:
            cp_host = VSP_VIP
            print(f"  {_WARN} Discovery failed — falling back to VIP {VSP_VIP}")

    # Connectivity test
    print(f"\n{_DIM}Testing SSH connectivity to {cp_host}...{_NC}")
    rc, _ = ssh_exec(cp_host, password, "echo PONG", timeout=20)
    if rc != 0:
        print(f"\n{_RED}ERROR:{_NC} Cannot SSH to {cp_host} as {VSP_USER}.")
        print(f"  If the VIP is down, run kube-fix.py first, or use --host <CP-node-IP>")
        sys.exit(2)
    print(f"  {_OK} SSH to {cp_host} successful")

    # Run remediation steps
    ok1 = fix_pgdata_permissions(cp_host, password, args.dry_run, args.verbose)
    ok2 = rollout_restart_salt_stack(cp_host, password, args.dry_run, args.verbose)

    if not args.dry_run:
        ok3 = verify_salt_status(cp_host, password, args.verbose)
    else:
        ok3 = True

    # Final summary
    overall_ok = ok1 and ok2 and ok3
    color = _GREEN if overall_ok else _YELLOW

    print(f"\n{_CYAN}{'─' * 62}{_NC}")
    if args.dry_run:
        print(f"  {_YELLOW}{_BOLD}DRY-RUN complete — no changes were made{_NC}")
    elif overall_ok:
        print(f"  {_GREEN}{_BOLD}Salt infrastructure stabilization complete{_NC}")
        print(f"  {_DIM}Verify with: python3 vsp-health.py{_NC}")
    else:
        print(f"  {_YELLOW}{_BOLD}Stabilization completed with warnings (see above){_NC}")
        print(f"  {_DIM}Some components may still be starting up — wait 30s and re-run{_NC}")
        print(f"  {_DIM}Verify with: python3 vsp-health.py{_NC}")
    print(f"{_CYAN}{'─' * 62}{_NC}\n")

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
