#!/usr/bin/env python3
# vsp-health-monitor.py - HOLFY27 VSP Cluster Health Monitor & Remediator
# Version 1.0 - 2026-07-13
# Author - Burke Azbill and HOL Core Team
#
# PURPOSE
#   Detect and (optionally) remediate the recurring health failures that make
#   the VSP (VCF Services Runtime) Kubernetes cluster's user-facing services
#   (VCF Operations Lifecycle / Software Depot / Fleet LCM) show "Service or
#   view is not available at this time" or raw HTTP 500s in HOLFY27 labs.
#
#   Consolidates the remediations for the failure modes documented in the
#   vcf-troubleshooting skill:
#     #3  / #56  - VSP control-plane node flapping (etcd/kube-apiserver stalls,
#                  node-monitor-grace-period, undersized CP node)
#     #57        - vmsp-gateway Envoy data-plane / envoy-gateway controller
#                  CrashLoopBackOff -> fleet-01a/vsp-01a/instance-01a VIPs down
#     #6  / #11  - VCF component pods (fleet-lcm, vidb, depot, salt, ops-logs,
#                  vodap) evicted / CrashLoopBackOff after cluster instability
#
# WHERE IT RUNS
#   On the MANAGER VM, NOT on a VSP node. VSP control-plane nodes are CAPI
#   "cattle" -- they get rolling-replaced (observed 2026-07-13: vsp-01a-txhml ->
#   vsp-01a-x8z9d), so a systemd unit installed on a VSP node would be lost on
#   the next replacement. This DELIBERATELY diverges from the vcfa-stabilizer.sh
#   keeper pattern (which installs keepers on the more-static auto-platform node).
#   The manager is stable, owns config.ini + all HOL tooling, and already reaches
#   the cluster via SSH to the VSP control-plane VIP. lsf.ssh() uses
#   UserKnownHostsFile=/dev/null so the VIP's host-key change after a CP rolling
#   replace is handled transparently.
#
# HOW IT RUNS
#   1. Once at lab startup (invoked by Startup/VCFfinal.py) -- "prevention", so
#      the lab comes up clean.
#   2. On a recurring MANAGER-SIDE CRON JOB (default every 5 min, interval
#      configurable via config.ini [VSPMONITOR] interval_seconds) -- "ongoing
#      self-healing". Install/enable with: --install-timer (kept as the flag
#      name for VCFfinal compatibility; it installs a cron entry).
#
#   Cron, not a systemd timer: on the manager, holuser's sudoers only permits a
#   specific command allowlist (systemctl/apt/mount/ping/labstartup/reboot) --
#   NOT arbitrary writes to /etc/systemd/system -- so it cannot install a
#   systemd unit. holuser's own crontab needs no sudo at all, survives reboots,
#   and matches the existing @reboot labstartup cron. All of the monitor's
#   privileged remediation happens via SSH to the VSP nodes (vmware-system-user
#   sudo), which is unaffected by the manager's local sudo restrictions.
#
#   LAB-READY GATE: the recurring cron pass (plain --once) exits immediately if
#   the lab has not reached "Ready" (a ^ready line in startup_status.txt, same
#   signal checkready.sh uses) -- so it never hammers powered-off / uninitialized
#   servers before startup finishes. The VCFfinal startup pass (--install-timer,
#   or --ignore-ready) bypasses the gate: it is the intentional pre-Ready cleanup.
#
# CONFIG (config.ini [VSPMONITOR], all optional -- sensible defaults if absent):
#   enabled                        = true            # master on/off
#   remediate                      = true            # false = detect/log only
#   interval_seconds               = 300             # cron cadence (rounded to min)
#   vsp_control_plane_ip           = 10.1.1.142
#   checks                         = gateway,node_flap,crashloop_pods,vip
#   crashloop_restart_threshold    = 5               # min restartCount to act
#   crashloop_max_restarts_per_cycle = 15            # safety cap per run
#   crashloop_exclude_namespaces   =                 # extra ns to skip (csv)
#
# CHANGELOG
#   v1.0 - 2026-07-13: Initial version. gateway, node_flap, crashloop_pods, vip
#          checks; --once / --dry-run / --install-timer / --uninstall-timer /
#          --ignore-ready. Recurring schedule is a manager crontab entry (holuser
#          sudoers cannot install systemd units); --install-timer installs the
#          cron job. Cron pass gates on lab-Ready (startup_status.txt) so it does
#          nothing until the lab is up. Gateway check acts only on genuine
#          CrashLoopBackOff/Error (not transient ready<total) to avoid thrash;
#          node_flap grace-period patch routed through a scp'd script (verified).
#   v1.1 - 2026-07-13: Added vsp_size check (runs FIRST) — verifies + re-asserts
#          the vsp ComponentVersion active size profile's control-plane cpu >= 12
#          (the SUPPORTED, operator-honored lever; workers untouched). This is the
#          durable root-cause fix for the leader-election storm: an undersized
#          4-vCPU CP (size=small default) is the resource-stress trigger that lets
#          Kyverno's fail-closed webhooks ignite cluster-wide lease-renewal
#          blocking. (An earlier kyverno_failpolicy webhook-flip check was tried
#          and dropped — Kyverno re-asserts failurePolicy: Fail within ~2min, so a
#          5-min re-assert is ineffective and a faster keeper fights Kyverno
#          continuously; no durable in-cluster Kyverno lever exists. See #59.)

import os
import sys
import json
import time
import argparse
import subprocess

# Manager has /home/holuser/hol on PYTHONPATH (see @reboot cron / systemd unit)
sys.path.insert(0, '/home/holuser/hol')
import lsfunctions as lsf


#==============================================================================
# DEFAULTS
#==============================================================================

SCRIPT_VERSION = '1.1'
LOG_FILE = '/home/holuser/hol/vsp-health-monitor.log'

DEFAULTS = {
    'enabled': False,
    'remediate': True,
    'interval_seconds': 300,
    'vsp_control_plane_ip': '10.1.1.142',
    'checks': ['vsp_size', 'gateway', 'node_flap', 'crashloop_pods', 'vip'],
    'crashloop_restart_threshold': 5,
    'crashloop_max_restarts_per_cycle': 15,
    'crashloop_exclude_namespaces': [],
}

VSP_SSH_USER = 'vmware-system-user'

# Control-plane static pods are handled by the node_flap check (or must not be
# blindly bounced); the gateway check owns the gateway pods. Both are excluded
# from the broad crashloop sweep to bound its blast radius.
CP_STATIC_POD_PREFIXES = (
    'etcd-', 'kube-apiserver-', 'kube-controller-manager-',
    'kube-scheduler-', 'kube-vip-',
)
GATEWAY_POD_SUBSTRINGS = ('envoy-gateway', 'vmsp-gateway', 'ops-logs-gateway')

# Marker comment used to find/replace our own crontab line idempotently
CRON_MARKER = '# vsp-health-monitor (HOLFY27 auto-installed)'

# Lab "Ready" signal — same file checkready.sh polls (lsf.lab_status).
# The recurring cron run gates on this so it never hammers powered-off /
# uninitialized servers before the lab has finished starting.
LAB_STATUS_FILES = ('/lmchol/hol/startup_status.txt', '/wmchol/hol/startup_status.txt')


#==============================================================================
# LOGGING
#==============================================================================

def log(msg):
    """Write to both lsf output (console/labstartup.log) and our own log file."""
    lsf.write_output(msg)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(f'{msg}\n')
    except Exception:
        pass


#==============================================================================
# CONFIG
#==============================================================================

def load_config():
    """Load [VSPMONITOR] from config.ini, falling back to DEFAULTS per-key."""
    cfg = dict(DEFAULTS)
    if not lsf.config.has_section('VSPMONITOR'):
        return cfg

    def _get_bool(key, default):
        raw = lsf.config.get('VSPMONITOR', key, fallback=str(default)).strip().lower()
        return raw in ('1', 'true', 'yes', 'on')

    def _get_int(key, default):
        try:
            return int(lsf.config.get('VSPMONITOR', key, fallback=str(default)).strip())
        except (ValueError, TypeError):
            return default

    def _get_list(key, default):
        raw = lsf.config.get('VSPMONITOR', key, fallback='').strip()
        if not raw:
            return list(default)
        return [x.strip() for x in raw.replace('\n', ',').split(',') if x.strip()]

    cfg['enabled'] = _get_bool('enabled', DEFAULTS['enabled'])
    cfg['remediate'] = _get_bool('remediate', DEFAULTS['remediate'])
    cfg['interval_seconds'] = _get_int('interval_seconds', DEFAULTS['interval_seconds'])
    cfg['vsp_control_plane_ip'] = lsf.config.get(
        'VSPMONITOR', 'vsp_control_plane_ip',
        fallback=DEFAULTS['vsp_control_plane_ip']).strip()
    cfg['checks'] = _get_list('checks', DEFAULTS['checks'])
    cfg['crashloop_restart_threshold'] = _get_int(
        'crashloop_restart_threshold', DEFAULTS['crashloop_restart_threshold'])
    cfg['crashloop_max_restarts_per_cycle'] = _get_int(
        'crashloop_max_restarts_per_cycle', DEFAULTS['crashloop_max_restarts_per_cycle'])
    cfg['crashloop_exclude_namespaces'] = _get_list(
        'crashloop_exclude_namespaces', DEFAULTS['crashloop_exclude_namespaces'])
    return cfg


#==============================================================================
# LAB READY GATE
#==============================================================================

def is_lab_ready():
    """Return True if the lab has reached 'Ready' status. Mirrors checkready.sh:
    a line matching ^ready (case-insensitive, whole word) in startup_status.txt.
    Returns False if the file is missing/unreadable (i.e. still starting)."""
    import re
    for path in LAB_STATUS_FILES:
        try:
            with open(path) as f:
                content = f.read()
        except Exception:
            continue
        for line in content.splitlines():
            if re.match(r'^\s*ready\b', line.strip(), re.IGNORECASE):
                return True
        # File exists but no ready line -> still starting (or failed)
        return False
    return False


#==============================================================================
# KUBECTL / SSH HELPERS (manager -> VSP control-plane VIP)
#==============================================================================

def _clean_stdout(result):
    """Extract stdout from an lsf.ssh CompletedProcess."""
    return (getattr(result, 'stdout', '') or '')


def _parse_json(raw):
    """Find the first JSON object/array in noisy SSH stdout (login banner,
    sudo prompt) and parse it. Returns {} on failure."""
    for opener in ('{', '['):
        idx = raw.find(opener)
        if idx >= 0:
            try:
                return json.loads(raw[idx:])
            except Exception:
                continue
    return {}


def kubectl(cp_ip, args, password):
    """Run 'kubectl <args>' on the VSP control plane via sudo -S -i."""
    cmd = f"echo '{password}' | sudo -S -i kubectl {args}"
    return lsf.ssh(cmd, f'{VSP_SSH_USER}@{cp_ip}', password)


def kubectl_json(cp_ip, args, password):
    """Run a kubectl get and return parsed JSON (adds -o json)."""
    result = kubectl(cp_ip, f'{args} -o json', password)
    if result.returncode != 0:
        return None
    return _parse_json(_clean_stdout(result))


def crictl(cp_ip, args, password):
    """Run 'crictl <args>' on the VSP control plane via sudo -S -i."""
    cmd = f"echo '{password}' | sudo -S -i crictl {args}"
    return lsf.ssh(cmd, f'{VSP_SSH_USER}@{cp_ip}', password)


def kubectl_patch(cp_ip, resource, name, namespace, patch_obj, password, ptype='merge'):
    """kubectl patch a resource using a scp'd --patch-file (avoids the nested
    double-quote trap of inline -p '{...}' through lsf.ssh)."""
    patch_json = json.dumps(patch_obj)
    ns_arg = f'-n {namespace}' if namespace else ''
    script = (
        '#!/bin/bash\n'
        "cat > /tmp/vsp_mon_patch.json << 'PJSON'\n"
        f'{patch_json}\n'
        'PJSON\n'
        f'kubectl patch {resource} {name} {ns_arg} --type={ptype} '
        f'--patch-file=/tmp/vsp_mon_patch.json\n'
        'rm -f /tmp/vsp_mon_patch.json\n'
    )
    return run_remote_script(cp_ip, script, password)


def run_remote_script(cp_ip, script_text, password):
    """scp a shell script to the CP node and run it via sudo -S -i bash.

    Used for anything with embedded quotes/sed expressions — lsf.ssh wraps the
    command in double quotes, so a `bash -c "..."` with inner double quotes gets
    mangled (nested-quoting trap). Staging a script file sidesteps quoting
    entirely (the pattern used throughout this repo's remediation code)."""
    import tempfile
    local = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as f:
            f.write(script_text)
            local = f.name
    except Exception:
        return None
    remote = f'/tmp/vsp_mon_{int(time.time() * 1000)}.sh'
    try:
        scp_r = lsf.scp(local, f'{VSP_SSH_USER}@{cp_ip}:{remote}', password)
        if getattr(scp_r, 'returncode', 1) != 0:
            return None
        r = lsf.ssh(f"echo '{password}' | sudo -S -i bash {remote}",
                    f'{VSP_SSH_USER}@{cp_ip}', password)
        lsf.ssh(f"rm -f {remote}", f'{VSP_SSH_USER}@{cp_ip}', password)
        return r
    finally:
        if local:
            try:
                os.remove(local)
            except OSError:
                pass


#==============================================================================
# CHECK: VSP CONTROL-PLANE SIZE (durable root-cause prevention — supervisor-k8s
# #56/#59). Verifies + re-asserts the vsp ComponentVersion's active size profile
# control-plane cpu >= 12 (the SUPPORTED, operator-honored lever). Workers are
# left untouched. Re-patches only if it has been reverted, and the operator only
# rolls the CP node when the value actually changes — so this is a cheap, safe
# backstop, not a churn source.
#==============================================================================

VSP_MIN_CP_CPU = 12


def check_vsp_size(cp_ip, password, remediate, dry_run):
    """Ensure the vsp ComponentVersion's ACTIVE size profile gives the control
    plane >= 12 vCPU. This is the durable, supported fix for the undersized-CP
    root cause of the leader-election storm (see #56/#59): editing
    Cluster.spec.topology or VSphereMachineTemplates is reverted by vmsp-operator
    within ~90s, but the ComponentVersion `sizes` profile is the input the
    operator honors. Bumps ONLY the active profile's control-plane cpu (leaves
    its `worker` size untouched, so worker nodes are unaffected). Idempotent."""
    comp = kubectl_json(cp_ip, 'get components.api.vmsp.vmware.com vsp', password)
    if not comp or 'spec' not in comp:
        return 'WARN', ['vsp_size: vsp Component not found (lab may not use VSP) — skipping']
    active = comp['spec'].get('size', 'small')
    cv_name = comp['spec'].get('versionRef', {}).get('name', '')
    if not cv_name:
        return 'WARN', ['vsp_size: vsp Component has no versionRef — skipping']

    cv = kubectl_json(cp_ip, f'get componentversions.api.vmsp.vmware.com {cv_name}', password)
    if not cv or 'spec' not in cv:
        return 'WARN', [f'vsp_size: could not read ComponentVersion {cv_name} — skipping']
    sizes = cv['spec'].get('sizes', [])
    prof = next((s for s in sizes if s.get('name') == active), None)
    if prof is None:
        return 'WARN', [f'vsp_size: active profile "{active}" not in ComponentVersion sizes']

    try:
        cpu_max = float(str(prof.get('resources', {}).get('cpu', {}).get('max', '0')))
    except (ValueError, TypeError):
        cpu_max = 0.0
    worker = prof.get('worker', {}).get('size', '?')

    if cpu_max >= VSP_MIN_CP_CPU:
        return 'PASS', [f'vsp_size: "{active}" profile CP cpu.max={cpu_max} '
                        f'(>= {VSP_MIN_CP_CPU}), worker="{worker}" - OK']

    msgs = [f'vsp_size: "{active}" profile CP cpu.max={cpu_max} UNDERSIZED '
            f'(< {VSP_MIN_CP_CPU}); worker="{worker}" (untouched)']
    if dry_run:
        msgs.append('vsp_size: [DRY-RUN] would bump the profile CP cpu to max 12/min 8')
        return 'FAIL', msgs
    if not remediate:
        return 'FAIL', msgs

    new_sizes = []
    for s in sizes:
        s = json.loads(json.dumps(s))  # deep copy
        if s.get('name') == active:
            s.setdefault('resources', {}).setdefault('cpu', {})
            s['resources']['cpu']['max'] = str(VSP_MIN_CP_CPU)
            s['resources']['cpu']['min'] = '8'
        new_sizes.append(s)
    pres = kubectl_patch(cp_ip, 'componentversions.api.vmsp.vmware.com', cv_name, '',
                         {'spec': {'sizes': new_sizes}}, password, ptype='merge')
    if pres is not None and 'patched' in (_clean_stdout(pres) or '').lower():
        msgs.append(f'vsp_size: bumped "{active}" profile CP cpu -> 12 (worker "{worker}" '
                    f'unchanged); operator re-renders CP to 12 vCPU (only the CP rolls)')
    else:
        msgs.append('vsp_size: WARNING — ComponentVersion patch did not confirm')
    return 'FAIL', msgs


#==============================================================================
# CHECK: GATEWAY (vcf-troubleshooting #57)
#==============================================================================

def _pod_health(pod):
    """Return (ready, total, max_restarts, waiting_reason) for a pod object."""
    cstats = pod.get('status', {}).get('containerStatuses', [])
    ready = sum(1 for cs in cstats if cs.get('ready', False))
    total = len(cstats)
    max_restarts = max((cs.get('restartCount', 0) for cs in cstats), default=0)
    waiting = ''
    for cs in cstats:
        w = cs.get('state', {}).get('waiting', {})
        if w.get('reason'):
            waiting = w['reason']
            break
    phase = pod.get('status', {}).get('phase', '')
    return ready, total, max_restarts, waiting, phase


def check_gateway(cp_ip, password, remediate, dry_run, restart_threshold):
    """Ensure the envoy-gateway controller and the vmsp-gateway/ops-logs-gateway
    Envoy data-plane pods are healthy. Restart the controller first (so the
    data-plane refetches fresh xDS config), then any unhealthy data-plane pod.
    Returns (status, [messages])."""
    msgs = []
    data = kubectl_json(cp_ip, 'get pods -n vmsp-platform', password)
    if not data or 'items' not in data:
        return 'WARN', ['gateway: could not list vmsp-platform pods']

    controller = []
    dataplane = []
    for pod in data['items']:
        name = pod.get('metadata', {}).get('name', '')
        labels = pod.get('metadata', {}).get('labels', {})
        if labels.get('app.kubernetes.io/name') == 'envoy-gateway' or name.startswith('envoy-gateway-'):
            controller.append(pod)
        elif name.startswith('vmsp-gateway') or name.startswith('ops-logs-gateway'):
            dataplane.append(pod)

    def _unhealthy(pod):
        # Act ONLY on genuinely stuck pods (a container in CrashLoopBackOff/Error),
        # NOT on a pod that is merely ready<total. A freshly-restarted or
        # rolling-out gateway pod is transiently 1/2 or 0/2 while it starts, and
        # restarting it then just fights normal pod lifecycle (observed causing
        # thrash + cascading VIP-unreachable blips). A stuck container reliably
        # shows waiting.reason=CrashLoopBackOff/Error, which is the real #57 signal.
        ready, total, restarts, waiting, phase = _pod_health(pod)
        if waiting in ('CrashLoopBackOff', 'Error', 'CreateContainerError'):
            return True, f'{waiting} (restarts={restarts})'
        return False, f'{ready}/{total} ready, restarts={restarts}, phase={phase or "?"}'

    status = 'PASS'
    controller_bounced = False

    # --- Controller first (so a restarted data plane refetches fresh xDS) ---
    for pod in controller:
        name = pod['metadata']['name']
        bad, detail = _unhealthy(pod)
        if bad:
            status = 'FAIL'
            msgs.append(f'gateway: controller {name} unhealthy: {detail}')
            if remediate and not dry_run:
                kubectl(cp_ip, f'delete pod {name} -n vmsp-platform --grace-period=0 --force', password)
                msgs.append(f'gateway: restarted controller {name}')
                controller_bounced = True
            elif dry_run:
                msgs.append(f'gateway: [DRY-RUN] would restart controller {name}')
        else:
            msgs.append(f'gateway: controller {name} OK ({detail})')

    if controller_bounced:
        time.sleep(20)  # let the controller come back before touching data plane

    # --- Data plane (only if genuinely CrashLoopBackOff/Error) ---
    for pod in dataplane:
        name = pod['metadata']['name']
        bad, detail = _unhealthy(pod)
        if bad:
            status = 'FAIL'
            msgs.append(f'gateway: data-plane {name} needs restart: {detail}')
            if remediate and not dry_run:
                kubectl(cp_ip, f'delete pod {name} -n vmsp-platform --grace-period=0 --force', password)
                msgs.append(f'gateway: restarted data-plane {name}')
            elif dry_run:
                msgs.append(f'gateway: [DRY-RUN] would restart data-plane {name}')
        else:
            msgs.append(f'gateway: data-plane {name} OK ({detail})')

    if status == 'PASS':
        msgs.append('gateway: all gateway pods healthy')
    return status, msgs


#==============================================================================
# CHECK: NODE FLAP (vcf-troubleshooting #3 / #56)
#==============================================================================

def check_node_flap(cp_ip, password, remediate, dry_run):
    """Ensure node-monitor-grace-period=90s is set (idempotent), and bounce
    etcd/kube-apiserver only when a node is CURRENTLY NotReady (not merely on a
    stale event) to avoid churning the control plane on every monitor cycle.
    Returns (status, [messages])."""
    msgs = []
    status = 'PASS'
    manifest = '/etc/kubernetes/manifests/kube-controller-manager.yaml'

    # (a) grace-period check + patch (cheap, idempotent). Both routed through a
    # scp'd script to avoid the lsf.ssh double-quote-wrapper / bash -c nested
    # quoting trap that silently no-ops a sed with embedded quotes.
    check_script = (
        '#!/bin/bash\n'
        f'grep -q node-monitor-grace-period {manifest} && echo PRESENT || echo ABSENT\n'
    )
    gres = run_remote_script(cp_ip, check_script, password)
    gstate = _clean_stdout(gres) if gres is not None else ''
    if 'ABSENT' in gstate:
        status = 'FAIL'
        msgs.append('node_flap: node-monitor-grace-period missing (still 40s default)')
        if remediate and not dry_run:
            patch_script = (
                '#!/bin/bash\n'
                'set -e\n'
                f"sed -i '/- --use-service-account-credentials=true/"
                f"a\\    - --node-monitor-grace-period=90s' {manifest}\n"
                f'grep -q node-monitor-grace-period {manifest} && echo PATCHED || echo PATCH_FAILED\n'
            )
            pres = run_remote_script(cp_ip, patch_script, password)
            if pres is not None and 'PATCHED' in _clean_stdout(pres):
                msgs.append('node_flap: patched node-monitor-grace-period=90s (verified present)')
            else:
                msgs.append('node_flap: WARNING — grace-period patch did not verify')
        elif dry_run:
            msgs.append('node_flap: [DRY-RUN] would patch node-monitor-grace-period=90s')
    elif 'PRESENT' in gstate:
        msgs.append('node_flap: node-monitor-grace-period already set - OK')
    else:
        msgs.append('node_flap: could not read kube-controller-manager manifest')

    # (b) current NotReady node?
    nodes = kubectl_json(cp_ip, 'get nodes', password)
    notready = []
    cp_nodes = []
    if nodes and 'items' in nodes:
        for n in nodes['items']:
            name = n.get('metadata', {}).get('name', '')
            labels = n.get('metadata', {}).get('labels', {})
            if any(k.startswith('node-role.kubernetes.io/control-plane') for k in labels):
                cp_nodes.append(name)
            for c in n.get('status', {}).get('conditions', []):
                if c.get('type') == 'Ready' and c.get('status') != 'True':
                    notready.append(name)

    if notready:
        status = 'FAIL'
        msgs.append(f'node_flap: node(s) currently NotReady: {", ".join(notready)}')
        if remediate and not dry_run:
            for cp in cp_nodes:
                etcd_ids = _clean_stdout(crictl(
                    cp_ip, f'pods --name etcd-{cp} -s Ready -q', password)).strip().splitlines()
                etcd_ids = [i for i in etcd_ids if i.strip() and '{' not in i]
                if etcd_ids:
                    crictl(cp_ip, f'stopp {etcd_ids[-1].strip()}', password)
                    msgs.append(f'node_flap: restarted etcd on {cp}')
            time.sleep(15)
            for cp in cp_nodes:
                api_ids = _clean_stdout(crictl(
                    cp_ip, f'pods --name kube-apiserver-{cp} -s Ready -q', password)).strip().splitlines()
                api_ids = [i for i in api_ids if i.strip() and '{' not in i]
                if api_ids:
                    crictl(cp_ip, f'stopp {api_ids[-1].strip()}', password)
                    msgs.append(f'node_flap: restarted kube-apiserver on {cp}')
        elif dry_run:
            msgs.append('node_flap: [DRY-RUN] would bounce etcd/kube-apiserver on CP node(s)')
    else:
        msgs.append('node_flap: no NotReady nodes - OK')

    return status, msgs


#==============================================================================
# CHECK: CRASHLOOP PODS (broad, cluster-wide; vcf-troubleshooting #6 / #11)
#==============================================================================

def check_crashloop_pods(cp_ip, password, remediate, dry_run,
                          threshold, max_per_cycle, exclude_namespaces):
    """Restart pods stuck in CrashLoopBackOff/Error above the restart threshold,
    cluster-wide. Excludes control-plane static pods (node_flap owns those) and
    gateway pods (the gateway check owns those). Capped at max_per_cycle.
    Returns (status, [messages])."""
    msgs = []
    data = kubectl_json(cp_ip, 'get pods -A', password)
    if not data or 'items' not in data:
        return 'WARN', ['crashloop_pods: could not list pods cluster-wide']

    candidates = []
    for pod in data['items']:
        name = pod.get('metadata', {}).get('name', '')
        ns = pod.get('metadata', {}).get('namespace', '')
        if ns in exclude_namespaces:
            continue
        if any(name.startswith(p) for p in CP_STATIC_POD_PREFIXES):
            continue
        if any(sub in name for sub in GATEWAY_POD_SUBSTRINGS):
            continue
        ready, total, restarts, waiting, phase = _pod_health(pod)
        if waiting in ('CrashLoopBackOff', 'Error') and restarts >= threshold:
            candidates.append((ns, name, waiting, restarts))

    if not candidates:
        return 'PASS', ['crashloop_pods: no CrashLoopBackOff pods above threshold - OK']

    # Sort worst-first so the per-cycle cap hits the most-broken pods
    candidates.sort(key=lambda c: c[3], reverse=True)
    status = 'FAIL'
    acted = 0
    for ns, name, waiting, restarts in candidates:
        if acted >= max_per_cycle:
            msgs.append(f'crashloop_pods: per-cycle cap ({max_per_cycle}) reached; '
                        f'{len(candidates) - acted} more left for next cycle')
            break
        msgs.append(f'crashloop_pods: {ns}/{name} {waiting} (restarts={restarts})')
        if remediate and not dry_run:
            kubectl(cp_ip, f'delete pod {name} -n {ns} --grace-period=0 --force', password)
            msgs.append(f'crashloop_pods: restarted {ns}/{name}')
            acted += 1
        elif dry_run:
            msgs.append(f'crashloop_pods: [DRY-RUN] would restart {ns}/{name}')
            acted += 1

    return status, msgs


#==============================================================================
# CHECK: VIP REACHABILITY
#==============================================================================

def check_vip(cp_ip, password):
    """Verify each LoadBalancer Service VIP is reachable. Detect-only: a VIP
    down while its backing pod is healthy indicates a kube-vip issue that should
    be surfaced (not blindly auto-restarted). Returns (status, [messages])."""
    msgs = []
    data = kubectl_json(cp_ip, 'get svc -A', password)
    if not data or 'items' not in data:
        return 'WARN', ['vip: could not list services']

    vips = []
    for svc in data['items']:
        if svc.get('spec', {}).get('type') != 'LoadBalancer':
            continue
        name = svc.get('metadata', {}).get('name', '')
        for ing in svc.get('status', {}).get('loadBalancer', {}).get('ingress', []):
            ip = ing.get('ip')
            if ip:
                vips.append((name, ip))

    if not vips:
        return 'PASS', ['vip: no LoadBalancer VIPs found']

    status = 'PASS'
    for name, ip in vips:
        if lsf.test_ping(ip, count=2, timeout=2):
            msgs.append(f'vip: {name} ({ip}) reachable - OK')
        else:
            status = 'FAIL'
            msgs.append(f'vip: {name} ({ip}) UNREACHABLE - check backing gateway '
                        f'pod (gateway check) / kube-vip')
    return status, msgs


#==============================================================================
# ORCHESTRATION
#==============================================================================

def run_all(cfg, dry_run):
    """Run all enabled checks. Returns overall exit code (0 = healthy/remediated,
    2 = something FAILed and could not be remediated / remediate disabled)."""
    password = lsf.get_password()
    cp_ip = cfg['vsp_control_plane_ip']
    remediate = cfg['remediate'] and not dry_run

    log('=' * 64)
    log(f'VSP Health Monitor v{SCRIPT_VERSION} — checks={",".join(cfg["checks"])} '
        f'remediate={cfg["remediate"]} dry_run={dry_run}')
    log('=' * 64)

    if not lsf.test_ping(cp_ip, count=2, timeout=2):
        log(f'VSP control plane VIP {cp_ip} not reachable — '
            f'this lab may not use a VSP cluster; nothing to do')
        return 0

    any_fail = False
    for check in cfg['checks']:
        try:
            if check == 'vsp_size':
                st, msgs = check_vsp_size(cp_ip, password, remediate, dry_run)
            elif check == 'gateway':
                st, msgs = check_gateway(cp_ip, password, remediate, dry_run,
                                          cfg['crashloop_restart_threshold'])
            elif check == 'node_flap':
                st, msgs = check_node_flap(cp_ip, password, remediate, dry_run)
            elif check == 'crashloop_pods':
                st, msgs = check_crashloop_pods(
                    cp_ip, password, remediate, dry_run,
                    cfg['crashloop_restart_threshold'],
                    cfg['crashloop_max_restarts_per_cycle'],
                    cfg['crashloop_exclude_namespaces'])
            elif check == 'vip':
                st, msgs = check_vip(cp_ip, password)
            else:
                st, msgs = 'WARN', [f'{check}: unknown check name — skipped']
        except Exception as e:
            st, msgs = 'WARN', [f'{check}: check raised exception (non-fatal): {e}']

        for m in msgs:
            log(f'  {m}')
        if st == 'FAIL':
            any_fail = True
            log(f'  [{check}] FAIL')
        else:
            log(f'  [{check}] {st}')

    if any_fail and not remediate:
        log('One or more checks FAILED and remediate=false (detect-only) — '
            'not remediating')
        return 2
    log('VSP Health Monitor pass complete')
    return 0


#==============================================================================
# RECURRING SCHEDULE INSTALL (manager crontab — no sudo required)
#==============================================================================

def _read_crontab():
    """Return the current holuser crontab lines (empty list if none)."""
    r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return r.stdout.splitlines()


def _write_crontab(lines):
    """Replace holuser's crontab with the given lines."""
    payload = ('\n'.join(lines).rstrip('\n') + '\n') if lines else ''
    r = subprocess.run(['crontab', '-'], input=payload, capture_output=True, text=True)
    return r


def install_timer(cfg):
    """Install/refresh the manager crontab entry that runs
    'vsp-health-monitor.py --once' every interval_seconds (rounded to whole
    minutes). Idempotent — replaces any prior vsp-health-monitor cron line.
    Named install_timer for VCFfinal compatibility; uses cron because holuser
    cannot install systemd units (see module header)."""
    interval = cfg['interval_seconds']
    minutes = max(1, round(interval / 60))
    script_path = os.path.abspath(__file__)
    py = sys.executable or '/usr/bin/python3'

    cron_line = (
        f"*/{minutes} * * * * "
        f"PYTHONPATH=/usr/lib/python3/dist-packages:/home/holuser/hol "
        f"{py} {script_path} --once >> {LOG_FILE} 2>&1 {CRON_MARKER}"
    )

    # Preserve every existing line except a prior copy of ours
    lines = [l for l in _read_crontab() if 'vsp-health-monitor' not in l]
    lines.append(cron_line)

    r = _write_crontab(lines)
    if r.returncode == 0:
        log(f'Installed vsp-health-monitor cron job (every {minutes} min '
            f'= {interval}s target)')
        return True
    log(f'  WARNING: cron install failed (rc={r.returncode}): {r.stderr.strip()[:200]}')
    return False


def uninstall_timer():
    """Remove the vsp-health-monitor cron line, preserving all other entries."""
    current = _read_crontab()
    lines = [l for l in current if 'vsp-health-monitor' not in l]
    if len(lines) == len(current):
        log('No vsp-health-monitor cron job found — nothing to uninstall')
        return True
    _write_crontab(lines)
    log('Removed vsp-health-monitor cron job')
    return True


#==============================================================================
# MAIN
#==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='HOLFY27 VSP Cluster Health Monitor & Remediator',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--once', action='store_true',
                        help='Run one check/remediate pass and exit (default)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Detect and log only; never remediate this run')
    parser.add_argument('--install-timer', action='store_true',
                        help='Install + enable the recurring manager systemd timer')
    parser.add_argument('--uninstall-timer', action='store_true',
                        help='Disable + remove the recurring cron job')
    parser.add_argument('--ignore-ready', action='store_true',
                        help='Skip the lab-Ready gate (used by the VCFfinal '
                             'startup pass, which intentionally runs pre-Ready)')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {SCRIPT_VERSION}')
    args = parser.parse_args()

    lsf.init(router=False)
    cfg = load_config()

    if args.uninstall_timer:
        uninstall_timer()
        return

    if not cfg['enabled']:
        log('VSP Health Monitor disabled via config.ini [VSPMONITOR] enabled=false — exiting')
        return

    if args.install_timer:
        # Installing the recurring cron job is a startup action (VCFfinal): do it
        # regardless of Ready state, and run one immediate pre-Ready cleanup pass.
        install_timer(cfg)
        sys.exit(run_all(cfg, args.dry_run))

    # Plain --once (the cron cadence path): gate on lab Ready so we never hammer
    # powered-off / uninitialized servers before the lab has finished starting.
    if not args.ignore_ready and not is_lab_ready():
        log('Lab has not reached Ready status yet — skipping VSP health pass '
            '(will retry on the next cron cycle)')
        return

    sys.exit(run_all(cfg, args.dry_run))


if __name__ == '__main__':
    main()
