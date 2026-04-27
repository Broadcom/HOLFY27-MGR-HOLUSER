#!/usr/bin/env python3
# shutdown_helpers.py - HOLFY27 Shutdown shared utilities
# Version 1.0 - 2026-04-27
# Author - Burke Azbill and HOL Core Team
# Phase expansion, approximate ETA tracking, vSphere session connect/disconnect, heartbeat helper.
#
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
    '1', '1b', '2', '2b', '3b', '3', '4', '5', '6', '7',
    '8', '9', '10', '11', '12', '13',
    '14', '15', '16', '17', '17b', '17c',
    '18', '19', '19b', '19c', '20',
)

ALL_VALID_PHASES: FrozenSet[str] = frozenset(CANONICAL_PHASE_ORDER)

# Phases that need vCenter API inventory ([RESOURCES] vCenters / Phase 2 style connect)
NEED_VCENTER_CONNECT_PHASES: FrozenSet[str] = frozenset({
    '3b', '3', '4', '5', '6', '7',
    '8', '9', '10', '11', '12', '13',
    '14', '15', '16', '17',
})

# Phases that inventory VMs via direct ESXi vim (after vCenter is down in full run)
NEED_ESXI_VIM_PREREQ_PHASES: FrozenSet[str] = frozenset({'17c', '19b'})

# Rough per-phase budgets (seconds) for approximate ETA — not a SLA
DEFAULT_PHASE_BUDGET_SEC: dict[str, int] = {
    '1': 3600,
    '1b': 900,
    '2': 180,
    '2b': 1200,
    '3b': 1800,
    '3': 600,
    '4': 2400,
    '5': 900,
    '6': 900,
    '7': 1200,
    '8': 600,
    '9': 600,
    '10': 600,
    '11': 300,
    '12': 600,
    '13': 1200,
    '14': 900,
    '15': 900,
    '16': 1200,
    '17': 1200,
    '17b': 300,
    '17c': 600,
    '18': 600,
    '19': 5400,
    '19b': 900,
    '19c': 1800,
    '20': 600,
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
