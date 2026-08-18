#!/usr/bin/env python3
"""
vsp-scale-down.py
Version 1.3.2 - 2026-08-18
Author: Burke Azbill and HOL Core Team

v1.3.2: Standardized ANSI blue color (_BLUE) to standard 16-color ANSI (\033[0;34m) for universal terminal compatibility.

End-to-end automation of the VSP Kubernetes cluster worker resize/scale
walkthrough documented in vsp-cluster-sizing.md:

  Step 3  Resize worker machine type   (PackageDeployment -> machineType/size)
          Resize CP machine type       (PackageDeployment -> cluster.machineType)
  Step 4  Scale worker replica count   (PackageDeployment -> minReplicas/maxReplicas)

Both steps patch PackageDeployment/vmsp-platform exclusively -- never the
KubernetesCluster CR directly -- per the ownership chain documented there.
Everything else (cluster name, MachineDeployment name, current sizing) is
auto-discovered; only the Control Plane VIP, a password, and at least one
target (--machine-type, --cp-machine-type, and/or --worker-count/--min-replicas/--max-replicas)
are required.

Available MachineType Reference:
  Control Plane:
    cp.small: 4 vCPU, 10 GiB RAM
    cp.medium: 6 vCPU, 12 GiB RAM
    cp.large: 8 vCPU, 14 GiB RAM
  Worker (Management):
    management.small: 4 vCPU, 8 GiB RAM
    management.medium: 8 vCPU, 16 GiB RAM
    management.large: 12 vCPU, 24 GiB RAM
    management.xlarge: 16 vCPU, 32 GiB RAM

Per-node CPU/memory utilization (via `kubectl top nodes`) is captured before
any change and again during final verification, so you can see the effect of
the resize/scale on actual load, and get a warning if any node is running hot.

If the cluster-autoscaler hits the known "size increase too large" stuck
loop while draining excess workers, this script detects it from the
autoscaler's own logs and automatically patches MachineDeployment.spec.replicas
directly to unblock it (safe -- that object is CAPI-owned, not Flux-owned).
It will NOT force a scale-down the autoscaler is correctly refusing on
CPU-eligibility grounds (see "unremovable ... above the scale-down utilization
threshold" in its logs) -- that's a real capacity floor, not a bug.

DISCLAIMER: This is not a Broadcom-documented or supported procedure.
Use only in non-critical lab/dev environments. See vsp-cluster-sizing.md.

Exit codes:
  0  Completed successfully (or --dry-run completed)
  1  User aborted, or a requested change was invalid
  2  Cannot connect / authenticate to the VSP Control Plane
  3  A step failed or timed out -- cluster may be left mid-change
"""
import argparse
import base64
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time

VERSION = "1.3.2"
DATE = "2026-08-18"

DEFAULT_VIP = "10.1.1.142"
DEFAULT_USER = "vmware-system-user"
DEFAULT_CREDS_FILE = "/home/holuser/creds.txt"
NAMESPACE = "vmsp-platform"
PACKAGEDEPLOYMENT_NAME = "vmsp-platform"
DEFAULT_CPU_WARN_PCT = 80

# ─── Colors ────────────────────────────────────────────────────────────────
if sys.stdout.isatty():
    _CYAN, _BLUE, _GREEN, _RED, _YELLOW, _BOLD, _DIM, _NC = (
        '\033[0;36m', '\033[0;34m', '\033[0;32m',
        '\033[0;31m', '\033[1;33m', '\033[1m', '\033[2m', '\033[0m'
    )
else:
    _CYAN = _BLUE = _GREEN = _RED = _YELLOW = _BOLD = _DIM = _NC = ''

_OK = f"{_GREEN}✓{_NC}"
_FAIL = f"{_RED}✗{_NC}"
_WARN = f"{_YELLOW}⚠{_NC}"


# ─── Help ──────────────────────────────────────────────────────────────────
def show_help():
    W = 70
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VSP Worker Resize / Scale-Down':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}")
    print("   vsp-scale-down.py [--host VIP] [--machine-type TYPE] [--cp-machine-type TYPE] [--worker-count N] [--min-replicas N] [--max-replicas N] [options]\n")
    print("   HOL Usage example: python3 /home/holuser/hol/Tools/vsp-health/vsp-scale-down.py --host 10.1.1.142 --machine-type management.medium --cp-machine-type cp.medium --min-replicas 4 --max-replicas 7 --yes\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--host{_NC} <VIP>              VSP Control Plane VIP/IP  (default: {DEFAULT_VIP})")
    print(f"    {_GREEN}--user{_NC} <name>             SSH user  (default: {DEFAULT_USER})")
    print(f"    {_GREEN}--creds-file{_NC} <path>       File with password as first line  (default: {DEFAULT_CREDS_FILE})")
    print(f"    {_GREEN}--password-file{_NC} <path>    Explicit password file (overrides --creds-file)")
    print(f"    {_GREEN}--machine-type{_NC} <type>     Target worker machine type, e.g. management.medium")
    print(f"    {_GREEN}--cp-machine-type{_NC} <type>  Target control plane machine type, e.g. cp.medium")
    print(f"    {_GREEN}--worker-count{_NC} <N>        Target worker count (sets min == max == N)")
    print(f"    {_GREEN}--min-replicas{_NC} <N>        Target worker floor (advanced -- use with --max-replicas)")
    print(f"    {_GREEN}--max-replicas{_NC} <N>        Target worker ceiling (advanced -- use with --min-replicas)")
    print(f"    {_GREEN}--autoscaler{_NC} <mode>       Final autoscaler state: auto, enable, disable (default: auto)")
    print(f"    {_GREEN}-y, --yes{_NC}                 Skip the disclaimer/confirmation prompt")
    print(f"    {_GREEN}--no-auto-fix-autoscaler{_NC}  Don't auto-patch MachineDeployment if autoscaler gets stuck")
    print(f"    {_GREEN}--dry-run{_NC}                 Discover and print planned changes, apply nothing")
    print(f"    {_GREEN}--resize-timeout{_NC} <min>    Max wait for Step 3 rollout  (default: 60)")
    print(f"    {_GREEN}--scale-timeout{_NC} <min>     Max wait for Step 4 drain    (default: 60)")
    print(f"    {_GREEN}--poll-interval{_NC} <sec>     Seconds between status polls  (default: 20)")
    print(f"    {_GREEN}--cpu-warn-pct{_NC} <pct>      Flag a node as hot at/above this CPU or memory %  (default: {DEFAULT_CPU_WARN_PCT})")
    print(f"    {_GREEN}-v, --verbose{_NC}             Show raw kubectl output during polling")
    print(f"    {_GREEN}-h, --help{_NC}                Show this help message\n")
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Minimal -- prompts for host (default shown) and password only{_NC}")
    print("    python3 vsp-scale-down.py --worker-count 5\n")
    print(f"    {_GREEN}# Resize workers to management.medium and scale to 5 nodes, no prompts{_NC}")
    print("    python3 vsp-scale-down.py --machine-type management.medium --worker-count 5 --yes\n")
    print(f"    {_GREEN}# Just change the worker count, leave machine type alone{_NC}")
    print("    python3 vsp-scale-down.py --worker-count 5 --yes\n")
    print(f"    {_GREEN}# Preview what would change without touching the cluster{_NC}")
    print("    python3 vsp-scale-down.py --machine-type management.medium --worker-count 5 --dry-run\n")
    print(f"    {_GREEN}# Site-B, explicit password file, fully unattended{_NC}")
    print("    python3 vsp-scale-down.py --host 10.2.1.142 --password-file /tmp/pw.txt \\")
    print("        --machine-type management.medium --worker-count 5 --yes\n")
    print(f"{_BOLD}EXIT CODES:{_NC}")
    print("    0  Success   1  Aborted/invalid   2  Cannot connect   3  Step failed/timed out")
    sys.exit(0)


class _HelpOnErrorParser(argparse.ArgumentParser):
    def error(self, message):
        print(f"{_RED}ERROR:{_NC} {message}\n", file=sys.stderr)
        show_help()


# ─── SSH / kubectl helpers ───────────────────────────────────────────────────
_SUDO_RE = re.compile(r"\[sudo\] password for [^:]+:\s*")
_NOISE = ("Welcome to Photon", "Warning: Permanently added",
          "Connection to ", "Killed by signal")


def ssh_exec(host, user, password, cmd, timeout=60):
    """Run cmd as root on host via sshpass + sudo -S -i + base64. Returns (rc, output)."""
    cmd_b64 = base64.b64encode(cmd.encode()).decode()
    outer = (
        f"echo '{password}' | sudo -S -i "
        f"bash -c \"$(echo {cmd_b64} | base64 -d)\" 2>&1"
    )
    try:
        r = subprocess.run(
            ["sshpass", "-p", password, "ssh",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "LogLevel=ERROR",
             "-o", "ConnectTimeout=15",
             f"{user}@{host}", outer],
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
        return 1, "sshpass not found -- install it first (apt-get install sshpass / brew install sshpass)"
    except Exception as exc:
        return 1, str(exc)


def kctl(host, user, password, args, timeout=60):
    """Run a kubectl command on the VSP Control Plane, with the NO_PROXY/Forbidden fix applied inline."""
    cmd = (
        f'export NO_PROXY="$NO_PROXY,{host}"; export no_proxy="$no_proxy,{host}"; '
        f"kubectl {args}"
    )
    rc, out = ssh_exec(host, user, password, cmd, timeout=timeout)
    if "Unable to connect to the server: Forbidden" in out:
        print(f"  {_WARN} Hit the proxy/Forbidden issue despite the inline NO_PROXY fix.")
        print(f"  {_DIM}See vsp-cluster-sizing.md Step 1 -- check /etc/environment on {host} manually.{_NC}")
    return rc, out


def kctl_json(host, user, password, args, timeout=60):
    """Run a kubectl command expected to return JSON. Returns (ok, parsed_or_None, raw_output)."""
    rc, out = kctl(host, user, password, f"{args} -o json", timeout=timeout)
    if rc != 0 or not out.strip():
        return False, None, out
    try:
        return True, json.loads(out), out
    except json.JSONDecodeError:
        return False, None, out


# ─── Discovery ────────────────────────────────────────────────────────────────
def test_connectivity(host, timeout=5):
    """Quick TCP check on SSH (22) and the Kubernetes API (6443)."""
    for port, label in ((22, "SSH"), (6443, "Kubernetes API")):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                print(f"  {_OK} {label} port {port} reachable on {host}")
        except OSError as e:
            print(f"  {_FAIL} {label} port {port} unreachable on {host}: {e}")
            return False
    return True


def discover_cluster_name(host, user, password):
    ok, data, raw = kctl_json(host, user, password, f"get kubernetescluster -n {NAMESPACE}")
    if not ok or not data or not data.get("items"):
        return None, raw
    items = data["items"]
    if len(items) > 1:
        print(f"  {_WARN} Multiple KubernetesCluster objects found; using the first: "
              f"{[i['metadata']['name'] for i in items]}")
    return items[0]["metadata"]["name"], raw


def discover_machinedeployment_name(host, user, password):
    ok, data, raw = kctl_json(host, user, password, f"get machinedeployment -n {NAMESPACE}")
    if not ok or not data or not data.get("items"):
        return None, raw
    items = data["items"]
    if len(items) > 1:
        print(f"  {_WARN} Multiple MachineDeployment objects found; using the first: "
              f"{[i['metadata']['name'] for i in items]}")
    return items[0]["metadata"]["name"], raw


def get_packagedeployment_worker(host, user, password):
    ok, data, raw = kctl_json(host, user, password,
                               f"get packagedeployment {PACKAGEDEPLOYMENT_NAME} -n {NAMESPACE}")
    if not ok or not data:
        return None, raw
    worker = (data.get("spec", {}).get("values", {})
                  .get("cluster", {}).get("worker", {}))
    return worker, raw


def get_packagedeployment_cluster(host, user, password):
    ok, data, raw = kctl_json(host, user, password,
                               f"get packagedeployment {PACKAGEDEPLOYMENT_NAME} -n {NAMESPACE}")
    if not ok or not data:
        return None, raw
    cluster = (data.get("spec", {}).get("values", {})
                  .get("cluster", {}))
    return cluster, raw


def get_controlplane_status(host, user, password):
    ok, data, raw = kctl_json(host, user, password, f"get kubeadmcontrolplane -n {NAMESPACE}")
    if not ok or not data or not data.get("items"):
        return None, raw
    item = data["items"][0]
    spec = item.get("spec", {})
    status = item.get("status", {})
    template_name = (spec.get("machineTemplate", {})
                         .get("spec", {})
                         .get("infrastructureRef", {})
                         .get("name"))
    return {
        "desired": spec.get("replicas", 0),
        "ready": status.get("readyReplicas", 0),
        "template": template_name,
    }, raw


def get_kubernetescluster_workers(host, user, password, cluster_name):
    ok, data, raw = kctl_json(host, user, password,
                               f"get kubernetescluster {cluster_name} -n {NAMESPACE}")
    if not ok or not data:
        return None, raw
    return data.get("spec", {}).get("workers", []), raw


def get_machinedeployment_status(host, user, password, md_name):
    ok, data, raw = kctl_json(host, user, password,
                               f"get machinedeployment {md_name} -n {NAMESPACE}")
    if not ok or not data:
        return None, raw
    spec = data.get("spec", {})
    status = data.get("status", {})
    return {
        "desired": spec.get("replicas", 0),
        "ready": status.get("readyReplicas", 0),
        "updated": status.get("updatedReplicas", 0),
        "available": status.get("availableReplicas", 0),
        "current": status.get("replicas", 0),
        "template": spec.get("template", {}).get("spec", {}).get("infrastructureRef", {}).get("name"),
    }, raw


def get_template_sizing(host, user, password, template_name):
    if not template_name:
        return None, None
    ok, data, raw = kctl_json(host, user, password,
                               f"get vspheremachinetemplate {template_name} -n {NAMESPACE}")
    if not ok or not data:
        return None, None
    spec = data.get("spec", {}).get("template", {}).get("spec", {})
    return spec.get("numCPUs"), spec.get("memoryMiB")


def find_autoscaler_pod(host, user, password):
    rc, out = kctl(host, user, password, f"get pods -n {NAMESPACE} --no-headers")
    for line in out.splitlines():
        parts = line.split()
        if parts and "cluster-autoscaler" in parts[0]:
            return parts[0]
    return None


def autoscaler_stuck(host, user, password, ca_pod):
    if not ca_pod:
        return False
    rc, out = kctl(host, user, password,
                   f"logs -n {NAMESPACE} {ca_pod} --tail=200", timeout=30)
    return "size increase too large" in out


def count_pending_pods(host, user, password):
    rc, out = kctl(host, user, password,
                   "get pods -A --field-selector=status.phase=Pending --no-headers")
    if rc != 0:
        return None
    lines = [l for l in out.splitlines() if l.strip()]
    return len(lines)


def get_node_utilization(host, user, password):
    """Return a list of {name, cpu_cores, cpu_pct, mem, mem_pct} via `kubectl top nodes`.
    Returns None if metrics-server isn't available or output can't be parsed."""
    rc, out = kctl(host, user, password, "top nodes --no-headers", timeout=20)
    if rc != 0 or not out.strip():
        return None
    nodes = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            nodes.append({
                "name": parts[0],
                "cpu_cores": parts[1],
                "cpu_pct": int(parts[2].rstrip("%")),
                "mem": parts[3],
                "mem_pct": int(parts[4].rstrip("%")),
            })
        except ValueError:
            continue
    return nodes or None


def print_node_utilization(nodes, label, warn_pct):
    """Print a per-node CPU/memory utilization table. Returns the list of hot node names."""
    if not nodes:
        print(f"  {_DIM}(kubectl top nodes unavailable -- metrics-server may not be ready){_NC}")
        return []
    print(f"  {_DIM}Node utilization ({label}):{_NC}")
    hot = []
    for n in sorted(nodes, key=lambda x: -x["cpu_pct"]):
        is_hot = n["cpu_pct"] >= warn_pct or n["mem_pct"] >= warn_pct
        flag = f"  {_WARN}" if is_hot else ""
        print(f"    {n['name']:<20} CPU {n['cpu_pct']:>3}%   MEM {n['mem_pct']:>3}%{flag}")
        if is_hot:
            hot.append(n["name"])
    return hot


def print_utilization_delta(before, after):
    """Print a before -> after comparison table for nodes present in both snapshots."""
    if not before or not after:
        return
    before_map = {n["name"]: n for n in before}
    after_map = {n["name"]: n for n in after}
    common = sorted(set(before_map) & set(after_map))
    removed = sorted(set(before_map) - set(after_map))
    added = sorted(set(after_map) - set(before_map))
    if not (common or removed or added):
        return
    print(f"  {_DIM}Utilization change (before -> after):{_NC}")
    for name in common:
        b, a = before_map[name], after_map[name]
        print(f"    {name:<20} CPU {b['cpu_pct']:>3}% -> {a['cpu_pct']:>3}%   "
              f"MEM {b['mem_pct']:>3}% -> {a['mem_pct']:>3}%")
    if removed:
        print(f"    {_DIM}Removed: {', '.join(removed)}{_NC}")
    if added:
        print(f"    {_DIM}Added:   {', '.join(added)}{_NC}")


def get_autoscaler_state(host, user, password):
    ok, data, _ = kctl_json(host, user, password, "get deploy cluster-autoscaler-clusterapi-cluster-autoscaler -n vmsp-platform")
    if not ok or not data:
        return None
    replicas = data.get("spec", {}).get("replicas", 0)
    return replicas > 0


def set_autoscaler_state(host, user, password, enable):
    if enable:
        rc1, out1 = kctl(host, user, password, "patch helmrelease cluster-autoscaler -n vmsp-platform --type=merge -p '{\"spec\": {\"suspend\": true}}'")
        rc2, out2 = kctl(host, user, password, "scale deploy cluster-autoscaler-clusterapi-cluster-autoscaler -n vmsp-platform --replicas=1")
        if rc1 != 0 or rc2 != 0:
            print(f"  {_WARN} Failed to enable autoscaler. Patch: {out1}, Scale: {out2}")
    else:
        rc1, out1 = kctl(host, user, password, "scale deploy cluster-autoscaler-clusterapi-cluster-autoscaler -n vmsp-platform --replicas=0")
        rc2, out2 = kctl(host, user, password, "patch helmrelease cluster-autoscaler -n vmsp-platform --type=merge -p '{\"spec\": {\"suspend\": false}}'")
        if rc1 != 0 or rc2 != 0:
            print(f"  {_WARN} Failed to disable autoscaler. Scale: {out1}, Patch: {out2}")


# ─── Polling ──────────────────────────────────────────────────────────────────
def poll_until(label, check_fn, timeout_min, interval_sec, verbose=False):
    """check_fn() returns (done: bool, status_line: str). Polls until done or timeout."""
    deadline = time.time() + (timeout_min * 60)
    start = time.time()
    while True:
        done, status_line = check_fn()
        elapsed = int(time.time() - start)
        print(f"  {_DIM}[{elapsed:>5}s] {status_line}{_NC}")
        if done:
            print(f"  {_OK} {label} complete after {elapsed}s")
            return True
        if time.time() >= deadline:
            print(f"  {_FAIL} Timed out after {elapsed}s waiting for: {label}")
            return False
        time.sleep(interval_sec)


# ─── Steps ────────────────────────────────────────────────────────────────────
def step2b_resize_cp_machine_type(ctx, target_type):
    print(f"\n{_BOLD}{_CYAN}──── Step 2b: Resize control plane machine type ────{_NC}")
    current, _ = get_packagedeployment_cluster(ctx["host"], ctx["user"], ctx["password"])
    current_type = (current or {}).get("machineType")
    if current_type == target_type:
        print(f"  {_OK} Already at target control plane machine type ({target_type}) -- nothing to do.")
        return True

    print(f"  Current CP machine type: {current_type or '(unknown)'}")
    print(f"  Target CP machine type:  {target_type}")

    if ctx["dry_run"]:
        print(f"  {_DIM}[dry-run] would patch PackageDeployment/{PACKAGEDEPLOYMENT_NAME}"
              f" cluster.machineType={target_type}{_NC}")
        return True

    patch = json.dumps({"spec": {"values": {"cluster": {
        "machineType": target_type
    }}}})
    rc, out = kctl(ctx["host"], ctx["user"], ctx["password"],
                   f"patch packagedeployment {PACKAGEDEPLOYMENT_NAME} -n {NAMESPACE} "
                   f"--type=merge -p='{patch}'")
    if rc != 0:
        print(f"  {_FAIL} Patch failed:\n{out}")
        return False
    print(f"  {_OK} PackageDeployment patched -- CP rolling replacement starting.")
    return True


def step3_resize_machine_type(ctx, target_type):
    print(f"\n{_BOLD}{_CYAN}──── Step 3: Resize worker machine type ────{_NC}")
    current, _ = get_packagedeployment_worker(ctx["host"], ctx["user"], ctx["password"])
    current_type = (current or {}).get("machineType")
    if current_type == target_type:
        print(f"  {_OK} Already at target machine type ({target_type}) -- nothing to do.")
        return True

    size = target_type.split(".")[-1] if "." in target_type else target_type
    print(f"  Current machine type: {current_type or '(unknown)'}")
    print(f"  Target machine type:  {target_type} (size: {size})")

    if ctx["dry_run"]:
        print(f"  {_DIM}[dry-run] would patch PackageDeployment/{PACKAGEDEPLOYMENT_NAME}"
              f" worker.machineType={target_type}, worker.size={size}{_NC}")
        return True

    patch = json.dumps({"spec": {"values": {"cluster": {"worker": {
        "machineType": target_type, "size": size,
    }}}}})
    rc, out = kctl(ctx["host"], ctx["user"], ctx["password"],
                   f"patch packagedeployment {PACKAGEDEPLOYMENT_NAME} -n {NAMESPACE} "
                   f"--type=merge -p='{patch}'")
    if rc != 0:
        print(f"  {_FAIL} Patch failed:\n{out}")
        return False
    print(f"  {_OK} PackageDeployment patched -- rolling replacement starting.")

    def check():
        status, _ = get_machinedeployment_status(ctx["host"], ctx["user"], ctx["password"], ctx["md_name"])
        if not status:
            return False, "could not read MachineDeployment status"
        done = (status["desired"] == status["updated"] == status["ready"] == status["current"]
                 and status["desired"] > 0)
        line = (f"desired={status['desired']} current={status['current']} "
                f"ready={status['ready']} updated={status['updated']}")
        return done, line

    return poll_until("worker rollout", check, ctx["resize_timeout"], ctx["poll_interval"], ctx["verbose"])


def step4_scale_replicas(ctx, min_r, max_r):
    print(f"\n{_BOLD}{_CYAN}──── Step 4: Scale worker replica count ────{_NC}")
    current, _ = get_packagedeployment_worker(ctx["host"], ctx["user"], ctx["password"])
    cur_min = (current or {}).get("minReplicas")
    cur_max = (current or {}).get("maxReplicas")
    print(f"  Current bounds: minReplicas={cur_min} maxReplicas={cur_max}")
    print(f"  Target bounds:  minReplicas={min_r} maxReplicas={max_r}")

    if cur_min == min_r and cur_max == max_r:
        print(f"  {_OK} Already at target replica bounds -- nothing to do.")
        return True

    if ctx["dry_run"]:
        print(f"  {_DIM}[dry-run] would patch PackageDeployment/{PACKAGEDEPLOYMENT_NAME}"
              f" worker.minReplicas={min_r}, worker.maxReplicas={max_r}{_NC}")
        return True

    patch = json.dumps({"spec": {"values": {"cluster": {"worker": {
        "minReplicas": min_r, "maxReplicas": max_r,
    }}}}})
    rc, out = kctl(ctx["host"], ctx["user"], ctx["password"],
                   f"patch packagedeployment {PACKAGEDEPLOYMENT_NAME} -n {NAMESPACE} "
                   f"--type=merge -p='{patch}'")
    if rc != 0:
        print(f"  {_FAIL} Patch failed:\n{out}")
        return False
    print(f"  {_OK} PackageDeployment patched.")

    print(f"  Waiting for Flux/vmsp-operator to propagate to KubernetesCluster "
          f"(~10 min window)...")

    def check_propagation():
        workers, _ = get_kubernetescluster_workers(ctx["host"], ctx["user"], ctx["password"], ctx["cluster_name"])
        if not workers:
            return False, "could not read KubernetesCluster spec.workers"
        w = workers[0]
        done = w.get("minReplicas") == min_r and w.get("maxReplicas") == max_r
        return done, f"KubernetesCluster shows minReplicas={w.get('minReplicas')} maxReplicas={w.get('maxReplicas')}"

    if not poll_until("KubernetesCluster propagation", check_propagation, 15, ctx["poll_interval"], ctx["verbose"]):
        return False

    print(f"  Waiting for the worker pool to drain/scale to the new bounds...")
    ca_pod = find_autoscaler_pod(ctx["host"], ctx["user"], ctx["password"])
    autoscaler_fix_applied = False

    def check_drain():
        nonlocal autoscaler_fix_applied
        status, _ = get_machinedeployment_status(ctx["host"], ctx["user"], ctx["password"], ctx["md_name"])
        if not status:
            return False, "could not read MachineDeployment status"
        done = (status["desired"] <= max_r and status["current"] == status["desired"]
                 and status["ready"] == status["desired"])
        line = f"desired={status['desired']} current={status['current']} ready={status['ready']}"

        if not done and not autoscaler_fix_applied and status["desired"] > max_r:
            if autoscaler_stuck(ctx["host"], ctx["user"], ctx["password"], ca_pod):
                print(f"\n  {_WARN} Detected the known cluster-autoscaler bug: "
                      f"\"size increase too large\" in {ca_pod} logs.")
                print(f"  {_DIM}The autoscaler cannot step down from {status['desired']} through "
                      f"intermediate values above maxReplicas={max_r} -- see vsp-cluster-sizing.md "
                      f"Step 4, 'If the autoscaler gets stuck'.{_NC}")
                if ctx["auto_fix_autoscaler"]:
                    print(f"  {_BOLD}Auto-fixing:{_NC} patching MachineDeployment/{ctx['md_name']} "
                          f"spec.replicas={max_r} directly (safe -- CAPI-owned, not Flux-owned).")
                    fix_patch = json.dumps({"spec": {"replicas": max_r}})
                    rc, out = kctl(ctx["host"], ctx["user"], ctx["password"],
                                   f"patch machinedeployment {ctx['md_name']} -n {NAMESPACE} "
                                   f"--type=merge -p='{fix_patch}'")
                    if rc == 0:
                        print(f"  {_OK} MachineDeployment.spec.replicas patched to {max_r}.\n")
                    else:
                        print(f"  {_FAIL} Auto-fix patch failed:\n{out}\n")
                    autoscaler_fix_applied = True
                else:
                    print(f"  {_DIM}--no-auto-fix-autoscaler set -- not intervening. "
                          f"Patch MachineDeployment.spec.replicas={max_r} manually if this doesn't clear.{_NC}\n")
        return done, line

    return poll_until("worker scale-down/up", check_drain, ctx["scale_timeout"], ctx["poll_interval"], ctx["verbose"])


def verify_final_state(ctx):
    print(f"\n{_BOLD}{_CYAN}──── Verify final state ────{_NC}")
    ok = True

    ok_cluster, cluster_data, _ = kctl_json(ctx["host"], ctx["user"], ctx["password"],
                                             f"get kubernetescluster {ctx['cluster_name']} -n {NAMESPACE}")
    phase = (cluster_data or {}).get("status", {}).get("phase") if ok_cluster else None
    if phase == "Ready":
        print(f"  {_OK} KubernetesCluster phase: Ready")
    else:
        print(f"  {_WARN} KubernetesCluster phase: {phase or 'unknown'}")
        ok = False

    status, _ = get_machinedeployment_status(ctx["host"], ctx["user"], ctx["password"], ctx["md_name"])
    if status:
        print(f"  {_OK} Worker count: {status['current']} "
              f"(ready={status['ready']}, updated={status['updated']})")
    else:
        print(f"  {_WARN} Could not read final MachineDeployment status")
        ok = False

    pending = count_pending_pods(ctx["host"], ctx["user"], ctx["password"])
    if pending is None:
        print(f"  {_WARN} Could not check for Pending pods")
    elif pending == 0:
        print(f"  {_OK} No pods stuck Pending")
    else:
        print(f"  {_WARN} {pending} pod(s) currently Pending -- check `kubectl get pods -A "
              f"--field-selector=status.phase=Pending` before assuming this is fine")
        ok = False

    after_util = get_node_utilization(ctx["host"], ctx["user"], ctx["password"])
    hot = print_node_utilization(after_util, "after", ctx["cpu_warn_pct"])
    print_utilization_delta(ctx.get("before_util"), after_util)
    if after_util is None:
        pass  # already reported unavailable above
    elif hot:
        print(f"  {_WARN} {len(hot)} node(s) at/above {ctx['cpu_warn_pct']}% CPU or memory: {', '.join(hot)}")
        ok = False
    else:
        print(f"  {_OK} All nodes below {ctx['cpu_warn_pct']}% CPU/memory utilization")

    return ok


# ─── Main ─────────────────────────────────────────────────────────────────────
def build_parser():
    p = _HelpOnErrorParser(add_help=False)
    p.add_argument("--host", "-H")
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--creds-file", default=DEFAULT_CREDS_FILE)
    p.add_argument("--password-file")
    p.add_argument("--machine-type")
    p.add_argument("--cp-machine-type")
    p.add_argument("--worker-count", type=int)
    p.add_argument("--min-replicas", type=int)
    p.add_argument("--max-replicas", type=int)
    p.add_argument("--autoscaler", choices=["auto", "enable", "disable"], default="auto",
                   help="Desired cluster-autoscaler state after scaling operations complete or time out: auto, enable, disable (default: auto).")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--no-auto-fix-autoscaler", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resize-timeout", type=int, default=60)
    p.add_argument("--scale-timeout", type=int, default=60)
    p.add_argument("--poll-interval", type=int, default=20)
    p.add_argument("--cpu-warn-pct", type=int, default=DEFAULT_CPU_WARN_PCT)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    return p


def resolve_password(args):
    if args.password_file:
        try:
            with open(args.password_file) as f:
                return f.read().strip()
        except OSError as e:
            print(f"{_RED}ERROR:{_NC} Cannot read {args.password_file}: {e}", file=sys.stderr)
            sys.exit(2)
    if os.path.exists(args.creds_file):
        try:
            with open(args.creds_file) as f:
                pw = f.readline().strip()
            if pw:
                print(f"  {_DIM}(password auto-sourced from {args.creds_file}){_NC}")
                return pw
        except OSError:
            pass
    return getpass.getpass(f"Password for {args.user}@{args.host}: ")


def main():
    args = sys.argv[1:]
    if not args or "--help" in args or "-h" in args:
        show_help()

    parser = build_parser()
    parsed = parser.parse_args(args)
    if parsed.help:
        show_help()

    host = parsed.host or input(f"VSP Control Plane VIP/IP [{DEFAULT_VIP}]: ").strip() or DEFAULT_VIP

    if (parsed.min_replicas is None) != (parsed.max_replicas is None):
        print(f"{_RED}ERROR:{_NC} --min-replicas and --max-replicas must be given together.", file=sys.stderr)
        sys.exit(1)
    if parsed.worker_count is not None and parsed.min_replicas is not None:
        print(f"{_RED}ERROR:{_NC} Use --worker-count OR --min-replicas/--max-replicas, not both.", file=sys.stderr)
        sys.exit(1)

    min_r = max_r = None
    if parsed.worker_count is not None:
        min_r = max_r = parsed.worker_count
    elif parsed.min_replicas is not None:
        min_r, max_r = parsed.min_replicas, parsed.max_replicas

    if not parsed.machine_type and not parsed.cp_machine_type and min_r is None and parsed.autoscaler == "auto":
        print(f"{_RED}ERROR:{_NC} Nothing to do -- specify --machine-type, --cp-machine-type, "
              f"--worker-count (or --min-replicas/--max-replicas), and/or --autoscaler enable/disable.", file=sys.stderr)
        sys.exit(1)

    if min_r is not None and min_r > max_r:
        print(f"{_RED}ERROR:{_NC} --min-replicas ({min_r}) cannot exceed --max-replicas ({max_r}).", file=sys.stderr)
        sys.exit(1)

    password = resolve_password(argparse.Namespace(
        password_file=parsed.password_file, creds_file=parsed.creds_file,
        user=parsed.user, host=host,
    ))

    W = 70
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VSP Worker Resize / Scale-Down':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")

    print(f"\n{_RED}DISCLAIMER:{_NC} This is not a Broadcom-documented or supported procedure.")
    print("Use only in non-critical lab/dev environments where the VSP cluster is expendable.")
    print("See vsp-cluster-sizing.md for full details and the ownership-chain rationale.\n")

    print(f"Target host:        {host}")
    print(f"CP Machine type:    {parsed.cp_machine_type or '(unchanged)'}")
    print(f"Worker Machine type:{parsed.machine_type or '(unchanged)'}")
    print(f"Replica bounds:     {f'min={min_r} max={max_r}' if min_r is not None else '(unchanged)'}")
    print(f"Mode:               {'DRY RUN -- no changes will be applied' if parsed.dry_run else 'LIVE'}\n")

    if not parsed.yes and not parsed.dry_run:
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(1)

    print(f"\n{_BOLD}{_CYAN}──── Connectivity ────{_NC}")
    if not test_connectivity(host):
        sys.exit(2)

    rc, out = kctl(host, parsed.user, password, "get nodes --no-headers", timeout=30)
    if rc != 0:
        print(f"  {_FAIL} Could not reach the Kubernetes API via SSH:\n{out}")
        sys.exit(2)
    print(f"  {_OK} kubectl works -- {len([l for l in out.splitlines() if l.strip()])} node(s) visible")

    print(f"\n{_BOLD}{_CYAN}──── Discovery ────{_NC}")
    cluster_name, _ = discover_cluster_name(host, parsed.user, password)
    if not cluster_name:
        print(f"  {_FAIL} Could not discover the KubernetesCluster name.")
        sys.exit(2)
    print(f"  {_OK} KubernetesCluster: {cluster_name}")

    md_name, _ = discover_machinedeployment_name(host, parsed.user, password)
    if not md_name:
        print(f"  {_FAIL} Could not discover the MachineDeployment name.")
        sys.exit(2)
    print(f"  {_OK} MachineDeployment: {md_name}")

    # Discover and display starting Control Plane state & specs
    cp_status, _ = get_controlplane_status(host, parsed.user, password)
    cp_cpu, cp_mem = get_template_sizing(host, parsed.user, password,
                                          (cp_status or {}).get("template"))
    cp_cluster, _ = get_packagedeployment_cluster(host, parsed.user, password)
    cp_type = (cp_cluster or {}).get("machineType", "unknown")
    cp_mem_gb = f"{round(cp_mem / 1024, 1)} GB" if cp_mem else "? GB"
    cp_cpu_str = f"{cp_cpu} vCPU" if cp_cpu else "? vCPU"
    cp_des = (cp_status or {}).get("desired", "?")
    cp_ready = (cp_status or {}).get("ready", "?")
    print(f"  {_OK} Starting Control Plane: machineType={cp_type} "
          f"({cp_cpu_str} / {cp_mem_gb} each), replicas={cp_ready}/{cp_des}")

    # Discover and display starting Worker state & specs
    before_status, _ = get_machinedeployment_status(host, parsed.user, password, md_name)
    before_cpu, before_mem = get_template_sizing(host, parsed.user, password,
                                                  (before_status or {}).get("template"))
    before_worker, _ = get_packagedeployment_worker(host, parsed.user, password)
    worker_type = (before_worker or {}).get("machineType", "unknown")
    worker_mem_gb = f"{round(before_mem / 1024, 1)} GB" if before_mem else "? GB"
    worker_cpu_str = f"{before_cpu} vCPU" if before_cpu else "? vCPU"
    print(f"  {_OK} Starting Workers: {(before_status or {}).get('current', '?')} worker(s), "
          f"machineType={worker_type} "
          f"({worker_cpu_str} / {worker_mem_gb} each), "
          f"minReplicas={(before_worker or {}).get('minReplicas', '?')}, "
          f"maxReplicas={(before_worker or {}).get('maxReplicas', '?')}")

    before_util = get_node_utilization(host, parsed.user, password)
    print_node_utilization(before_util, "before", parsed.cpu_warn_pct)

    ctx = {
        "host": host, "user": parsed.user, "password": password,
        "cluster_name": cluster_name, "md_name": md_name,
        "dry_run": parsed.dry_run, "verbose": parsed.verbose,
        "resize_timeout": parsed.resize_timeout, "scale_timeout": parsed.scale_timeout,
        "poll_interval": parsed.poll_interval,
        "auto_fix_autoscaler": not parsed.no_auto_fix_autoscaler,
        "cpu_warn_pct": parsed.cpu_warn_pct,
        "before_util": before_util,
    }

    overall_ok = True
    initial_autoscaler_enabled = None
    autoscaler_temporarily_enabled = False
    state_str = "UNKNOWN"

    try:
        print(f"\n{_BOLD}{_CYAN}──── Autoscaler Pre-Operation Setup ────{_NC}")
        initial_autoscaler_enabled = get_autoscaler_state(host, parsed.user, password)
        if initial_autoscaler_enabled is None:
            print(f"  {_WARN} Could not determine cluster-autoscaler state (deployment not found).")
        else:
            state_str = "ENABLED" if initial_autoscaler_enabled else "DISABLED"
            print(f"  {_OK} Initial cluster-autoscaler state: {state_str}")

            # Ensure cluster-autoscaler is enabled during scaling operations
            # so machine rollouts, drains, and replica scaling can take place smoothly.
            if not initial_autoscaler_enabled:
                print(f"  {_OK} Temporarily enabling cluster-autoscaler during scaling operations...")
                if not parsed.dry_run:
                    set_autoscaler_state(host, parsed.user, password, True)
                autoscaler_temporarily_enabled = True
            else:
                print(f"  {_OK} Cluster-autoscaler is active for scaling operations.")

        if parsed.cp_machine_type:
            overall_ok = step2b_resize_cp_machine_type(ctx, parsed.cp_machine_type) and overall_ok

        if parsed.machine_type:
            overall_ok = step3_resize_machine_type(ctx, parsed.machine_type) and overall_ok

        if min_r is not None:
            if not overall_ok:
                print(f"\n  {_WARN} Skipping Step 4 -- Step 3 did not complete successfully.")
            else:
                overall_ok = step4_scale_replicas(ctx, min_r, max_r) and overall_ok

        if parsed.dry_run:
            print(f"\n{_GREEN}Dry run complete -- no changes were applied.{_NC}")
            sys.exit(0)

        verify_ok = verify_final_state(ctx)

        print(f"\n{_CYAN}{'─' * 64}{_NC}")
        if overall_ok and verify_ok:
            print(f"  {_GREEN}Scale operation completed successfully.{_NC}")
            print(f"{_CYAN}{'─' * 64}{_NC}\n")
            sys.exit(0)
        elif overall_ok:
            print(f"  {_WARN} Scale operation completed, but verification raised warnings above.")
            print(f"{_CYAN}{'─' * 64}{_NC}\n")
            sys.exit(3)
        else:
            print(f"  {_FAIL} Scale operation did not complete -- cluster may be left mid-change. "
                  f"See warnings above.")
            print(f"{_CYAN}{'─' * 64}{_NC}\n")
            sys.exit(3)

    finally:
        if initial_autoscaler_enabled is not None:
            print(f"\n{_BOLD}{_CYAN}──── Final Autoscaler Configuration ────{_NC}")
            if parsed.dry_run:
                if parsed.autoscaler == "disable":
                    print(f"  {_DIM}[dry-run] would disable cluster-autoscaler (--autoscaler disable){_NC}")
                elif parsed.autoscaler == "enable":
                    print(f"  {_DIM}[dry-run] would ensure cluster-autoscaler is ENABLED (--autoscaler enable){_NC}")
                elif parsed.autoscaler == "auto":
                    if autoscaler_temporarily_enabled:
                        print(f"  {_DIM}[dry-run] would restore cluster-autoscaler to initial state (DISABLED){_NC}")
                    else:
                        print(f"  {_DIM}[dry-run] would leave cluster-autoscaler in initial state ({state_str}){_NC}")
            else:
                if parsed.autoscaler == "disable":
                    print(f"  {_OK} Disabling cluster-autoscaler as requested (--autoscaler disable)...")
                    set_autoscaler_state(host, parsed.user, password, False)
                elif parsed.autoscaler == "enable":
                    print(f"  {_OK} Ensuring cluster-autoscaler is ENABLED as requested (--autoscaler enable)...")
                    set_autoscaler_state(host, parsed.user, password, True)
                elif parsed.autoscaler == "auto":
                    if autoscaler_temporarily_enabled:
                        print(f"  {_OK} Restoring cluster-autoscaler to initial state (DISABLED)...")
                        set_autoscaler_state(host, parsed.user, password, False)
                    else:
                        print(f"  {_OK} Cluster-autoscaler left in initial state ({state_str}).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)