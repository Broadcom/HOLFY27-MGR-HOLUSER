#!/usr/bin/env python3
# vsp-health-monitor.py - HOLFY27 VSP Cluster Health Monitor & Remediator
# Version 2.3 - 2026-07-16
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
#   vsp_worker_fqdn                = vsp-01a.site-a.vcf.lab   # for CP-IP discovery when the VIP itself is down
#   checks                         = host_contention,vsp_size,kvip_manifest,cp_pod_crash,gateway,
#                                     node_flap,crashloop_pods,postgres,salt_stack,vodap,
#                                     component_health,argo_cleanup,proxy_config,cert_renewal,vip
#   crashloop_restart_threshold    = 5               # min restartCount to act
#   crashloop_max_restarts_per_cycle = 15            # safety cap per run
#   crashloop_exclude_namespaces   =                 # extra ns to skip (csv)
#   host_contention_load_multiplier = 1.5            # 1-min load > nproc*this -> skip remediation this cycle
#   cert_renewal_threshold_days    = 60              # passed to vsp_cert_renewer.py --threshold-days
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
#   v1.2 - 2026-07-15: Added host_contention check (runs FIRST, ahead of
#          vsp_size) — the #59 storm can recur even with a correctly-sized
#          (12-vCPU) CP when the underlying nested/physical ESXi host is itself
#          oversubscribed (hypervisor CPU steal — see #62). nproc/Component.
#          spec.size look "fine" from inside the guest in that case, so this
#          check instead reads 1-min load average vs nproc directly from the CP
#          node. If load is far above nproc, every other check for this cycle is
#          downgraded to detect-only (no force-deletes/patches) — remediation
#          itself is API-server write traffic, and piling that on top of a
#          CPU-starved node compounds the storm instead of damping it. Safe to
#          skip a cycle: kubelet's own crash-loop backoff rides it out, and the
#          next cron pass (once contention clears) resumes normal remediation.
#   v1.3 - 2026-07-15: Moved LOG_FILE to /tmp (was /home/holuser/hol). Console
#          rendering restyled to match vsp-health.py's interactive look (same
#          color palette, ✓/✗/⚠ symbols, boxed header, colored "RESULT: N/M"
#          summary) instead of raw timestamped log lines. lsf.write_output()'s
#          own console echo is now suppressed (console=False) so every line
#          only prints once, through the new render layer — colors auto-drop
#          to plain text when stdout isn't a tty (e.g. under cron), same as
#          vsp-health.py. LOG_FILE/labstartup.log content is unchanged (still
#          plain text, no ANSI codes, written by log() exactly as before).
#   v1.4 - 2026-07-15: log() no longer calls lsf.write_output() at all —
#          console=False (v1.3) only stopped the console echo; write_output()
#          was still appending every message (with its own timestamp prefix)
#          to the shared labstartup.log AND its lmchol NFS copy on every
#          cycle. A 5-min cron job writing a dozen-plus lines per pass would
#          steadily bury the lab's real startup/lifecycle log in monitor
#          noise. log() now writes ONLY to our own LOG_FILE (/tmp).
#   v2.0 - 2026-07-15: Closed the gap between this monitor's 6 checks and the
#          manual remediation tools (kube-fix.py, salt-stabilize.py,
#          vodap-fix.py) and VCFfinal.py's own inline VSP-scoped logic — a
#          survey found real recurring failure modes (dropped CP VIP, crashed
#          kube-scheduler/kube-controller-manager, stuck Postgres/Salt/vodap,
#          scaled-to-0 components, stale Argo shutdown workflows) that none of
#          the 6 existing checks covered, so they only got fixed at the next
#          full VCFfinal.py re-run or manual intervention. Added 9 checks,
#          each porting the corresponding tool's detection+fix logic (adding a
#          detection GATE where the source script fired unconditionally, e.g.
#          salt_stack, since a 5-min cron job must not rollout-restart a
#          healthy stack every cycle):
#            kvip_manifest    - kube-vip vip_preserve_on_leadership_loss=true
#                               patch (kube-fix.py fix_kvip_manifest)
#            cp_pod_crash     - crictl-clears crashed kube-controller-manager/
#                               kube-scheduler containers (kube-fix.py
#                               fix_kube_controller_manager/fix_kube_scheduler)
#                               — neither node_flap (etcd/apiserver only) nor
#                               crashloop_pods (excludes CP static pods) could
#                               ever reach these two, incl. the exact #59/#62
#                               leader-election-storm symptom from this lab.
#            postgres         - pgdatabase-0 pgdata chmod 700 fix
#                               (salt-stabilize.py fix_pgdata_permissions) +
#                               PostgresInstance/Zalando unsuspend + replica
#                               restore from the vcf.lab/original-instances
#                               annotation (VCFfinal.py Task 2e)
#            salt_stack       - ordered redis->raas->salt-master->salt-minion
#                               rollout restart (salt-stabilize.py), gated on
#                               a NEW detection pass (readiness + log-error
#                               grep) since the source script always restarts
#                               unconditionally when a human runs it on demand
#            vodap            - ClickHouse live-served-cert-vs-secret mismatch
#                               + StatefulSet restart, and fluentd
#                               /buffers/backup purge (vodap-fix.py
#                               fix_clickhouse/fix_fluentd — both already
#                               internally gated on a detected problem)
#            component_health - components.api.vmsp.vmware.com
#                               operational-status NotRunning->Running
#                               annotation fix, plus scale-up of any
#                               [VCFFINAL] vcfcomponents entry below its
#                               vcf.lab/original-replicas annotation
#                               (VCFfinal.py Task 2e) — this is what actually
#                               fixes the "0/0 scaled to 0 — run VCFfinal.py
#                               Task 2e" case vsp-health.py could only report
#            argo_cleanup     - KB 440862 pre-flight: deletes stale
#                               system-shutdown-* Argo Workflows, replays the
#                               power-off-marker ConfigMap's saved replica
#                               counts, uncordons non-condemned
#                               SchedulingDisabled nodes (VCFfinal.py)
#            proxy_config     - idempotent VSP-node proxy config repair
#                               (/etc/sysconfig/proxy, containerd/kubelet
#                               drop-ins, /etc/environment) against
#                               lsf.LAB_PROXY_URL/build_lab_no_proxy(); only
#                               ever sets the canonical values, never clears
#                               (VCFfinal.py's proxy-push block)
#            cert_renewal     - subprocess call to the existing
#                               vsp_cert_renewer.py --cluster vsp tool
#                               (kubeadm/kubelet/cert-manager/Antrea cert
#                               renewal); delegated rather than reimplemented
#                               since that tool already owns this logic
#          VIP restore (kube-fix.py fix_vip_restore) is NOT a toggleable
#          check — it is a pre-flight step inside run_all() itself, since
#          nothing else in this file can run at all while the CP VIP is
#          down. It discovers a reachable node via vsp_worker_fqdn (a WORKER
#          node's FQDN, unaffected by the VIP being down) the same way
#          vsp-health.py's own discover_cp() does, then restores the VIP
#          (ip addr add + arping -U) before falling through to the normal
#          ping-gate and check loop.
#          Explicitly NOT ported: the SKU-gated (HOL-2701/2702/2703 only)
#          Fleet-LCM friendly-names cache fix from VCFfinal.py — it requires
#          an OAuth2 token exchange against vcf-iam-vcfa-admin plus scanning
#          a /27 of node IPs for a specific container to bounce, is narrow in
#          scope (cosmetic depot-metadata display naming, not a functional
#          break), and its risk/complexity is disproportionate to running it
#          unattended every 5 minutes. Left as a manual VCFfinal.py re-run.
#   v2.1 - 2026-07-15: Investigated "monitor takes a long time and doesn't
#          seem to fix anything" — found two real, distinct causes:
#          (1) The cron job had never actually been installed on this lab
#              (`crontab -l` was empty) — everything observed came from
#              manual --once/--dry-run invocations, not a running schedule.
#          (2) A real --once pass measured 212s total, with cert_renewal
#              alone taking 118s (56% of the total) since vsp_cert_renewer.py
#              walks 5 separate cert populations every call. 212s against
#              the 300s cron interval left dangerously little margin — a
#              slow cycle (e.g. during the exact host contention #62 exists
#              to detect) could overrun into the next cron firing.
#          Also: on that same run, host_contention correctly fired
#          (load1=18.29 > threshold 18.0) and downgraded the entire cycle to
#          detect-only — which is why crashloop_pods showed detections with
#          no "restarted" actions. Working as designed, but looked
#          indistinguishable from "the monitor is broken" without context;
#          the summary now says so explicitly (see below).
#          Changes:
#            - Added total + per-check elapsed-time reporting (print_row/
#              print_summary; also logged to LOG_FILE) so slowness is
#              visible going forward instead of having to time it by hand.
#            - cert_renewal now runs ONCE PER BOOT SESSION instead of every
#              cycle — state file lives in /tmp, which this OS clears on
#              reboot, so mere file presence already means "checked since
#              boot"; no timestamp/interval math needed. Cuts ~118s off
#              every cycle after the first.
#            - component_health's scale-up check now does 2 bulk
#              deployments/statefulsets fetches instead of one kubectl call
#              per vcfcomponents entry (was 25 sequential round-trips,
#              ~22s; matches the bulk-fetch pattern vsp-health.py already
#              uses for the same data).
#            - proxy_config's per-node drift checks now run concurrently
#              (ThreadPoolExecutor) instead of sequentially — was 6
#              sequential SSH round-trips (~15s), all independent I/O.
#            - Added a PID lockfile (/tmp/vsp-health-monitor.lock) so an
#              overrunning cycle causes the next cron firing to skip rather
#              than stack a second concurrent instance (a stale lock from a
#              dead PID is detected and reclaimed automatically).
#            - print_summary now explicitly names host_contention as the
#              reason remediation didn't happen when that's why, instead of
#              a generic "remediate=false" that reads the same as a config
#              setting.
#   v2.2 - 2026-07-15: proxy_config now also skips nodes already confirmed
#          this boot session, same /tmp-state-file pattern as cert_renewal.
#          Tracks a SET of confirmed node IPs rather than one boolean,
#          because VSP nodes (esp. the control-plane node) are CAPI cattle
#          that get rolling-replaced (#56) — a replacement node has never
#          been checked and must still get caught even if some OTHER node
#          was already confirmed this boot. The node-IP list is re-fetched
#          every cycle either way (cheap) and diffed against the confirmed
#          set, so a new/replaced node is detected and checked automatically
#          without waiting for a reboot.
#   v2.3 - 2026-07-16: CP host resolution on VIP-down now mirrors
#          vsp-health.py's resolve_cp_host() three-tier order — configured
#          vsp_control_plane_ip, then the hardcoded default VIP (in case the
#          config value was overridden to something now stale), then the
#          existing VIP-restore remediation, then (new) falling all the way
#          back to running this cycle's checks directly against the
#          auto-discovered CP node IP if the VIP still won't come back —
#          instead of skipping the entire cycle whenever restore fails or
#          remediate=false.

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

SCRIPT_VERSION = '2.3'
LOG_FILE = '/tmp/vsp-health-monitor.log'

DEFAULTS = {
    'enabled': False,
    'remediate': True,
    'interval_seconds': 300,
    'vsp_control_plane_ip': '10.1.1.142',
    'vsp_worker_fqdn': 'vsp-01a.site-a.vcf.lab',
    'checks': [
        'host_contention', 'vsp_size', 'kvip_manifest', 'cp_pod_crash', 'gateway',
        'node_flap', 'crashloop_pods', 'postgres', 'salt_stack', 'vodap',
        'component_health', 'argo_cleanup', 'proxy_config', 'password_expiration',
        'cert_renewal', 'vip',
    ],
    'crashloop_restart_threshold': 5,
    'crashloop_max_restarts_per_cycle': 15,
    'crashloop_exclude_namespaces': [],
    'host_contention_load_multiplier': 1.5,
    'cert_renewal_threshold_days': 60,
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

# ─── Colors (same palette as vsp-health.py, for a consistent visual style
# across both tools) ──────────────────────────────────────────────────────────
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
_STATUS_SYMBOL = {'PASS': _OK, 'FAIL': _FAIL, 'WARN': _WARN}


#==============================================================================
# LOGGING (persists to disk; console is rendered separately, see below)
#==============================================================================

def log(msg):
    """Persist msg to our own LOG_FILE ONLY — deliberately does NOT call
    lsf.write_output(), which would also append every message (with its own
    timestamp prefix) to the shared labstartup.log / lmchol labstartup.log.
    Those are the lab's real startup/lifecycle logs; a 5-min cron job
    writing a dozen lines per cycle would steadily bury them in monitor
    noise. Console echo is handled separately by
    print_header()/print_row()/print_summary()."""
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(f'{msg}\n')
    except Exception:
        pass


#==============================================================================
# CONSOLE RENDERING (styled to match vsp-health.py's interactive look)
#==============================================================================

def print_header(cfg, dry_run):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    mode = 'DRY-RUN' if dry_run else ('remediate' if cfg['remediate'] else 'detect-only')
    version_line = f'Version {SCRIPT_VERSION}  —  {len(cfg["checks"])} checks'
    ts_line = f'{ts}  ({mode})'
    W = 70
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{'VSP Health Monitor':^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{version_line:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ts_line:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")


def print_row(status, check, msgs, elapsed=None):
    symbol = _STATUS_SYMBOL.get(status, _WARN)
    timing = f"  {_DIM}({elapsed:.1f}s){_NC}" if elapsed is not None else ""
    print(f"  {symbol} {check}{timing}")
    for m in msgs:
        print(f"      {_DIM}{m}{_NC}")


def print_summary(total, failed, remediate, elapsed=None, contention_downgraded=False):
    color = _GREEN if failed == 0 else _RED
    timing = f"  {_DIM}(total: {elapsed:.1f}s){_NC}" if elapsed is not None else ""
    print(f"\n{_CYAN}{'─' * 64}{_NC}")
    print(f"  {color}{_BOLD}RESULT: {total - failed}/{total} checks passed{_NC}{timing}")
    if failed:
        print(f"  {_RED}  {failed} check(s) require attention — see {_FAIL} rows above{_NC}")
        if contention_downgraded:
            print(f"  {_YELLOW}  host_contention downgraded this cycle to detect-only — "
                  f"failures above were NOT remediated (host was CPU-contended, not a "
                  f"config setting); they should get fixed once contention clears{_NC}")
        elif not remediate:
            print(f"  {_DIM}  remediate=false (detect-only) — no changes were made{_NC}")
    else:
        print(f"  {_GREEN}  VSP cluster monitor: all checks passed{_NC}")
    print(f"{_CYAN}{'─' * 64}{_NC}\n")


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

    def _get_float(key, default):
        try:
            return float(lsf.config.get('VSPMONITOR', key, fallback=str(default)).strip())
        except (ValueError, TypeError):
            return default

    cfg['enabled'] = _get_bool('enabled', DEFAULTS['enabled'])
    cfg['remediate'] = _get_bool('remediate', DEFAULTS['remediate'])
    cfg['interval_seconds'] = _get_int('interval_seconds', DEFAULTS['interval_seconds'])
    cfg['vsp_control_plane_ip'] = lsf.config.get(
        'VSPMONITOR', 'vsp_control_plane_ip',
        fallback=DEFAULTS['vsp_control_plane_ip']).strip()
    cfg['vsp_worker_fqdn'] = lsf.config.get(
        'VSPMONITOR', 'vsp_worker_fqdn',
        fallback=DEFAULTS['vsp_worker_fqdn']).strip()
    cfg['checks'] = _get_list('checks', DEFAULTS['checks'])
    cfg['crashloop_restart_threshold'] = _get_int(
        'crashloop_restart_threshold', DEFAULTS['crashloop_restart_threshold'])
    cfg['crashloop_max_restarts_per_cycle'] = _get_int(
        'crashloop_max_restarts_per_cycle', DEFAULTS['crashloop_max_restarts_per_cycle'])
    cfg['crashloop_exclude_namespaces'] = _get_list(
        'crashloop_exclude_namespaces', DEFAULTS['crashloop_exclude_namespaces'])
    cfg['host_contention_load_multiplier'] = _get_float(
        'host_contention_load_multiplier', DEFAULTS['host_contention_load_multiplier'])
    cfg['cert_renewal_threshold_days'] = _get_int(
        'cert_renewal_threshold_days', DEFAULTS['cert_renewal_threshold_days'])
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


def ssh_run(cp_ip, remote_cmd, password):
    """Run a plain UNPRIVILEGED command on the VSP control-plane node (no sudo
    -- uptime/nproc need none of the root access kubectl/crictl require, so
    skip the scp'd-script overhead that run_remote_script() needs for sudo)."""
    return lsf.ssh(remote_cmd, f'{VSP_SSH_USER}@{cp_ip}', password)


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
# PRE-FLIGHT: VIP RESTORE (kube-fix.py fix_vip_restore). NOT a toggleable
# check in the normal sense — every other check needs the VIP up just to SSH
# in, so this runs before the ping-gate in run_all() whenever the initial
# ping to the VIP fails. Discovers a reachable CP IP via a WORKER node's FQDN
# (a different VM than the VIP, so it stays reachable even when the VIP
# itself is down), the same way vsp-health.py's own discover_cp() does, then
# restores the VIP on that node's eth0 and sends a gratuitous ARP.
#==============================================================================

def discover_cp_via_worker(worker_fqdn, password):
    """SSH to a WORKER node's FQDN (not the VIP) and read its kubeconfig to
    find the actual CP IP. Returns None on any DNS/SSH/parse failure."""
    import socket
    try:
        worker_ip = socket.gethostbyname(worker_fqdn)
    except socket.gaierror:
        return None
    res = ssh_run(worker_ip,
                  "grep server: /etc/kubernetes/node-agent.conf 2>/dev/null || "
                  "grep server: /etc/kubernetes/admin.conf 2>/dev/null",
                  password)
    out = _clean_stdout(res) if res is not None else ''
    import re
    m = re.search(r'https?://([0-9.]+):', out)
    return m.group(1) if m else None


def attempt_vip_restore(vip, worker_fqdn, password, dry_run):
    """Called from run_all() only when the initial VIP ping already failed.
    Returns True if the VIP is reachable by the end of this call (whether it
    needed restoring or came back on its own), False if restore could not be
    attempted or did not succeed. Never called when remediate=False — that
    path just reports VIP-down and stops, matching every other check's
    detect-only behavior under remediate=False."""
    if dry_run:
        log(f'vip_restore: [DRY-RUN] VIP {vip} down — would discover CP via '
            f'{worker_fqdn} and restore it')
        return False

    cp_host = discover_cp_via_worker(worker_fqdn, password)
    if not cp_host:
        log(f'vip_restore: VIP {vip} down and could not discover a CP IP via '
            f'worker {worker_fqdn} — cannot attempt restore this cycle')
        return False

    log(f'vip_restore: VIP {vip} down — discovered CP node {cp_host} via '
        f'{worker_fqdn}; attempting restore')
    restore_script = (
        '#!/bin/bash\n'
        f"ip addr show eth0 | grep -q '{vip}' || ip addr add {vip}/32 dev eth0 2>/dev/null\n"
        f"arping -c 3 -U -I eth0 {vip} 2>/dev/null || true\n"
        'echo VIP_RESTORE_ATTEMPTED\n'
    )
    run_remote_script(cp_host, restore_script, password)
    time.sleep(3)
    if lsf.test_ping(vip, count=2, timeout=2):
        log(f'vip_restore: VIP {vip} restored via {cp_host}')
        return True
    log(f'vip_restore: VIP {vip} still unreachable after restore attempt on {cp_host}')
    return False


def resolve_cp_host(configured_ip, worker_fqdn, password, remediate, dry_run):
    """Called from run_all() only when the initial ping to configured_ip
    (cfg['vsp_control_plane_ip']) already failed. Picks the host to use for
    the rest of this cycle by trying candidates in the same order as
    vsp-health.py's own resolve_cp_host(): the configured value (already
    ruled out by the caller), then the hardcoded default VIP (in case the
    config was overridden to something now stale), then the existing
    VIP-restore remediation, then — new — running this cycle's checks
    directly against the auto-discovered CP node IP if the VIP still won't
    come back. That last tier means a down VIP with remediate=false, or a
    restore attempt that doesn't stick, no longer skips the entire cycle as
    long as SOME node answers.
    Returns (cp_host, tier, tried) — cp_host is None if nothing answered;
    tier is one of 'default_vip' / 'restored' / 'discovered' / None."""
    tried = [configured_ip]

    default_vip = DEFAULTS['vsp_control_plane_ip']
    if default_vip != configured_ip:
        tried.append(default_vip)
        if lsf.test_ping(default_vip, count=2, timeout=2):
            log(f'resolve_cp_host: configured VIP {configured_ip} down, '
                f'hardcoded default {default_vip} answered — using it this cycle')
            return default_vip, 'default_vip', tried

    restored = (attempt_vip_restore(configured_ip, worker_fqdn, password, dry_run)
                if remediate else False)
    if restored:
        return configured_ip, 'restored', tried

    discovered = discover_cp_via_worker(worker_fqdn, password)
    if discovered and discovered not in tried:
        tried.append(discovered)
        if lsf.test_ping(discovered, count=2, timeout=2):
            log(f'resolve_cp_host: VIP {configured_ip} still down — running this '
                f'cycle directly against auto-discovered CP node {discovered}')
            return discovered, 'discovered', tried

    return None, None, tried


#==============================================================================
# CHECK: HOST CONTENTION GATE (supervisor-k8s #62). The #59 leader-election
# storm can recur even with a correctly-sized (12-vCPU) CP when the underlying
# nested/physical ESXi host is itself oversubscribed (hypervisor CPU steal) —
# nproc/Component.spec.size look fine from inside the guest in that case, so
# this reads 1-min load average vs nproc directly. Runs FIRST, ahead of
# vsp_size: if the node is contended, every other check this cycle is
# downgraded to detect-only (no force-deletes/patches). Remediation itself is
# API-server write traffic; piling that onto an already CPU-starved node
# compounds the storm instead of damping it. Safe to skip a cycle — kubelet's
# own crash-loop backoff rides it out, and the next cron pass (once contention
# clears) resumes normal remediation.
#==============================================================================

def get_node_load(cp_ip, password):
    """Return (load1, nproc) read from the VSP control-plane node, or
    (None, None) on any SSH/parse failure. Fails OPEN on a read failure (i.e.
    does not report contention) — a measurement failure alone should not
    suppress remediation; the per-check thresholds/caps already bound blast
    radius on their own."""
    res = ssh_run(cp_ip, 'uptime && nproc', password)
    out = _clean_stdout(res) if res is not None else ''
    if not out:
        return None, None
    try:
        load_part = out.rsplit('load average:', 1)[1]
        load1 = float(load_part.split(',')[0].strip())
        nproc = int(out.strip().splitlines()[-1].strip())
        return load1, nproc
    except (IndexError, ValueError):
        return None, None


def check_host_contention(cp_ip, password, multiplier):
    """Detect hypervisor-level CPU steal/contention on the CP node. Returns
    (status, [messages], contended_bool)."""
    load1, nproc = get_node_load(cp_ip, password)
    if load1 is None or nproc is None:
        return ('WARN',
                ['host_contention: could not read uptime/nproc from CP node — '
                 'skipping gate this cycle'],
                False)

    threshold = nproc * multiplier
    if load1 > threshold:
        return ('FAIL',
                [f'host_contention: load1={load1:.2f} > {threshold:.1f} '
                 f'({multiplier}x nproc={nproc}) — CP node under active host '
                 f'contention'],
                True)
    return ('PASS',
            [f'host_contention: load1={load1:.2f} <= {threshold:.1f} '
             f'({multiplier}x nproc={nproc}) - OK'],
            False)


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
# CHECK: KVIP MANIFEST (kube-fix.py fix_kvip_manifest). Ensures
# vip_preserve_on_leadership_loss=true in the kube-vip static-pod manifest —
# with the false default, kube-vip drops the CP VIP when it panics during
# leader election under API-server load at boot, which is what the
# pre-flight VIP-restore step above has to clean up after. This check makes
# the drop itself less likely to recur. Persistent once set (survives
# reboots); cheap and idempotent to re-check every cycle.
#==============================================================================

KVIP_MANIFEST_PATH = '/etc/kubernetes/manifests/kube-vip.yaml'


def check_kvip_manifest(cp_ip, password, remediate, dry_run):
    """Returns (status, [messages])."""
    check_script = (
        '#!/bin/bash\n'
        f'grep -A1 vip_preserve {KVIP_MANIFEST_PATH} 2>/dev/null\n'
    )
    res = run_remote_script(cp_ip, check_script, password)
    out = _clean_stdout(res) if res is not None else ''
    if not out.strip():
        return 'WARN', ['kvip_manifest: manifest not found or unreadable — skipping']
    if 'true' in out:
        return 'PASS', ['kvip_manifest: vip_preserve_on_leadership_loss=true - OK']

    msgs = [f'kvip_manifest: vip_preserve_on_leadership_loss NOT true '
            f'(current: {out.strip()!r})']
    if dry_run:
        msgs.append('kvip_manifest: [DRY-RUN] would patch value to "true"')
        return 'FAIL', msgs
    if not remediate:
        return 'FAIL', msgs

    patch_script = (
        '#!/bin/bash\n'
        f"sed -i '/vip_preserve_on_leadership_loss/{{n; s/\"false\"/\"true\"/}}' {KVIP_MANIFEST_PATH}\n"
        f'grep -A1 vip_preserve {KVIP_MANIFEST_PATH} 2>/dev/null\n'
    )
    pres = run_remote_script(cp_ip, patch_script, password)
    pout = _clean_stdout(pres) if pres is not None else ''
    if 'true' in pout:
        msgs.append('kvip_manifest: patched to "true" (verified) — kubelet will '
                    're-read the manifest and restart kube-vip')
    else:
        msgs.append('kvip_manifest: WARNING — patch did not verify')
    return 'FAIL', msgs


#==============================================================================
# CHECK: CP POD CRASH (kube-fix.py fix_kube_controller_manager/
# fix_kube_scheduler). Force-clears a crashed kube-controller-manager or
# kube-scheduler static-pod container via crictl so kubelet recreates it
# immediately instead of waiting out its backoff. Neither node_flap (only
# bounces etcd/kube-apiserver) nor crashloop_pods (deliberately excludes ALL
# CP static pods, by CP_STATIC_POD_PREFIXES, so node_flap/this check can own
# them) could ever reach these two — this was the exact gap behind the
# #59/#62 leader-election-storm symptom investigated in this lab: KCM/
# scheduler sit crash-looping with no automated recovery path until now.
#==============================================================================

def _crictl_container_state(cp_ip, password, keyword):
    """Return (found: bool, running: bool, states_str) for the crictl ps -a
    line(s) containing keyword. Column layout is CONTAINER_ID IMAGE
    CREATED(3 tokens: "N unit ago") STATE NAME ATTEMPT POD_ID POD_NAME, so
    STATE is always at split()[5] — NOT split()[3], which lands inside the
    "N unit ago" phrase (e.g. "seconds"/"minutes") and was silently wrong."""
    res = crictl(cp_ip, 'ps -a', password)
    out = _clean_stdout(res) if res is not None else ''
    matching = [ln for ln in out.splitlines() if keyword in ln]
    if not matching:
        return False, False, ''
    running = any('Running' in ln for ln in matching)
    states = ', '.join(ln.split()[5] if len(ln.split()) > 5 else '?' for ln in matching)
    return True, running, states


def _clear_crashed_static_pod(cp_ip, password, keyword, friendly_name, dry_run):
    """Shared logic for kube-controller-manager / kube-scheduler. Returns
    (status, [messages])."""
    found, running, states = _crictl_container_state(cp_ip, password, keyword)
    if not found:
        return 'PASS', [f'cp_pod_crash: {friendly_name}: no container found in '
                        f'crictl ps -a — kubelet will create it - OK']
    if running:
        return 'PASS', [f'cp_pod_crash: {friendly_name}: Running - OK']

    msgs = [f'cp_pod_crash: {friendly_name}: found in state [{states}] — '
            f'not Running']
    if dry_run:
        msgs.append(f'cp_pod_crash: [DRY-RUN] would crictl rm -f the {friendly_name} '
                    f'container so kubelet recreates it')
        return 'FAIL', msgs

    rm_script = (
        '#!/bin/bash\n'
        f'crictl ps -a 2>/dev/null | grep {keyword} | awk \'{{print $1}}\' | '
        f'xargs -r crictl rm -f 2>/dev/null && echo REMOVED\n'
    )
    rres = run_remote_script(cp_ip, rm_script, password)
    rout = _clean_stdout(rres) if rres is not None else ''
    if 'REMOVED' in rout:
        msgs.append(f'cp_pod_crash: force-removed crashed {friendly_name} container '
                    f'— kubelet will recreate it (check again next cycle)')
    else:
        msgs.append(f'cp_pod_crash: WARNING — crictl rm -f did not confirm for '
                    f'{friendly_name}')
    return 'FAIL', msgs


def check_cp_pod_crash(cp_ip, password, remediate, dry_run):
    """Returns (status, [messages]) covering both kube-controller-manager and
    kube-scheduler."""
    if dry_run:
        remediate_effective = False
    else:
        remediate_effective = remediate

    msgs = []
    status = 'PASS'
    for keyword, friendly_name in (
        ('kube-controller', 'kube-controller-manager'),
        ('kube-scheduler', 'kube-scheduler'),
    ):
        if remediate_effective or dry_run:
            st, sub_msgs = _clear_crashed_static_pod(cp_ip, password, keyword,
                                                      friendly_name, dry_run)
        else:
            # detect-only: reuse the same state read, just never remediate
            found, running, states = _crictl_container_state(cp_ip, password, keyword)
            if not found or running:
                st, sub_msgs = 'PASS', [f'cp_pod_crash: {friendly_name}: '
                                        f'{"Running" if running else "no container found"} - OK']
            else:
                st, sub_msgs = 'FAIL', [f'cp_pod_crash: {friendly_name}: found in '
                                        f'state [{states}] — not Running (remediate=false)']
        msgs.extend(sub_msgs)
        if st == 'FAIL':
            status = 'FAIL'
    return status, msgs


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
# CHECK: POSTGRES (salt-stabilize.py fix_pgdata_permissions + VCFfinal.py
# Task 2e Postgres/Zalando unsuspend). Two independent Postgres-related
# fixes bundled together since both only ever act on a genuinely detected
# problem (unlike salt_stack below, no extra gating needed — the pod-
# readiness / suspended-label checks already ARE the gate):
#   (a) pgdatabase-0 (salt-raas) not fully ready -> chmod 700 the pgdata dir
#       via the walg sidecar, then delete the pod so Spilo restarts clean.
#   (b) any PostgresInstance CRD labeled suspended=true -> remove the label;
#       any Zalando postgresqls.acid.zalan.do CRD scaled below its saved
#       vcf.lab/original-instances annotation -> patch numberOfInstances
#       back up.
#==============================================================================

def check_postgres(cp_ip, password, remediate, dry_run):
    msgs = []
    status = 'PASS'

    # --- (a) pgdatabase-0 permissions ---
    pg_pod = kubectl_json(cp_ip, 'get pod pgdatabase-0 -n salt-raas', password)
    if pg_pod and pg_pod.get('kind') == 'Pod':
        ready, total, _, _, _ = _pod_health(pg_pod)
        if total > 0 and ready == total:
            msgs.append(f'postgres: pgdatabase-0: {ready}/{total} containers Ready - OK')
        else:
            msgs.append(f'postgres: pgdatabase-0: {ready}/{total} containers Ready — '
                        f'likely pgdata permission issue')
            status = 'FAIL'
            if dry_run:
                msgs.append('postgres: [DRY-RUN] would chmod 700 pgdata via walg '
                            'sidecar and delete the pod')
            elif remediate:
                cres = kubectl(cp_ip, 'exec -n salt-raas pgdatabase-0 -c walg -- '
                               'chmod 700 /home/postgres/pgdata/pgroot/data', password)
                if getattr(cres, 'returncode', 1) == 0:
                    msgs.append('postgres: chmod 700 applied via walg sidecar')
                else:
                    msgs.append('postgres: WARNING — chmod exec did not confirm; '
                                'deleting pod anyway')
                kubectl(cp_ip, 'delete pod pgdatabase-0 -n salt-raas --grace-period=0', password)
                msgs.append('postgres: deleted pgdatabase-0 to restart with corrected '
                            'permissions (will confirm 3/3 next cycle)')
    else:
        msgs.append('postgres: pgdatabase-0: not found or unreadable — skipping '
                    'permission check')

    # --- (b) PostgresInstance suspended label + Zalando replica restore ---
    pg_inst = kubectl_json(cp_ip, 'get postgresinstances.database.vmsp.vmware.com -A', password)
    if not pg_inst or 'items' not in pg_inst:
        msgs.append('postgres: could not list PostgresInstance CRDs — skipping '
                    'suspend/replica check')
        return status, msgs

    for item in pg_inst['items']:
        ns = item.get('metadata', {}).get('namespace', '')
        name = item.get('metadata', {}).get('name', '')
        labels = item.get('metadata', {}).get('labels', {})
        suspended = labels.get('database.vmsp.vmware.com/suspended', '')

        if suspended == 'true':
            status = 'FAIL'
            msgs.append(f'postgres: {ns}/{name} labeled suspended=true')
            if dry_run:
                msgs.append(f'postgres: [DRY-RUN] would remove suspended label from {ns}/{name}')
            elif remediate:
                kubectl(cp_ip, f'label postgresinstances.database.vmsp.vmware.com '
                              f'{name} -n {ns} database.vmsp.vmware.com/suspended-', password)
                msgs.append(f'postgres: removed suspended label from {ns}/{name}')

        zalando = kubectl_json(cp_ip, f'get postgresqls.acid.zalan.do {name} -n {ns}', password)
        if not zalando:
            continue
        current = zalando.get('spec', {}).get('numberOfInstances', 0) or 0
        anno = zalando.get('metadata', {}).get('annotations', {}).get(
            'vcf.lab/original-instances', '')
        intended = int(anno) if anno.isdigit() and int(anno) > 0 else 1

        if current < intended:
            status = 'FAIL'
            msgs.append(f'postgres: {ns}/{name} Zalando numberOfInstances={current} '
                        f'< intended {intended}')
            if dry_run:
                msgs.append(f'postgres: [DRY-RUN] would patch numberOfInstances -> {intended}')
            elif remediate:
                kubectl_patch(cp_ip, 'postgresqls.acid.zalan.do', name, ns,
                             {'spec': {'numberOfInstances': intended}}, password)
                msgs.append(f'postgres: patched {ns}/{name} numberOfInstances -> {intended}')

    if status == 'PASS' and len(msgs) <= 1:
        msgs.append('postgres: PostgresInstance/Zalando CRDs — none suspended, '
                    'all at intended replica count - OK')
    return status, msgs


#==============================================================================
# CHECK: SALT STACK (salt-stabilize.py rollout_restart_salt_stack). The
# source script always rollout-restarts unconditionally when a human runs it
# on demand — that is wrong for a 5-min cron job, so this adds a detection
# gate (readiness across all 4 pods + the same salt-master log-error grep
# vsp-health.py uses) and only restarts the ordered redis->raas->salt-master
# ->salt-minion chain when something is actually broken.
#==============================================================================

SALT_STACK_STEPS = [
    ('redis', 'salt-raas', 'deployment/redis'),
    ('raas', 'salt-raas', 'deployment/raas'),
    ('salt-master', 'salt', 'deployment/salt-master'),
    ('salt-minion', 'salt', 'deployment/salt-minion'),
]
SALT_LOG_ERROR_PATTERNS = (
    'SSL CERTIFICATE_VERIFY_FAILED', 'This Minion was scheduled to stop',
    '530 Unknown', 'RAAS is not available', 'Connection refused to',
)


def _salt_stack_needs_restart(cp_ip, password):
    """Returns (needs_restart: bool, [detail messages])."""
    msgs = []
    needs = False
    for label, ns, resource in SALT_STACK_STEPS:
        _, name = resource.split('/', 1)
        pod = kubectl_json(cp_ip, f'get pod -n {ns} -l app={name}', password)
        items = (pod or {}).get('items', [])
        if not items:
            msgs.append(f'salt_stack: {label} ({ns}): no pod found')
            needs = True
            continue
        ready, total, _, _, _ = _pod_health(items[0])
        if total == 0 or ready < total:
            msgs.append(f'salt_stack: {label} ({ns}): {ready}/{total} Ready')
            needs = True

    log_res = kubectl(cp_ip, 'logs -n salt --selector=app=salt-master --tail=80', password)
    log_out = _clean_stdout(log_res) if log_res is not None else ''
    found_errors = [p for p in SALT_LOG_ERROR_PATTERNS if p in log_out]
    if found_errors:
        msgs.append(f'salt_stack: salt-master logs show: {"; ".join(found_errors)}')
        needs = True

    return needs, msgs


def check_salt_stack(cp_ip, password, remediate, dry_run):
    needs_restart, msgs = _salt_stack_needs_restart(cp_ip, password)
    if not needs_restart:
        return 'PASS', ['salt_stack: redis/raas/salt-master/salt-minion all Ready, '
                        'no known log errors - OK']

    status = 'FAIL'
    if dry_run:
        msgs.append('salt_stack: [DRY-RUN] would rollout-restart redis -> raas -> '
                    'salt-master -> salt-minion in order')
        return status, msgs
    if not remediate:
        return status, msgs

    for _, ns, resource in SALT_STACK_STEPS:
        kubectl(cp_ip, f'rollout restart {resource} -n {ns}', password)
        msgs.append(f'salt_stack: triggered rollout restart {ns}/{resource}')
    msgs.append('salt_stack: ordered restart triggered — readiness will be '
               'confirmed next cycle')
    return status, msgs


#==============================================================================
# CHECK: VODAP (vodap-fix.py fix_clickhouse + fix_fluentd — both already
# internally gated on a detected problem, ported near-verbatim).
#   (a) ClickHouse: compares the cert it's actually SERVING (live
#       openssl s_client) against the vcf-obs-clickhouse-cert secret — a
#       cert-manager Certificate-resource check alone can't see this, since
#       ClickHouse loads its TLS cert at startup and never hot-reloads, so a
#       Certificate can show Ready/valid while ClickHouse still serves a
#       stale cert in memory. Restarts the StatefulSet only if the served
#       cert is expired, mismatched vs. the secret, or the secret itself
#       expires within 7 days.
#   (b) logging-operator-fluentd-0: readiness probe fails once /buffers disk
#       usage or backup-chunk file count crosses a threshold; a pod restart
#       alone doesn't help since the PVC persists — purges
#       /buffers/backup/* inside the running container instead.
#==============================================================================

CLICKHOUSE_CLIENTS = ['vcf-obs-data-query-service', 'vcf-obs-netops-collector-service']


def _openssl_dates(pem_bytes):
    """Return (not_before, not_after, expires_within_7d) as datetime|None,
    datetime|None, bool, by shelling out to the LOCAL openssl (manager VM)."""
    from datetime import datetime, timezone
    fmt = '%b %d %H:%M:%S %Y %Z'
    dates = subprocess.run(['openssl', 'x509', '-noout', '-dates'],
                           input=pem_bytes, capture_output=True)
    not_before = not_after = None
    for line in dates.stdout.decode('utf-8', errors='ignore').splitlines():
        if line.startswith('notBefore='):
            try:
                not_before = datetime.strptime(line.split('=', 1)[1].strip(), fmt).replace(
                    tzinfo=timezone.utc)
            except ValueError:
                pass
        elif line.startswith('notAfter='):
            try:
                not_after = datetime.strptime(line.split('=', 1)[1].strip(), fmt).replace(
                    tzinfo=timezone.utc)
            except ValueError:
                pass
    check7 = subprocess.run(['openssl', 'x509', '-noout', '-checkend', str(7 * 86400)],
                            input=pem_bytes, capture_output=True)
    return not_before, not_after, (check7.returncode != 0)


def check_vodap(cp_ip, password, remediate, dry_run):
    import base64 as _b64
    from datetime import datetime, timezone
    msgs = []
    status = 'PASS'

    # --- (a) ClickHouse cert ---
    sec = kubectl_json(cp_ip, 'get secret vcf-obs-clickhouse-cert -n vodap', password)
    if not sec:
        msgs.append('vodap: vcf-obs-clickhouse-cert secret not found — vodap may not '
                    'be deployed yet')
    else:
        cert_b64 = sec.get('data', {}).get('tls.crt', '')
        secret_pem = _b64.b64decode(cert_b64) if cert_b64 else b''
        na, expiring_soon = (None, False)
        if secret_pem:
            _, na, expiring_soon = _openssl_dates(secret_pem)
        now = datetime.now(timezone.utc)

        # jsonpath's embedded quotes conflict with lsf.ssh's own double-quote
        # wrapping (the nested-quoting trap noted throughout this file) —
        # route through run_remote_script() to sidestep it entirely.
        svc_ip_script = (
            '#!/bin/bash\n'
            "kubectl get service clickhouse-vcf-obs -n vodap "
            "-o jsonpath='{.spec.clusterIP}'\n"
        )
        svc_ip_res = run_remote_script(cp_ip, svc_ip_script, password)
        chi_svc_ip = (_clean_stdout(svc_ip_res) if svc_ip_res is not None else '').strip().strip('"')

        served_expired = False
        served_na = None
        if chi_svc_ip:
            s_script = (
                '#!/bin/bash\n'
                f'openssl s_client -connect {chi_svc_ip}:8443 -showcerts </dev/null 2>/dev/null | '
                f'openssl x509 -noout -dates 2>/dev/null\n'
            )
            s_res = run_remote_script(cp_ip, s_script, password)
            s_out = _clean_stdout(s_res) if s_res is not None else ''
            served_str = ''
            for ln in s_out.splitlines():
                if ln.startswith('notAfter='):
                    served_str = ln.split('=', 1)[1].strip()
            if served_str:
                try:
                    served_na = datetime.strptime(served_str, '%b %d %H:%M:%S %Y %Z').replace(
                        tzinfo=timezone.utc)
                    served_expired = served_na < now
                    if not served_expired and na and served_na.date() != na.date():
                        served_expired = True  # stale-in-memory mismatch
                except ValueError:
                    pass

        failing_clients = []
        for dep in CLICKHOUSE_CLIENTS:
            dep_data = kubectl_json(cp_ip, f'get deployment {dep} -n vodap', password)
            if dep_data:
                ready = dep_data.get('status', {}).get('readyReplicas', 0) or 0
                desired = dep_data.get('spec', {}).get('replicas', 1) or 1
                if desired > 0 and ready < desired:
                    failing_clients.append(dep)

        needs_restart = served_expired or expiring_soon or bool(failing_clients)
        if not needs_restart:
            msgs.append(f'vodap: ClickHouse cert current (notAfter='
                       f'{na.date() if na else "?"}) and clients ready - OK')
        else:
            status = 'FAIL'
            detail = []
            if served_expired:
                detail.append('serving expired/mismatched cert')
            if expiring_soon:
                detail.append('secret cert expiring within 7d')
            if failing_clients:
                detail.append(f'clients not ready: {", ".join(failing_clients)}')
            msgs.append(f'vodap: ClickHouse needs restart — {"; ".join(detail)}')
            if dry_run:
                msgs.append('vodap: [DRY-RUN] would restart statefulset/'
                           'chi-vcf-obs-vcf-obs-0-0')
            elif remediate:
                kubectl(cp_ip, 'rollout restart statefulset/chi-vcf-obs-vcf-obs-0-0 -n vodap',
                       password)
                msgs.append('vodap: triggered ClickHouse StatefulSet restart (rollout '
                           'can take 3+ min; will confirm next cycles)')

    # --- (b) fluentd buffer backlog ---
    fpod = kubectl_json(cp_ip, 'get pod logging-operator-fluentd-0 -n vmsp-platform', password)
    if not fpod or fpod.get('kind') != 'Pod':
        msgs.append('vodap: logging-operator-fluentd-0 not found — skipping buffer check')
        return status, msgs

    ready, total, _, _, _ = _pod_health(fpod)
    if total == 0:
        msgs.append('vodap: logging-operator-fluentd-0: no container status yet')
        return status, msgs
    if ready == total:
        msgs.append(f'vodap: logging-operator-fluentd-0: {ready}/{total} Ready - OK')
        return status, msgs

    status = 'FAIL'
    msgs.append(f'vodap: logging-operator-fluentd-0: {ready}/{total} Ready')
    if dry_run:
        msgs.append('vodap: [DRY-RUN] would purge /buffers/backup/* in the fluentd container')
        return status, msgs
    if not remediate:
        return status, msgs

    buf_script = (
        '#!/bin/bash\n'
        'kubectl exec -n vmsp-platform logging-operator-fluentd-0 -c fluentd -- '
        'sh -c "df -h /buffers | tail -1; '
        'find /buffers/backup -type f 2>/dev/null | wc -l"\n'
    )
    bres = run_remote_script(cp_ip, buf_script, password)
    bout = _clean_stdout(bres) if bres is not None else ''
    lines = [l for l in bout.splitlines() if l.strip()]
    disk_pct, backup_files = 0, 0
    if lines:
        parts = lines[0].split()
        if len(parts) >= 5 and '%' in parts[4]:
            try:
                disk_pct = int(parts[4].rstrip('%'))
            except ValueError:
                pass
    if len(lines) >= 2 and lines[1].strip().isdigit():
        backup_files = int(lines[1].strip())
    msgs.append(f'vodap: fluentd buffer disk={disk_pct}% backup_files={backup_files}')

    if backup_files > 0 or disk_pct > 80:
        purge_script = (
            '#!/bin/bash\n'
            'kubectl exec -n vmsp-platform logging-operator-fluentd-0 -c fluentd -- '
            'sh -c "rm -rf /buffers/backup/* 2>/dev/null"\n'
        )
        run_remote_script(cp_ip, purge_script, password)
        msgs.append(f'vodap: purged {backup_files} stale backup chunks from '
                   f'/buffers/backup (will confirm readiness next cycle)')
    else:
        msgs.append('vodap: no backup chunks to purge — probe may resolve on its own')
    return status, msgs


#==============================================================================
# CHECK: COMPONENT HEALTH (VCFfinal.py Task 2e). Two related fixes for the
# vmsp-operator's Component CRDs:
#   (a) components.api.vmsp.vmware.com annotated
#       component.vmsp.vmware.com/operational-status=NotRunning ->
#       patched to Running (must happen BEFORE scale-up, else the operator
#       races the scale-up back down to 0).
#   (b) any [VCFFINAL] vcfcomponents entry (namespace:resource, e.g.
#       "ops-logs:statefulset/log-processor") scaled below its saved
#       vcf.lab/original-replicas annotation -> scaled back up. This is
#       what actually FIXES the "0/0 scaled to 0 — run VCFfinal.py Task 2e"
#       case vsp-health.py's VCF MANAGED COMPONENTS section could only
#       report — including ops-logs, which has its own dedicated rescue in
#       VCFfinal.py (Task 6) but is really just another vcfcomponents entry.
#==============================================================================

def _vcfcomponents_list():
    """Read [VCFFINAL] vcfcomponents from config.ini — same config-driven
    list VCFfinal.py Task 2e uses (namespace:resource_type/resource_name
    entries), so this check never drifts from what VCFfinal.py considers
    "managed". Falls back to an empty list if the section/key is absent
    (e.g. a non-VSP lab)."""
    raw = lsf.config.get('VCFFINAL', 'vcfcomponents', fallback='').strip()
    if not raw:
        return []
    entries = []
    for line in raw.replace('\n', ',').split(','):
        line = line.strip()
        if not line or ':' not in line:
            continue
        ns, resource = line.split(':', 1)
        entries.append((ns.strip(), resource.strip()))
    return entries


def check_component_health(cp_ip, password, remediate, dry_run):
    msgs = []
    status = 'PASS'

    # --- (a) NotRunning -> Running annotation ---
    comp_data = kubectl_json(cp_ip, 'get components.api.vmsp.vmware.com', password)
    if comp_data and 'items' in comp_data:
        for item in comp_data['items']:
            crd_name = item.get('metadata', {}).get('name', '')
            ann = item.get('metadata', {}).get('annotations', {})
            op_status = ann.get('component.vmsp.vmware.com/operational-status', '')
            if op_status == 'NotRunning':
                status = 'FAIL'
                msgs.append(f'component_health: Component {crd_name} annotated NotRunning')
                if dry_run:
                    msgs.append(f'component_health: [DRY-RUN] would annotate '
                               f'{crd_name} operational-status=Running')
                elif remediate:
                    kubectl(cp_ip, f'annotate components.api.vmsp.vmware.com {crd_name} '
                                  f'component.vmsp.vmware.com/operational-status=Running '
                                  f'--overwrite', password)
                    msgs.append(f'component_health: annotated {crd_name} -> Running')
    else:
        msgs.append('component_health: could not list Component CRDs — skipping '
                   'operational-status check')

    # --- (b) scale-up from saved original-replicas annotation ---
    entries = _vcfcomponents_list()
    if not entries:
        msgs.append('component_health: no [VCFFINAL] vcfcomponents configured — '
                   'skipping scale-up check')
        return status, msgs

    # Two bulk fetches instead of one kubectl call per entry (was 25 sequential
    # SSH round-trips at ~0.9s each on this lab, ~22s total just for this loop).
    deps_data = kubectl_json(cp_ip, 'get deployments -A', password)
    sts_data = kubectl_json(cp_ip, 'get statefulsets -A', password)
    wl = {}
    for data, kind in ((deps_data, 'deployment'), (sts_data, 'statefulset')):
        if not data:
            continue
        for item in data.get('items', []):
            ns_ = item.get('metadata', {}).get('namespace', '')
            name_ = item.get('metadata', {}).get('name', '')
            wl[(ns_, kind, name_)] = item

    below_intended = 0
    for ns, resource in entries:
        kind, name = resource.split('/', 1)
        comp = wl.get((ns, kind, name))
        if not comp:
            continue
        current = comp.get('spec', {}).get('replicas', 0) or 0
        anno = comp.get('metadata', {}).get('annotations', {}).get(
            'vcf.lab/original-replicas', '')
        intended = int(anno) if anno.isdigit() and int(anno) > 0 else 1
        if current < intended:
            below_intended += 1
            status = 'FAIL'
            msgs.append(f'component_health: {ns}/{resource} replicas={current} '
                       f'< intended {intended}')
            if dry_run:
                msgs.append(f'component_health: [DRY-RUN] would scale {ns}/{resource} '
                           f'-> {intended}')
            elif remediate:
                kubectl(cp_ip, f'scale {resource} -n {ns} --replicas={intended}', password)
                msgs.append(f'component_health: scaled {ns}/{resource} -> {intended}')

    if below_intended == 0:
        msgs.append(f'component_health: all {len(entries)} vcfcomponents entries at '
                   f'intended replica count - OK')
    return status, msgs


#==============================================================================
# CHECK: ARGO CLEANUP (VCFfinal.py KB 440862 pre-flight). Three related
# recovery steps for a boot deadlock where stale system-shutdown Argo
# Workflows re-cordon nodes and re-scale vmsp-platform to 0 on every restart
# of the Argo controller:
#   (a) delete any system-shutdown-* Workflow in vmsp-platform
#   (b) replay the power-off-marker ConfigMap's saved replica counts
#       (vmspcontent: "name=N,..." for vmsp-platform deployments; content:
#       "namespace.kind.name=N,..." for tenant-service resources), then
#       delete the ConfigMap once replayed
#   (c) uncordon any SchedulingDisabled node that is NOT tainted
#       ToBeDeletedByClusterAutoscaler (i.e. not a legitimately-condemned
#       autoscaler node)
#==============================================================================

def check_argo_cleanup(cp_ip, password, remediate, dry_run):
    msgs = []
    status = 'PASS'

    # --- (a) stale system-shutdown workflows ---
    wf_res = kubectl(cp_ip, 'get workflow -n vmsp-platform --no-headers', password)
    wf_out = _clean_stdout(wf_res) if wf_res is not None else ''
    stale_wfs = [ln.split()[0] for ln in wf_out.splitlines()
                if 'system-shutdown' in ln and ln.split()]
    if stale_wfs:
        status = 'FAIL'
        msgs.append(f'argo_cleanup: {len(stale_wfs)} stale system-shutdown workflow(s) found')
        if dry_run:
            msgs.append(f'argo_cleanup: [DRY-RUN] would delete: {", ".join(stale_wfs)}')
        elif remediate:
            for wf in stale_wfs:
                kubectl(cp_ip, f'delete workflow -n vmsp-platform {wf} --grace-period=0', password)
            msgs.append(f'argo_cleanup: deleted {len(stale_wfs)} stale workflow(s)')
    else:
        msgs.append('argo_cleanup: no stale system-shutdown workflows - OK')

    # --- (b) power-off-marker ConfigMap replay ---
    pom = kubectl_json(cp_ip, 'get configmap power-off-marker -n vmsp-platform', password)
    if pom and 'data' in pom:
        status = 'FAIL'
        msgs.append('argo_cleanup: power-off-marker ConfigMap present — recovery incomplete')
        vmsp_data = pom.get('data', {}).get('vmspcontent', '')
        tenant_data = pom.get('data', {}).get('content', '')
        if dry_run:
            msgs.append('argo_cleanup: [DRY-RUN] would replay saved replica counts '
                       'and delete the ConfigMap')
        elif remediate:
            scaled = 0
            for entry in vmsp_data.split(','):
                n, _, r = entry.strip().partition('=')
                if n.strip() and r.strip().isdigit():
                    kubectl(cp_ip, f'scale deploy {n.strip()} --replicas={r.strip()} '
                                  f'-n vmsp-platform', password)
                    scaled += 1
            for entry in tenant_data.split(','):
                k, _, r = entry.strip().partition('=')
                parts = k.strip().split('.', 2)
                if len(parts) == 3 and r.strip().isdigit():
                    ns, kind, name = parts
                    kubectl(cp_ip, f'scale {kind.lower().strip()} {name.strip()} '
                                  f'--replicas={r.strip()} -n {ns.strip()}', password)
                    scaled += 1
            kubectl(cp_ip, 'delete configmap power-off-marker -n vmsp-platform', password)
            msgs.append(f'argo_cleanup: replayed {scaled} saved replica count(s), '
                       f'deleted power-off-marker ConfigMap')
    else:
        msgs.append('argo_cleanup: no power-off-marker ConfigMap present - OK')

    # --- (c) uncordon non-condemned nodes ---
    nodes = kubectl_json(cp_ip, 'get nodes', password)
    to_uncordon = []
    if nodes and 'items' in nodes:
        for item in nodes['items']:
            name = item.get('metadata', {}).get('name', '')
            taints = item.get('spec', {}).get('taints', [])
            unschedulable = item.get('spec', {}).get('unschedulable', False)
            if unschedulable and not any(
                    t.get('key') == 'ToBeDeletedByClusterAutoscaler' for t in taints):
                to_uncordon.append(name)

    if to_uncordon:
        status = 'FAIL'
        msgs.append(f'argo_cleanup: {len(to_uncordon)} SchedulingDisabled node(s) not '
                   f'condemned by autoscaler: {", ".join(to_uncordon)}')
        if dry_run:
            msgs.append(f'argo_cleanup: [DRY-RUN] would uncordon: {", ".join(to_uncordon)}')
        elif remediate:
            for node in to_uncordon:
                kubectl(cp_ip, f'uncordon {node}', password)
            msgs.append(f'argo_cleanup: uncordoned {len(to_uncordon)} node(s)')
    else:
        msgs.append('argo_cleanup: no unexpected SchedulingDisabled nodes - OK')

    return status, msgs


#==============================================================================
# CHECK: PROXY CONFIG (VCFfinal.py proxy-push block). Idempotent, diff-gated
# repair of VSP-node proxy config against the lab's canonical values
# (lsf.LAB_PROXY_URL / lsf.build_lab_no_proxy()) — only ever SETS the
# canonical values, never clears proxy config (clearing is a judgment call
# about whether this specific lab needs a proxy at all, which this recurring
# monitor should not make unattended; VCFfinal.py already applies this
# unconditionally at every boot, this just repairs drift between boots).
#==============================================================================

PROXY_CONFIG_STATE_FILE = '/tmp/vsp-health-monitor-proxy-config.state'


def _load_confirmed_proxy_nodes():
    """Returns the set of node IPs already confirmed to match canonical
    proxy config since last boot (/tmp is cleared on reboot). A SET of
    per-node IPs, not a single boolean flag, because VSP nodes (especially
    the control-plane node) are CAPI "cattle" that get rolling-replaced
    during normal cluster lifecycle (see #56) — a replacement node has never
    been checked and must not be silently skipped just because some OTHER
    node was already confirmed this boot."""
    try:
        return set(json.loads(open(PROXY_CONFIG_STATE_FILE).read()))
    except Exception:
        return set()


def _save_confirmed_proxy_nodes(confirmed):
    try:
        with open(PROXY_CONFIG_STATE_FILE, 'w') as f:
            f.write(json.dumps(sorted(confirmed)))
    except Exception:
        pass


def check_proxy_config(cp_ip, password, remediate, dry_run):
    proxy_url = getattr(lsf, 'LAB_PROXY_URL', None)
    no_proxy = lsf.build_lab_no_proxy() if hasattr(lsf, 'build_lab_no_proxy') else None
    if not proxy_url or not no_proxy:
        return 'WARN', ['proxy_config: lsf.LAB_PROXY_URL/build_lab_no_proxy() not '
                        'available — skipping']

    # jsonpath's embedded quotes conflict with lsf.ssh's own double-quote
    # wrapping (nested-quoting trap) — route through run_remote_script().
    node_ips_script = (
        '#!/bin/bash\n'
        "kubectl get nodes -o jsonpath='{range .items[*]}"
        "{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}'\n"
    )
    node_ips_res = run_remote_script(cp_ip, node_ips_script, password)
    node_ips_out = (_clean_stdout(node_ips_res) if node_ips_res is not None else '').strip()
    node_ips = [ip for ip in node_ips_out.split() if ip]
    if not node_ips:
        return 'WARN', ['proxy_config: could not list node IPs — skipping']

    # Once a node is confirmed matching canonical config, nothing else in
    # this lab changes it — skip re-checking it every cycle. Prune the
    # confirmed set to nodes that still exist (drop entries for
    # replaced/removed nodes) and only actually check the rest.
    confirmed = _load_confirmed_proxy_nodes() & set(node_ips)
    to_check = [ip for ip in node_ips if ip not in confirmed]

    if not to_check:
        return 'PASS', [f'proxy_config: all {len(node_ips)} node(s) already confirmed '
                        f'this boot session - OK']

    check_script = (
        '#!/bin/bash\n'
        f'grep -qF "{proxy_url}" /etc/sysconfig/proxy 2>/dev/null && '
        f'grep -qF "{proxy_url}" /etc/systemd/system/containerd.service.d/http-proxy.conf 2>/dev/null && '
        f'grep -qF "{proxy_url}" /etc/systemd/system/kubelet.service.d/http-proxy.conf 2>/dev/null && '
        f'echo PROXY_OK || echo PROXY_DRIFT\n'
    )

    # Independent per-node SSH checks — run concurrently (was 6 sequential
    # calls at ~2.5s each = ~15s serially; all I/O-bound, safe to parallelize).
    import concurrent.futures
    drifted = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(to_check)) as pool:
        futures = {pool.submit(run_remote_script, ip, check_script, password): ip
                   for ip in to_check}
        for future in concurrent.futures.as_completed(futures):
            ip = futures[future]
            try:
                res = future.result()
            except Exception:
                res = None
            out = _clean_stdout(res) if res is not None else ''
            if 'PROXY_OK' in out:
                confirmed.add(ip)
            else:
                drifted.append(ip)

    if not dry_run:
        _save_confirmed_proxy_nodes(confirmed)

    if not drifted:
        return 'PASS', [f'proxy_config: proxy config matches canonical value on all '
                        f'{len(node_ips)} node(s) - OK ({len(to_check)} newly checked, '
                        f'{len(node_ips) - len(to_check)} already confirmed)']

    msgs = [f'proxy_config: {len(drifted)}/{len(node_ips)} node(s) missing/drifted '
           f'proxy config: {", ".join(drifted)}']
    if dry_run:
        msgs.append(f'proxy_config: [DRY-RUN] would push {proxy_url} '
                   f'(NO_PROXY={no_proxy}) to drifted node(s) and restart '
                   f'containerd/kubelet')
        return 'FAIL', msgs
    if not remediate:
        return 'FAIL', msgs

    for ip in drifted:
        push_script = (
            '#!/bin/bash\n'
            f'echo \'http_proxy="{proxy_url}"\' > /etc/sysconfig/proxy\n'
            f'echo \'https_proxy="{proxy_url}"\' >> /etc/sysconfig/proxy\n'
            f'echo \'no_proxy="{no_proxy}"\' >> /etc/sysconfig/proxy\n'
            'mkdir -p /etc/systemd/system/containerd.service.d /etc/systemd/system/kubelet.service.d\n'
            f'printf "[Service]\\nEnvironment=\\"HTTP_PROXY={proxy_url}\\"\\n'
            f'Environment=\\"HTTPS_PROXY={proxy_url}\\"\\nEnvironment=\\"NO_PROXY={no_proxy}\\"\\n" '
            f'> /etc/systemd/system/containerd.service.d/http-proxy.conf\n'
            f'cp /etc/systemd/system/containerd.service.d/http-proxy.conf '
            f'/etc/systemd/system/kubelet.service.d/http-proxy.conf\n'
            f'grep -q "^http_proxy=" /etc/environment && '
            f'sed -i "s#^http_proxy=.*#http_proxy={proxy_url}#" /etc/environment || '
            f'echo "http_proxy={proxy_url}" >> /etc/environment\n'
            f'grep -q "^https_proxy=" /etc/environment && '
            f'sed -i "s#^https_proxy=.*#https_proxy={proxy_url}#" /etc/environment || '
            f'echo "https_proxy={proxy_url}" >> /etc/environment\n'
            f'grep -q "^no_proxy=" /etc/environment && '
            f'sed -i "s#^no_proxy=.*#no_proxy={no_proxy}#" /etc/environment || '
            f'echo "no_proxy={no_proxy}" >> /etc/environment\n'
            f'grep -q "^HTTP_PROXY=" /etc/environment && '
            f'sed -i "s#^HTTP_PROXY=.*#HTTP_PROXY={proxy_url}#" /etc/environment || '
            f'echo "HTTP_PROXY={proxy_url}" >> /etc/environment\n'
            f'grep -q "^HTTPS_PROXY=" /etc/environment && '
            f'sed -i "s#^HTTPS_PROXY=.*#HTTPS_PROXY={proxy_url}#" /etc/environment || '
            f'echo "HTTPS_PROXY={proxy_url}" >> /etc/environment\n'
            f'grep -q "^NO_PROXY=" /etc/environment && '
            f'sed -i "s#^NO_PROXY=.*#NO_PROXY={no_proxy}#" /etc/environment || '
            f'echo "NO_PROXY={no_proxy}" >> /etc/environment\n'
            'systemctl daemon-reload\n'
            'systemctl restart containerd\n'
            'systemctl restart kubelet\n'
            'echo PROXY_CONFIGURED\n'
        )
        pres = run_remote_script(ip, push_script, password)
        pout = _clean_stdout(pres) if pres is not None else ''
        if 'PROXY_CONFIGURED' in pout:
            msgs.append(f'proxy_config: repaired {ip} (containerd/kubelet restarted)')
            confirmed.add(ip)
        else:
            msgs.append(f'proxy_config: WARNING — repair on {ip} did not confirm')
            # Leave it out of `confirmed` so it's retried next cycle.
    _save_confirmed_proxy_nodes(confirmed)
    return 'FAIL', msgs


#==============================================================================
# CHECK: CERT RENEWAL. Delegates entirely to the existing, already-tested
# vsp_cert_renewer.py tool (kubeadm/kubelet/cert-manager-leaf/Antrea/
# containerd-CA renewal) rather than reimplementing its PKI logic here —
# that tool already owns this domain and is invoked the same way
# VCFfinal.py Task 2e calls it.
#
# RUNS ONCE PER BOOT SESSION, not every monitor cycle. Certs only need
# checking once after the manager comes up (60-day threshold; any actual
# renewal is itself immediate and logged when it happens, so there's nothing
# to "catch" by re-checking every 5 min). The state file lives in /tmp, which
# this OS clears on every reboot — so mere presence of the file already means
# "checked since last boot", no timestamp/interval math needed. The tool
# itself is also slow (measured 118s on this lab — over half the entire
# monitor cycle, since it walks 5 separate cert populations: kubeadm/
# kubelet/cert-manager/Antrea/containerd-CA-sync), so paying that cost once
# per boot instead of every cycle is also what keeps the monitor's total
# runtime safely under the cron interval.
#==============================================================================

CERT_RENEWER_SCRIPT = '/home/holuser/hol/Tools/vsp_cert_renewer.py'
CERT_RENEWAL_STATE_FILE = '/tmp/vsp-health-monitor-cert-renewal.state'


def _cert_renewal_already_checked():
    """True if checked since last boot (state file survives until /tmp is
    cleared on reboot — no timestamp/interval math needed)."""
    return os.path.isfile(CERT_RENEWAL_STATE_FILE)


def _mark_cert_renewal_checked():
    try:
        with open(CERT_RENEWAL_STATE_FILE, 'w') as f:
            f.write(str(time.time()))
    except Exception:
        pass


def check_cert_renewal(cp_ip, password, remediate, dry_run, threshold_days):
    if not os.path.isfile(CERT_RENEWER_SCRIPT):
        return 'WARN', ['cert_renewal: vsp_cert_renewer.py not found — skipping']

    if _cert_renewal_already_checked():
        checked_at = time.strftime('%Y-%m-%d %H:%M:%S',
                                   time.localtime(os.path.getmtime(CERT_RENEWAL_STATE_FILE)))
        return 'PASS', [f'cert_renewal: already checked this boot session '
                        f'(at {checked_at}) - OK']

    args = ['python3', '-u', CERT_RENEWER_SCRIPT, '--cluster', 'vsp',
           '--threshold-days', str(threshold_days), '--no-timestamps']
    if dry_run or not remediate:
        args.append('--dry-run')

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return 'WARN', ['cert_renewal: vsp_cert_renewer.py timed out after 180s']
    except Exception as e:
        return 'WARN', [f'cert_renewal: failed to invoke vsp_cert_renewer.py: {e}']

    out = (proc.stdout or '') + (proc.stderr or '')
    # vsp_cert_renewer.py's own log format tags each line with a level
    # (CHECK/ACTION/RENEWED/SKIP/WARN/ERROR/INFO — see its _log()). Section
    # headers like "Phase 1: kubeadm ... renewal" and "Renewal target: ..."
    # contain the word "renew" on EVERY run regardless of outcome, so a plain
    # substring match on "renew" false-positives every single cycle — match
    # the RENEWED/ERROR tags themselves instead.
    error_lines = [ln for ln in out.splitlines() if 'ERROR  :' in ln]
    renewed_lines = [ln for ln in out.splitlines() if 'RENEWED:' in ln]

    if error_lines:
        # Don't mark as checked — a discovery/connectivity hiccup should
        # retry on the next cycle rather than wait for the next reboot.
        return 'WARN', [f'cert_renewal: {len(error_lines)} error(s) reported — '
                        f'{error_lines[-1].strip()}']

    # Clean completion (PASS or a genuine RENEWED action) — don't re-check
    # again until the next reboot. dry_run never persists state, matching
    # every other check's convention.
    if not dry_run:
        _mark_cert_renewal_checked()

    if renewed_lines:
        return 'FAIL', [f'cert_renewal: {len(renewed_lines)} cert(s) renewed — '
                        f'{renewed_lines[-1].strip()}']
    if proc.returncode == 0:
        return 'PASS', [f'cert_renewal: all VSP certs healthy (threshold '
                        f'{threshold_days}d), no renewal needed - OK']
    tail = out.strip().splitlines()[-1] if out.strip() else '(no output)'
    return 'WARN', [f'cert_renewal: vsp_cert_renewer.py exited {proc.returncode} — {tail}']


#==============================================================================
# CHECK: PASSWORD EXPIRATION
#==============================================================================

def check_password_expiration(cp_ip, password, remediate, dry_run):
    """Ensure all nodes have password expiration > 1 year.
    If drifted (<= 365d or never), forcefully reset to 999 days.
    """
    node_ips_script = (
        '#!/bin/bash\n'
        "kubectl get nodes -o jsonpath='{range .items[*]}"
        "{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}'\n"
    )
    node_ips_res = run_remote_script(cp_ip, node_ips_script, password)
    node_ips_out = (_clean_stdout(node_ips_res) if node_ips_res is not None else '').strip()
    node_ips = [ip for ip in node_ips_out.split() if ip]
    if not node_ips:
        return 'WARN', ['password_expiration: could not list node IPs — skipping']

    check_script = (
        '#!/bin/bash\n'
        'DRIFT=0\n'
        'for user in root vmware-system-user; do\n'
        '  exp=$(chage -l $user | grep "Password expires" | cut -d: -f2- | xargs)\n'
        '  if [ "$exp" = "never" ]; then\n'
        '    DRIFT=1\n'
        '  else\n'
        '    exp_sec=$(date -d "$exp" +%s 2>/dev/null)\n'
        '    now_sec=$(date +%s 2>/dev/null)\n'
        '    if [ -n "$exp_sec" ] && [ -n "$now_sec" ]; then\n'
        '      diff_days=$(( (exp_sec - now_sec) / 86400 ))\n'
        '      if [ $diff_days -le 365 ]; then\n'
        '        DRIFT=1\n'
        '      fi\n'
        '    else\n'
        '      DRIFT=1\n'
        '    fi\n'
        '  fi\n'
        'done\n'
        'if [ $DRIFT -eq 1 ]; then\n'
        '  echo PASS_DRIFT\n'
        'else\n'
        '  echo PASS_OK\n'
        'fi\n'
    )

    import concurrent.futures
    drifted = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(node_ips)) as pool:
        futures = {pool.submit(run_remote_script, ip, check_script, password): ip
                   for ip in node_ips}
        for future in concurrent.futures.as_completed(futures):
            ip = futures[future]
            try:
                res = future.result()
            except Exception:
                res = None
            out = _clean_stdout(res) if res is not None else ''
            if 'PASS_OK' not in out:
                drifted.append(ip)

    if not drifted:
        return 'PASS', [f'password_expiration: all {len(node_ips)} node(s) expire in > 1 year - OK']

    msgs = [f'password_expiration: {len(drifted)}/{len(node_ips)} node(s) drifted '
           f'(<= 365d or never): {", ".join(drifted)}']
           
    if dry_run:
        msgs.append('password_expiration: [DRY-RUN] would forcefully remediate to 999 days')
        return 'FAIL', msgs
    if not remediate:
        return 'FAIL', msgs

    push_script = (
        '#!/bin/bash\n'
        'today=$(date +%Y-%m-%d)\n'
        'for user in root vmware-system-user; do\n'
        '  chage -d "$today" -M 999 "$user" >/dev/null 2>&1\n'
        'done\n'
        'echo REMEDIATED\n'
    )

    failed_push = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(drifted)) as pool:
        futures = {pool.submit(run_remote_script, ip, push_script, password): ip
                   for ip in drifted}
        for future in concurrent.futures.as_completed(futures):
            ip = futures[future]
            try:
                res = future.result()
            except Exception:
                res = None
            out = _clean_stdout(res) if res is not None else ''
            if 'REMEDIATED' not in out:
                failed_push.append(ip)

    if failed_push:
        msgs.append(f'password_expiration: failed to push fix to: {", ".join(failed_push)}')
        return 'FAIL', msgs
    
    msgs.append(f'password_expiration: successfully set to 999 days on {len(drifted)} node(s)')
    return 'RECOVERED', msgs


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
    run_start = time.time()
    password = lsf.get_password()
    cp_ip = cfg['vsp_control_plane_ip']
    remediate = cfg['remediate'] and not dry_run

    log('=' * 64)
    log(f'VSP Health Monitor v{SCRIPT_VERSION} — checks={",".join(cfg["checks"])} '
        f'remediate={cfg["remediate"]} dry_run={dry_run}')
    log('=' * 64)
    print_header(cfg, dry_run)

    print(f"{_DIM}  Testing connectivity to VSP control plane {cp_ip}...{_NC}",
          end='', flush=True)
    if not lsf.test_ping(cp_ip, count=2, timeout=2):
        print(f" {_FAIL}")
        # resolve_cp_host() is unconditional (not gated by cfg['checks']) — every
        # other check needs SOME reachable node just to SSH in. It tries the
        # hardcoded default VIP, then attempt_vip_restore() (itself gated by
        # cfg['remediate']; handles dry_run internally), then falls back to the
        # auto-discovered CP node IP directly rather than skipping the cycle.
        resolved, tier, tried = resolve_cp_host(
            cp_ip, cfg['vsp_worker_fqdn'], password, cfg['remediate'], dry_run)
        if not resolved:
            elapsed = time.time() - run_start
            msg = (f'VSP control plane VIP {cp_ip} not reachable — tried '
                   f'{", ".join(tried)}; this lab may not use a VSP cluster, or '
                   f'restore/discovery failed or was skipped (remediate='
                   f'{cfg["remediate"]}); nothing else to do this cycle '
                   f'(total: {elapsed:.1f}s)')
            log(msg)
            print(f"\n  {_WARN} {msg}\n")
            return 0 if cfg['remediate'] else 2
        if tier == 'restored':
            print(f"  {_OK} VIP restored — continuing with normal checks\n")
        elif tier == 'default_vip':
            print(f"  {_OK} Hardcoded default VIP {resolved} reachable — using it this cycle\n")
        else:
            print(f"  {_WARN} VIP still down — running this cycle directly against "
                  f"auto-discovered CP node {resolved}\n")
        cp_ip = resolved
    else:
        print(f" {_OK}\n")

    any_fail = False
    checks_run = 0
    failed_count = 0
    contention_downgraded = False
    for check in cfg['checks']:
        check_start = time.time()
        try:
            if check == 'host_contention':
                st, msgs, contended = check_host_contention(
                    cp_ip, password, cfg['host_contention_load_multiplier'])
                if contended and remediate:
                    msgs.append(
                        'host_contention: downgrading this cycle to detect-only for '
                        'all remaining checks (no force-deletes/patches) — remediating '
                        'now would add more API-server write load on top of an '
                        'already CPU-starved node and risks compounding the storm')
                    remediate = False
                    contention_downgraded = True
            elif check == 'vsp_size':
                st, msgs = check_vsp_size(cp_ip, password, remediate, dry_run)
            elif check == 'kvip_manifest':
                st, msgs = check_kvip_manifest(cp_ip, password, remediate, dry_run)
            elif check == 'cp_pod_crash':
                st, msgs = check_cp_pod_crash(cp_ip, password, remediate, dry_run)
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
            elif check == 'postgres':
                st, msgs = check_postgres(cp_ip, password, remediate, dry_run)
            elif check == 'salt_stack':
                st, msgs = check_salt_stack(cp_ip, password, remediate, dry_run)
            elif check == 'vodap':
                st, msgs = check_vodap(cp_ip, password, remediate, dry_run)
            elif check == 'component_health':
                st, msgs = check_component_health(cp_ip, password, remediate, dry_run)
            elif check == 'argo_cleanup':
                st, msgs = check_argo_cleanup(cp_ip, password, remediate, dry_run)
            elif check == 'proxy_config':
                st, msgs = check_proxy_config(cp_ip, password, remediate, dry_run)
            elif check == 'password_expiration':
                st, msgs = check_password_expiration(cp_ip, password, remediate, dry_run)
            elif check == 'cert_renewal':
                st, msgs = check_cert_renewal(cp_ip, password, remediate, dry_run,
                                              cfg['cert_renewal_threshold_days'])
            elif check == 'vip':
                st, msgs = check_vip(cp_ip, password)
            else:
                st, msgs = 'WARN', [f'{check}: unknown check name — skipped']
        except Exception as e:
            st, msgs = 'WARN', [f'{check}: check raised exception (non-fatal): {e}']

        check_elapsed = time.time() - check_start
        for m in msgs:
            log(f'  {m}')
        log(f'  [{check}] {st} ({check_elapsed:.1f}s)')
        print_row(st, check, msgs, check_elapsed)

        checks_run += 1
        if st == 'FAIL':
            any_fail = True
            failed_count += 1

    total_elapsed = time.time() - run_start
    print_summary(checks_run, failed_count, remediate, total_elapsed, contention_downgraded)

    if any_fail and not remediate:
        reason = ('host_contention downgraded this cycle to detect-only'
                  if contention_downgraded else
                  'remediate=false (detect-only)')
        log(f'One or more checks FAILED and {reason} — not remediating '
            f'(total: {total_elapsed:.1f}s)')
        return 2
    log(f'VSP Health Monitor pass complete (total: {total_elapsed:.1f}s)')
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
# CONCURRENCY LOCK — prevents overlapping cron runs. Measured total runtime
# (212s) came uncomfortably close to the 300s default cron interval; if a
# cycle ever runs long (e.g. during the exact host contention #62 is meant
# to detect — slower SSH round-trips are a symptom of that same contention),
# cron would fire a second overlapping instance, doubling the write load on
# an already-struggling API server. A stale lock (owning PID no longer
# alive, e.g. after a kill -9 or manager reboot) is treated as not-locked.
#==============================================================================

LOCK_FILE = '/tmp/vsp-health-monitor.lock'


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def acquire_lock():
    """Returns True if the lock was acquired (or was stale and reclaimed),
    False if another instance is genuinely still running."""
    try:
        if os.path.isfile(LOCK_FILE):
            existing_pid = int(open(LOCK_FILE).read().strip())
            if _pid_alive(existing_pid):
                return False
            log(f'Lock file held by dead PID {existing_pid} — reclaiming as stale')
    except Exception:
        pass  # unreadable/corrupt lock file — treat as stale, overwrite below
    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


#==============================================================================
# MAIN
#==============================================================================


def run_all_sites(cfg, dry_run):
    overall_exit = 0
    has_custom_ip = False
    try:
        if lsf.config.has_option('VSPMONITOR', 'vsp_control_plane_ip'):
            has_custom_ip = True
    except:
        pass
        
    if has_custom_ip:
        sites = [(cfg['vsp_control_plane_ip'], cfg['vsp_worker_fqdn'])]
    else:
        sites = [('10.1.1.142', 'vsp-01a.site-a.vcf.lab'), ('10.2.1.142', 'vsp-01b.site-b.vcf.lab')]
        
    for cp_ip, worker in sites:
        if has_custom_ip or lsf.test_ping(cp_ip, count=1, timeout=1) or lsf.test_ping(worker, count=1, timeout=1):
            cfg['vsp_control_plane_ip'] = cp_ip
            cfg['vsp_worker_fqdn'] = worker
            exit_code = run_all(cfg, dry_run)
            if exit_code > overall_exit:
                overall_exit = exit_code
                
    return overall_exit

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

    if not cfg['enabled'] and not args.once:
        log('VSP Health Monitor disabled via config.ini [VSPMONITOR] enabled=false — exiting')
        return

    if args.install_timer:
        # Installing the recurring cron job is a startup action (VCFfinal): do it
        # regardless of Ready state, and run one immediate pre-Ready cleanup pass.
        install_timer(cfg)
        if not acquire_lock():
            log('Another vsp-health-monitor.py instance is still running — '
                'skipping this pass (lock held by a live PID)')
            return
        try:
            sys.exit(run_all_sites(cfg, args.dry_run))
        finally:
            release_lock()

    # Plain --once (the cron cadence path): gate on lab Ready so we never hammer
    # powered-off / uninitialized servers before the lab has finished starting.
    if not args.ignore_ready and not is_lab_ready():
        log('Lab has not reached Ready status yet — skipping VSP health pass '
            '(will retry on the next cron cycle)')
        return

    if not acquire_lock():
        log('Another vsp-health-monitor.py instance is still running (previous '
            'cycle overran the cron interval) — skipping this pass rather than '
            'stacking a second concurrent run')
        return
    try:
        sys.exit(run_all_sites(cfg, args.dry_run))
    finally:
        release_lock()


if __name__ == '__main__':
    main()
