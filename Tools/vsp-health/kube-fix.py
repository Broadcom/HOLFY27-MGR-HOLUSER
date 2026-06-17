#!/usr/bin/env python3
"""
kube-fix.py
Version 1.0.0 - 2026-06-17
Author: Burke Azbill and HOL Core Team

Remediates Kubernetes control-plane instability on the VSP (Supervisor)
cluster caused by kube-vip VIP drops and kube-controller-manager
CrashLoopBackOff after cold boot.

Root causes addressed:
  1. kube-vip: Default setting vip_preserve_on_leadership_loss=false causes
     the VIP (10.1.1.142) to be dropped when kube-vip panics during leader
     election under API server load at boot.  Without the VIP, kube-scheduler
     and kube-controller-manager lose their API connection and crash.
     Fix: patch /etc/kubernetes/manifests/kube-vip.yaml to set the value
     to "true".  kubelet re-reads the static pod manifest automatically.

  2. kube-controller-manager CrashLoopBackOff: After losing the VIP, KCM
     enters a 5-minute exponential backoff.  During this window it cannot
     update Endpoints/EndpointSlices, so restarted pods (e.g. Redis) are
     never added to their Services — causing the Salt cert-timing race.
     Fix: force-remove the crashed KCM container via crictl so kubelet
     immediately creates a fresh one without waiting for the backoff.

  3. kube-scheduler CrashLoopBackOff: Same backoff issue as KCM.
     Fix: same crictl force-remove approach.

  4. Dropped VIP: If 10.1.1.142 is not currently assigned to the CP node's
     eth0, restore it manually and send a gratuitous ARP so switches update
     their MAC tables.

Use --dry-run to preview actions without making changes.
Use vsp-health.py to verify status before/after.

Exit codes:
  0  All steps completed (or dry-run)
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

# kube-vip.yaml location on the CP node (static pod manifest)
KVIP_MANIFEST = "/etc/kubernetes/manifests/kube-vip.yaml"

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
    print(f"{_CYAN}║{_NC}{_BLUE}{'Kubernetes Control Plane Fix':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    kube-fix.py [--host IP] [--worker FQDN] [--dry-run] [-v]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--host{_NC} <IP>       VSP control-plane host/VIP  (default: {VSP_VIP})")
    print(f"    {_GREEN}--worker{_NC} <FQDN>   VSP worker FQDN for CP discovery (default: {VSP_WORKER})")
    print(f"    {_GREEN}--vip{_NC} <IP>        Kubernetes VIP to restore if down (default: {VSP_VIP})")
    print(f"    {_GREEN}--skip-vip{_NC}        Skip VIP restore step")
    print(f"    {_GREEN}--skip-kvip{_NC}       Skip kube-vip manifest patch step")
    print(f"    {_GREEN}--skip-kcm{_NC}        Skip kube-controller-manager reset step")
    print(f"    {_GREEN}--dry-run{_NC}         Show what would be done without making changes")
    print(f"    {_GREEN}-v, --verbose{_NC}     Show full command output for each step")
    print(f"    {_GREEN}-h, --help{_NC}        Show this help message\n")
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Run all control-plane fixes (typical usage){_NC}")
    print(f"    python3 kube-fix.py\n")
    print(f"    {_GREEN}# Preview without making changes{_NC}")
    print(f"    python3 kube-fix.py --dry-run\n")
    print(f"    {_GREEN}# Fix kube-vip manifest only (skip VIP restore and KCM reset){_NC}")
    print(f"    python3 kube-fix.py --skip-vip --skip-kcm\n")
    print(f"    {_GREEN}# If VIP is down, specify the actual CP node IP instead{_NC}")
    print(f"    python3 kube-fix.py --host 10.1.1.143\n")
    print(f"{_BOLD}NOTE:{_NC}")
    print(f"    The kube-vip manifest patch (step 2) is persistent — it survives")
    print(f"    reboots and only needs to be applied once per node.")
    print(f"\n    After running, wait ~30s then verify: python3 vsp-health.py")
    print(f"    If Salt is still down after this: python3 salt-stabilize.py\n")
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


def ping_host(ip, timeout=2):
    """Return True if ip responds to a single ping."""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


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
    for line in out.splitlines()[:30]:
        print(f"{pad}{_DIM}{line}{_NC}")


# ─── Fix steps ────────────────────────────────────────────────────────────────

def fix_vip_restore(cp_host, password, vip, dry_run, verbose):
    """Restore the VIP on eth0 if it is not currently reachable."""
    step(f"VIP Check — restore {vip} on eth0 if dropped")

    vip_up = ping_host(vip)
    if vip_up:
        success(f"VIP {vip} is already reachable — no restore needed")
        return True

    warn(f"VIP {vip} is NOT reachable — attempting restore on {cp_host}")
    if dry_run:
        info(f"[dry-run] Would run on {cp_host}: ip addr add {vip}/32 dev eth0 && arping ...")
        return True

    restore_cmd = (
        f"ip addr show eth0 | grep -q '{vip}' || ip addr add {vip}/32 dev eth0 2>/dev/null; "
        f"arping -c 3 -U -I eth0 {vip} 2>/dev/null || true; "
        f"echo VIP_RESTORED"
    )
    _, out = ssh_exec(cp_host, password, restore_cmd, timeout=30)
    if verbose:
        verbose_out(out)

    time.sleep(3)
    vip_now = ping_host(vip)
    if vip_now:
        success(f"VIP {vip} restored and reachable")
        return True
    else:
        warn(f"VIP {vip} still unreachable after restore attempt")
        warn(f"  The CP node at {cp_host} may not be the correct host for the VIP")
        warn(f"  Check vCenter console for the CP node VM and try --host <actual-CP-IP>")
        return False


def fix_kvip_manifest(cp_host, password, dry_run, verbose):
    """Patch kube-vip.yaml to set vip_preserve_on_leadership_loss=true."""
    step(f"kube-vip manifest — set vip_preserve_on_leadership_loss=true")

    # Read current setting
    _, check_out = ssh_exec(cp_host, password,
                             f"grep -A1 vip_preserve {KVIP_MANIFEST} 2>/dev/null")
    if verbose:
        verbose_out(check_out)

    if not check_out:
        warn(f"kube-vip manifest not found at {KVIP_MANIFEST} — may not be a CP node")
        return True  # non-fatal, worker nodes don't have this

    already_true = "true" in check_out
    if already_true:
        success(f"vip_preserve_on_leadership_loss already set to true — no patch needed")
        return True

    info(f"Current setting: {check_out.strip()!r}")
    info("Patching manifest to set value: \"true\" ...")

    if dry_run:
        info(f"[dry-run] Would run: sed -i '/vip_preserve_on_leadership_loss/{{n; s/\"false\"/\"true\"/}}' {KVIP_MANIFEST}")
        return True

    # sed: find the env var name line, advance to next line, replace "false" with "true"
    patch_cmd = (
        f"sed -i '/vip_preserve_on_leadership_loss/{{n; s/\"false\"/\"true\"/}}' {KVIP_MANIFEST}"
    )
    _, patch_out = ssh_exec(cp_host, password, patch_cmd, timeout=20)
    if verbose:
        verbose_out(patch_out)

    # Verify
    _, verify_out = ssh_exec(cp_host, password,
                              f"grep -A1 vip_preserve {KVIP_MANIFEST} 2>/dev/null")
    if "true" in verify_out:
        success("kube-vip manifest patched — vip_preserve_on_leadership_loss=true")
        info("kubelet will re-read the static pod manifest and restart kube-vip")
        return True
    else:
        failure("Manifest patch did not take effect")
        info(f"  Current value: {verify_out.strip()!r}")
        info(f"  Manual fix: edit {KVIP_MANIFEST} on {cp_host}")
        info(f"  Look for the env var 'vip_preserve_on_leadership_loss' and set its value to \"true\"")
        return False


def fix_crashed_control_plane_pod(cp_host, password, pod_kw, friendly_name, dry_run, verbose):
    """Force-remove a crashed static pod container so kubelet creates a fresh one."""
    # Check crictl ps -a for the pod in non-Running state
    _, crictl_out = ssh_exec(cp_host, password, "crictl ps -a 2>/dev/null", timeout=20)
    if verbose:
        verbose_out(crictl_out)

    matching_lines = [ln for ln in crictl_out.splitlines() if pod_kw in ln]
    if not matching_lines:
        info(f"{friendly_name}: no container found in crictl ps -a — kubelet will create it")
        return True

    running = any("Running" in ln for ln in matching_lines)
    if running:
        success(f"{friendly_name}: already Running — no reset needed")
        return True

    # Container exists but is not Running (Exited, Unkown, etc.) → likely CrashLoopBackOff
    states = ", ".join(ln.split()[3] if len(ln.split()) > 3 else "?" for ln in matching_lines)
    warn(f"{friendly_name}: found in state [{states}] — force-removing to reset backoff")

    if dry_run:
        info(f"[dry-run] Would run: crictl ps -a | grep {pod_kw} | awk '{{print $1}}' | xargs crictl rm -f")
        return True

    rm_cmd = f"crictl ps -a 2>/dev/null | grep {pod_kw} | awk '{{print $1}}' | xargs -r crictl rm -f 2>/dev/null && echo REMOVED"
    _, rm_out = ssh_exec(cp_host, password, rm_cmd, timeout=30)
    if verbose:
        verbose_out(rm_out)

    info(f"Waiting 20s for kubelet to recreate {friendly_name}...")
    time.sleep(20)

    _, check_out = ssh_exec(cp_host, password, "crictl ps 2>/dev/null", timeout=20)
    running_now = any(pod_kw in ln and "Running" in ln for ln in check_out.splitlines())
    if running_now:
        success(f"{friendly_name}: Running after reset")
        return True
    else:
        warn(f"{friendly_name}: not yet Running — may still be starting (check again with vsp-health.py)")
        return False


def fix_kube_controller_manager(cp_host, password, dry_run, verbose):
    step("kube-controller-manager — reset CrashLoopBackOff if needed")
    ok = fix_crashed_control_plane_pod(cp_host, password,
                                       "kube-controller", "kube-controller-manager",
                                       dry_run, verbose)
    return ok


def fix_kube_scheduler(cp_host, password, dry_run, verbose):
    step("kube-scheduler — reset CrashLoopBackOff if needed")
    ok = fix_crashed_control_plane_pod(cp_host, password,
                                       "kube-scheduler", "kube-scheduler",
                                       dry_run, verbose)
    return ok


def verify_cp_status(cp_host, password, vip, verbose):
    """Quick control-plane health check after remediation."""
    step("Verify control-plane status")

    vip_ok = ping_host(vip)
    if vip_ok:
        success(f"VIP {vip}: reachable")
    else:
        failure(f"VIP {vip}: still unreachable")

    _, crictl_out = ssh_exec(cp_host, password, "crictl ps 2>/dev/null", timeout=20)
    all_ok = vip_ok
    for kw, label in [
        ("kube-controller", "kube-controller-manager"),
        ("kube-scheduler",  "kube-scheduler"),
        ("kube-vip",        "kube-vip"),
        ("etcd",            "etcd"),
    ]:
        running = any(kw in ln and "Running" in ln for ln in crictl_out.splitlines())
        if running:
            success(f"{label}: Running")
        else:
            failure(f"{label}: not Running")
            all_ok = False

    if verbose:
        verbose_out(crictl_out)

    # K8s node status (quick check)
    _, nodes_out = ssh_exec(cp_host, password,
                            "kubectl get nodes --no-headers 2>/dev/null", timeout=25)
    if nodes_out:
        not_ready = [ln for ln in nodes_out.splitlines() if "Ready" not in ln or "NotReady" in ln]
        if not_ready:
            for ln in not_ready:
                failure(f"Node issue: {ln.strip()}")
                all_ok = False
        else:
            success(f"All nodes Ready")
    else:
        warn("kubectl get nodes returned no output — API server may still be starting")

    return all_ok


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host",       default=None,       metavar="IP")
    parser.add_argument("--worker",     default=VSP_WORKER, metavar="FQDN")
    parser.add_argument("--vip",        default=VSP_VIP,    metavar="IP")
    parser.add_argument("--skip-vip",   action="store_true")
    parser.add_argument("--skip-kvip",  action="store_true")
    parser.add_argument("--skip-kcm",   action="store_true")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    password = get_password()
    ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W        = 68

    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'Kubernetes Control Plane Fix':^{W}}{_NC}{_CYAN}║{_NC}")
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
            cp_host = args.vip
            print(f"  {_WARN} Discovery failed — falling back to VIP {args.vip}")

    # Connectivity test
    print(f"\n{_DIM}Testing SSH connectivity to {cp_host}...{_NC}")
    rc, _ = ssh_exec(cp_host, password, "echo PONG", timeout=20)
    if rc != 0:
        print(f"\n{_RED}ERROR:{_NC} Cannot SSH to {cp_host} as {VSP_USER}.")
        print(f"  If VIP {args.vip} is down and SSH via VIP fails:")
        print(f"  1. Find the actual CP node IP via vCenter console (look for the VSP CP VM)")
        print(f"  2. Retry: python3 kube-fix.py --host <actual-CP-IP>")
        sys.exit(2)
    print(f"  {_OK} SSH to {cp_host} successful")

    results = []

    if not args.skip_vip:
        results.append(fix_vip_restore(cp_host, password, args.vip, args.dry_run, args.verbose))

    if not args.skip_kvip:
        results.append(fix_kvip_manifest(cp_host, password, args.dry_run, args.verbose))

    if not args.skip_kcm:
        results.append(fix_kube_controller_manager(cp_host, password, args.dry_run, args.verbose))
        results.append(fix_kube_scheduler(cp_host, password, args.dry_run, args.verbose))

    if not args.dry_run:
        info_banner = "Waiting 15s for static pods to stabilize before verification..."
        print(f"\n  {_DIM}{info_banner}{_NC}")
        time.sleep(15)
        ok_verify = verify_cp_status(cp_host, password, args.vip, args.verbose)
        results.append(ok_verify)

    overall_ok = all(r for r in results)
    print(f"\n{_CYAN}{'─' * 62}{_NC}")
    if args.dry_run:
        print(f"  {_YELLOW}{_BOLD}DRY-RUN complete — no changes were made{_NC}")
    elif overall_ok:
        print(f"  {_GREEN}{_BOLD}Control-plane fix complete{_NC}")
        print(f"  {_DIM}Next: python3 salt-stabilize.py  (if Salt is still down){_NC}")
        print(f"  {_DIM}Then: python3 vsp-health.py      (full verification){_NC}")
    else:
        print(f"  {_YELLOW}{_BOLD}Fix completed with warnings — review {_FAIL} items above{_NC}")
        print(f"  {_DIM}Some components may still be starting up — wait 30s and re-run{_NC}")
        print(f"  {_DIM}If issues persist: python3 vsp-health.py for detailed status{_NC}")
    print(f"{_CYAN}{'─' * 62}{_NC}\n")

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
