#!/usr/bin/env python3
# shutdown_helpers.py - HOLFY27 Shutdown shared utilities
# Version 1.6 - 2026-05-14
# Author - Burke Azbill and HOL Core Team
# Phase expansion, approximate ETA tracking, vSphere session connect/disconnect, heartbeat helper.
#
# v 1.6 Changes (2026-05-14):
# - Recalibrated three persistently mis-budgeted phases based on 10-run sample
#   (shutdown.1 through shutdown.10 in pod-looop-results/):
#     '1b':  30 → 120  Phase 1b always shuts down auto-platform-a via vCenter
#                       regardless of whether Fleet LCM handled auto-a; actual 100-116s.
#     '17c': 120 → 30  auto-platform-a is already off by Phase 17c (powered down in
#                       Phase 1b); only license-a remains; actual 16s.
#     '20':   60 → 5   ESXi host shutdown almost never fires (shutdown_hosts=false in
#                       most HOL configs); budget reduced to match no-op path.
# - ESA total budget: ~44 min (down from 44 min — nearly identical; the 1b increase
#   and 17c/20 decreases approximately cancel out, keeping the user-visible ETA stable).
# - NOTE: ESA detection for the initial banner relies on check_vsan_esa() in
#   VCFshutdown.py v3.4 (was calling non-existent is_vsan_esa() — fixed there).
#
# v 1.5 Changes (2026-05-13):
# - Trimmed "phantom" phase budgets (phases that consistently take 0s in this lab)
#   to eliminate 21 min of budget inflation and bring ESA ETA to ~44 min:
#     '1':   720 → 660   (still ~20% headroom over actual 549s)
#     '1b':  120 → 30    (0s when Fleet API succeeds; 30s keeps fallback estimate)
#     '2':    60 → 10    (0s when already connected from Phase 1b)
#     '2b':  300 → 0     (placeholder phase; never executes meaningful work)
#     '3b':  120 → 0     (disabled with `and False` guard in v3.1)
#     '4':   120 → 90    (actual 69s; 30% headroom)
#     '5':   120 → 100   (actual 82s; ~20% headroom)
#     '10':   60 → 5     (instant "not configured" log, not deployed in lab)
#     '11':   60 → 5     (instant "not configured" log, not deployed in lab)
#     '12':   60 → 5     (instant "not configured" log, not deployed in lab)
#     '14':   60 → 5     (instant "no mgmt edges" log, none in this lab)
#     '17b':  60 → 5     (ESXi session switch; actual ~0s)
#     '20':  300 → 60    (ESXi shutdown commands only, ~35s; Phase 22 handles wait)
#   ESA dynamic override also trimmed from 60s → 5s in VCFshutdown.py.
#
# v 1.4 Changes (2026-05-12):
# - DEFAULT_PHASE_BUDGET_SEC recalibrated from shutdown.log 2026-05-12 (33:09 total,
#   no ESXi host shutdown). Phases with sequential-to-parallel upgrade (17c, 19c)
#   have budgets trimmed to reflect parallel execution time:
#     '1':   2400 → 720   (Fleet LCM direct API path: actual 549s)
#     '3':     60 → 30    (WCP stop: actual 7s)
#     '4':    480 → 120   (workload VMs w/ vna- excluded: actual 69s)
#     '5':     60 → 120   (NSX edges sequential: actual 82s)
#     '6':    180 → 150   (WLD NSX mgr: actual 101s)
#     '7':    180 → 60    (WLD vCenter: actual 36s)
#     '8':    240 → 150   (OpsNet VMs: actual 117s)
#     '9':    180 → 150   (collector: actual 116s)
#     '13':   120 → 90    (ops-a: actual 60s)
#     '15':   180 → 150   (mgmt NSX mgr: actual 101s)
#     '16':   120 → 60    (SDDC mgr: actual 30s)
#     '17':   420 → 360   (mgmt vCenter: actual 316s)
#     '17c':  120 → 120   (post-edge VMs now parallel; bottleneck ~106s)
#     '18':    60 → 15    (ESXi settings: actual 4s)
#     '19c':  240 → 150   (stragglers now parallel; bottleneck ~115s incl. reauth)
#
# v 1.3 Changes (2026-05-12):
# - DEFAULT_PHASE_BUDGET_SEC updated from actual shutdown.log (2026-05-11, 37:01 total):
#     '1b': 60 → 120   (actual 116s)
#     '4':  300 → 480  (actual 443s; still ~480 after vna- exclusion due to vm-7737)
#     '4b': 300 → 180  (actual 146s for 7 VSP VMs in parallel)
#     '8':  180 → 240  (actual 228s for 2 OpsNet VMs)
#
# v 1.2 Changes (2026-05-11):
# - Phase 19b renamed to Phase 4b; moved before NSX Edges (Phase 5)
# - '4b' added to NEED_VCENTER_CONNECT_PHASES (primary path uses vCenter)
# - '19b' removed from NEED_ESXI_VIM_PREREQ_PHASES (fallback is internal to Phase 4b)
# - DEFAULT_PHASE_BUDGET_SEC: '19b' -> '4b' with 300s budget (vCenter path is faster)
#
# v 1.1 Changes:
# - Updated phase timing based on actual lab shutdown times

# v 1.0 Changes (2026-04-27):
# - CANONICAL_PHASE_ORDER, expand_phase_plan(), validate_phase_tokens(), parse_phases_csv()
# - Auto-insert Phase 2 (vCenter) and Phase 17b (ESXi vim) prerequisites for selective runs
# - ensure_sessions_for_phase / disconnect_vsphere_sessions for Shutdown.py and VCFshutdown.py
# - ShutdownEtaTracker (DEFAULT_PHASE_BUDGET_SEC) and heartbeat_still_waiting() (90s)

"""
Shared helpers for Shutdown.py and VCFshutdown.py:
phase expansion, ETA tracking, and vSphere session teardown.
"""

from __future__ import annotations

import time
from typing import Callable, FrozenSet, List, Optional, Sequence, Set

# Canonical VCF shutdown order (must match VCFshutdown.py phase blocks)
CANONICAL_PHASE_ORDER: Sequence[str] = (
    '1', '1b', '2', '2b', '3b', '3', '4', '4b', '5', '6', '7',
    '8', '9', '10', '11', '12', '13',
    '14', '15', '16', '17', '17b', '17c',
    '18', '19', '19c', '20',
)

ALL_VALID_PHASES: FrozenSet[str] = frozenset(CANONICAL_PHASE_ORDER)

# Phases that need vCenter API inventory ([RESOURCES] vCenters / Phase 2 style connect)
NEED_VCENTER_CONNECT_PHASES: FrozenSet[str] = frozenset({
    '3b', '3', '4', '4b', '5', '6', '7',
    '8', '9', '10', '11', '12', '13',
    '14', '15', '16', '17',
})

# Phases that inventory VMs via direct ESXi vim (after vCenter is down in full run)
# Phase 4b manages its own ESXi fallback internally and restores vCenter sessions,
# so it does NOT need '17b' inserted as an external prerequisite.
NEED_ESXI_VIM_PREREQ_PHASES: FrozenSet[str] = frozenset({'17c'})

# Rough per-phase budgets (seconds) for approximate ETA — not a SLA
# Last calibrated against 10-run sample (shutdown.1–10, pod-looop-results/) 2026-05-14.
# ESA runtime (with dynamic Phase 19 override = 5s): ~44 min.
# OSA runtime (Phase 19 = 2700s vSAN elevator): ~89 min.
DEFAULT_PHASE_BUDGET_SEC: dict[str, int] = {
    '1':    660,  # Fleet LCM API direct path; actual 423-779s across runs
    '1b':   120,  # VCF Automation VM (auto-platform-a) via vCenter; actual 100-116s
    '2':     10,  # vCenter connect; 0s when already connected from Phase 1b
    '2b':     0,  # VSP Component Service annotation (placeholder — never executes)
    '3b':     0,  # Supervisor workload shutdown (disabled in v3.1 — WCP manages this)
    '3':     30,  # WCP service stop; actual 7s
    '4':     90,  # Workload VM shutdown + dynamic discovery; actual 62-392s (vm-7737 variable)
    '4b':   180,  # VSP Platform VMs (7 VMs parallel via vCenter); actual 101-131s
    '5':    100,  # WLD NSX Edges (sequential); actual 82s
    '6':    150,  # WLD NSX Manager; actual 90-101s
    '7':     60,  # WLD vCenter; actual 25-36s
    '8':    150,  # VCF Operations for Networks (OpsNet VMs); actual 112-117s
    '9':    150,  # VCF Operations Collector; actual 85-116s
    '10':     5,  # VCF Operations for Logs; instant "not configured" (not deployed)
    '11':     5,  # VCF Identity Broker; instant "not configured" (not deployed)
    '12':     5,  # VCF Operations Fleet Management; instant "not configured" (not deployed)
    '13':    90,  # VCF Operations (ops-a vrops); actual 60s
    '14':     5,  # Mgmt NSX Edges; instant "no mgmt edges found" (none in this lab)
    '15':   150,  # Mgmt NSX Manager; actual 90-101s
    '16':    60,  # SDDC Manager; actual 50s
    '17':   360,  # Mgmt vCenter (longest single-VM shutdown); actual 314-317s
    '17b':    5,  # Switch to direct ESXi sessions; actual ~0s
    '17c':   30,  # Post-edge VMs (parallel); auto-platform-a already off; only license-a ~16s
    '18':    15,  # ESXi host advanced settings; actual 4s
    '19':  2700,  # vSAN OSA elevator (dynamically overridden to 5s for ESA)
    '19c':  150,  # Pre-ESXi audit / stragglers (parallel); bottleneck ~101-115s (SupervisorCPVM)
    '20':     5,  # ESXi host shutdown commands; almost always no-op (shutdown_hosts=false)
}

HEARTBEAT_SEC = 90


def parse_phases_csv(s: str) -> List[str]:
    """Split comma/semicolon-separated phase list into stripped tokens."""
    if not s or not str(s).strip():
        return []
    out: List[str] = []
    for chunk in str(s).replace(';', ',').split(','):
        t = chunk.strip().lower()
        if t:
            out.append(t)
    return out


def validate_phase_tokens(tokens: Sequence[str]) -> None:
    bad = [t for t in tokens if t not in ALL_VALID_PHASES]
    if bad:
        raise ValueError(
            f'Invalid phase(s): {", ".join(bad)}. Valid: {", ".join(sorted(ALL_VALID_PHASES))}'
        )


def expand_phase_plan(requested: Sequence[str]) -> List[str]:
    """
    Auto-insert prerequisite phases, then return phases in canonical order.

    - vCenter inventory phases insert '2' if missing.
    - Phases needing ESXi vim inventory insert '17b' if missing.
    """
    validate_phase_tokens(requested)
    expanded: Set[str] = set(requested)
    if expanded & NEED_VCENTER_CONNECT_PHASES and '2' not in expanded:
        expanded.add('2')
    if expanded & NEED_ESXI_VIM_PREREQ_PHASES and '17b' not in expanded:
        expanded.add('17b')
    return [p for p in CANONICAL_PHASE_ORDER if p in expanded]


def disconnect_vsphere_sessions(lsf) -> None:
    """Disconnect all pyVmomi sessions held in lsf.sis / lsf.sisvc."""
    try:
        from pyVim import connect
    except ImportError:
        return
    for si in list(getattr(lsf, 'sis', []) or []):
        try:
            connect.Disconnect(si)
        except Exception:
            pass
    try:
        lsf.sis.clear()
        lsf.sisvc.clear()
    except Exception:
        pass


def ensure_vcenter_sessions(
    lsf,
    dry_run: bool,
    mgmt_hosts: Sequence[str],
    write: Callable[[str], None],
) -> None:
    """
    Connect to vCenter(s) from [RESOURCES] vCenters or mgmt_hosts fallback,
    mirroring VCFshutdown Phase 2 logic when no sessions exist.
    """
    if dry_run:
        write('Would ensure vCenter sessions (dry-run)')
        return
    if getattr(lsf, 'sis', None) and len(lsf.sis) > 0:
        write(f'vCenter sessions already active ({len(lsf.sis)} endpoint(s))')
        return
    vcenters: List[str] = []
    if lsf.config.has_option('RESOURCES', 'vCenters'):
        raw = lsf.config.get('RESOURCES', 'vCenters')
        vcenters = [v.strip() for v in raw.split('\n')
                    if v.strip() and not v.strip().startswith('#')]
    if vcenters:
        write(f'Auto-connecting to {len(vcenters)} vCenter(s) for this phase...')
        for vc in vcenters:
            write(f'  - {vc}')
        lsf.connect_vcenters(vcenters)
        write(f'Connected to {len(lsf.sis)} vSphere endpoint(s)')
        return
    if mgmt_hosts:
        write('No vCenters in [RESOURCES] vCenters; connecting to management ESXi entries')
        for host in mgmt_hosts:
            write(f'  - {host}')
        lsf.connect_vcenters(list(mgmt_hosts))
        write(f'Connected to {len(lsf.sis)} endpoint(s)')
        return
    write('WARNING: No vCenters or mgmt hosts configured — VM phases may find nothing')


def ensure_esxi_vim_sessions(
    lsf,
    dry_run: bool,
    mgmt_hosts: Sequence[str],
    write: Callable[[str], None],
) -> None:
    """
    Disconnect any vCenter sessions and connect directly to ESXi hosts
    (same as Phase 17b) when inventory must be done against hosts.
    """
    if dry_run:
        write('Would switch to direct ESXi vim sessions (dry-run)')
        return
    write('Ensuring direct ESXi host connections for inventory...')
    disconnect_vsphere_sessions(lsf)
    if not mgmt_hosts:
        write('WARNING: No [VCF] vcfmgmtcluster hosts — cannot connect to ESXi')
        return
    for host in mgmt_hosts:
        write(f'  - {host}')
    lsf.connect_vcenters(list(mgmt_hosts))
    write(f'Connected to {len(lsf.sis)} ESXi endpoint(s)')


def ensure_sessions_for_phase(
    phase_id: str,
    lsf,
    dry_run: bool,
    mgmt_hosts: Sequence[str],
    write: Callable[[str], None],
) -> None:
    """Phase-documented vSphere / ESXi prerequisites."""
    pid = phase_id.lower()
    if pid == '2':
        ensure_vcenter_sessions(lsf, dry_run, mgmt_hosts, write)
        return
    if pid == '17b':
        ensure_esxi_vim_sessions(lsf, dry_run, mgmt_hosts, write)
        return
    if pid in NEED_ESXI_VIM_PREREQ_PHASES:
        ensure_esxi_vim_sessions(lsf, dry_run, mgmt_hosts, write)
        return
    if pid in NEED_VCENTER_CONNECT_PHASES:
        ensure_vcenter_sessions(lsf, dry_run, mgmt_hosts, write)


class ShutdownEtaTracker:
    """Approximate remaining time based on configured phase budgets."""

    def __init__(
        self,
        ordered_phases: Sequence[str],
        write: Callable[[str], None],
        budget_map: Optional[dict[str, int]] = None,
    ):
        self._order = list(ordered_phases)
        self._write = write
        self._budget = dict(budget_map or DEFAULT_PHASE_BUDGET_SEC)
        self._run_start = time.monotonic()
        self._phase_start: Optional[float] = None
        self._current: Optional[str] = None

    def total_budget_sec(self) -> int:
        return sum(self._budget.get(p, 600) for p in self._order)

    def log_run_start(self) -> None:
        total = self.total_budget_sec()
        self._write(
            f'ETA (approx): total budget ~{total // 60} min for '
            f'{len(self._order)} phase(s) — actual time varies widely'
        )

    def phase_begin(self, phase_id: str) -> None:
        self._current = phase_id.lower()
        self._phase_start = time.monotonic()
        try:
            idx = self._order.index(self._current)
        except ValueError:
            idx = 0
        tail = self._order[idx:]
        rem_budget = sum(self._budget.get(p, 600) for p in tail)
        elapsed_run = int(time.monotonic() - self._run_start)
        self._write(
            f'>>> Phase {self._current} starting | '
            f'approx remaining budget ~{rem_budget // 60} min | '
            f'elapsed since shutdown start {elapsed_run}s'
        )

    def phase_end(self, phase_id: str) -> None:
        pid = phase_id.lower()
        if self._phase_start is not None:
            dt = int(time.monotonic() - self._phase_start)
            self._write(f'<<< Phase {pid} finished in {dt}s')
        try:
            idx = self._order.index(pid)
            tail = self._order[idx + 1:]
        except ValueError:
            tail = []
        if tail:
            rem = sum(self._budget.get(p, 600) for p in tail)
            self._write(f'ETA (approx): ~{rem // 60} min remaining across {len(tail)} phase(s)')
        else:
            self._write('ETA (approx): no further VCF phases in this plan')


def heartbeat_still_waiting(
    write: Callable[[str], None],
    label: str,
    last_emit: float,
    now: Optional[float] = None,
) -> float:
    """
    If >= HEARTBEAT_SEC since last_emit, log a STILL_RUNNING line and return now.
    Otherwise return last_emit unchanged.
    """
    t = now if now is not None else time.monotonic()
    if t - last_emit >= HEARTBEAT_SEC:
        write(f'STILL_RUNNING: {label} (heartbeat every {HEARTBEAT_SEC}s)')
        return t
    return last_emit
