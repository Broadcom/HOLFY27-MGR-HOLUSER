#!/usr/bin/env python3
"""
vcf-lab-tuner.py
Version 1.7.1 - 2026-08-21
Author: HOL Core Team

v1.7.1: VCFA Envoy Gateway OOM & Drift Keeper Hardening:
  - KEEPER_BODY: Set KUBECONFIG (/etc/kubernetes/admin.conf / node-agent.conf) so keeper kubectl commands succeed under systemd.
  - KEEPER_BODY: Fixed ReleaseTemplate/HelmRelease/Deployment checks so empty/drifted limits trigger 4Gi patch instead of skipping.
  - KEEPER_BODY: Fixed vsphere-cpi leader-election args patch to preserve base args (--cloud-provider, --cloud-config) preventing CPI panic.
  - CLUSTER_CONFIGS: Added 'footprint' section to 'vcfa' sections so footprint.envoy_gateway is audited and remediated during standard tuner passes.
  - chk_footprint: Enhanced footprint.envoy_gateway to patch HelmRelease and deployment resources directly for immediate stabilization.

v1.7.0: 100% Functionality parity with vsp-stabilizer.sh (Sections A, B, and C):
  - Section A: VSP probe timeout and memory patches for 9 services (depot-service, fleetbuild, envoy-gateway, vidb-service, sddcbuild, sddcupgrade, prometheus, kube-state-metrics, node-exporter) added to chk_vcf and embedded in vcf-lab-keeper.
  - Section B: Added etcd static manifest auto-compaction (--auto-compaction-mode=periodic, --auto-compaction-retention=1h) to chk_cp / _etcd_compaction_check.
  - Section C: Direct inspection, strategic patch, and pod restart for vsphere-cpi DaemonSet leader election lease args (60s/40s/6s) in chk_vcf / _check_vsphere_cpi_tuning.
  - Section C: Direct inspection and JSON patch for kyverno-cleanup-validating-webhook-cfg failurePolicy: Ignore in chk_kyverno.
  - Revert: Full support for --revert across static manifests, vsphere-cpi DaemonSet, and kyverno webhooks.

v1.5.0: OpenSSH ControlMaster connection sharing, remote batching, ASCII 16-color standardization, and Broadcom KB traceability.
  - SshMuxManager: OpenSSH ControlMaster multiplexing (/tmp/.vlt-ssh-<pid>) with 0700 permissions and atexit/signal socket teardown (~70-80% runtime reduction).
  - Remote Batching: Single-shot remote queries and batch patch payloads for WCP services (vmon-cli), deployments, SDS NACK, gateway 503, and storm mitigation.
  - Poll Interval Optimization: Reduced default poll intervals in sizing and convergence loops from 20s/15s to 5s.
  - ASCII 16-color Standardization: Standardized on 16-color ANSI codes (\033[0;34m) and clean box rendering across serial consoles and tmux/screen.
  - Broadcom KB Traceability: Integrated official Broadcom KB article references (KB 380701, KB 326110, KB 327477, KB 322724, KB 426075, KB 440167, KB 392417, KB 372624, KB 417831, KB 435491, KB 439264, KB 424402, KB 326114, KB 326113, KB 314495, KB 343810, KB 313904, KB 368062) across banners, descriptors, docstrings, check row details, and help output.

v1.4.2: Fixed Site-B certificate renewal delegation.
  - Passes `--site` argument down to `vsp_cert_renewer.py` so it targets the correct cluster.
  - Fixed `_delegate_cert_renewal` to return a warning (not failure) if no certs needed renewal.

v1.4.1: Added dynamic Site-B support.
  - Added `--site` argument (defaults to 'a') and `resolve_site_config()` helper.
  - Refactored `CLUSTER_CONFIGS` to generate dynamic FQDNs, VIPs, and subnets 
    (10.1.1.x vs 10.2.1.x) based on the target site.
  - Updated `_discover_cp` to correctly sweep the dynamic subnet prefix.

v1.4.0: Achieved 100% functional parity with vcfa-stabilizer.sh for --cluster vcfa,
enabling legacy vcfa-stabilizer.sh retirement:
  - edge.rm: Self-dial gRPC deadlock auto-remediation (sets publishNotReadyAddresses=true
    on Service resource-manager-grpc and restarts resource-manager-server pod).
  - edge.rabbitmq: StatefulSet copy-config init container restoration via targeted
    JSON patch and .erlang.cookie permission repair (fix-cookie init container).
  - certs.service_tls: Service-TLS certificate freshness correlation across all 24
    prelude deployments with automatic rollout restart for stale pods.
  - SDS SAN-without-CA NACK fix (_fix_sds_sni / --fix-sds-sni): Copies platform-trust
    ConfigMap across BackendTLSPolicy namespaces and applies Kyverno ClusterPolicy
    vcfa-btp-wellknown-to-carefs.
  - CPU Tuning & Rollback (--cpu-tune / --rollback-cpu-tune): Prometheus scrape/retention
    tuning, FluentBit flush interval, Kyverno admission replica scale, and provisioning-service
    exemplars disable.
  - Gateway 503 Recovery (--recover-gateway-503): Executes SDS NACK fix and rollout-restarts
    gateway-adjacent deployments across vmsp-platform and prelude.
  - Pod sweep enhancement (chk_pods): Automatically sweeps terminal one-shot Job/Workflow
    error pods on VCFA without waiting for restart count accumulation.
  - VIP Watchdog unit (chk_cp): Auto-enables and starts vcfa-vip-watchdog.service if inactive.

v1.3.1: Resolved PackageDeployment worker `size` vs `machineType` Go template
override bug in the `sizing` section. Patching `machineType` now automatically
patches `size` in lockstep (e.g. `management.medium` -> `size: "medium"`),
preventing `ReleaseTemplate/vmsp-global-config`'s `if .Values.cluster.worker.size`
macro from ignoring the requested `machineType`. Enhanced worker replica scaling
to directly patch `MachineDeployment.spec.replicas` during convergence while
`cluster-autoscaler` is temporarily enabled, and strengthened rollout verification
(`_md_rolled_out`) to validate that `VSphereMachineTemplate` CPU capacity matches
target machineType specs.

v1.3.0: Responded to Reports/remediate-lab-parity-report.md, an independently
generated audit claiming 68% functional parity with remediate-lab.sh. The
report itself had inaccuracies (wrong flag names --autoscaler-pin/
--no-autoscaler-pin vs the real --pin-autoscaler/--unpin-autoscaler; claimed
"Ported" for --kyverno-resync-relax and --envoy-gateway-fix on VSP when
neither actually existed there - chk_kyverno never touched resyncPeriod and
there was no VSP envoy-gateway ReleaseTemplate patch, only the drift-keeper's
partial memory-only assertion), so every claim was re-verified against the
real code before acting on it, not trusted at face value.

Six new, genuinely safe (no VM power-cycle, no node deletion, kubelet's own
file watcher recreates the pod on any manifest edit) gaps closed:

  cp        (vsp AND vcfa, per remediate-lab.sh's own "Family B runs per-node
            on both nodes"): KCM/scheduler --leader-elect-lease-duration/
            -renew-deadline/-retry-period static-manifest tuning (delete-then-
            insert right after --leader-elect=true, refusing to guess an
            insertion point if that line is absent - remediate-lab.sh:1480);
            etcd CPU request enforcement, per-cluster target (vsp 2500m, vcfa
            1000m), refusing to guess if resources.requests.cpu is entirely
            absent (remediate-lab.sh:1603); kube-vip's own numeric lease-
            ordering VALIDITY guard (remediate-lab.sh:400), now shared via
            _kubevip_lease_guard() between chk_cp (new, both clusters) and
            chk_storm (existing, vcfa) so the two copies cannot drift apart -
            found and fixed a real duplicate-constant risk in the same pass:
            LEASE_TRIPLE was independently defined twice with identical
            values near the keeper and would have silently diverged on the
            next edit to either copy.
  kyverno   (vsp) backgroundController.resyncPeriod -> 1h via ReleaseTemplate
            (remediate-lab.sh:1988) - a DIFFERENT field from chk_storm's
            existing cleanupController.resyncPeriod fix for vcfa; both real,
            from two different remediate-lab.sh call sites, now both covered.
  footprint (vsp) full envoy-gateway-fix: ReleaseTemplate memory.limit +
            leaderElection.disable=true (remediate-lab.sh:2105), refusing to
            disable leader election on a genuinely multi-replica deployment
            exactly like the source. Uses the SAME EG_MEM_LIMIT/EG_MEM_REQUEST
            constants (4Gi/512Mi) the drift-keeper already asserts rather than
            remediate-lab.sh's own differing 8Gi/1536Mi - the two disagreeing
            is exactly what F2 already documented as a churn-causing bug
            class; picked the value already verified LIVE on this lab.
  entropy   NEW section, NEW top-level capability: the AMD Zen4/5 esxcli
            entropySources RDRAND workaround (remediate-lab.sh:3022), driven
            by govc on the manager via a new Runner.read_local() (a read-only
            counterpart to local() - local() unconditionally raises in every
            read-only mode, which made it unusable for a pure status probe).
            Config-only and NEVER reboots a host, so this is one of the very
            few "remediate" actions with none of the risk that keeps CP/
            worker VM power-cycling and node consolidation unported below.
  --revert / --kubelet-reload / --purge-legacy-keepers
            --revert restores the newest backup this tool itself wrote for
            the four manifests above (remediate-lab.sh's revert_leader_elect/
            revert_etcd). --kubelet-reload is the opt-in, disruptive escape
            hatch for the rare case where kubelet's file watcher is stuck
            (remediate-lab.sh explicitly excludes this from its own default
            run; so does this tool - 5s abort window, like --storm-logging).
            --purge-legacy-keepers extends --remove-keeper to actually remove
            the units in legacy_keeper_units, which previously were used only
            to detect-and-refuse, never to clean up.

Still deliberately NOT ported, restated because the parity report asked for
"resolution" and the answer here is a considered no, not a miss: govc VM
hardware CP/worker resize+power-cycle, node cordon/drain/delete
(--consolidate), Cluster.spec.paused toggling, and --kube-vip-cluster-patch
(CP VM replace). All four depend on remediate-lab.sh's node_preflight/
wait_cp_ready safety logic - written specifically to not repeat an incident
where polling only the kube-vip VIP during a CP reboot left a cluster
permanently PAUSED - which has not been verified faithfully enough here to
trust unattended. Automating VM power-cycling and node deletion without that
logic risks reintroducing the exact incident it exists to prevent. This
project's own sizing section already provides the GitOps-correct equivalent
for machine-type changes; the raw hypervisor path remains remediate-lab.sh's
job until this receives dedicated, live-validated review.

All 58 offline stub-transport assertions pass (16 new, covering the lease/
etcd/kube-vip-guard refuse-to-guess paths and the entropy no-govc fallback).

v1.2.0: Two more legacy scripts closed out, plus one bug found while auditing them.

  vsp-scale-down.py reaches full parity. It had never actually been ported --
  a grep for "sizing" across this file found zero hits, despite three tables
  in vcf-lab-tuner.md describing it as done. New `sizing` section: CP/worker
  machineType resize, worker replica-bound scaling (two-phase Flux-propagation
  + autoscaler-drain poll, with the documented cluster-autoscaler stuck-loop
  auto-fix), autoscaler enable/disable/auto, node-utilization before/after.
  Every write still targets PackageDeployment/vmsp-platform.spec.values, the
  same ownership-chain doctrine F1 established elsewhere. Unlike every other
  section this one takes CLI target values instead of detecting drift, since
  "the right size" is an operator decision -- with no target given it is pure
  reporting, which the source script itself could never do (it required a
  target just to look).

  remediate-lab.sh re-audited in full (3450 lines, end to end) after a request
  to verify every one of Ben Sier's capabilities has a path here. Two new
  sections closed genuine, safe gaps:

    footprint (vsp)  right-size 9 oversized vodap/ops-logs requests, reduce 8
                     HA controllers + coredns to 1, safe-to-evict annotations,
                     CAPI/CAPV leader-election off (replicas==1 gated), and a
                     DURABLE autoscaler pin via the cluster-autoscaler
                     ReleaseTemplate's replicaCount -- deliberately a
                     DIFFERENT lever from sizing's --autoscaler (which pauses
                     the HelmRelease temporarily to let a bounds change
                     converge, then restores it). Two compatible knobs, not
                     two implementations of one.
    storm (vcfa)     full port of the embedded vcfa-storm-mitigation.sh
                     companion's "apply": CAPI/CAPV+coredns footprint, kyverno
                     resync relax, raise-only prelude probe-tolerance relax
                     (skips operator-owned Deployments), kube-vip lease-
                     validity repair, service-kube-vip VIP-preserve+lease via
                     RT, EnvoyProxy CR probe/tmp-mount hardening (the 5-6 min
                     ":443 Unable to connect" fix), UI-tier BestEffort escape.
                     The two disruptive opt-in levers (disable-le, logging --
                     the latter restarts the tenant-manager cell) are gated
                     behind --storm-disable-le/--storm-logging, exactly as
                     opt-in as the source script frames them.

  Deliberately NOT ported: remediate-lab.sh's VSP-fleet CAPI/VM-lifecycle
  actions (--cp-resize/--worker-resize via raw govc VM hardware changes that
  bypass CAPI, --consolidate, --pause/--unpause, --kube-vip-cluster-patch,
  --entropy-fix). These depend on the script's node_preflight/wait_cp_ready
  safety logic, written specifically to not repeat an incident where polling
  only the kube-vip VIP during a CP reboot left a cluster permanently PAUSED.
  That logic was not carried forward faithfully enough to trust unattended --
  blind-porting VM power-cycling without it risks reintroducing the exact
  incident the source script exists to prevent. vcf-lab-tuner.md documents
  this as a deliberate gap, not a silent one.

  Bug found while auditing vsp_cert_renewer.py's own coverage: its delegation
  call (_delegate_cert_renewal, invoked only from chk_kubeadm) was never
  reachable for the vcfa cluster, because "kubeadm" had never been added to
  CLUSTERS["vcfa"]["sections"] -- despite vsp_cert_renewer.py itself always
  having supported --cluster vcfa. A VCFA kubeadm cert nearing expiry had zero
  renewal path. Fixed by adding the section; chk_kubeadm and
  _delegate_cert_renewal were already fully cluster-agnostic.

  show_help()'s own PORTING STATUS block still read "not yet ported: vcfa,
  supervisor, and all mutating modes" -- true in v0.x, silently wrong since
  v1.0.0 shipped full remediation everywhere. Replaced with a CLUSTER COVERAGE
  block generated from the live CLUSTERS/SECTION_ACT_MODES registries so it
  cannot drift the same way again.

  All 42 offline stub-transport assertions still pass with zero changes
  required.

v1.1.0: COVERAGE AUDIT. v1.0.0 claimed "complete" on the strength of parity
across the sections it had chosen to port -- which was circular, because that set
was much smaller than what the legacy readers cover. An audit against the
authoritative SECTION_MAPs found SIX of vsp-health.py's 14 sections and FOUR of
auto-health.py's 11 had never been ported at all:

  added for vsp  : vcf (managed components + workload replicas), redis (incl. the
                   redis-service endpoint check, which detects the cert-timing
                   race and which the monitor lacks entirely), salt (gated, not
                   the unconditional restart salt-stabilize.py does), argo (stale
                   system-shutdown workflows), kyverno (UpdateRequest backlog +
                   controllers), password (expiry, repairing BOTH -M and the
                   last-change date)
  added for vcfa : gateway (LB VIPs + envoy dataplane), edge (support-bundle
                   runaway, resource-manager self-dial deadlock, RabbitMQ
                   copy-config), etcd (fragmentation, threshold-gated defrag)

Also added the Node Capacity vs Resource Requests Allocation table from
vsp-health.py:467 / auto-health.py:448, rendered identically (+---+ grid, _DIM,
same columns, same "N/A (Untolerated Taint)" handling) and shown in every mode
because it is the fastest way to see why pods are Pending. _parse_cpu now returns
MILLICORES like the legacy helper: returning cores rendered "~3.795m CPU" where
legacy shows "~3795m CPU".

report mode now emits a row PER certificate instead of one collapsed row, which
is most of why a report legitimately shows far more checks than a preflight -
per-item detail, not extra coverage. preflight keeps the aggregate so the verdict
stays readable.

Counts after this: vsp report 159 checks (vsp-health.py: 148), vcfa report 107
(auto-health.py: 41).

Two severity corrections found by verifying rather than trusting: the password
check matched "Number of days of warning before password expires" as well as the
real line and read every node as unparseable; and the hashed-envoy-dataplane
check was a hard FAIL when vcfa-stabilizer.sh:1070 treats it as a warning unless
strict mode is set -- on this build nothing matches envoy-vmsp-platform* while
both LB VIPs are held and /automation returns 200, so failing on it was a false
alarm about a working gateway.

The ctx["nodes"] prefetch requirement is now declared in SECTIONS_NEEDING_NODES
rather than hardcoded in run_cluster. Getting it wrong is silent, and it had
already bitten pods, proxy and password.

v1.0.0: Feature-complete. Ported the SUPERVISOR cluster - the last outstanding
piece - which needed a two-hop transport: the Supervisor control plane is not
routable from the manager, and its own vCenter is the only thing that knows the
CP's address and password (decryptK8Pwd.py). Added VCenterTransport (manager ->
vCenter) and VCenterHopTransport (manager -> vCenter -> SCP), with the second-hop
password written to a 0600 file ON the vCenter rather than interpolated into the
command, so it never reaches the vCenter's process table -
supervisor_stabilizer.py:1673 does this correctly for the inner hop but :1239
does not for the outer; this uses the safe form for both. Temp filenames carry
pid+ms because fixed paths (/tmp/.scppwd_hop) make concurrent runs clobber each
other.

New supervisor sections: services (vCenter vapi-endpoint / trustmanagement / wcp
autostart - vapi-endpoint being down is what makes the CSI controller fail to log
in to vCenter, which then stalls volume attachment) and webhooks (storage-quota
caBundle vs its CA secret; when they diverge every PVC and pod create fails
admission with 'x509: certificate signed by unknown authority'). nodes, pods and
certs come free from the cluster-agnostic handlers. A lab with no Supervisor is
reported as normal, not as an error.

v0.4.1: Two entry-point robustness fixes, both prompted by live behaviour, plus
SECTION_ACT_MODES to make the tune-vs-remediate split explicit instead of
scattered across handlers (a stub-transport test caught that the split was real
but undocumented, and therefore free to drift).
  - Retry each candidate once before writing off a cluster. A VCFA probe failed
    on all four candidates while the node was pingable, port 22 open and
    /automation serving HTTP 200 - it was simply busy for a moment.
  - STOP probing on sshpass rc 5 (auth failure) instead of walking the rest of
    the candidate list. Repeated password attempts trip pam_faillock and these
    appliances share a PAM database; vsp_cert_renewer.py:2176 skips a node on
    rc 5 for exactly this reason. Racing four IPs with a stale password is how
    you lock yourself out of the cluster you came to inspect.

v0.4.0: Remediation complete for every ported section.

  cp        Shadow sweep FIRST (remediate-lab.sh:1261 - seven stale *.bak.* files
            in staticPodPath made every static-pod edit inert for 2.5 months and
            a kubelet restart does not clear it, so editing a manifest without
            sweeping first reports success and changes nothing). Then: VIP re-add
            + gratuitous ARP as an explicit backstop, vip_preserve manifest fix,
            crashed-container removal for kube-controller-manager and
            kube-scheduler ONLY (etcd and the apiserver are too load-bearing to
            bounce blind), and plndr-cp-lock lease reset.
  proxy     Writes the canonical per-node config from lsfunctions and restarts
            ONLY the service whose drop-in checksum changed - systemd
            Environment= takes effect on restart, so a bare daemon-reload leaves
            it inert until reboot (same defect fixed in confighol-9.1.py 2.30).
  certs     DELEGATES to vsp_cert_renewer.py via Runner.local() rather than
            reimplementing it: its CA-rotation guards were won the hard way and a
            second implementation would be strictly worse.
  kubeadm   Same delegation, driven by --threshold-days.
  deploys   rollout restart when available < desired; for replicas==0 it restores
            the RECORDED vcf.lab/original-replicas and otherwise refuses to
            guess. supervisor_stabilizer.py:1937's `scale --all --replicas=1`
            silently down-scales anything intentionally running more.
  endpoint  Deliberately DETECT-ONLY: a non-200 is a symptom whose causes each
            have their own section with their own guards.

Runner gained a tier gate: --mode tune applies persistent config but SKIPS
transient actions, so the confighol template-prep path can never quietly restart
a control plane. Runner.local() routes manager-side helpers through the same
mode/dry-run gating as remote writes.

v0.3.0: First remediating release. --mode remediate now acts on three sections,
each honouring --dry-run through Runner.write():

  postgres  NEW section. Sweeps every Patroni/spilo namespace for the pgdata
            permission fault and corrects it. This exists because the legacy fix
            (salt-stabilize.py:267, vsp-health-monitor.py:1449a) HARDCODES
            salt-raas/pgdatabase-0: on 2026-08-14 this tool found
            vidb-external/vidb-postgres-instance-0 sitting at 2/3 for 43 days
            with the identical fault (postgres container ready:false, pgdata at
            2770) and nothing watching it, plus vcf-sddc-lcm-db-1 at 2770 while
            still 3/3 - a LATENT failure, because postgres only validates the
            permission at startup. Corrects permissions everywhere; restarts only
            genuinely not-ready pods, since bouncing a serving database over a
            latent problem is the more disruptive choice.
  pods      Damped crashloop sweep: restartCount >= 5, worst-first, capped at 15
            per pass, skipping static pods (kubelet owns those) and the gateway /
            CSI pods that have ordered handling. --aggressive opts into the
            unthresholded legacy behaviour. A capped pass SAYS it was capped
            rather than reading as "everything handled".
  nodes     Uncordon, but never a node tainted ToBeDeletedByClusterAutoscaler -
            that node is being drained on purpose.

v0.2.0: Closed three parity gaps found by diffing against the legacy readers.
(1) Pod CP-vs-Worker breakdown (vsp-health.py v2.9.0 had it, v0.1.0 did not).
The NODE column must NOT be indexed positionally: RESTARTS renders as
"3 (2d ago)", three whitespace fields, which shifted every later column and made
the split read 1 CP instead of 10. Matched against known node names instead.
(2) NotReady detection: a pod can be STATUS=Running and still broken ("1/2"
means a container fails readiness). auto-health.py catches this; a STATUS-only
check does not. Adding it immediately surfaced a real, previously-invisible
finding on VSP -- vidb-external/vidb-postgres-instance-0 running 2/3 for 43 days
with the postgres container itself ready:false -- which vsp-health.py reports as
a healthy namespace.
(3) The split is now suppressed unless node roles are actually known, because
printing "0 CP / N Worker" when the node list was never fetched looks like a
finding rather than missing data. Ported the VCFA cluster read-only: cp (now covering all
three owned VIPs, the plndr-cp-lock lease floor, and the vip-watchdog unit),
nodes, pods, deployments (core/auth inventory from auto-health.py:104-141),
certs, and endpoint (/automation probed via its gateway VIP from the node, since
curling the node's own IP returns connection-refused and reads as a false
outage). Implemented --install-keeper / --remove-keeper, which EMIT a small
dependency-free on-node keeper rather than running this tool every 60s, and
which REFUSE to install while a legacy colliding keeper unit is present - the
fix for report finding F2, where remediate-lab.sh and vsp-stabilizer.sh install
the same unit names with different payloads and fight every minute.

v0.1.0: Foundation release. Implements the architecture specified in
Tools/vcf-lab-tuner.md: the --cluster x --mode surface, the CLUSTERS registry,
transport adapters, Runner-enforced dry-run, CheckResult, single-source policy
constants, the vsp-health.py style contract, and the legacy CHECK:/SKIP: render
contract that Tools/vpodchecker.py screen-scrapes.

Section coverage in this release is deliberately READ-ONLY and VSP-only, so the
tool can be validated for parity against Tools/vsp-health/vsp-health.py before
any mutating path is trusted. Modes tune/remediate and clusters vcfa/supervisor
parse and dispatch correctly but report their sections as not-yet-ported rather
than pretending to act. See "PORTING STATUS" below.

One parameterized tool intended to replace the pre-flight / tuning / remediation
/ reporting logic currently spread across 15 scripts and three Kubernetes
clusters. Callable from Tools/confighol-9.1.py at template prep, from
Startup/VCFfinal.py or VVFfinal.py at boot, and by hand for any single section.

Design and rationale:  Tools/vcf-lab-tuner.md
Evidence base:         Tools/vsp-analysis-report-opus.md  (findings F1-F13)

Sections reported (VSP, read-only in this release):
  1. CP              VIPs, kube-vip lease/manifest, CP static pods
  2. NODES           Ready / SchedulingDisabled, capacity vs requests
  3. PODS            one row per namespace, bad-state detection
  4. CERTS           cert-manager Certificate readiness and expiry
  5. PROXY           per-node proxy drift vs the canonical lsfunctions values
  6. KUBEADM         kubeadm control-plane certificate expiry
  7. POSTGRES        Patroni/spilo pgdata permissions and readiness
  8. DEPLOYMENTS     named core/auth deployments available (VCFA)
  9. ENDPOINT        user-facing URL via its gateway VIP (VCFA)

PORTING STATUS (v0.2.0)
  vsp,  read-only   : cp, nodes, pods, certs, proxy, kubeadm
  vcfa, read-only   : cp, nodes, pods, deployments, certs, endpoint
  remediating       : every ported section on both clusters -
                      cp, nodes, pods, postgres, certs, kubeadm, proxy,
                      deployments. endpoint is detect-only by design.
  mutating, other   : --install-keeper / --remove-keeper (needs --mode tune)
  supervisor        : services, nodes, pods, certs, webhooks (via the two-hop
                      transport). Feature-complete - no cluster is unported.
  An unported cluster emits a WARN row naming the legacy tool that still owns
  it. It never silently reports success.

--mode preflight and --mode report remain incapable of mutating: Runner.write()
raises in those modes. Remediation happens only under --mode remediate, and
keeper management only under --mode tune. Both honour --dry-run, which cannot
reach the transport at all.

Exit codes:
  0  All checks passed
  1  One or more checks failed
  2  Cannot connect to the target cluster
"""

import argparse
import base64
import configparser
import json
import os
import re
import atexit
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

# lsfunctions owns the canonical proxy values. Import them - never re-hardcode.
# vsp-health.py:95 duplicates LAB_PROXY_URL with a comment promising it matches
# lsfunctions, which is exactly how the two drift.
sys.path.insert(0, '/home/holuser/hol')
try:
    import lsfunctions as lsf
    _HAVE_LSF = True
except Exception:                                    # pragma: no cover
    lsf = None
    _HAVE_LSF = False

VERSION = "1.6.0"
DATE    = "2026-08-20"

CREDS_FILE  = "/home/holuser/creds.txt"
LOG_FILE    = "/tmp/vcf-lab-tuner.log"
LOCK_DIR    = "/tmp"


class SshMuxManager:
    """Manages OpenSSH ControlMaster connection sharing and socket lifecycle."""

    def __init__(self, socket_dir=None):
        self.pid = os.getpid()
        self.socket_dir = socket_dir or f"/tmp/.vlt-ssh-{self.pid}"
        self.control_path = os.path.join(self.socket_dir, "cm-%C")
        self._init_dir()
        self._register_handlers()

    def _init_dir(self):
        try:
            os.makedirs(self.socket_dir, mode=0o700, exist_ok=True)
            os.chmod(self.socket_dir, 0o700)
        except Exception:
            pass

    def _register_handlers(self):
        atexit.register(self.cleanup)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev = signal.getsignal(sig)
                def _sig_handler(signum, frame, old_h=prev):
                    self.cleanup()
                    if callable(old_h) and old_h not in (signal.SIG_IGN, signal.SIG_DFL):
                        old_h(signum, frame)
                    else:
                        sys.exit(128 + signum)
                signal.signal(sig, _sig_handler)
            except Exception:
                pass

    def get_ssh_opts(self):
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlPersist=300s",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
        ]

    def cleanup(self):
        if not os.path.exists(self.socket_dir):
            return
        try:
            for fname in os.listdir(self.socket_dir):
                sock_path = os.path.join(self.socket_dir, fname)
                if os.path.exists(sock_path):
                    subprocess.run(
                        ["ssh", "-o", f"ControlPath={sock_path}", "-O", "exit", "dummy-target"],
                        capture_output=True, timeout=3
                    )
        except Exception:
            pass
        try:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
        except Exception:
            pass


SSH_MUX = SshMuxManager()

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=15",
] + SSH_MUX.get_ssh_opts()


# ─── Policy constants: ONE definition each ───────────────────────────────────
# The analysis found four bad-state lists with three different memberships
# (vsp-health.py:98, auto-health.py:139, vsp-health-monitor.py:1094,
# supervisor_stabilizer.py:536). Report the union; act only on the narrow set.

BAD_POD_STATES = (
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled",
    "Error", "CreateContainerConfigError", "RunContainerError",
    "InvalidImageName", "ContainerCannotRun", "StartError",
)

# Sweeping is damped by default: --aggressive opts in to the unthresholded,
# uncapped behaviour (see vcf-lab-tuner.md F8).
ACTIONABLE_POD_STATES = ("CrashLoopBackOff", "Error", "CreateContainerError")
POD_RESTART_THRESHOLD = 5
POD_SWEEP_CAP         = 15

# kubelet owns static pods; deleting one does not do what you want.
CP_STATIC_POD_PREFIXES = ("etcd-", "kube-apiserver-", "kube-controller-manager-",
                          "kube-scheduler-", "kube-vip-")
# Gateway and CSI pods have their own ordered handling; a blind sweep of them
# caused thrash (vsp-health-monitor.py:1086-1095).
SWEEP_EXCLUDE_SUBSTRINGS = ("envoy-gateway", "vmsp-gateway", "ops-logs-gateway",
                            "vsphere-csi-controller", "vsphere-csi-node")

# Patroni/spilo data directory, and the permission forms postgres accepts
# (0700 or 0750, with or without the setgid bit).
PGDATA_DIR      = "/home/postgres/pgdata/pgroot/data"
PGDATA_OK_PERMS = ("700", "2700", "750", "2750")

CERT_THRESHOLD_DAYS = 60
CERT_WARN_DAYS      = 30

# Namespace display order for the pod overview.
NS_PRIORITY = (
    "kube-system", "vmsp-platform", "vcf-fleet-lcm", "vcf-sddc-lcm",
    "vidb-external", "vodap", "ops-logs", "salt", "salt-raas",
)


def resolve_site_config(args):
    """
    Determine the target site suffix and subnet based on the CLI arguments.
    Returns (site_suffix, subnet_prefix).
    """
    site = "a"
    if hasattr(args, "site") and args.site:
        site = args.site.lower()
    elif hasattr(args, "host") and args.host:
        if "site-b" in args.host.lower() or args.host.endswith("b"):
            site = "b"
        elif args.host.startswith("10.2.1."):
            site = "b"
            
    if site == "b":
        return "site-b.vcf.lab", "10.2.1"
    return "site-a.vcf.lab", "10.1.1"

# ─── Cluster registry ────────────────────────────────────────────────────────
# Modeled on vsp_cert_renewer.py:71-92, the one registry in the legacy set that
# already spans clusters cleanly. Cluster targeting is explicit and
# parameterized - never implied by a filename. Note vmsp-platform exists in BOTH
# the VSP fleet and VCFA clusters with different contents, so every emitted row
# is tagged with its cluster.

def get_cluster_configs(args):
    """
    Returns the CLUSTERS dictionary with dynamic site resolution applied.
    """
    site_suffix, subnet = resolve_site_config(args)
    
    return {
        "vsp": {
            "label": "VSP",
            "user": "vmware-system-user",
            "transport": "direct",
            "sudo": "login",                     # kubectl is only on root's PATH via -i
            "cp_vips": [f"{subnet}.142"],
            "owned_vips": None,                  # only the CP VIP
            "vip_hint": "dropped — kube-fix.py restores it",
            "worker_fqdn": f"vsp-01{site_suffix[5]}.{site_suffix}",
            "discover_octets": range(141, 151),  # vsp-health.py:301
            "static_pods": ("etcd", "kube-apiserver", "kube-controller-manager",
                            "kube-scheduler", "kube-vip"),
            "check_cp_lease": False,
            "vip_watchdog_unit": None,
            "keeper_unit": "vcf-lab-keeper",
            "legacy_keeper_units": ["vsp-fleet-depot-keeper"],
            # Every namespace that runs a Patroni/spilo cluster. The legacy fix
            # hardcodes only salt-raas, which is how vidb-external went unnoticed.
            "pg_namespaces": ("salt-raas", "vcf-fleet-lcm", "vcf-sddc-lcm",
                              "vidb-external"),
            # remediate-lab.sh:91,637 - Family B etcd CPU request target. Values
            # agree for VSP; VCFA uses a lower value (kept per-cluster on purpose).
            "etcd_cpu_request": "2500m",
            # Full parity with vsp-health.py's 14 sections, same order.
            "sections": ["cp", "nodes", "pods", "vcf", "postgres", "redis", "salt",
                         "certs", "argo", "kyverno", "vodap", "proxy", "kubeadm",
                         "password", "sizing", "footprint", "entropy"],
        },
        "vcfa": {
            "label": "VCFA",
            "user": "vmware-system-user",
            # Verified live on 10.1.1.73: sudo -S -i with plain kubectl works.
            # vcfa-stabilizer.sh:686 uses sudo -S (no -i) plus an explicit
            # --kubeconfig; auto-health.py:246 uses -i. The -i form is the one that
            # needs no kubeconfig argument, so it is what we use.
            "transport": "direct",
            "sudo": "login",
            "cp_vips": [f"{subnet}.72", f"{subnet}.73", f"{subnet}.71", f"{subnet}.74"],
            "owned_vips": [f"{subnet}.72", f"{subnet}.69", f"{subnet}.70"],
            "vip_hint": "dropped — vcfa-stabilizer.sh --fix-overload re-pins it",
            "worker_fqdn": f"auto-{site_suffix[5]}.{site_suffix}",
            "discover_octets": (),
            "static_pods": ("etcd", "kube-apiserver", "kube-controller-manager",
                            "kube-scheduler", "kube-vip"),
            "check_cp_lease": True,
            "vip_watchdog_unit": "vcfa-vip-watchdog.service",
            "endpoint_fqdn": f"auto-{site_suffix[5]}.{site_suffix}",
            "endpoint_vip": f"{subnet}.70",
            "endpoint_path": "/automation",
            "keeper_unit": "vcf-lab-keeper-vcfa",
            "legacy_keeper_units": ["vcfa-eg-mem-keeper",
                                    "vcfa-vmsp-kube-vip-keeper",
                                    "vcfa-support-bundle-keeper",
                                    "vcfa-prelude-le-keeper"],
            # (namespace, name, severity) - ported from auto-health.py:104-141.
            # trust-manager-sds-server degrades to warn there by design.
            "deployments": [
                ("vmsp-platform", "vmsp-gateway", "fail"),
                ("vmsp-platform", "vcfa-gateway-configuration", "fail"),
                ("vmsp-platform", "envoy-gateway", "fail"),
                ("vmsp-platform", "cert-manager", "fail"),
                ("vmsp-platform", "cert-manager-cainjector", "fail"),
                ("vmsp-platform", "cert-manager-webhook", "fail"),
                ("vmsp-platform", "trust-manager", "fail"),
                ("vmsp-platform", "trust-manager-sds-server", "warn"),
                ("vmsp-platform", "capi-ipam-in-cluster-controller-manager", "fail"),
                ("vmsp-policies", "kyverno-admission-controller", "fail"),
                ("vmsp-policies", "kyverno-background-controller", "fail"),
                ("vmsp-policies", "kyverno-cleanup-controller", "fail"),
                ("prelude", "authentication-server", "fail"),
                ("prelude", "resource-manager-server", "fail"),
                ("prelude", "account-manager-server", "fail"),
                ("prelude", "encryption-manager", "fail"),
                ("prelude", "intent-server", "fail"),
                ("prelude", "vcfa-service-manager", "fail"),
            ],
            "pg_namespaces": ("prelude", "vcd-migrator"),
            "etcd_cpu_request": "1000m",
            "gateway_services": [("vcfa-gateway-configuration", f"{subnet}.70"),
                                 ("vmsp-gateway", f"{subnet}.69")],
            # Parity with auto-health.py's 11 sections (core+auth merged into
            # deployments, which covers both inventories).
            # "kubeadm" added so _delegate_cert_renewal()'s vsp_cert_renewer.py call is
            # actually reachable for this cluster - vsp_cert_renewer.py has always
            # supported --cluster vcfa, but nothing in this tool called it, so a VCFA
            # kubeadm cert nearing expiry had no delegation path at all.
            # "footprint" covers envoy-gateway 4Gi memory limit + CAPI LE false + replicas.
            "sections": ["cp", "nodes", "pods", "deployments", "gateway", "endpoint",
                         "postgres", "certs", "kubeadm", "argo", "edge", "etcd", "footprint", "storm"],
        },
        "supervisor": {
            "label": "SUPERVISOR",
            "user": "root",
            "transport": "vcenter_hop",
            "sudo": "none",
            "cp_vips": [],
            "owned_vips": None,
            "worker_fqdn": "",
            "discover_octets": (),
            "static_pods": (),
            "check_cp_lease": False,
            "vip_watchdog_unit": None,
            "keeper_unit": None,
            "legacy_keeper_units": [],
            "pg_namespaces": (),
            # vCenter list comes from config.ini [RESOURCES] vCenters; the Supervisor
            # is discovered per-vCenter via decryptK8Pwd.py.
            "sections": ["services", "contentlib", "nodes", "pods", "certs", "webhooks"],
        },
    }

# Provide a default CLUSTERS dictionary for static analysis/help screens
class DummyArgs:
    site = None
    host = None
CLUSTERS = get_cluster_configs(DummyArgs())

SECTION_MAP = {
    "cp":          ("CONTROL PLANE",        "VIPs, kube-vip lease/manifest, CP static pods [KB 380701, KB 326110]"),
    "nodes":       ("KUBERNETES NODES",     "Ready status, SchedulingDisabled, capacity [KB 380701]"),
    "pods":        ("POD HEALTH OVERVIEW",  "per namespace, CP/Worker split, bad states [KB 326114, KB 326113]"),
    "deployments": ("CORE DEPLOYMENTS",     "named core/auth deployments available [KB 322724, KB 426075]"),
    "certs":       ("TLS CERTIFICATES",     "cert-manager Certificate readiness/expiry [KB 440167]"),
    "endpoint":    ("USER ENDPOINT",        "user-facing URL via its gateway VIP"),
    "proxy":       ("NODE PROXY CONFIG",    "per-node proxy drift vs lsfunctions values"),
    "kubeadm":     ("KUBEADM CERTIFICATES", "control-plane cert expiry"),
    "postgres":    ("POSTGRESQL (SPILO)",   "pgdata permissions + Patroni readiness [KB 372624]"),
    "services":    ("WCP SERVICES",         "vCenter vapi-endpoint/trustmanagement/wcp [KB 314495, KB 343810]"),
    "contentlib":  ("CONTENT LIBRARY TRUST & SYNC", "vCenter subscribed content library trust store and synchronization"),
    "webhooks":    ("ADMISSION WEBHOOKS",   "webhook caBundle vs its injected CA [KB 313904, KB 368062]"),
    "vodap":       ("VODAP / OBSERVABILITY", "ClickHouse served cert + fluentd buffers"),
    "vcf":         ("VCF MANAGED COMPONENTS", "operational-status + workload replicas [KB 326114]"),
    "redis":       ("REDIS & SALT RAAS",     "readiness + redis-service endpoint"),
    "salt":        ("SALT STACK",            "pod readiness + salt-master log signatures"),
    "argo":        ("ARGO WORKFLOWS",        "stale system-shutdown + power-off marker"),
    "kyverno":     ("KYVERNO POLICIES",      "UpdateRequest backlog + controllers"),
    "password":    ("PASSWORD EXPIRATION",   "node account expiry vs policy"),
    "gateway":     ("GATEWAY DATAPLANE",     "LB VIPs + envoy dataplane services [KB 439264, KB 424402]"),
    "edge":        ("KNOWN EDGE CASES",      "support-bundle, RM deadlock, RabbitMQ [KB 440167, KB 392417, KB 435491]"),
    "etcd":        ("ETCD FRAGMENTATION",    "dbSize slack (informational; KB 327477)"),
    "sizing":      ("CLUSTER SIZING",        "CP/worker machine type, replica bounds, autoscaler, node utilization"),
    "footprint":   ("FOOTPRINT & HA",        "oversized requests, HA counts, safe-to-evict, CAPI LE, autoscaler pin [KB 417831]"),
    "storm":       ("CPU STORM MITIGATION",  "footprint, probe relax, kube-vip guard, gateway/UI-tier hardening [KB 322724, KB 439264]"),
    "entropy":     ("ESXI ENTROPY SOURCE",   "AMD Zen4/5 esxcli entropySources RDRAND workaround (via govc)"),
}

MODES = ("preflight", "tune", "remediate", "report")
READ_ONLY_MODES = ("preflight", "report")

# Which modes each section is allowed to ACT in. Made explicit because the
# distinction is easy to get wrong by accident and impossible to see at a glance
# once it is scattered across handlers:
#
#   tune       durable CONFIGURATION - what confighol applies at template prep.
#              Must be safe on a healthy lab and must not restart anything.
#   remediate  REPAIR of something currently broken. May restart.
#
# So the proxy files and the kube-vip/lease manifest settings are tune-able
# config, while a pgdata permission repair, a pod sweep, an uncordon and a
# scale-up are remediation - confighol has no business doing those at
# template-prep time even though they are technically durable.
SECTION_ACT_MODES = {
    "cp":          ("tune", "remediate"),   # manifest settings are config
    "proxy":       ("tune", "remediate"),   # node proxy files are config
    "certs":       ("tune", "remediate"),   # pre-provisioning is a tune-time job
    "kubeadm":     ("tune", "remediate"),
    "postgres":    ("remediate",),          # repair, not config
    "nodes":       ("remediate",),
    "pods":        ("remediate",),
    "deployments": ("remediate",),
    "vodap":       ("remediate",),          # repair: restarts and buffer purges
    "vcf":         ("remediate",),
    "redis":       ("remediate",),
    "salt":        ("remediate",),
    "argo":        ("remediate",),
    "kyverno":     ("remediate",),
    "password":    ("tune", "remediate"),   # chage is durable config
    "gateway":     (),                      # detect-only: repair lives in cp/pods
    "edge":        ("remediate",),
    "etcd":        ("remediate",),          # defrag briefly stalls the apiserver
    "services":    ("tune", "remediate"),   # starting a stopped service is config-ish
    "contentlib":  ("tune", "remediate"),   # trust store and sync
    "webhooks":    ("tune", "remediate"),   # caBundle is durable config
    "endpoint":    (),                      # detect-only by design
    "sizing":      ("remediate",),          # an operator decision, never auto-applied by tune
    "footprint":   ("remediate",),          # opt-in density reduction, not "fixing" anything
    "storm":       ("remediate",),          # mitigation of a live symptom, not durable config
    "entropy":     ("tune", "remediate"),   # config-only, NEVER reboots -- safe at template-prep time
}


# Sections that need the bulk node list in ctx["nodes"]. Declared here rather
# than inline in run_cluster, because getting it wrong is silent: the section
# reports "node list unavailable" (or worse, computes from an empty set and looks
# like a finding). That has now happened three times - pods, proxy, password.
SECTIONS_NEEDING_NODES = frozenset({"nodes", "pods", "proxy", "password"})


def may_act(r, section):
    """True when this section is permitted to mutate in the Runner's current mode."""
    return r.mode in SECTION_ACT_MODES.get(section, ())


# ─── Colors ──────────────────────────────────────────────────────────────────
# Resolved once. Matches vsp-health.py:109-120 exactly, plus a --no-color escape
# hatch that file lacks.

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _set_color(enabled):
    global _CYAN, _BLUE, _GREEN, _RED, _YELLOW, _BOLD, _DIM, _NC, _OK, _FAIL, _WARN
    if enabled:
        _CYAN, _BLUE, _GREEN, _RED, _YELLOW, _BOLD, _DIM, _NC = (
            '\033[0;36m', '\033[0;34m', '\033[0;32m',
            '\033[0;31m', '\033[1;33m', '\033[1m', '\033[2m', '\033[0m'
        )
    else:
        _CYAN = _BLUE = _GREEN = _RED = _YELLOW = _BOLD = _DIM = _NC = ''
    _OK   = f"{_GREEN}✓{_NC}"
    _FAIL = f"{_RED}✗{_NC}"
    _WARN = f"{_YELLOW}⚠{_NC}"


_set_color(_COLOR)


# ─── Output ──────────────────────────────────────────────────────────────────
# vsp-health.py shadows the print() builtin, which works but breaks linters,
# swallows every write error, ignores end= (splitting progress lines in the log),
# and writes an unbounded untimestamped file. Use an explicit emit() instead:
# same rendered result, none of those properties.

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
_QUIET = False          # --json suppresses human output entirely
_LOG_FH = None


def _log_handle():
    global _LOG_FH
    if _LOG_FH is None:
        try:
            _LOG_FH = open(LOG_FILE, 'a')
            _LOG_FH.write(f"\n===== vcf-lab-tuner.py {VERSION} "
                          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        except OSError:
            _LOG_FH = False
    return _LOG_FH or None


def emit(text="", end="\n", stderr=False):
    """Render to the console and append an ANSI-stripped, timestamped copy to LOG_FILE."""
    stream = sys.stderr if stderr else sys.stdout
    if not (_QUIET and not stderr):
        stream.write(text + end)
        stream.flush()
    fh = _log_handle()
    if fh and not stderr:
        try:
            ts = datetime.now().strftime('%H:%M:%S')
            fh.write(f"{ts} {_ANSI_RE.sub('', text)}{end if end == chr(10) else chr(10)}")
            fh.flush()
        except OSError:
            pass


def banner(title, subtitle=""):
    w = 70
    emit()
    emit(f"{_CYAN}╔{'═' * w}╗{_NC}")
    emit(f"{_CYAN}║{_NC}{_BLUE}{title:^{w}}{_NC}{_CYAN}║{_NC}")
    if subtitle:
        emit(f"{_CYAN}║{_NC}{subtitle:^{w}}{_CYAN}║{_NC}")
    emit(f"{_CYAN}║{_NC}{datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^{w}}{_CYAN}║{_NC}")
    emit(f"{_CYAN}╚{'═' * w}╝{_NC}")


def section(title):
    bar = '─' * max(0, 60 - len(title))
    emit(f"\n{_BOLD}{_CYAN}──── {title} {bar}{_NC}")


def row(res):
    """Render one CheckResult. Label asserts the desired state; detail carries the deviation."""
    glyph = {"pass": _OK, "fail": _FAIL, "warn": _WARN}[res.state]
    color = {"pass": _DIM, "fail": _RED, "warn": _YELLOW}[res.state]
    suffix = f"  {color}{res.detail}{_NC}" if res.detail else ""
    emit(f"  {glyph} {res.label}{suffix}")


def row_verbose(msg, indent=6):
    emit(f"{' ' * indent}{_DIM}{msg}{_NC}")


def render_legacy(res):
    """Emit the CHECK:/SKIP: line shape that Tools/vpodchecker.py:3149-3175 screen-scrapes.

    Preserved verbatim, including the two-space / three-space padding: if these
    strings change, vpodchecker silently reports NOTHING - no error, zero
    findings. Driven from CheckResult so residual_days=None renders correctly
    instead of needing the fake '0d remaining' hack at
    supervisor_stabilizer.py:2394. Delete this function once vpodchecker
    consumes --json.
    """
    if res.residual_days is None:
        return
    tag = "CHECK  :" if res.state != "pass" else "SKIP   :"
    cid = res.cluster or "-"
    emit(f"      [{cid}] {tag} {res.label} — {res.residual_days}d remaining")


# ─── Result type ─────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    key: str
    label: str
    state: str                      # "pass" | "fail" | "warn"
    detail: str = ""
    cluster: str = ""
    residual_days: int | None = None
    action: str | None = None

    def as_dict(self):
        return {
            "key": self.key, "label": self.label, "state": self.state,
            "detail": self.detail, "cluster": self.cluster,
            "residual_days": self.residual_days, "action": self.action,
        }


def ok(key, label, detail="", cluster="", residual_days=None):
    return CheckResult(key, label, "pass", detail, cluster, residual_days)


def fail(key, label, detail="", cluster="", residual_days=None):
    return CheckResult(key, label, "fail", detail, cluster, residual_days)


def warn(key, label, detail="", cluster="", residual_days=None):
    return CheckResult(key, label, "warn", detail, cluster, residual_days)


# ─── Transport ───────────────────────────────────────────────────────────────
# One implementation, adapters per target shape. The legacy set has ~9 copies of
# this logic, each re-inventing base64 wrapping, sshpass handling, banner
# scrubbing and timeouts - and they have drifted (only vsp-health.py:293 carries
# the .141-.150 discovery sweep).

_SUDO_RE = re.compile(r"\[sudo\] password for [^:]+:\s*")
_NOISE = ("Welcome to Photon", "Warning: Permanently added",
          "Connection to ", "Killed by signal", "Last login:")


def _scrub(text):
    text = _SUDO_RE.sub("", text or "")
    keep = [ln for ln in text.splitlines()
            if not any(n in ln for n in _NOISE)]
    return "\n".join(keep).strip()


class _PasswordFile:
    """Password in a 0600 temp file, never in argv.

    supervisor_stabilizer.py:1239 passes `sshpass -p '<pw>'` on a shell=True
    command line, so the credential lands in the process table on both ends.
    Its own SCP hop does the right thing (sshpass -f + chmod 600); this is that,
    everywhere. Filenames carry pid+ms because fixed names (/tmp/.scppwd_hop)
    make two concurrent runs clobber each other.
    """

    def __init__(self, password):
        self.password = password
        self.path = None

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(
            prefix=f".vlt-{os.getpid()}-{int(time.time() * 1000) % 100000}-", dir="/tmp")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.password + "\n")
        return self.path

    def __exit__(self, *exc):
        try:
            if self.path:
                os.unlink(self.path)
        except OSError:
            pass
        return False


class DirectTransport:
    """Manager -> node over SSH, command shipped base64 so quoting never bites.

    The sudo password goes to sudo -S on STDIN rather than being interpolated
    into the remote command string, so it does not appear in the remote process
    list either. argv is a list (no shell=True), so no local quoting either.
    """

    def __init__(self, host, user, password, sudo="login"):
        self.host, self.user, self.password, self.sudo = host, user, password, sudo

    def _wrap(self, cmd):
        """Ship the payload so that NO outer shell ever sees a '$'.

        The obvious form -- bash -c "$(echo <b64> | base64 -d)" -- is broken with
        `sudo -i`: sudo joins its arguments and hands them to a LOGIN shell,
        which re-parses the already-substituted text. Any shell variable in the
        payload ($f, $?, ...) gets expanded a level too early, where it is unset,
        and silently becomes an empty string. Found live: a proxy check looped
        `for f in ...; do grep "$f"; done` and reported all three files missing on
        every node because $f arrived empty. vsp-health.py:215 uses the same shape
        and gets away with it only because its payloads are single kubectl calls
        with no variables.

        Instead the payload is base64 inside a SINGLE-quoted argument and is
        decoded and executed by the innermost bash, which reads the script from a
        pipe. sudo has already consumed the password line from its own stdin by
        then, so the pipe is free.
        """
        b64 = base64.b64encode(cmd.encode()).decode()
        payload = f"'echo {b64} | base64 -d | bash'"
        if self.sudo == "login":
            return f'sudo -S -i bash -c {payload}'
        if self.sudo == "plain":
            return f'sudo -S bash -c {payload}'
        return f'bash -c {payload}'

    def exec(self, cmd, timeout=60):
        with _PasswordFile(self.password) as pwfile:
            argv = (["sshpass", "-f", pwfile, "ssh"] + SSH_OPTS
                    + [f"{self.user}@{self.host}", self._wrap(cmd)])
            try:
                proc = subprocess.run(
                    argv, input=self.password + "\n",
                    capture_output=True, text=True, timeout=timeout)
                return proc.returncode, _scrub(proc.stdout + proc.stderr)
            except subprocess.TimeoutExpired:
                return 1, f"<timeout after {timeout}s>"
            except FileNotFoundError as exc:
                return 1, f"<missing tool: {exc}>"
            except Exception as exc:                 # never raise out of transport
                return 1, f"<transport error: {exc}>"


class VCenterTransport:
    """Manager -> vCenter appliance as root. Photon OS, no sudo needed."""

    def __init__(self, host, password):
        self.host, self.password = host, password

    def exec(self, cmd, timeout=60):
        b64 = base64.b64encode(cmd.encode()).decode()
        with _PasswordFile(self.password) as pwfile:
            argv = (["sshpass", "-f", pwfile, "ssh"] + SSH_OPTS
                    + [f"root@{self.host}", f"echo {b64} | base64 -d | bash"])
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      timeout=timeout)
                return proc.returncode, _scrub(proc.stdout + proc.stderr)
            except subprocess.TimeoutExpired:
                return 1, f"<timeout after {timeout}s>"
            except Exception as exc:
                return 1, f"<transport error: {exc}>"


class VCenterHopTransport:
    """Manager -> vCenter -> Supervisor control plane. Two hops, both as root.

    The Supervisor CP is not routable from the manager; you go through its
    vCenter, which is also the only place that can tell you the CP's address and
    password (decryptK8Pwd.py). The password for the second hop is written to a
    0600 file ON the vCenter rather than interpolated into the command, so it
    does not land in the vCenter's process table - supervisor_stabilizer.py:1673
    does this correctly for the inner hop while :1239 does not for the outer, so
    this uses the safe form for both.

    Temp filenames carry pid+ms: supervisor_stabilizer.py uses fixed paths
    (/tmp/.scppwd_hop), so two concurrent runs clobber each other's password file.
    """

    def __init__(self, vc_host, vc_password, scp_ip, scp_password):
        self.vc = VCenterTransport(vc_host, vc_password)
        self.scp_ip = scp_ip
        self.scp_password = scp_password
        self.mux_tag = f"{os.getpid()}-{id(self)}"

    def exec(self, cmd, timeout=90):
        pw_b64 = base64.b64encode((self.scp_password + "\n").encode()).decode()
        cmd_b64 = base64.b64encode(cmd.encode()).decode()
        pwf = f"/tmp/.vlt-scppw-{self.mux_tag}"
        inner_dir = f"/tmp/.vlt-ssh-inner-{self.mux_tag}"
        hop = (
            f"echo {pw_b64} | base64 -d > {pwf} && chmod 600 {pwf} && "
            f"mkdir -p -m 700 {inner_dir} && "
            f"sshpass -f {pwf} ssh -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15 "
            f"-o ControlMaster=auto -o ControlPath={inner_dir}/cm-%C -o ControlPersist=300s "
            f"-o ServerAliveInterval=15 -o ServerAliveCountMax=4 "
            f"root@{self.scp_ip} 'echo {cmd_b64} | base64 -d | bash'; "
            f"rc=$?; rm -f {pwf}; exit $rc"
        )
        return self.vc.exec(hop, timeout=timeout)


def discover_supervisor(vc_host, vc_password):
    """Ask a vCenter for its Supervisor CP address and password.

    Returns (scp_ip, scp_password, cluster_id) or (None, None, None).

    PAGER=cat TERM=dumb and the trailing `| cat` matter: decryptK8Pwd.py paginates
    otherwise and the read hangs (supervisor_stabilizer.py:1539).
    """
    vc = VCenterTransport(vc_host, vc_password)
    rc, out = vc.exec(
        "PAGER=cat TERM=dumb /usr/lib/vmware-wcp/decryptK8Pwd.py 2>&1 | cat", 120)
    if rc != 0 or not out:
        return None, None, None
    ip = pwd = cid = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Cluster:"):
            cid = line.split(":", 1)[1].strip().split(":")[0]
        elif line.startswith("IP:"):
            ip = line.split(":", 1)[1].strip()
        elif line.startswith("PWD:"):
            pwd = line.split(":", 1)[1].strip()
    if ip and pwd:
        return ip, pwd, cid
    return None, None, None


# ─── Runner: makes dry-run structural, not conventional ──────────────────────

class Runner:
    """Every remote operation goes through here. .write() is the ONLY mutation path.

    F4 in the analysis exists because dry-run was a per-call convention and one
    code path forgot it: supervisor_stabilizer.py's Phase 2/A restarted
    hypercrypt and started kubelet under --dry-run, and could block 1800s. Here
    a write in a read-only mode raises, and a write under --dry-run cannot reach
    the transport at all.
    """

    def __init__(self, transport, mode, dry_run, cluster):
        self.t = transport
        self.mode = mode
        self.dry_run = dry_run
        self.cluster = cluster
        self.planned = []
        self.skipped = []
        self.vcenter_transport = None      # set for the supervisor cluster

    def read(self, cmd, timeout=60):
        return self.t.exec(cmd, timeout)

    def write_on_node(self, host, cmd, desc, tier="transient", timeout=60):
        """Mutate a peer node in the same cluster, with the same gating as write()."""
        if self.mode in READ_ONLY_MODES:
            raise RuntimeError(
                f"write attempted in read-only mode '{self.mode}': {desc}")
        if self.mode == "tune" and tier == "transient":
            self.skipped.append(desc)
            row_verbose(f"[tune] skipping transient action: {desc}")
            return 0, "<skipped>"
        if self.dry_run:
            self.planned.append(desc)
            row_verbose(f"[dry-run] would {desc}")
            return 0, "<dry-run>"
        cfg = CLUSTERS[self.cluster]
        peer = DirectTransport(host, cfg["user"], get_password(), cfg["sudo"])
        return peer.exec(cmd, timeout)

    def read_on(self, host, cmd, timeout=60):
        """Read from a peer node in the same cluster (per-node checks need this).

        Routed through the Runner on purpose: no check may hold its own transport,
        or the choke point stops being a choke point and a future mutating check
        can quietly sidestep dry-run.
        """
        cfg = CLUSTERS[self.cluster]
        peer = DirectTransport(host, cfg["user"], get_password(), cfg["sudo"])
        return peer.exec(cmd, timeout)

    def read_on_vcenter(self, cmd, timeout=60):
        """Read from the vCenter appliance itself (not the Supervisor behind it)."""
        if not self.vcenter_transport:
            return 1, "<no vCenter transport for this cluster>"
        return self.vcenter_transport.exec(cmd, timeout)

    def write_on_vcenter(self, cmd, desc, tier="transient", timeout=60):
        if self.mode in READ_ONLY_MODES:
            raise RuntimeError(
                f"write attempted in read-only mode '{self.mode}': {desc}")
        if self.mode == "tune" and tier == "transient":
            self.skipped.append(desc)
            row_verbose(f"[tune] skipping transient action: {desc}")
            return 0, "<skipped>"
        if self.dry_run:
            self.planned.append(desc)
            row_verbose(f"[dry-run] would {desc}")
            return 0, "<dry-run>"
        if not self.vcenter_transport:
            return 1, "<no vCenter transport for this cluster>"
        return self.vcenter_transport.exec(cmd, timeout)

    def read_json(self, cmd, timeout=60):
        rc, out = self.read(cmd, timeout)
        start = out.find("{")
        if start < 0:
            return None
        try:
            return json.loads(out[start:])
        except ValueError:
            return None

    def write(self, cmd, desc, tier="transient", timeout=60):
        if self.mode in READ_ONLY_MODES:
            raise RuntimeError(
                f"write attempted in read-only mode '{self.mode}': {desc}")
        if tier == "futile":
            raise ValueError(
                f"refusing '{desc}': targets a layer a controller reverts in <60s")
        # tune applies DURABLE configuration only. It must not restart pods or
        # bounce services: confighol calls it at template-prep time and a
        # config-only step that quietly reboots the control plane is a nasty
        # surprise. Transient repair belongs to remediate.
        if self.mode == "tune" and tier == "transient":
            self.skipped.append(desc)
            row_verbose(f"[tune] skipping transient action: {desc}")
            return 0, "<skipped: transient action in tune mode>"
        if self.dry_run:
            self.planned.append(desc)
            row_verbose(f"[dry-run] would {desc}")
            return 0, "<dry-run>"
        return self.t.exec(cmd, timeout)

    def local(self, argv, desc, timeout=600):
        """Run a helper ON THE MANAGER (e.g. vsp_cert_renewer.py), not on a node.

        Still routed through the Runner so mode/dry-run gating is identical -
        vsp-health-monitor.py:2219 shells out to the cert renewer directly, which
        means its own --dry-run has to be threaded through by hand each time.
        """
        if self.mode in READ_ONLY_MODES:
            raise RuntimeError(
                f"local write attempted in read-only mode '{self.mode}': {desc}")
        if self.dry_run:
            self.planned.append(desc)
            row_verbose(f"[dry-run] would {desc}")
            return 0, "<dry-run>"
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 1, f"<timeout after {timeout}s>"
        except Exception as exc:
            return 1, f"<local exec error: {exc}>"

    def read_local(self, argv, timeout=60):
        """Read-only counterpart to local() -- runs on the MANAGER, ungated.

        local() is a write path and raises in every read-only mode, so a
        section that only needs to inspect manager-local state (e.g. probing
        govc for the entropy-fix section) cannot use it just to look. This
        never mutates anything itself, so it always runs regardless of mode.
        """
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 1, f"<timeout after {timeout}s>"
        except Exception as exc:
            return 1, f"<local exec error: {exc}>"


# ─── Helpers ─────────────────────────────────────────────────────────────────

_cached_password = None


def get_password():
    global _cached_password
    if _cached_password is None:
        try:
            with open(CREDS_FILE) as fh:
                _cached_password = fh.readline().strip()
        except OSError as exc:
            emit(f"{_RED}ERROR:{_NC} Cannot read {CREDS_FILE}: {exc}", stderr=True)
            sys.exit(2)
    return _cached_password


def _vcenters_from_config(path="/tmp/config.ini"):
    """vCenter FQDNs from [RESOURCES] vCenters, same source supervisor_stabilizer uses.

    Format per line: <fqdn>:<os>:<sso-user>, continuation lines indented, '#'
    comments out an entry.
    """
    hosts = []
    try:
        with open(path) as fh:
            in_key = False
            for raw in fh:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if stripped.lower().startswith("vcenters"):
                    in_key = True
                    stripped = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                elif in_key and (not line[:1].isspace() or not stripped):
                    if stripped.startswith("[") or not stripped:
                        in_key = False
                        continue
                elif not in_key:
                    continue
                if not stripped or stripped.startswith("#"):
                    continue
                fqdn = stripped.split(":", 1)[0].strip()
                if fqdn and "." in fqdn and fqdn not in hosts:
                    hosts.append(fqdn)
    except OSError:
        pass
    if not hosts:
        # Fall back to the standard Holodeck pair rather than doing nothing.
        hosts = ["vc-wld01-a.site-a.vcf.lab", "vc-mgmt-a.site-a.vcf.lab"]
    return hosts


def ping(host, timeout=2):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", str(timeout), host],
                              capture_output=True, timeout=timeout + 3).returncode == 0
    except Exception:
        return False


SSHPASS_RC_AUTH_FAILURE = 5


def _probe(host, cfg, password, timeout=25):
    """Single reachability probe. Returns (ok, rc)."""
    t = DirectTransport(host, cfg["user"], password, cfg["sudo"])
    # Verify not just SSH, but that kubectl is functional (rules out worker nodes)
    rc, _ = t.exec("kubectl get nodes >/dev/null 2>&1 && echo PONG", timeout=timeout)
    return rc == 0, rc


def resolve_entry_point(cfg, host_override, password):
    """--host, then configured VIPs, then a discovery sweep. Returns (host, tried).

    Two behaviours learned the hard way, both on 2026-08-14:

    1. One failed probe does not mean the cluster is gone. A VCFA probe failed on
       all four candidates while the node was pingable, port 22 was open and
       /automation was serving HTTP 200 - it was briefly busy. Each candidate now
       gets a second attempt before being written off.
    2. STOP probing on an AUTHENTICATION failure (sshpass rc 5) rather than
       walking the rest of the candidate list. Repeated password attempts trip
       pam_faillock, and on these appliances the candidates share a PAM database -
       vsp_cert_renewer.py:2176-2188 skips a node on rc 5 for exactly this
       reason ("10.1.1.143 shares the same PAM DB as the VIP 10.1.1.142"). Racing
       through four IPs with a wrong password is how you lock yourself out of the
       cluster you were trying to inspect.
    """
    tried = []
    candidates = []
    if host_override:
        candidates.append(host_override)
    else:
        candidates.extend(cfg["cp_vips"])

    for cand in candidates:
        tried.append(cand)
        emit(f"{_DIM}  probing {cand} ...{_NC}", end="")
        ok_, rc = _probe(cand, cfg, password)
        if not ok_ and rc != SSHPASS_RC_AUTH_FAILURE:
            # Transient failures happen; give it one more chance before writing
            # off the whole cluster.
            ok_, rc = _probe(cand, cfg, password)
        if ok_:
            emit(f" {_OK}")
            return cand, tried
        if rc == SSHPASS_RC_AUTH_FAILURE:
            emit(f" {_FAIL}")
            emit(f"  {_YELLOW}Authentication failed on {cand}. Stopping here rather "
                 f"than probing the remaining candidates:{_NC}")
            emit(f"  {_DIM}repeated password attempts trip pam_faillock, and these "
                 f"appliances share a PAM database.{_NC}")
            emit(f"  {_DIM}Check the password in {CREDS_FILE} "
                 f"(SDDC Manager rotates some of these), then retry.{_NC}")
            return None, tried
        emit(f" {_FAIL}")

    # Discovery sweep: only vsp-health.py:293 carried this; the sibling copies
    # still do DNS-then-single-IP and silently lose the ability to find a
    # rolling-replaced CP node.
    if not host_override and cfg["discover_octets"] and cfg["worker_fqdn"]:
        try:
            worker_ip = socket.gethostbyname(cfg["worker_fqdn"])
            prefix = ".".join(worker_ip.split(".")[:3]) + "."
        except OSError:
            prefix = None
        if prefix:
            for octet in cfg["discover_octets"]:
                cand = f"{prefix}{octet}"
                if cand in tried or not ping(cand, 1):
                    continue
                tried.append(cand)
                ok_, rc = _probe(cand, cfg, password, timeout=20)
                if ok_:
                    emit(f"{_DIM}  discovered {cand}{_NC} {_OK}")
                    return cand, tried
                if rc == SSHPASS_RC_AUTH_FAILURE:
                    emit(f"  {_YELLOW}Authentication failed during discovery — "
                         f"stopping to avoid pam_faillock.{_NC}")
                    return None, tried
    return None, tried


def print_node_capacity_table(describe_out):
    """Node Capacity vs Resource Requests Allocation, from `kubectl describe nodes`.

    Ported from vsp-health.py:467 (also in auto-health.py:448) with its rendering
    preserved exactly: `+---+` grid, all _DIM, two-space indent, same column set
    and the same "N/A (Untolerated Taint)" treatment for control-plane and
    NoSchedule nodes, where remaining capacity is not meaningful for ordinary
    workloads.

    This is diagnostic context rather than a pass/fail check, so it prints
    alongside the node rows and contributes nothing to the check count.
    """
    if not describe_out or "Name:" not in describe_out:
        return

    rows = []
    for block in re.split(r'\n(?=Name:\s+)', describe_out):
        name_m = re.search(r'Name:\s+(\S+)', block)
        if not name_m:
            continue
        name = name_m.group(1)

        roles_m = re.search(r'Roles:\s+([^\n]+)', block)
        roles_raw = roles_m.group(1).strip() if roles_m else '<none>'
        role = 'Control Plane' if 'control-plane' in roles_raw else 'Worker'

        taints_m = re.search(r'Taints:\s+([^\n]+)', block)
        taints_raw = taints_m.group(1).strip() if taints_m else '<none>'
        taints = ('None' if taints_raw in ('<none>', 'none', '')
                  else taints_raw.replace('node-role.kubernetes.io/', ''))

        alloc_idx = block.find('Allocatable:')
        if alloc_idx != -1:
            ab = block[alloc_idx:alloc_idx + 300]
            m_cpu = re.search(r'cpu:\s+(\S+)', ab)
            m_mem = re.search(r'memory:\s+(\S+)', ab)
            cpu_alloc_m = _parse_cpu(m_cpu.group(1)) if m_cpu else 0
            mem_alloc_mib = _parse_mem_mib(m_mem.group(1)) if m_mem else 0
        else:
            cpu_alloc_m = mem_alloc_mib = 0

        ar_idx = block.find('Allocated resources:')
        if ar_idx != -1:
            rb = block[ar_idx:ar_idx + 400]
            cpu_m = re.search(r'cpu\s+(\S+)\s+\(([^)]+)\)', rb)
            mem_m = re.search(r'memory\s+(\S+)\s+\(([^)]+)\)', rb)
            cpu_req_str = f"{cpu_m.group(2)} ({cpu_m.group(1)})" if cpu_m else "N/A"
            mem_req_str = f"{mem_m.group(2)} ({mem_m.group(1)})" if mem_m else "N/A"
            cpu_req_m = _parse_cpu(cpu_m.group(1)) if cpu_m else 0
            mem_req_mib = _parse_mem_mib(mem_m.group(1)) if mem_m else 0
        else:
            cpu_req_str = mem_req_str = "N/A"
            cpu_req_m = mem_req_mib = 0

        if 'NoSchedule' in taints or role == 'Control Plane':
            rem_str = 'N/A (Untolerated Taint)'
        else:
            rem_cpu = max(0, cpu_alloc_m - cpu_req_m)
            rem_mem = max(0, int(mem_alloc_mib - mem_req_mib))
            rem_str = f"~{rem_cpu}m CPU / ~{rem_mem:,} Mi Memory"

        rows.append({'node': name, 'role': role, 'taints': taints,
                     'cpu_req': cpu_req_str, 'mem_req': mem_req_str,
                     'remaining': rem_str})

    if not rows:
        return

    headers = ['Node', 'Role', 'Taints', 'CPU Requests', 'Memory Requests',
               'Allocatable CPU / Memory Remaining']
    keys = ['node', 'role', 'taints', 'cpu_req', 'mem_req', 'remaining']
    widths = [len(h) for h in headers]
    for r_ in rows:
        for i, k in enumerate(keys):
            widths[i] = max(widths[i], len(r_[k]))

    sep = '+' + '+'.join('-' * (w + 2) for w in widths) + '+'
    header_line = ('| ' + ' | '.join(f"{headers[i]:<{widths[i]}}"
                                     for i in range(len(headers))) + ' |')

    emit(f"\n{_DIM}  Node Capacity vs. Resource Requests Allocation:{_NC}")
    emit(f"{_DIM}  {sep}{_NC}")
    emit(f"{_DIM}  {header_line}{_NC}")
    emit(f"{_DIM}  {sep}{_NC}")
    for r_ in rows:
        line = ('| ' + ' | '.join(f"{r_[keys[i]]:<{widths[i]}}"
                                  for i in range(len(keys))) + ' |')
        emit(f"{_DIM}  {line}{_NC}")
    emit(f"{_DIM}  {sep}{_NC}")


def _parse_mem_mib(v):
    """Kubernetes memory quantity -> MiB."""
    if not v:
        return 0
    v = str(v).strip()
    units = (("Ki", 1 / 1024), ("Mi", 1), ("Gi", 1024), ("Ti", 1024 * 1024),
             ("K", 1 / 1024), ("M", 1), ("G", 1024))
    for suf, mult in units:
        if v.endswith(suf):
            try:
                return float(v[:-len(suf)]) * mult
            except ValueError:
                return 0
    try:
        return float(v) / (1024 * 1024)      # bare bytes
    except ValueError:
        return 0


def _parse_cpu(v):
    """Kubernetes CPU quantity -> MILLICORES (int), matching vsp-health.py:443.

    Millicores, not cores: the capacity table labels the remainder "m", so
    returning cores rendered "~3.795m CPU" where legacy correctly shows
    "~3795m CPU". Keep the units aligned with the label.
    """
    v = str(v or "").strip()
    if not v:
        return 0
    if v.endswith("m"):
        try:
            return int(v[:-1])
        except ValueError:
            return 0
    try:
        return int(float(v) * 1000)
    except ValueError:
        return 0


def _days_until(iso):
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (dt - datetime.now(timezone.utc)).days


def _parse_openssl_date(s):
    if not s:
        return None
    try:
        clean = s.replace("GMT", "").strip()
        dt = datetime.strptime(clean, "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _parse_iso_date(s):
    if not s:
        return None
    try:
        clean = s.rstrip("Z")
        dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


# ─── Static-manifest lease/CPU tuning (remediate-lab.sh Family B, both clusters) ─
# KCM/scheduler leader-election timing, etcd CPU request, and kube-vip's own
# numeric lease timing. Shared between chk_cp (vsp AND vcfa - Family B runs
# "per-node on both nodes" per remediate-lab.sh's own header) and chk_storm
# (which also asserts the kube-vip guard as part of its composite).
LEASE_TRIPLE = ("60s", "40s", "6s")
LEASE_DURATION, RENEW_DEADLINE, RETRY_PERIOD = LEASE_TRIPLE
KCM_MANIFEST = "/etc/kubernetes/manifests/kube-controller-manager.yaml"
SCHEDULER_MANIFEST = "/etc/kubernetes/manifests/kube-scheduler.yaml"
ETCD_MANIFEST = "/etc/kubernetes/manifests/etcd.yaml"
KUBEVIP_MANIFEST = "/etc/kubernetes/manifests/kube-vip.yaml"
VIP_LEASE_DURATION, VIP_RENEW_DEADLINE, VIP_RETRY_PERIOD = "60", "40", "6"


def _lease_tuning_check(r, cl, component, manifest):
    """--leader-elect-{lease-duration,renew-deadline,retry-period} static-
    manifest tuning, ported from remediate-lab.sh:1480 patch_leader_elect.

    Delete-then-insert right after the existing --leader-elect=true line,
    because a sed substitution alone can't both correct a drifted value AND
    add a genuinely absent one in a single pass. Refuses to guess an insertion
    point if --leader-elect=true itself isn't present, exactly like the
    source. Static-manifest edit only - kubelet's own file watcher recreates
    the pod, no service restart needed unless the watcher is stuck (see the
    --kubelet-reload flag).
    """
    rc, cur = r.read(
        "grep -oE -- '--leader-elect-(lease-duration|renew-deadline|retry-period)="
        f"[^[:space:]\"]+' {manifest} 2>/dev/null", 30)
    found = {}
    for line in (cur or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            found[k.strip()] = v.strip()
    ld = found.get("--leader-elect-lease-duration", "")
    rd = found.get("--leader-elect-renew-deadline", "")
    rp = found.get("--leader-elect-retry-period", "")
    label = (f"{component}: leader-elect lease/renew/retry == "
            f"{LEASE_DURATION}/{RENEW_DEADLINE}/{RETRY_PERIOD}")
    if ld == LEASE_DURATION and rd == RENEW_DEADLINE and rp == RETRY_PERIOD:
        return ok("cp.lease_tuning", label, cluster=cl)

    rc, has_le = r.read(f"grep -c -- '--leader-elect=true' {manifest} 2>/dev/null", 30)
    if not (has_le or "").strip().split("\n")[0].strip().isdigit() or \
            int((has_le or "0").strip().split("\n")[0].strip() or "0") < 1:
        return warn("cp.lease_tuning", label,
                    f"{manifest} not readable or has no --leader-elect=true line "
                    "to insert after — not guessing", cluster=cl)

    # kcp-patch equivalent (remediate-lab.sh --kcp-patch: print, never apply the
    # KubeadmControlPlane-level version of the same change) folded into detail
    # so it is visible even when this section is only reporting, not acting.
    kcp_hint = (f"equivalent KubeadmControlPlane patch: extraArgs."
               f"leader-elect-lease-duration={LEASE_DURATION},"
               f"leader-elect-renew-deadline={RENEW_DEADLINE},"
               f"leader-elect-retry-period={RETRY_PERIOD}")
    res = fail("cp.lease_tuning", label,
               f"currently {ld or 'unset'}/{rd or 'unset'}/{rp or 'unset'} — {kcp_hint}",
               cluster=cl)
    if may_act(r, "cp"):
        r.write(
            f"mkdir -p /root/manifest-bak && "
            f"cp {manifest} {manifest}.bak.$(date +%s) 2>/dev/null; "
            "sed -i -E '/--leader-elect-lease-duration=/d; "
            "/--leader-elect-renew-deadline=/d; /--leader-elect-retry-period=/d' "
            f"{manifest} && "
            "sed -i '/--leader-elect=true/a\\    - --leader-elect-lease-duration="
            f"{LEASE_DURATION}\\n    - --leader-elect-renew-deadline={RENEW_DEADLINE}"
            f"\\n    - --leader-elect-retry-period={RETRY_PERIOD}' {manifest}",
            f"set {component} leader-elect lease/renew/retry -> "
            f"{LEASE_DURATION}/{RENEW_DEADLINE}/{RETRY_PERIOD}",
            tier="persistent", timeout=60)
        res.action = f"lease -> {LEASE_DURATION}/{RENEW_DEADLINE}/{RETRY_PERIOD}"
        if not r.dry_run:
            res.state = "warn"
            res.detail = (f"was {ld or 'unset'}/{rd or 'unset'}/{rp or 'unset'}; "
                          "patched (kubelet recreates the pod)")
    return res


def _etcd_cpu_check(r, cl, target_cpu):
    """etcd static-manifest CPU request enforcement, ported from
    remediate-lab.sh:1603 etcd_cpu_apply. Refuses to guess an insertion point
    if resources.requests.cpu is entirely absent, exactly like the source -
    this raises/lowers an EXISTING request to an explicit target, it does not
    add one to a container that never declared it."""
    rc, cur = r.read(
        f"grep -A1 'requests:' {ETCD_MANIFEST} 2>/dev/null | grep 'cpu:' | "
        "awk '{print $2}'", 30)
    cur = [c.strip() for c in (cur or "").splitlines() if c.strip()]
    cur = cur[0] if cur else ""
    label = f"etcd: cpu request == {target_cpu}"
    if cur == target_cpu:
        return ok("cp.etcd_cpu", label, cluster=cl)
    if not cur:
        return warn("cp.etcd_cpu", label,
                    "no resources.requests.cpu found in etcd.yaml — not "
                    "guessing where to insert", cluster=cl)
    res = fail("cp.etcd_cpu", label, f"currently {cur}", cluster=cl)
    if may_act(r, "cp"):
        r.write(
            f"mkdir -p /root/manifest-bak && "
            f"cp {ETCD_MANIFEST} {ETCD_MANIFEST}.bak.$(date +%s) 2>/dev/null; "
            f"sed -i '0,/cpu: {cur}/s//cpu: {target_cpu}/' {ETCD_MANIFEST}",
            f"set etcd cpu request {cur} -> {target_cpu}",
            tier="persistent", timeout=60)
        res.action = f"cpu request -> {target_cpu}"
        if not r.dry_run:
            res.state = "warn"
            res.detail = f"was {cur}; patched (kubelet recreates the pod)"
    return res


def _etcd_compaction_check(r, cl):
    """etcd static-manifest auto-compaction enforcement, ported from
    vsp-stabilizer.sh:374 etcd_compaction_apply.
    """
    rc, mode = r.read(
        f"grep -oE -- '--auto-compaction-mode=[^[:space:]\"]+' {ETCD_MANIFEST} 2>/dev/null | "
        "head -1 | cut -d= -f2", 30)
    rc, retention = r.read(
        f"grep -oE -- '--auto-compaction-retention=[^[:space:]\"]+' {ETCD_MANIFEST} 2>/dev/null | "
        "head -1 | cut -d= -f2", 30)
    cur_mode = (mode or "").strip().splitlines()
    cur_mode = cur_mode[-1].strip() if cur_mode else ""
    cur_retention = (retention or "").strip().splitlines()
    cur_retention = cur_retention[-1].strip() if cur_retention else ""

    label = "etcd: auto-compaction == periodic (1h retention)"
    if cur_mode == "periodic" and cur_retention == "1h":
        return ok("cp.etcd_compaction", label, cluster=cl)

    res = fail("cp.etcd_compaction", label,
               f"currently mode='{cur_mode or 'unset'}' retention='{cur_retention or 'unset'}'",
               cluster=cl)
    if may_act(r, "cp"):
        r.write(
            f"mkdir -p /root/manifest-bak && "
            f"cp {ETCD_MANIFEST} {ETCD_MANIFEST}.bak.$(date +%s) 2>/dev/null; "
            f"sed -i -E '/--auto-compaction-mode=/d; /--auto-compaction-retention=/d' {ETCD_MANIFEST} && "
            f"sed -i '/--election-timeout=/a\\    - --auto-compaction-mode=periodic\\n    - --auto-compaction-retention=1h' {ETCD_MANIFEST}",
            "enable etcd auto-compaction (periodic, 1h retention)",
            tier="persistent", timeout=60)
        res.action = "auto-compaction -> periodic/1h"
        if not r.dry_run:
            res.state = "warn"
            res.detail = "patched (kubelet recreates the pod)"
    return res


def _kubevip_lease_guard(r, cl, section):
    """kube-vip static-manifest lease VALIDITY guard, ported from
    remediate-lab.sh:400 kubevip_guard. Only repairs an INVALID ordering
    (leaseduration>renewdeadline>retryperiod) - it never forces the exact
    60/40/6 numbers onto a site that is already validly ordered with
    different values, matching the source exactly. An invalid ordering
    panic-crashloops the CP-VIP pod."""
    rc, lease = r.read(
        "ld=$(awk '/name: vip_leaseduration/{getline; gsub(/[^0-9]/,\"\"); "
        f"print; exit}}' {KUBEVIP_MANIFEST}); "
        "rd=$(awk '/name: vip_renewdeadline/{getline; gsub(/[^0-9]/,\"\"); "
        f"print; exit}}' {KUBEVIP_MANIFEST}); "
        "rp=$(awk '/name: vip_retryperiod/{getline; gsub(/[^0-9]/,\"\"); "
        f"print; exit}}' {KUBEVIP_MANIFEST}); "
        'echo "LD=$ld RD=$rd RP=$rp"', 30)
    ld = rd = rp = ""
    for tok in (lease or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            if k == "LD": ld = v
            elif k == "RD": rd = v
            elif k == "RP": rp = v
    label = "kube-vip static manifest: lease ordering valid (leaseduration>renewdeadline>retryperiod)"
    key = f"{section}.kubevip_lease"
    if ld.isdigit() and rd.isdigit() and rp.isdigit() and int(ld) > int(rd) > int(rp):
        return ok(key, label, f"{ld}/{rd}/{rp}", cluster=cl)
    if not (ld and rd and rp):
        return warn(key, label, "manifest not readable/parseable", cluster=cl)
    res = fail(key, label, f"INVALID: {ld}/{rd}/{rp} — panic-crashloops the "
                           "CP-VIP pod", cluster=cl)
    if may_act(r, section):
        r.write(
            f"mkdir -p /root/manifest-bak; cp {KUBEVIP_MANIFEST} "
            f"/root/manifest-bak/kube-vip.yaml.bak.$(date +%s) 2>/dev/null; "
            "sed -E -i "
            "'/name: vip_leaseduration/{n;s/value: *\"?[0-9]+\"?/value: \""
            + VIP_LEASE_DURATION + "\"/}; "
            "/name: vip_renewdeadline/{n;s/value: *\"?[0-9]+\"?/value: \""
            + VIP_RENEW_DEADLINE + "\"/}; "
            "/name: vip_retryperiod/{n;s/value: *\"?[0-9]+\"?/value: \""
            + VIP_RETRY_PERIOD + "\"/}' "
            f"{KUBEVIP_MANIFEST}",
            f"repair invalid kube-vip lease ordering -> "
            f"{VIP_LEASE_DURATION}/{VIP_RENEW_DEADLINE}/{VIP_RETRY_PERIOD}",
            tier="transient", timeout=60)
        res.action = f"lease -> {VIP_LEASE_DURATION}/{VIP_RENEW_DEADLINE}/{VIP_RETRY_PERIOD}"
        if not r.dry_run:
            res.state, res.detail = "warn", f"was {ld}/{rd}/{rp}; repaired to 60/40/6"
    return res


# ─── Sections (VSP, read-only) ───────────────────────────────────────────────
# Each handler takes (r: Runner, ctx: dict) and returns list[CheckResult].
# vsp-health.py:740 breaks this contract by returning a tuple on one path, which
# silently records a 0 that is neither True nor counted as a failure.

def chk_cp(r, ctx):
    out = []
    cl = r.cluster
    cfg = CLUSTERS[cl]

    # Every VIP the cluster is supposed to own, not just the one we connected
    # through. On VCFA that is three (.72 CP, .69 vmsp-gateway, .70 vcfa-gateway)
    # and a dropped gateway VIP is exactly the "/automation is down" symptom.
    for vip in cfg.get("owned_vips") or [ctx["host"]]:
        reachable = ping(vip)
        hint = cfg.get("vip_hint", "dropped — check kube-vip")
        out.append(ok("cp.vip", f"VIP {vip}: reachable", cluster=cl) if reachable
                   else fail("cp.vip", f"VIP {vip}: reachable", hint, cluster=cl))

    # kube-vip rewrites plndr-cp-lock's leaseDurationSeconds from its own
    # hardcoded default on every renewal, so only a pathologically low value
    # means anything. auto-health.py v1.1 removed the != 120 warning for exactly
    # this reason; the death-spiral signal is < 10.
    if cfg.get("check_cp_lease"):
        rc, lease = r.read(
            "kubectl -n kube-system get lease plndr-cp-lock "
            "-o jsonpath={.spec.leaseDurationSeconds} 2>/dev/null", 30)
        val = (lease or "").strip().splitlines()
        val = val[-1].strip() if val else ""
        if rc == 0 and val.isdigit():
            n = int(val)
            if n < 10:
                out.append(fail("cp.lease", "plndr-cp-lock: leaseDurationSeconds >= 10",
                                f"={n} — kube-vip lease death spiral", cluster=cl))
            else:
                out.append(ok("cp.lease", "plndr-cp-lock: leaseDurationSeconds >= 10",
                              f"={n}", cluster=cl))
        else:
            out.append(warn("cp.lease", "plndr-cp-lock: leaseDurationSeconds >= 10",
                            "lease not readable", cluster=cl))

    if cfg.get("vip_watchdog_unit"):
        unit = cfg["vip_watchdog_unit"]
        rc, state = r.read(f"systemctl is-active {unit} 2>/dev/null", 30)
        st = (state or "").strip().splitlines()
        st = st[-1].strip() if st else ""
        if st == "active":
            out.append(ok("cp.watchdog", f"{unit}: active", cluster=cl))
        else:
            res_wd = warn("cp.watchdog", f"{unit}: active",
                          f"is '{st or 'not-found'}' — VIP re-add on drop is unprotected",
                          cluster=cl)
            if may_act(r, "cp"):
                r.write(f"systemctl enable --now {unit}",
                        f"enable and start {unit}", tier="persistent", timeout=60)
                res_wd.action = f"enabled and started {unit}"
                if not r.dry_run:
                    res_wd.state = "warn"
                    res_wd.detail = f"{unit} enabled and started"
            out.append(res_wd)

    rc, kvip = r.read(
        "grep -A1 vip_preserve_on_leadership_loss "
        "/etc/kubernetes/manifests/kube-vip.yaml 2>/dev/null", 30)
    if rc == 0 and "true" in kvip.lower():
        out.append(ok("cp.vip_preserve",
                      "kube-vip: vip_preserve_on_leadership_loss=true", cluster=cl))
    elif rc == 0 and kvip.strip():
        out.append(fail("cp.vip_preserve",
                        "kube-vip: vip_preserve_on_leadership_loss=true",
                        "reads false — kube-fix.py --skip-vip fixes the manifest",
                        cluster=cl))
    else:
        out.append(warn("cp.vip_preserve",
                        "kube-vip: vip_preserve_on_leadership_loss=true",
                        "manifest not readable", cluster=cl))

    rc, ps = r.read("crictl ps 2>/dev/null", 45)
    if rc != 0 or not ps.strip():
        out.append(warn("cp.static", "CP static pods: running",
                        "crictl unavailable or returned nothing", cluster=cl))
    else:
        for comp in cfg.get("static_pods", ()):
            present = comp in ps
            out.append(ok(f"cp.static.{comp}", f"{comp}: Running", cluster=cl) if present
                       else fail(f"cp.static.{comp}", f"{comp}: Running",
                                 "not in crictl ps — check CrashLoopBackOff", cluster=cl))
        if ctx["verbose"]:
            for line in ps.splitlines()[:12]:
                row_verbose(f"  {line}")

    # remediate-lab.sh Family B: KCM/scheduler lease timing + etcd CPU request +
    # kube-vip's own numeric lease-ordering guard. Runs on BOTH clusters -
    # remediate-lab.sh's own header states these families run "per-node on
    # both nodes", not VSP-only.
    lease_results = [
        _lease_tuning_check(r, cl, "kube-controller-manager", KCM_MANIFEST),
        _lease_tuning_check(r, cl, "kube-scheduler", SCHEDULER_MANIFEST),
        _etcd_cpu_check(r, cl, cfg.get("etcd_cpu_request", "2500m")),
        _etcd_compaction_check(r, cl),
        _kubevip_lease_guard(r, cl, "cp"),
    ]
    out.extend(lease_results)

    # --revert (remediate-lab.sh --revert-lease / revert_leader_elect / revert_etcd):
    # restore the newest backup this tool itself wrote for each manifest above.
    if ctx.get("cp_revert") and may_act(r, "cp"):
        for manifest, label in ((KCM_MANIFEST, "kube-controller-manager"),
                                (SCHEDULER_MANIFEST, "kube-scheduler"),
                                (ETCD_MANIFEST, "etcd"),
                                (KUBEVIP_MANIFEST, "kube-vip")):
            rc, latest = r.read(f"ls -t {manifest}.bak.* 2>/dev/null | head -1", 30)
            latest = (latest or "").strip().splitlines()
            latest = latest[-1].strip() if latest else ""
            res_label = f"{label}: reverted from newest backup"
            if not latest:
                out.append(warn("cp.revert", res_label,
                                "no backup found — nothing to revert", cluster=cl))
                continue
            res = ok("cp.revert", res_label, f"restoring {latest}", cluster=cl)
            r.write(f"cp {latest} {manifest}", f"revert {manifest} from {latest}",
                    tier="persistent", timeout=60)
            res.action = f"reverted from {latest}"
            out.append(res)

    # --kubelet-reload (opt-in, DISRUPTIVE per remediate-lab.sh's own framing -
    # excluded from its default run): kubelet's file watcher normally recreates
    # a static pod on manifest change without a full service restart; this is
    # only for the rare case where that watcher is stuck.
    if (ctx.get("kubelet_reload") and may_act(r, "cp") and not r.dry_run
            and any(res.action for res in lease_results)):
        r.write("systemctl restart kubelet", "restart kubelet (--kubelet-reload, "
                "forcing it to reconcile a stuck static-pod watcher)",
                tier="transient", timeout=90)
        out.append(CheckResult("cp.kubelet_reload", "kubelet: restarted to reconcile",
                               "warn", "requested via --kubelet-reload", cluster=cl,
                               action="restarted"))

    if may_act(r, "cp"):
        out.extend(_remediate_cp(r, ctx, out))
    return out


def chk_nodes(r, ctx):
    out = []
    cl = r.cluster
    data = ctx.get("nodes")
    if not data:
        return [warn("nodes", "Node list: readable", "kubectl get nodes failed", cluster=cl)]

    for item in data.get("items", []):
        name = item["metadata"]["name"]
        conds = {c["type"]: c["status"] for c in item.get("status", {}).get("conditions", [])}
        ready = conds.get("Ready") == "True"
        cordoned = item.get("spec", {}).get("unschedulable", False)
        if ready and not cordoned:
            out.append(ok("nodes.ready", f"Node {name}: Ready", cluster=cl))
        elif ready and cordoned:
            # Do NOT uncordon a node the cluster-autoscaler is deliberately
            # draining - vsp-health-monitor.py:2019 skips nodes tainted
            # ToBeDeletedByClusterAutoscaler for exactly this reason.
            taints = item.get("spec", {}).get("taints", []) or []
            autoscaler = any("ToBeDeletedByClusterAutoscaler" in (t.get("key") or "")
                             for t in taints)
            res = warn("nodes.ready", f"Node {name}: Ready and schedulable",
                       "cordoned; stale Argo system-shutdown workflow?", cluster=cl)
            if autoscaler:
                res.detail = ("cordoned by cluster-autoscaler "
                              "(ToBeDeletedByClusterAutoscaler) — leaving alone")
            elif may_act(r, "nodes"):
                r.write(f"kubectl uncordon {name}", f"uncordon node {name}",
                        tier="transient", timeout=60)
                res.action = "uncordoned"
                if not r.dry_run:
                    res.detail = "was cordoned; uncordoned"
            out.append(res)
        else:
            out.append(fail("nodes.ready", f"Node {name}: Ready",
                            f"condition={conds.get('Ready', 'unknown')}", cluster=cl))

    # Diagnostic context, not a check: the capacity-vs-requests table both legacy
    # readers print. It is the fastest way to see WHY pods are Pending, so it
    # shows in every mode rather than only under -v.
    rc, describe = r.read("kubectl describe nodes 2>/dev/null", 90)
    if rc == 0 and describe:
        print_node_capacity_table(describe)
    return out


def _cp_node_names(nodes_data):
    """Set of control-plane node names, for the CP-vs-Worker pod breakdown."""
    cp = set()
    for item in (nodes_data or {}).get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        if ("node-role.kubernetes.io/control-plane" in labels
                or "node-role.kubernetes.io/master" in labels):
            cp.add(item["metadata"]["name"])
    return cp


def chk_pods(r, ctx):
    out = []
    cl = r.cluster
    # -o wide adds the NODE column, which is what makes the CP/Worker split
    # possible. vsp-health.py:592-598 notes -o json for all pods returns
    # megabytes and can exceed SSH pipe buffers; the text form is ~100x smaller.
    rc, text = r.read("kubectl get pods -A -o wide --no-headers 2>/dev/null", 60)
    if rc != 0 or not text.strip():
        return [warn("pods", "Pod list: readable", "kubectl get pods -A failed", cluster=cl)]

    cp_nodes = _cp_node_names(ctx.get("nodes"))
    all_nodes = {item["metadata"]["name"]
                 for item in (ctx.get("nodes") or {}).get("items", [])}

    by_ns = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ns, name, ready_col, status = parts[0], parts[1], parts[2], parts[3]
        # Do NOT index the NODE column positionally. RESTARTS renders as
        # "3 (2d ago)", which splits into three whitespace fields and shifts
        # every later column - that made the CP/Worker split read 1 CP instead
        # of 10. Match against the known node names instead: they are unique
        # tokens, so an exact field match is unambiguous regardless of layout.
        node = next((p for p in parts[4:] if p in all_nodes), "")
        rec = by_ns.setdefault(
            ns, {"total": 0, "healthy": 0, "bad": [], "cp": 0, "worker": 0})
        rec["total"] += 1

        # A pod can be STATUS=Running and still be broken: "1/2" means a
        # container is not passing readiness. auto-health.py catches this
        # (logging-operator-fluentd-0 NotReady(1/2) is the canonical example -
        # the fluentd buffer-fill failure mode); a STATUS-only check misses it
        # entirely. Completed/Succeeded pods legitimately read 0/1, so they are
        # exempt.
        not_ready = ""
        if "/" in ready_col and status not in ("Completed", "Succeeded"):
            a, _, b = ready_col.partition("/")
            if a.isdigit() and b.isdigit() and int(a) < int(b):
                not_ready = f"NotReady({ready_col})"

        healthy = status in ("Running", "Completed", "Succeeded") and not not_ready
        if healthy:
            rec["healthy"] += 1
        if status in BAD_POD_STATES:
            rec["bad"].append((name, status, node))
        elif not_ready:
            rec["bad"].append((name, not_ready, node))
        # Unscheduled pods have no node yet; count them as neither.
        if node and node != "<none>":
            if node in cp_nodes:
                rec["cp"] += 1
            else:
                rec["worker"] += 1

    def order(ns):
        return (NS_PRIORITY.index(ns) if ns in NS_PRIORITY else len(NS_PRIORITY), ns)

    width = 34
    node_count = len((ctx.get("nodes") or {}).get("items", []))
    # Show the split only when we actually know the node roles. Printing
    # "0 CP / N Worker" because the node list was never fetched is worse than
    # printing nothing - it looks like a finding.
    show_split = bool(cp_nodes) and node_count > 1

    for ns in sorted(by_ns, key=order):
        rec = by_ns[ns]
        # Parity with vsp-health.py v2.9.0's breakdown. Suppressed on a
        # single-node cluster (VCFA), where "5 CP / 0 Worker" is just noise.
        split = (f" ({rec['cp']} CP / {rec['worker']} Worker)" if show_split else "")
        label = f"{ns:<{width}} {rec['healthy']}/{rec['total']} healthy{split}"
        if rec["bad"]:
            detail = ", ".join(f"{n}={s}" for n, s, _ in rec["bad"][:3])
            if len(rec["bad"]) > 3:
                detail += f" (+{len(rec['bad']) - 3} more)"
            out.append(fail("pods.ns", label, detail, cluster=cl))
            if ctx["verbose"]:
                for n, s, node in rec["bad"]:
                    role = "CP" if node in cp_nodes else "Worker"
                    row_verbose(f"  {ns}/{n}: {s}  [{role}: {node or '-'}]")
        else:
            out.append(ok("pods.ns", label, cluster=cl))

    if may_act(r, "pods"):
        out.extend(_sweep_bad_pods(r, by_ns, ctx))
    return out


def _sweep_bad_pods(r, by_ns, ctx):
    """Delete stuck pods so their controller recreates them. DAMPED by default.

    Two legacy sweepers implement irreconcilable policies (report finding F8):
    supervisor_stabilizer.py:1959 force-deletes anything terminal or stuck with
    no restart threshold, no per-cycle cap and no exclusions, and also deletes
    Succeeded pods as housekeeping; vsp-health-monitor.py:1384 is deliberately
    damped "to avoid thrash" with restartCount >= 5, a 15-per-cycle cap and
    static-pod/gateway/CSI exclusions.

    The damped policy is the default here because every one of its exclusions
    has a documented reason, while the aggressive one deletes legitimately
    completed Job pods. --aggressive opts into the unthresholded behaviour.
    """
    cl = r.cluster
    aggressive = ctx.get("aggressive", False)

    candidates = []
    for ns, rec in by_ns.items():
        for name, state, _node in rec["bad"]:
            if not aggressive:
                if state not in ACTIONABLE_POD_STATES:
                    continue                      # NotReady(x/y) is not deletable
                if any(name.startswith(p) for p in CP_STATIC_POD_PREFIXES):
                    continue                      # kubelet owns these, not us
                if any(s in name for s in SWEEP_EXCLUDE_SUBSTRINGS):
                    continue
            candidates.append((ns, name, state))

    if not candidates:
        return []

    out = []
    if not aggressive:
        # Restart count gates the delete: a pod that has restarted once is
        # probably still starting, not wedged.
        gated = []
        for ns, name, state in candidates:
            # VCFA one-shot terminal job/workflow pods (configure-component-*, etc.) stay at restartCount=0.
            # Allow them to be swept when in Error/Failed state without requiring >= POD_RESTART_THRESHOLD.
            is_vcfa_terminal_job = (cl == "vcfa" and state in ("Error", "Failed", "CrashLoopBackOff") and
                                   ("-job-" in name or "-execute-script-" in name or "-workflow-" in name or name.startswith("system-shutdown-")))
            rc, rs = r.read(
                f"kubectl get pod {name} -n {ns} "
                "-o jsonpath={.status.containerStatuses[*].restartCount} 2>/dev/null", 40)
            counts = [int(x) for x in (rs or "").split() if x.isdigit()]
            worst = max(counts) if counts else 0
            if worst >= POD_RESTART_THRESHOLD or is_vcfa_terminal_job:
                gated.append((ns, name, state, worst))
        gated.sort(key=lambda t: t[3], reverse=True)      # worst first
        dropped = len(gated) - POD_SWEEP_CAP
        gated = gated[:POD_SWEEP_CAP]
        if dropped > 0:
            # Never silently truncate: a capped sweep that says nothing reads as
            # "everything handled".
            out.append(warn("pods.sweep", f"pod sweep: all candidates addressed",
                            f"capped at {POD_SWEEP_CAP}; {dropped} left for the "
                            f"next pass", cluster=cl))
        selected = [(ns, n, s) for ns, n, s, _ in gated]
        skipped = len(candidates) - len(selected) - max(0, dropped)
        if skipped > 0:
            out.append(ok("pods.sweep",
                          f"pod sweep: {skipped} pod(s) below the "
                          f"{POD_RESTART_THRESHOLD}-restart threshold left alone",
                          cluster=cl))
    else:
        selected = candidates

    for ns, name, state in selected:
        r.write(f"kubectl delete pod {name} -n {ns} --grace-period=0 --force",
                f"force-delete {ns}/{name} ({state})", tier="transient", timeout=60)
        res = warn("pods.sweep", f"{ns}/{name}: recreated by its controller",
                   f"was {state}", cluster=cl)
        res.action = "force-deleted"
        out.append(res)
    return out


def chk_certs(r, ctx):
    out = []
    cl = r.cluster
    data = r.read_json("kubectl get certificates -A -o json 2>/dev/null", 60)
    if not data:
        return [warn("certs", "cert-manager Certificates: readable",
                     "no Certificate CRDs or kubectl failed", cluster=cl)]

    items = data.get("items", [])
    healthy = 0
    needs_renewal = False
    for item in items:
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        conds = {c["type"]: c["status"] for c in item.get("status", {}).get("conditions", [])}
        not_after = item.get("status", {}).get("notAfter", "")
        days = _days_until(not_after)
        label = f"{ns}/{name}: Ready and valid >{CERT_WARN_DAYS}d"

        if days is not None and days < ctx["threshold_days"]:
            needs_renewal = True

        if conds.get("Ready") != "True":
            out.append(fail("certs.ready", label, "Ready=False", cluster=cl,
                            residual_days=days))
        elif days is None:
            out.append(warn("certs.expiry", label, "notAfter unparseable", cluster=cl))
        elif days < 0:
            out.append(fail("certs.expiry", label, f"EXPIRED {abs(days)}d ago ⚠",
                            cluster=cl, residual_days=days))
        elif days < CERT_WARN_DAYS:
            out.append(warn("certs.expiry", label, f"expires in {days}d ⚠",
                            cluster=cl, residual_days=days))
        else:
            healthy += 1
            if ctx["verbose"]:
                row_verbose(f"  {ns}/{name}: {days}d remaining")

    if healthy:
        if r.mode == "report":
            # report is the diagnostic view: emit a row PER certificate, matching
            # vsp-health.py, which lists each one. This is most of why a report
            # shows many more checks than a preflight - it is per-item detail, not
            # extra coverage.
            for item in items:
                conds = {c["type"]: c["status"]
                         for c in item.get("status", {}).get("conditions", [])}
                if conds.get("Ready") != "True":
                    continue
                days = _days_until(item.get("status", {}).get("notAfter", ""))
                if days is None or days < CERT_WARN_DAYS:
                    continue
                out.append(ok("certs.item",
                              f"{item['metadata']['namespace']}/"
                              f"{item['metadata']['name']}: Ready",
                              f"expires in {days}d", cluster=cl, residual_days=days))
        else:
            # preflight/tune/remediate want a verdict, so collapse the healthy bulk
            # rather than inflating the denominator (auto-health.py:776 does this).
            out.append(ok("certs.bulk",
                          f"{healthy}/{len(items)} certificates Ready and valid "
                          f">{CERT_WARN_DAYS}d", cluster=cl))

        if cl == "vcfa":
            # Service-TLS cert freshness check across prelude deployments (vcfa-stabilizer.sh check_and_fix_ccs_k3s_cert port)
            rc_sec, cert_b64 = r.read("kubectl get secret service-tls -n prelude -o jsonpath='{.data.tls\\.crt}' 2>/dev/null", 30)
            if rc_sec == 0 and (cert_b64 or "").strip():
                rc_dt, nbf_str = r.read(f"echo '{(cert_b64 or '').strip()}' | base64 -d | openssl x509 -noout -startdate 2>/dev/null", 30)
                nbf_val = (nbf_str or "").partition("=")[2].strip()
                cert_nbf = _parse_openssl_date(nbf_val)
                if cert_nbf:
                    prelude_deps = [
                        "abx-service-app", "approval-service-app", "catalog-service-app", "ccs-avi-eas-app",
                        "ccs-gateway-app", "ccs-infra-eas-app", "ccs-k3s-app", "ccs-nsx-eas-app", "ccs-vksm-eas",
                        "cgs-service-app", "cloud-automation-ui-app", "ebs-app", "encryption-manager",
                        "extensibility-ui-app", "hcmp-service-app", "orchestration-ui-app", "provisioning-service-app",
                        "provisioning-ui-app", "relocation-service-app", "relocation-ui-app",
                        "tango-blueprint-service-app", "tango-uber-service-app", "terraform-service-app",
                        "vcfa-service-manager"
                    ]
                    stale_deps = []
                    for dep in prelude_deps:
                        rc_pod, pstart = r.read(f"kubectl get pod -n prelude -l app={dep} --field-selector=status.phase=Running -o jsonpath='{{.items[0].status.startTime}}' 2>/dev/null", 20)
                        pstart_val = (pstart or "").strip().splitlines()
                        pstart_val = pstart_val[-1].strip() if pstart_val else ""
                        if pstart_val:
                            pod_ts = _parse_iso_date(pstart_val)
                            if pod_ts and pod_ts < cert_nbf:
                                stale_deps.append(dep)
                    if stale_deps:
                        res_stale = fail("certs.service_tls", f"prelude deployments: service-tls fresh across all {len(prelude_deps)} apps",
                                         f"{len(stale_deps)} deployment(s) running with stale in-memory certs: {', '.join(stale_deps[:3])}", cluster=cl)
                        if may_act(r, "certs"):
                            for sdep in stale_deps:
                                r.write(f"kubectl rollout restart deployment/{sdep} -n prelude",
                                        f"rollout restart {sdep} to mount renewed service-tls cert", tier="transient", timeout=60)
                            res_stale.action = f"restarted {len(stale_deps)} stale prelude deployment(s)"
                            if not r.dry_run:
                                res_stale.state = "warn"
                                res_stale.detail = f"rollout restart issued for {len(stale_deps)} deployment(s)"
                        out.append(res_stale)
                    else:
                        out.append(ok("certs.service_tls", f"prelude deployments: service-tls fresh across all {len(prelude_deps)} apps", cluster=cl))

    if needs_renewal and may_act(r, "certs") and not ctx.get("certs_renewed"):
        out.extend(_delegate_cert_renewal(r, ctx))
        ctx["certs_renewed"] = True

    return out


def chk_proxy(r, ctx):
    out = []
    cl = r.cluster
    if not _HAVE_LSF:
        return [warn("proxy", "Node proxy config: matches canonical values",
                     "lsfunctions not importable — cannot determine expected values",
                     cluster=cl)]

    expected_url = lsf.LAB_PROXY_URL
    data = ctx.get("nodes")
    if not data:
        return [warn("proxy", "Node proxy config: matches canonical values",
                     "node list unavailable", cluster=cl)]

    node_ips = []
    for item in data.get("items", []):
        for addr in item.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP":
                node_ips.append(addr["address"])

    for ip in node_ips:
        rc, res = r.read_on(
            ip,
            "for f in /etc/sysconfig/proxy "
            "/etc/systemd/system/containerd.service.d/http-proxy.conf "
            "/etc/systemd/system/kubelet.service.d/http-proxy.conf; do "
            f"grep -qF '{expected_url}' \"$f\" 2>/dev/null || echo \"MISSING:$f\"; done; "
            "echo DONE", 45)
        label = f"{ip}: proxy configured ({expected_url})"
        if rc != 0:
            out.append(warn("proxy.node", label, "node unreachable", cluster=cl))
        elif "MISSING:" in res:
            missing = [ln.split(":", 1)[1] for ln in res.splitlines()
                       if ln.startswith("MISSING:")]
            out.append(fail("proxy.node", label,
                            f"drift in {len(missing)} file(s) — "
                            f"{os.path.basename(missing[0])}…", cluster=cl))
            if ctx["verbose"]:
                for m in missing:
                    row_verbose(f"  missing/stale: {m}")
        else:
            out.append(ok("proxy.node", label, cluster=cl))
            continue

        # Durable repair: write the canonical files, then restart ONLY the
        # service whose drop-in actually changed. systemd Environment= takes
        # effect on restart, so a bare daemon-reload leaves the new proxy env
        # inert until the next reboot - the same defect found in
        # confighol-9.1.py and fixed there on 2026-08-14. Checksum-gating the
        # restart keeps a re-run from bouncing containerd for nothing.
        if may_act(r, "proxy"):
            no_proxy = lsf.build_lab_no_proxy()
            script = _proxy_repair_script(expected_url, no_proxy)
            b64 = base64.b64encode(script.encode()).decode()
            r.write(f"echo {b64} | base64 -d > /tmp/vlt-proxy.sh && "
                    f"bash /tmp/vlt-proxy.sh; rc=$?; rm -f /tmp/vlt-proxy.sh; exit $rc",
                    f"write canonical proxy config on {ip} and restart only the "
                    f"services whose drop-in changed",
                    tier="persistent", timeout=180)
            res = out[-1]
            res.action = "proxy config written"
            if not r.dry_run:
                res.state = "warn"
                res.detail = "drift corrected"
    return out


def _proxy_repair_script(proxy_url, no_proxy):
    """Canonical per-node proxy config. Values come from lsfunctions, never inline."""
    return f"""#!/bin/bash
set -u
CTD=/etc/systemd/system/containerd.service.d/http-proxy.conf
KUBE=/etc/systemd/system/kubelet.service.d/http-proxy.conf

CTD_OLD=$(md5sum "$CTD" 2>/dev/null | cut -d' ' -f1)
KUBE_OLD=$(md5sum "$KUBE" 2>/dev/null | cut -d' ' -f1)

cat > /etc/sysconfig/proxy <<'EOF'
PROXY_ENABLED="yes"
HTTP_PROXY="{proxy_url}"
HTTPS_PROXY="{proxy_url}"
FTP_PROXY=""
GOPHER_PROXY=""
SOCKS_PROXY=""
SOCKS5_SERVER=""
NO_PROXY="{no_proxy}"
EOF

sed -i '/^http_proxy=/d;/^https_proxy=/d;/^no_proxy=/d;/^HTTP_PROXY=/d;/^HTTPS_PROXY=/d;/^NO_PROXY=/d' /etc/environment
cat >> /etc/environment <<'EOF'
http_proxy={proxy_url}
https_proxy={proxy_url}
no_proxy={no_proxy}
HTTP_PROXY={proxy_url}
HTTPS_PROXY={proxy_url}
NO_PROXY={no_proxy}
EOF

mkdir -p /etc/systemd/system/containerd.service.d /etc/systemd/system/kubelet.service.d
cat > "$CTD" <<'EOF'
[Service]
Environment="HTTP_PROXY={proxy_url}"
Environment="HTTPS_PROXY={proxy_url}"
Environment="NO_PROXY={no_proxy}"
EOF
cp "$CTD" "$KUBE"

CTD_NEW=$(md5sum "$CTD" 2>/dev/null | cut -d' ' -f1)
KUBE_NEW=$(md5sum "$KUBE" 2>/dev/null | cut -d' ' -f1)

systemctl daemon-reload
if [ "$CTD_OLD" != "$CTD_NEW" ]; then
    systemctl restart containerd && echo CONTAINERD_RESTARTED
else
    echo CONTAINERD_UNCHANGED
fi
if [ "$KUBE_OLD" != "$KUBE_NEW" ]; then
    systemctl restart kubelet && echo KUBELET_RESTARTED
else
    echo KUBELET_UNCHANGED
fi
echo PROXY_CONFIGURED
"""


def chk_kubeadm(r, ctx):
    cl = r.cluster
    rc, text = r.read("kubeadm certs check-expiration 2>&1", 90)
    if rc != 0 or not text.strip():
        return [warn("kubeadm", "kubeadm certificates: valid",
                     "kubeadm certs check-expiration unavailable", cluster=cl)]

    worst = None
    rows = 0
    for line in text.splitlines():
        m = re.search(r"^(\S+)\s+.*?\s(\d+)([dyhm])\b", line)
        if not m:
            continue
        rows += 1
        n = int(m.group(2))
        days = n * 365 if m.group(3) == "y" else n
        if worst is None or days < worst[1]:
            worst = (m.group(1), days)
        if ctx["verbose"]:
            row_verbose(f"  {line.strip()}")

    if worst is None:
        return [warn("kubeadm", "kubeadm certificates: valid",
                     "could not parse expiration table", cluster=cl)]
    name, days = worst
    label = f"kubeadm certs: all {rows} valid >{ctx['threshold_days']}d"
    if days >= ctx["threshold_days"]:
        return [ok("kubeadm", label, f"soonest {name} {days}d", cluster=cl,
                   residual_days=days)]

    res = (fail if days < 0 else warn)(
        "kubeadm", label,
        (f"{name} EXPIRED" if days < 0 else f"soonest is {name} at {days}d"),
        cluster=cl, residual_days=days)
    out = [res]
    if may_act(r, "kubeadm") and not ctx.get("certs_renewed"):
        out.extend(_delegate_cert_renewal(r, ctx))
        ctx["certs_renewed"] = True
    return out


def _delegate_cert_renewal(r, ctx):
    """Hand certificate renewal to vsp_cert_renewer.py rather than reimplementing it.

    That script is the best-engineered file in the legacy set and its guards were
    won the hard way: CA rotation gated on remaining life (not desired duration,
    which made Phase 3.0 fire every boot and generate a NEW CA key pair each
    time, invalidating every leaf cert), the unconditional isCA skip, and the
    cert-manager Issuer-cache settle. Reimplementing any of that would be
    strictly worse. Runs on the MANAGER, so it goes through Runner.local().
    """
    cl = r.cluster
    script = "/home/holuser/hol/Tools/vsp_cert_renewer.py"
    if not os.path.isfile(script):
        return [warn("certs.renew", "certificate renewal: delegated",
                     f"{script} not found", cluster=cl)]
    if cl not in ("vsp", "vcfa"):
        return [warn("certs.renew", "certificate renewal: delegated",
                     f"no cert-renewer cluster mapping for '{cl}'", cluster=cl)]

    argv = ["python3", "-u", script, "--cluster", cl,
            "--threshold-days", str(ctx["threshold_days"]), "--no-timestamps"]
    if ctx.get("site"):
        argv.extend(["--site", ctx["site"]])
    if r.dry_run:
        argv.append("--dry-run")
    rc, out = r.local(argv, f"run vsp_cert_renewer.py --cluster {cl} "
                            f"--threshold-days {ctx['threshold_days']}",
                      timeout=900)
    # Its tag vocabulary is a contract (vsp-health-monitor.py:2260 parses these);
    # a bare "renew" substring match false-positived every cycle there.
    errors = [ln for ln in (out or "").splitlines() if "ERROR  :" in ln]
    renewed = [ln for ln in (out or "").splitlines() if "RENEWED:" in ln]
    if errors:
        return [fail("certs.renew", "certificate renewal: completed cleanly",
                     f"{len(errors)} error line(s) from vsp_cert_renewer.py",
                     cluster=cl)]
    # If it actually renewed something, it's a pass. If it didn't, it's a warning
    # that we called it but it decided not to act (which is what happened here).
    res = (ok if renewed else warn)("certs.renew", "certificate renewal: completed cleanly",
               f"{len(renewed)} cert(s) renewed" if renewed else "no renewal needed",
               cluster=cl)
    res.action = "delegated to vsp_cert_renewer.py"
    return [res]


MANIFEST_DIR = "/etc/kubernetes/manifests"
SHADOW_BAK_DIR = "/root/manifest-bak"
# Litter patterns, deliberately NOT an allowlist of expected pod names: this runs
# against clusters that legitimately carry different static pods, and an
# allowlist would silently move a real one out of the way
# (remediate-lab.sh:1307-1309).
SHADOW_GLOBS = ("*.bak*", "*~", "*.orig", "*.save", "*.rpmsave", "*.rpmnew",
                "*.dpkg-*", "*.tmp", "*.swp", "*.old", "*.disabled")


def _sweep_static_pod_shadows(r):
    """Move non-manifest litter out of the static-pod directory. MUST run first.

    This is the single most valuable piece of institutional knowledge in
    remediate-lab.sh (:1261-1270). On pod 2701, seven `*.bak.*` files had
    accumulated in staticPodPath and "EVERY static-pod edit since 2026-05-11 was
    inert -- the apiserver stuck at 250m while its manifest said 1000m". It was
    misdiagnosed for weeks, and a kubelet restart does NOT clear it.

    So: sweep before editing any manifest, or the edit reports success and
    changes nothing.
    """
    pats = " ".join(f"'{g}'" for g in SHADOW_GLOBS)
    rc, out = r.read(
        f"found=0; for pat in {pats}; do "
        f"for f in {MANIFEST_DIR}/$pat; do "
        f"[ -e \"$f\" ] || continue; echo \"SHADOW:$f\"; found=1; done; done; "
        "echo SWEEP_DONE", 45)
    shadows = [ln.split(":", 1)[1] for ln in (out or "").splitlines()
               if ln.startswith("SHADOW:")]
    if not shadows:
        return [], True

    res = fail("cp.shadow", f"{MANIFEST_DIR}: no shadow files",
               f"{len(shadows)} present — every static-pod edit is INERT until "
               f"they are moved", cluster=r.cluster)
    if may_act(r, "cp"):
        r.write(f"mkdir -p {SHADOW_BAK_DIR} && "
                f"for pat in {pats}; do "
                f"for f in {MANIFEST_DIR}/$pat; do "
                f"[ -e \"$f\" ] && mv -f \"$f\" {SHADOW_BAK_DIR}/; done; done; "
                "echo MOVED",
                f"move {len(shadows)} shadow file(s) out of {MANIFEST_DIR} "
                f"into {SHADOW_BAK_DIR}", tier="persistent", timeout=60)
        res.action = "swept"
        if not r.dry_run:
            res.state = "warn"
            res.detail = f"{len(shadows)} moved to {SHADOW_BAK_DIR}"
        return [res], True
    # Read-only: report, and tell the caller manifest edits cannot be trusted.
    return [res], False


def _remediate_cp(r, ctx, findings):
    """Repair actions for the cp section, ordered so each is actually effective."""
    out = []
    cl = r.cluster
    cfg = CLUSTERS[cl]
    keys = {f.key for f in findings if f.state == "fail"}

    # Shadow sweep first: it gates whether any manifest edit below can work.
    if keys & {"cp.vip_preserve"}:
        rows, manifests_trustworthy = _sweep_static_pod_shadows(r)
        out.extend(rows)
    else:
        manifests_trustworthy = True

    # Dropped VIP: re-add and gratuitous-ARP. A backstop only - kube-vip should
    # reclaim it; vcfa-stabilizer.sh:1483 labels it exactly that way.
    for f in findings:
        if f.key == "cp.vip" and f.state == "fail":
            vip = f.label.split()[1].rstrip(":")
            r.write(f"ip addr replace {vip}/32 dev eth0 valid_lft forever "
                    f"preferred_lft forever && "
                    f"(command -v arping >/dev/null && arping -c 3 -U -I eth0 {vip} "
                    f">/dev/null 2>&1; true) && echo VIP_ADDED",
                    f"re-add VIP {vip} to eth0 (backstop; kube-vip should reclaim)",
                    tier="transient", timeout=60)
            res = warn("cp.vip", f"VIP {vip}: reachable", "re-added as a backstop",
                       cluster=cl)
            res.action = "vip re-added"
            out.append(res)

    # vip_preserve_on_leadership_loss: on-disk manifest edit, durable.
    if "cp.vip_preserve" in keys:
        if not manifests_trustworthy:
            out.append(warn("cp.vip_preserve",
                            "kube-vip: vip_preserve_on_leadership_loss=true",
                            "not edited — shadow files present would make it inert",
                            cluster=cl))
        else:
            r.write(
                f"sed -i '/vip_preserve_on_leadership_loss/{{n; s/\"false\"/\"true\"/}}' "
                f"{MANIFEST_DIR}/kube-vip.yaml && "
                f"grep -A1 vip_preserve_on_leadership_loss {MANIFEST_DIR}/kube-vip.yaml",
                "set vip_preserve_on_leadership_loss=true in kube-vip.yaml",
                tier="persistent", timeout=60)
            res = warn("cp.vip_preserve",
                       "kube-vip: vip_preserve_on_leadership_loss=true",
                       "manifest updated", cluster=cl)
            res.action = "manifest patched"
            out.append(res)

    # Crashed control-plane containers. KCM and scheduler ONLY: kube-fix.py:398,406
    # restarts just those two, and removing etcd or the apiserver to "fix" them is
    # a much larger gamble than this is worth.
    for f in findings:
        if not (f.key.startswith("cp.static.") and f.state == "fail"):
            continue
        comp = f.key.rsplit(".", 1)[1]
        if comp not in ("kube-controller-manager", "kube-scheduler"):
            out.append(warn(f.key, f.label,
                            f"not auto-restarted: {comp} is too load-bearing to "
                            f"bounce blind — inspect it directly", cluster=cl))
            continue
        r.write(f"crictl ps -a --name '{comp}' -q | xargs -r crictl rm -f",
                f"remove crashed {comp} container so kubelet recreates it",
                tier="transient", timeout=90)
        res = warn(f.key, f.label, "container removed; kubelet will recreate",
                   cluster=cl)
        res.action = "crictl rm -f"
        out.append(res)

    # plndr-cp-lock lease death spiral: delete the lease, then set a sane floor.
    if "cp.lease" in keys:
        r.write("kubectl -n kube-system delete lease plndr-cp-lock && sleep 5 && "
                "kubectl -n kube-system patch lease plndr-cp-lock --type=merge "
                "-p '{\"spec\":{\"leaseDurationSeconds\":60}}'",
                "delete plndr-cp-lock and reset leaseDurationSeconds=60",
                tier="transient", timeout=90)
        res = warn("cp.lease", "plndr-cp-lock: leaseDurationSeconds >= 10",
                   "lease reset to 60", cluster=cl)
        res.action = "lease reset"
        out.append(res)

    return out


def chk_postgres(r, ctx):
    """Patroni/spilo data-directory permissions and readiness.

    PostgreSQL refuses to start when pgdata is group-writable:
        FATAL:  data directory ".../pgdata/pgroot/data" has invalid permissions
        DETAIL: Permissions should be u=rwx (0700) or u=rwx,g=rx (0750).
    The pod's fsGroup context can leave it at 2770 (setgid + g=rwx), which is
    neither accepted form. `chmod g-rwx` lands on 2700, the state every healthy
    pod in this lab has, and preserves setgid.

    Two things the legacy tooling gets wrong and this deliberately does not:

    1. salt-stabilize.py:267 and vsp-health-monitor.py:1449a both hardcode
       salt-raas/pgdatabase-0. Found live 2026-08-14:
       vidb-external/vidb-postgres-instance-0 had sat at 2/3 for 43 DAYS with
       the identical fault and nothing was looking at it. This sweeps every
       spilo pod in every known namespace instead.
    2. postgres validates the permission only at STARTUP, so a pod with bad
       permissions that is currently Running is a LATENT failure, not a healthy
       one - it breaks on its next restart. Found live on the same day:
       vcf-sddc-lcm-db-1 was 3/3 with 2770. So permissions are corrected
       everywhere, but only genuinely not-ready pods are restarted: bouncing a
       serving database to fix a latent problem is the more disruptive choice.
    """
    out = []
    cl = r.cluster
    cfg = CLUSTERS[cl]
    namespaces = cfg.get("pg_namespaces") or ()
    if not namespaces:
        return []

    for ns in namespaces:
        rc, names = r.read(
            f"kubectl get pods -n {ns} -l application=spilo -o name 2>/dev/null "
            "| cut -d/ -f2", 40)
        if rc != 0:
            continue
        for pod in [p.strip() for p in (names or "").splitlines() if p.strip()]:
            rc, info = r.read(
                f"kubectl exec {pod} -n {ns} -c walg -- stat -c %a {PGDATA_DIR} "
                f"2>/dev/null; kubectl get pod {pod} -n {ns} --no-headers "
                "2>/dev/null | awk '{print $2}'", 60)
            lines = [x.strip() for x in (info or "").splitlines() if x.strip()]
            perms = lines[0] if lines else ""
            ready = lines[1] if len(lines) > 1 else ""
            label = f"{ns}/{pod}: pgdata perms accepted by postgres"

            if not perms.isdigit():
                out.append(warn("postgres.perms", label,
                                "could not read permissions", cluster=cl))
                continue

            if perms in PGDATA_OK_PERMS:
                out.append(ok("postgres.perms", label,
                              f"{perms}, ready={ready}", cluster=cl))
                continue

            # Bad permissions. Distinguish "broken now" from "breaks next restart".
            have, _, want = ready.partition("/")
            not_ready = have != want if (have and want) else False
            detail = (f"{perms} — postgres requires 0700 or 0750"
                      + ("" if not_ready else "; currently Running, so this breaks"
                                              " on its NEXT restart (latent)"))
            res = fail("postgres.perms", label, detail, cluster=cl)

            if may_act(r, "postgres"):
                r.write(f"kubectl exec {pod} -n {ns} -c walg -- "
                        f"chmod g-rwx {PGDATA_DIR}",
                        f"chmod g-rwx {PGDATA_DIR} in {ns}/{pod} "
                        f"(perms {perms} -> 2700)",
                        tier="persistent", timeout=60)
                if not_ready:
                    r.write(f"kubectl delete pod {pod} -n {ns} --grace-period=30",
                            f"restart {ns}/{pod} so postgres retries crash recovery",
                            tier="transient", timeout=90)
                    res.action = "perms corrected; pod restarted"
                else:
                    res.action = "perms corrected; no restart (pod is serving)"
                if not r.dry_run:
                    res.state = "warn"
                    res.detail = f"was {perms}; {res.action}"
            out.append(res)

    return out


def chk_endpoint(r, ctx):
    """VCFA user-facing endpoint. Probed FROM the node, resolving to the gateway VIP.

    The distinction matters: /automation is served via the VIP only, so curling
    the node's own IP returns connection-refused and reads as an outage that
    isn't one. remediate-lab.sh:1094-1101 documents losing time to exactly this.
    """
    cl = r.cluster
    cfg = CLUSTERS[cl]
    fqdn, vip = cfg.get("endpoint_fqdn"), cfg.get("endpoint_vip")
    if not (fqdn and vip):
        return []
    rc, out = r.read(
        f"curl -k -s -o /dev/null -w '%{{http_code}}' --connect-timeout 8 "
        f"--resolve {fqdn}:443:{vip} https://{fqdn}{cfg['endpoint_path']} 2>/dev/null", 45)
    code = (out or "").strip().splitlines()
    code = code[-1].strip() if code else ""
    label = f"https://{fqdn}{cfg['endpoint_path']}: HTTP 200"
    if code == "200":
        return [ok("endpoint", label, cluster=cl)]
    if not code.isdigit():
        return [warn("endpoint", label, "no response code from curl", cluster=cl)]
    # DETECT-ONLY BY DESIGN. A non-200 here is a symptom, not a thing you fix at
    # this layer - the cause is upstream (gateway pods, prelude scaled to 0, a
    # stale shutdown workflow), and each has its own section with its own guards.
    # vsp-health-monitor.py:2594 makes the same call for VIPs: "a VIP down while
    # its backing pod is healthy should be surfaced (not blindly auto-restarted)".
    return [fail("endpoint", label,
                 f"HTTP {code} — symptom, not remediated here; check the pods / "
                 f"deployments sections, or vcfa-stabilizer.sh --fix-post-boot",
                 cluster=cl)]


def chk_deployments(r, ctx):
    """Named core / auth deployments must be fully available.

    Inventory ported from auto-health.py:104-141. Uses one bulk fetch, and the
    replicas=0 handling that vsp-health-monitor.py got wrong until 2.13: a
    deployment legitimately scaled to 0 must not read as 0/1 not-ready.
    """
    cl = r.cluster
    cfg = CLUSTERS[cl]
    wanted = cfg.get("deployments") or ()
    if not wanted:
        return []

    data = r.read_json("kubectl get deployments -A -o json 2>/dev/null", 60)
    if not data:
        return [warn("deploy", "Deployment list: readable",
                     "kubectl get deployments -A failed", cluster=cl)]

    live = {}
    live_annotations = {}
    for item in data.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        spec_rep = item.get("spec", {}).get("replicas")
        desired = spec_rep if spec_rep is not None else 1
        ready = item.get("status", {}).get("readyReplicas", 0) or 0
        live[(ns, name)] = (ready, desired)
        rec = (item["metadata"].get("annotations") or {}).get(
            "vcf.lab/original-replicas")
        if rec and rec.isdigit() and int(rec) > 0:
            live_annotations[(ns, name)] = int(rec)

    out = []
    for ns, name, severity in wanted:
        key = (ns, name)
        label = f"{ns}/{name}: available"
        if key not in live:
            out.append(warn("deploy", label, "not found", cluster=cl))
            continue
        ready, desired = live[key]
        if desired == 0:
            # Do NOT blind-scale to 1. supervisor_stabilizer.py:1937 runs
            # `scale --all --replicas=1`, which silently DOWN-scales anything
            # intentionally running more, and cannot tell "switched off on
            # purpose" from "scaled to 0 by a stale shutdown workflow".
            # vsp-health-monitor.py:1936 does it right: restore a RECORDED
            # intended count. Without that annotation, say so and stop.
            recorded = live_annotations.get(key)
            res = warn("deploy", f"{label} (currently scaled to 0)",
                       "0 replicas — intentional, or a stale shutdown workflow?",
                       cluster=cl)
            if recorded and may_act(r, "deployments"):
                r.write(f"kubectl scale deployment {name} -n {ns} "
                        f"--replicas={recorded}",
                        f"scale {ns}/{name} to its recorded "
                        f"vcf.lab/original-replicas={recorded}",
                        tier="transient", timeout=60)
                res.action = f"scaled to recorded {recorded}"
                if not r.dry_run:
                    res.detail = f"restored to recorded {recorded}"
            elif may_act(r, "deployments"):
                res.detail += " — no vcf.lab/original-replicas annotation, so the " \
                              "intended count is unknown; not guessing"
            out.append(res)
        elif ready >= desired:
            out.append(ok("deploy", label, f"{ready}/{desired}", cluster=cl))
        else:
            res = (warn if severity == "warn" else fail)(
                "deploy", label, f"{ready}/{desired}", cluster=cl)
            if may_act(r, "deployments") and severity != "warn":
                r.write(f"kubectl rollout restart deployment {name} -n {ns}",
                        f"rollout restart {ns}/{name} ({ready}/{desired} available)",
                        tier="transient", timeout=90)
                res.action = "rollout restarted"
                if not r.dry_run:
                    res.state = "warn"
                    res.detail = f"was {ready}/{desired}; rollout restarted"
            out.append(res)
    return out


SALT_PODS = (("salt-raas", "redis"), ("salt-raas", "raas"),
             ("salt", "salt-master"), ("salt", "salt-minion"))
SALT_LOG_ERRORS = ("SSL CERTIFICATE_VERIFY_FAILED", "This Minion was scheduled to stop",
                   "530 Unknown", "RAAS is not available", "Connection refused to")
PATRONI_CLUSTERS = (("salt-raas", "pgdatabase"), ("vcf-fleet-lcm", "vcf-fleet-lcm-db"),
                    ("vcf-sddc-lcm", "vcf-sddc-lcm-db"),
                    ("vidb-external", "vidb-postgres-instance"))
KYVERNO_UR_LIMIT = 50
KYVERNO_RESYNC_TARGET = "1h"
PASSWORD_WARN_DAYS = 60


def _duration_to_minutes(v):
    """'15m'/'1h'/'2h' -> minutes, or None if unparseable. Good enough for the
    handful of Kubernetes-style durations this tool ever compares (never
    combined units like '1h30m')."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        if v.endswith("h"):
            return float(v[:-1]) * 60
        if v.endswith("m"):
            return float(v[:-1])
        if v.endswith("s"):
            return float(v[:-1]) / 60
        return float(v)
    except ValueError:
        return None
PASSWORD_MAX_DAYS = 730
PASSWORD_USERS = ("root", "vmware-system-user")


VSP_PROBE_TARGETS = [
    ("vcf-fleet-depot", "deployment", "depot-service", "download-service", 10,
     '{"spec":{"template":{"spec":{"containers":[{"name":"download-service","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60},"resources":{"limits":{"memory":"2Gi"},"requests":{"cpu":"300m","memory":"512Mi"}}},{"name":"file-server","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15}},{"name":"proxy-forwarder","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}'),
    ("vcf-fleet-lcm", "deployment", "vcf-fleet-build-service-fleetbuild", "fleetbuild", 15,
     '{"spec":{"template":{"spec":{"containers":[{"name":"fleetbuild","livenessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}'),
    ("vidb-external", "deployment", "vidb-service", "vidb-service", 10,
     '{"spec":{"template":{"spec":{"containers":[{"name":"vidb-service","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":10}}]}}}}'),
    ("vcf-sddc-lcm", "deployment", "vcf-sddc-build-service-sddcbuild", "sddcbuild", 15,
     '{"spec":{"template":{"spec":{"containers":[{"name":"sddcbuild","livenessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}'),
    ("vcf-sddc-lcm", "deployment", "vcf-sddc-upgrade-service-sddcupgrade", "sddcupgrade", 15,
     '{"spec":{"template":{"spec":{"containers":[{"name":"sddcupgrade","livenessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}'),
    ("vmsp-platform", "statefulset", "prometheus-kube-prometheus-stack-prometheus", "prometheus", 10,
     '{"spec":{"template":{"spec":{"containers":[{"name":"prometheus","livenessProbe":{"timeoutSeconds":10,"failureThreshold":8,"periodSeconds":10},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":8,"periodSeconds":10},"resources":{"limits":{"memory":"4Gi"},"requests":{"memory":"1Gi"}}}]}}}}'),
    ("vmsp-platform", "deployment", "kube-prometheus-stack-kube-state-metrics", "kube-state-metrics", 10,
     '{"spec":{"template":{"spec":{"containers":[{"name":"kube-state-metrics","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6}}]}}}}'),
    ("vmsp-platform", "daemonset", "kube-prometheus-stack-prometheus-node-exporter", "node-exporter", 10,
     '{"spec":{"template":{"spec":{"containers":[{"name":"node-exporter","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6}}]}}}}'),
]


def _check_vsp_probe_and_memory_tuning(r, ctx):
    """VSP probe timeout and memory tuning for Section A of vsp-stabilizer.sh."""
    out = []
    cl = r.cluster
    for ns, kind, name, con, want_timeout, patch_json in VSP_PROBE_TARGETS:
        label = f"{ns}/{kind}/{name}: probes and resources tuned"
        rc, cur = r.read(
            f"kubectl -n {ns} get {kind} {name} -o jsonpath='{{.spec.template.spec.containers[?(@.name==\"{con}\")].livenessProbe.timeoutSeconds}}' 2>/dev/null", 30)
        val = (cur or "").strip().splitlines()
        val = val[-1].strip() if val else ""
        if val.isdigit() and int(val) == want_timeout:
            out.append(ok("vcf.probe", label, f"livenessTimeout={val}s", cluster=cl))
        elif not val:
            out.append(warn("vcf.probe", label, f"resource or container {con} not found", cluster=cl))
        else:
            res = fail("vcf.probe", label, f"livenessTimeout is '{val}' (target {want_timeout}s)", cluster=cl)
            if may_act(r, "vcf"):
                r.write(
                    f"kubectl -n {ns} patch {kind} {name} --type=strategic -p '{patch_json}'",
                    f"patch {ns}/{kind}/{name} probes and resources",
                    tier="persistent", timeout=60)
                res.action = "probes/resources patched"
                if not r.dry_run:
                    res.state = "warn"
                    res.detail = f"was {val}s; patched to {want_timeout}s"
            out.append(res)
    return out


def _check_vsphere_cpi_tuning(r, ctx):
    """vsphere-cpi DaemonSet leader election lease tuning for Section C of vsp-stabilizer.sh."""
    out = []
    cl = r.cluster
    rc, cur = r.read(
        "kubectl -n kube-system get daemonset vsphere-cpi -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null", 30)
    args_str = (cur or "").strip().splitlines()
    args_str = args_str[-1].strip() if args_str else ""
    label = "vsphere-cpi DaemonSet: leader-election lease tuned (60s/40s/6s)"

    if "--leader-elect-renew-deadline=" in args_str:
        out.append(ok("vcf.cpi", label, cluster=cl))
    elif not args_str:
        out.append(warn("vcf.cpi", label, "vsphere-cpi DaemonSet not found", cluster=cl))
    else:
        res = fail("vcf.cpi", label, f"args={args_str}", cluster=cl)
        if may_act(r, "vcf"):
            if args_str.endswith("]"):
                new_args = args_str[:-1] + ',"--leader-elect-lease-duration=60s","--leader-elect-renew-deadline=40s","--leader-elect-retry-period=6s"]'
            else:
                new_args = '["--cloud-provider=vsphere","--v=2","--cloud-config=/etc/cloud/vsphere.conf","--leader-elect-lease-duration=60s","--leader-elect-renew-deadline=40s","--leader-elect-retry-period=6s"]'
            r.write(
                f"kubectl -n kube-system patch daemonset vsphere-cpi --type=strategic -p '{{\"spec\":{{\"template\":{{\"spec\":{{\"containers\":[{{\"name\":\"vsphere-cpi\",\"args\":{new_args}}}]}}}}}}}}' && "
                "kubectl -n kube-system delete pod -l app=vsphere-cpi --grace-period=0 --force",
                "patch vsphere-cpi DaemonSet leader election args (60s/40s/6s) and restart pod",
                tier="persistent", timeout=60)
            res.action = "cpi patched & pod restarted"
            if not r.dry_run:
                res.state = "warn"
                res.detail = "patched leader election lease settings (60s/40s/6s)"
        out.append(res)

    if ctx.get("revert") and may_act(r, "vcf"):
        if "--leader-elect-renew-deadline=" in args_str:
            base_args = args_str
            for flag in (',"--leader-elect-lease-duration=60s"', ',"--leader-elect-renew-deadline=40s"', ',"--leader-elect-retry-period=6s"'):
                base_args = base_args.replace(flag, '')
            r.write(
                f"kubectl -n kube-system patch daemonset vsphere-cpi --type=strategic -p '{{\"spec\":{{\"template\":{{\"spec\":{{\"containers\":[{{\"name\":\"vsphere-cpi\",\"args\":{base_args}}}]}}}}}}}}' && "
                "kubectl -n kube-system delete pod -l app=vsphere-cpi --grace-period=0 --force",
                "revert vsphere-cpi DaemonSet leader election args to base",
                tier="persistent", timeout=60)
            rev_res = ok("vcf.cpi_revert", "vsphere-cpi DaemonSet: reverted leader election args", cluster=cl)
            rev_res.action = "reverted cpi args"
            out.append(rev_res)

    return out


def chk_vcf(r, ctx):
    """VCF managed components: operational-status annotation and replica counts.

    The components CRD is CLUSTER-SCOPED (components.api.vmsp.vmware.com) - passing
    -A or -n silently returns nothing, which is a documented trap. After a cold
    boot these sit annotated NotRunning with their workloads at 0 replicas.

    Remediation order matters: annotate operational-status=Running BEFORE scaling,
    or the operator races the replica count back to 0
    (vsp-health-monitor.py:1841-1844).
    """
    out = []
    cl = r.cluster
    data = r.read_json(
        "kubectl get components.api.vmsp.vmware.com -o json 2>/dev/null", 60)
    if not data:
        return [ok("vcf", "VCF components: readable",
                   "no components CRD on this cluster", cluster=cl)]

    not_running = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        ann = item["metadata"].get("annotations") or {}
        status = ann.get("component.vmsp.vmware.com/operational-status", "")
        label = f"component {name}: Running"
        if status == "Running":
            out.append(ok("vcf.component", label, cluster=cl))
        elif not status:
            out.append(warn("vcf.component", label, "no operational-status annotation",
                            cluster=cl))
        else:
            not_running.append(name)
            res = fail("vcf.component", label, f"annotated '{status}'", cluster=cl)
            if may_act(r, "vcf"):
                r.write(f"kubectl annotate components.api.vmsp.vmware.com {name} "
                        "component.vmsp.vmware.com/operational-status=Running "
                        "--overwrite",
                        f"annotate component {name} operational-status=Running",
                        tier="persistent", timeout=60)
                res.action = "annotated Running"
                if not r.dry_run:
                    res.state = "warn"
                    res.detail = f"was '{status}'; annotated Running"
            out.append(res)

    # Configured workloads from [VCFFINAL] vcfcomponents - the same config key
    # VCFfinal.py uses, deliberately shared so the two cannot drift.
    for ns, kind, name in _vcfcomponents_from_config():
        obj = r.read_json(f"kubectl get {kind} {name} -n {ns} -o json 2>/dev/null", 45)
        wlabel = f"{ns}/{kind}/{name}: at intended replicas"
        if not obj:
            out.append(warn("vcf.workload", wlabel, "not found", cluster=cl))
            continue
        spec_rep = obj.get("spec", {}).get("replicas")
        desired = spec_rep if spec_rep is not None else 1
        ready = obj.get("status", {}).get("readyReplicas", 0) or 0
        recorded = (obj["metadata"].get("annotations") or {}).get(
            "vcf.lab/original-replicas")
        if desired == 0:
            res = fail("vcf.workload", wlabel, "scaled to 0", cluster=cl)
            target = recorded if (recorded and recorded.isdigit()
                                  and int(recorded) > 0) else "1"
            if may_act(r, "vcf"):
                r.write(f"kubectl scale {kind} {name} -n {ns} --replicas={target}",
                        f"scale {ns}/{kind}/{name} to {target}"
                        + ("" if recorded else " (no recorded count; default 1)"),
                        tier="transient", timeout=60)
                res.action = f"scaled to {target}"
                if not r.dry_run:
                    res.state = "warn"
                    res.detail = f"was 0; scaled to {target}"
            out.append(res)
        elif ready >= desired:
            out.append(ok("vcf.workload", wlabel, f"{ready}/{desired}", cluster=cl))
        else:
            out.append(fail("vcf.workload", wlabel, f"{ready}/{desired}", cluster=cl))

    # Sections A and C of vsp-stabilizer.sh: probe/memory tuning and vsphere-cpi DaemonSet
    out.extend(_check_vsp_probe_and_memory_tuning(r, ctx))
    out.extend(_check_vsphere_cpi_tuning(r, ctx))

    return out


def _vcfcomponents_from_config(path="/tmp/config.ini"):
    """[VCFFINAL] vcfcomponents entries as (namespace, kind, name)."""
    items = []
    try:
        with open(path) as fh:
            in_key = False
            for raw in fh:
                stripped = raw.strip()
                if stripped.lower().startswith("vcfcomponents"):
                    in_key = True
                    continue
                if in_key:
                    if not stripped or stripped.startswith("[") or "=" in stripped.split(":")[0]:
                        if not raw[:1].isspace():
                            break
                    if stripped.startswith("#") or not stripped:
                        continue
                    if ":" in stripped and "/" in stripped:
                        ns, rest = stripped.split(":", 1)
                        kind, _, name = rest.partition("/")
                        if ns and kind and name:
                            items.append((ns.strip(), kind.strip(), name.strip()))
                    elif not raw[:1].isspace():
                        break
    except OSError:
        pass
    return items


def chk_redis(r, ctx):
    """Redis and RaaS readiness, plus the redis-service endpoint.

    The endpoint check is the one that matters and the monitor lacks it
    (vsp-health.py:954 detects it, nothing remediates): an EMPTY redis-service
    endpoint is the cert-timing race where redis loads its TLS cert before
    vsp_cert_renewer rotates it, ~18s later.
    """
    out = []
    cl = r.cluster
    for ns, app in (("salt-raas", "redis"), ("salt-raas", "raas")):
        rc, res = r.read(
            f"kubectl -n {ns} get pod -l app={app} --no-headers 2>/dev/null "
            "| awk '{print $2\" \"$3}'", 45)
        val = [x for x in (res or "").splitlines() if x.strip()]
        label = f"{ns}/{app}: ready"
        if not val:
            out.append(warn("redis.pod", label, "no pod found", cluster=cl))
            continue
        ready, _, phase = val[-1].strip().partition(" ")
        have, _, want = ready.partition("/")
        if have and want and have == want and phase.strip() == "Running":
            out.append(ok("redis.pod", label, f"{ready}", cluster=cl))
        else:
            out.append(fail("redis.pod", label, f"{ready} {phase}", cluster=cl))

    rc, eps = r.read(
        "kubectl -n salt-raas get endpoints redis-service "
        "-o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null", 45)
    addrs = (eps or "").strip().splitlines()
    addrs = addrs[-1].strip() if addrs else ""
    elabel = "salt-raas/redis-service: endpoint populated"
    if addrs:
        out.append(ok("redis.endpoint", elabel, f"{len(addrs.split())} address(es)",
                      cluster=cl))
    else:
        res = fail("redis.endpoint", elabel,
                   "EMPTY — the cert-timing race (redis loaded its TLS cert before "
                   "cert rotation); restarting redis reloads it", cluster=cl)
        if may_act(r, "redis"):
            r.write("kubectl -n salt-raas rollout restart deployment redis",
                    "rollout restart salt-raas/redis to reload its cert",
                    tier="transient", timeout=120)
            res.action = "redis restarted"
            if not r.dry_run:
                res.state = "warn"
                res.detail = "endpoint was empty; redis restarted"
        out.append(res)
    return out


def chk_salt(r, ctx):
    """Salt stack readiness plus salt-master log signatures.

    Restart is GATED on an actual fault. salt-stabilize.py:331 restarts all four
    unconditionally, which is why running it against a healthy stack causes the
    monitor's next pass to see transient unreadiness and restart it again. Order
    matters on repair: redis -> raas -> salt-master -> salt-minion.
    """
    out = []
    cl = r.cluster
    unhealthy = []
    for ns, app in SALT_PODS:
        rc, res = r.read(
            f"kubectl -n {ns} get pod -l app={app} --no-headers 2>/dev/null "
            "| awk '{print $2\" \"$3}'", 45)
        val = [x for x in (res or "").splitlines() if x.strip()]
        label = f"{ns}/{app}: ready"
        if not val:
            out.append(warn("salt.pod", label, "no pod found", cluster=cl))
            continue
        ready, _, phase = val[-1].strip().partition(" ")
        have, _, want = ready.partition("/")
        if have and want and have == want and phase.strip() == "Running":
            out.append(ok("salt.pod", label, ready, cluster=cl))
        else:
            out.append(fail("salt.pod", label, f"{ready} {phase}", cluster=cl))
            unhealthy.append(f"{ns}/{app}")

    rc, logs = r.read(
        "kubectl logs -n salt --selector=app=salt-master --tail=80 2>/dev/null", 60)
    hits = [p for p in SALT_LOG_ERRORS if p in (logs or "")]
    llabel = "salt-master: log clean of known error signatures"
    if hits:
        out.append(fail("salt.logs", llabel, "; ".join(hits[:2]), cluster=cl))
        unhealthy.append("salt-master logs")
    else:
        out.append(ok("salt.logs", llabel, cluster=cl))

    if unhealthy and may_act(r, "salt"):
        for ns, app in SALT_PODS:
            r.write(f"kubectl -n {ns} rollout restart deployment {app}",
                    f"rollout restart {ns}/{app} (ordered salt stack repair)",
                    tier="transient", timeout=90)
        out.append(warn("salt.repair", "salt stack: restarted in order",
                        f"triggered by {', '.join(unhealthy[:3])}", cluster=cl))
    return out


def chk_argo(r, ctx):
    """Stale Argo system-shutdown workflows and the power-off marker.

    Each Fleet LCM shutdown leaves a system-shutdown-* workflow in etcd. On the
    next boot the controller RESUMES it, which re-cordons the node and scales all
    prelude/component deployments to 0 - the documented cause of VCFA returning
    HTTP 500 roughly half an hour after a apparently-successful startup. Up to 30+
    accumulate.
    """
    out = []
    cl = r.cluster
    rc, wf = r.read(
        "kubectl get workflow -n vmsp-platform --no-headers 2>/dev/null "
        "| grep system-shutdown | awk '{print $1}'", 60)
    stale = [w.strip() for w in (wf or "").splitlines() if w.strip()]
    label = "vmsp-platform: no stale system-shutdown workflows"
    if not stale:
        out.append(ok("argo.workflows", label, cluster=cl))
    else:
        res = fail("argo.workflows", label,
                   f"{len(stale)} present — these RESUME on boot and re-cordon the "
                   f"node, scaling workloads back to 0", cluster=cl)
        if may_act(r, "argo"):
            r.write("kubectl get workflow -n vmsp-platform --no-headers "
                    "| grep system-shutdown | awk '{print $1}' "
                    "| xargs -r kubectl delete workflow -n vmsp-platform "
                    "--grace-period=0",
                    f"delete {len(stale)} stale system-shutdown workflow(s)",
                    tier="transient", timeout=180)
            res.action = f"deleted {len(stale)}"
            if not r.dry_run:
                res.state = "warn"
                res.detail = f"{len(stale)} deleted"
        out.append(res)

    rc, marker = r.read(
        "kubectl get configmap power-off-marker -n vmsp-platform "
        "--no-headers 2>/dev/null | wc -l", 45)
    cnt = (marker or "0").strip().splitlines()
    cnt = cnt[-1].strip() if cnt else "0"
    mlabel = "vmsp-platform: no power-off-marker ConfigMap"
    if cnt == "0":
        out.append(ok("argo.marker", mlabel, cluster=cl))
    else:
        out.append(warn("argo.marker", mlabel,
                        "present — KB 440862; workloads may be held down",
                        cluster=cl))
    return out


def chk_kyverno(r, ctx):
    """Kyverno UpdateRequest backlog and controller health.

    A large backlog stalls admission for everything else. The monitor's threshold
    is > 50 (vsp-health-monitor.py:2306).
    """
    out = []
    cl = r.cluster
    rc, cnt = r.read(
        "kubectl get updaterequests.kyverno.io -n vmsp-policies --no-headers "
        "2>/dev/null | wc -l", 60)
    val = (cnt or "").strip().splitlines()
    val = val[-1].strip() if val else ""
    n = int(val) if val.isdigit() else -1
    label = f"kyverno UpdateRequests: below {KYVERNO_UR_LIMIT}"
    if n < 0:
        out.append(warn("kyverno.queue", label, "not readable", cluster=cl))
    elif n <= KYVERNO_UR_LIMIT:
        out.append(ok("kyverno.queue", label, f"{n} queued", cluster=cl))
    else:
        res = fail("kyverno.queue", label, f"{n} queued — admission is stalling",
                   cluster=cl)
        if may_act(r, "kyverno"):
            r.write("kubectl delete updaterequests.kyverno.io --all -n vmsp-policies",
                    f"clear {n} kyverno UpdateRequests", tier="transient", timeout=180)
            r.write("kubectl delete pod -n vmsp-policies "
                    "-l app.kubernetes.io/component=background-controller",
                    "restart the kyverno background-controller",
                    tier="transient", timeout=90)
            res.action = "queue cleared"
            if not r.dry_run:
                res.state = "warn"
                res.detail = f"{n} cleared"
        out.append(res)

    for comp in ("background-controller", "admission-controller", "cleanup-controller"):
        rc, res2 = r.read(
            f"kubectl -n vmsp-policies get pod "
            f"-l app.kubernetes.io/component={comp} --no-headers 2>/dev/null "
            "| awk '{print $2\" \"$3}'", 45)
        val2 = [x for x in (res2 or "").splitlines() if x.strip()]
        clabel = f"kyverno {comp}: ready"
        if not val2:
            out.append(warn("kyverno.pod", clabel, "no pod found", cluster=cl))
            continue
        bad = [v for v in val2
               if v.split()[0].split("/")[0] != v.split()[0].split("/")[-1]]
        if bad:
            out.append(fail("kyverno.pod", clabel, "; ".join(bad[:2]), cluster=cl))
        else:
            out.append(ok("kyverno.pod", clabel, f"{len(val2)} pod(s)", cluster=cl))

    # --kyverno-resync-relax (remediate-lab.sh:1988 kyverno_resync_apply, Family C).
    # NOTE: a DIFFERENT field from chk_storm's VCFA-side kyverno fix -
    # backgroundController.resyncPeriod here, cleanupController.resyncPeriod
    # there - both real, from two different remediate-lab.sh call sites.
    rt = _storm_discover_rt(r, "kyverno-", exclude=("policies",))
    if not rt:
        out.append(warn("kyverno.resync", "kyverno backgroundController ReleaseTemplate discoverable",
                        "not found", cluster=cl))
    else:
        rc, resync = r.read(
            f"kubectl get releasetemplate {rt} -n vmsp-platform -o jsonpath="
            "'{.spec.helm.values.backgroundController.resyncPeriod}' 2>/dev/null", 30)
        resync = (resync or "").strip().splitlines()
        resync = resync[-1].strip() if resync else ""
        cur_min = _duration_to_minutes(resync)
        target_min = _duration_to_minutes(KYVERNO_RESYNC_TARGET)
        label = f"{rt}: backgroundController.resyncPeriod >= {KYVERNO_RESYNC_TARGET}"
        if cur_min is not None and cur_min >= target_min:
            out.append(ok("kyverno.resync", label, resync or "unset", cluster=cl))
        else:
            res = fail("kyverno.resync", label, f"currently {resync or 'unset (15m chart default)'}",
                       cluster=cl)
            if may_act(r, "kyverno"):
                r.write(
                    f"kubectl patch releasetemplate {rt} -n vmsp-platform --type=merge -p "
                    f"'{{\"spec\":{{\"helm\":{{\"values\":{{\"backgroundController\":"
                    f"{{\"resyncPeriod\":\"{KYVERNO_RESYNC_TARGET}\"}}}}}}}}}}'",
                    f"kyverno resync-relax: {rt} backgroundController.resyncPeriod -> "
                    f"{KYVERNO_RESYNC_TARGET}", tier="persistent", timeout=60)
                res.action = f"resyncPeriod -> {KYVERNO_RESYNC_TARGET}"
                if not r.dry_run:
                    res.state, res.detail = "warn", f"was {resync or 'unset'}; set to {KYVERNO_RESYNC_TARGET}"
            out.append(res)

    # Section C of vsp-stabilizer.sh: kyverno-cleanup-validating-webhook-cfg failurePolicy
    rc, fp = r.read(
        "kubectl get validatingwebhookconfiguration kyverno-cleanup-validating-webhook-cfg "
        "-o jsonpath='{.webhooks[0].failurePolicy}' 2>/dev/null", 30)
    fp_str = (fp or "").strip().splitlines()
    fp_str = fp_str[-1].strip() if fp_str else ""
    label_fp = "kyverno-cleanup-validating-webhook-cfg: failurePolicy == Ignore"
    if fp_str == "Ignore":
        out.append(ok("kyverno.webhook", label_fp, cluster=cl))
    elif not fp_str:
        out.append(warn("kyverno.webhook", label_fp, "webhook configuration not found", cluster=cl))
    else:
        res_fp = fail("kyverno.webhook", label_fp, f"currently failurePolicy='{fp_str}'", cluster=cl)
        if may_act(r, "kyverno"):
            r.write(
                "kubectl patch validatingwebhookconfiguration kyverno-cleanup-validating-webhook-cfg "
                "--type=json -p '[{\"op\":\"replace\",\"path\":\"/webhooks/0/failurePolicy\",\"value\":\"Ignore\"}]'",
                "patch kyverno-cleanup-validating-webhook-cfg failurePolicy=Ignore",
                tier="persistent", timeout=60)
            res_fp.action = "failurePolicy -> Ignore"
            if not r.dry_run:
                res_fp.state = "warn"
                res_fp.detail = f"was '{fp_str}'; patched to Ignore"
        out.append(res_fp)

    if ctx.get("revert") and may_act(r, "kyverno"):
        if fp_str == "Ignore":
            r.write(
                "kubectl patch validatingwebhookconfiguration kyverno-cleanup-validating-webhook-cfg "
                "--type=json -p '[{\"op\":\"replace\",\"path\":\"/webhooks/0/failurePolicy\",\"value\":\"Fail\"}]'",
                "revert kyverno-cleanup-validating-webhook-cfg failurePolicy=Fail",
                tier="persistent", timeout=60)
            rev_fp = ok("kyverno.webhook_revert", "kyverno-cleanup-validating-webhook-cfg: reverted failurePolicy=Fail", cluster=cl)
            rev_fp.action = "reverted failurePolicy"
            out.append(rev_fp)

    return out


def chk_password(r, ctx):
    """Node account password expiry.

    `never` is healthy. Otherwise anything inside 60 days is drift. The repair sets
    -M 730 AND -d today: without refreshing the last-change date the expiry is
    still computed from a stale baseline, which is what produced expiry dates in
    the past on template-derived labs.
    """
    out = []
    cl = r.cluster
    data = ctx.get("nodes")
    if not data:
        return [warn("password", "node password expiry: within policy",
                     "node list unavailable", cluster=cl)]
    ips = [a["address"] for item in data.get("items", [])
           for a in item.get("status", {}).get("addresses", [])
           if a.get("type") == "InternalIP"]

    for ip in ips:
        for user in PASSWORD_USERS:
            # Anchor to the start of the line and stop at the first match.
            # `grep -i 'Password expires'` also matches "Number of days of warning
            # before password expires : 7", so a loose match plus last-line
            # selection yields "7" and every node reads as unparseable.
            rc, res = r.read_on(
                ip, f"chage -l {user} 2>/dev/null | "
                    "awk -F: '/^Password expires/{print $2; exit}'", 45)
            val = (res or "").strip().splitlines()
            val = val[-1].strip() if val else ""
            label = f"{ip} {user}: password not expiring within {PASSWORD_WARN_DAYS}d"
            if not val:
                out.append(warn("password", label, "not readable", cluster=cl))
                continue
            if "never" in val.lower():
                out.append(ok("password", label, "never", cluster=cl))
                continue
            days = None
            try:
                exp = datetime.strptime(val.strip(), "%b %d, %Y")
                days = (exp - datetime.now()).days
            except ValueError:
                pass
            if days is None:
                out.append(warn("password", label, f"unparsed: {val}", cluster=cl))
            elif days > PASSWORD_WARN_DAYS:
                out.append(ok("password", label, f"{days}d", cluster=cl,
                              residual_days=days))
            else:
                res_row = fail("password", label, f"{days}d remaining", cluster=cl,
                               residual_days=days)
                if may_act(r, "password"):
                    r.write_on_node(
                        ip, f"chage -d $(date +%Y-%m-%d) -M {PASSWORD_MAX_DAYS} {user}",
                        f"reset {user} last-change date and set -M "
                        f"{PASSWORD_MAX_DAYS} on {ip}",
                        tier="persistent", timeout=60)
                    res_row.action = f"chage -M {PASSWORD_MAX_DAYS}"
                    if not r.dry_run:
                        res_row.state = "warn"
                        res_row.detail = f"was {days}d; extended"
                out.append(res_row)
    return out


# ─── Sizing (vsp-scale-down.py port) ─────────────────────────────────────────
# CP/worker machine-type resize, worker replica-bound scaling and
# cluster-autoscaler management for the VSP fleet cluster. Unlike every other
# section this one takes TARGET values on the CLI (--cp-machine-type,
# --worker-machine-type, --worker-count / --worker-min-replicas +
# --worker-max-replicas, --autoscaler) instead of detecting-and-fixing a
# drifted state, because "the right size" is an operator decision, not
# something to infer. With no target flags this is pure reporting - current
# machine types, replica bounds, autoscaler state, node utilization - which
# vsp-scale-down.py itself never offered: it required a target value just to
# look at the current state.
#
# vsp-scale-down.py's ownership-chain doctrine is preserved exactly: every
# write targets PackageDeployment/vmsp-platform's spec.values, never the
# KubernetesCluster or MachineDeployment directly - vmsp-operator reconciles
# those FROM the PackageDeployment and reverts a direct patch (this is the
# same F1 finding this tool exists to not repeat elsewhere).
SIZING_NAMESPACE = "vmsp-platform"
SIZING_PACKAGEDEPLOYMENT = "vmsp-platform"
SIZING_AUTOSCALER_HR = "cluster-autoscaler"
SIZING_AUTOSCALER_DEPLOY = "cluster-autoscaler-clusterapi-cluster-autoscaler"
# (vCPU, MiB) - vsp-scale-down.py's own docstring.
SIZING_MACHINE_TYPES = {
    "cp.small": (4, 10240), "cp.medium": (6, 12288), "cp.large": (8, 14336),
    "management.small": (4, 8192), "management.medium": (8, 16384),
    "management.large": (12, 24576), "management.xlarge": (16, 32768),
}
# vsp-scale-down.py step4: Flux propagation into KubernetesCluster.spec.workers[0]
# gets a fixed 15-minute window before the MachineDeployment-drain phase starts.
SIZING_FLUX_PROPAGATION_TIMEOUT = 15 * 60


def _sizing_last(out):
    lines = (out or "").strip().splitlines()
    return lines[-1].strip() if lines else ""


def _sizing_describe(machine_type):
    spec = SIZING_MACHINE_TYPES.get(machine_type)
    return f"{machine_type} ({spec[0]} vCPU / {spec[1] / 1024:.0f} GiB)" if spec else (machine_type or "unknown")


def _sizing_discover(r, kind):
    """First object of `kind` in the sizing namespace - one CAPI cluster/MD per lab."""
    rc, out = r.read(
        f"kubectl get {kind} -n {SIZING_NAMESPACE} -o name 2>/dev/null | head -1", 30)
    name = _sizing_last(out)
    return name.split("/", 1)[1] if "/" in name else name


def _sizing_pd_values(r):
    return r.read_json(
        f"kubectl get packagedeployment {SIZING_PACKAGEDEPLOYMENT} -n {SIZING_NAMESPACE} "
        "-o jsonpath='{.spec.values}' 2>/dev/null", 30) or {}


def _sizing_autoscaler_state(r):
    rc, susp = r.read(
        f"kubectl get helmrelease {SIZING_AUTOSCALER_HR} -n {SIZING_NAMESPACE} "
        "-o jsonpath='{.spec.suspend}' 2>/dev/null", 30)
    rc2, reps = r.read(
        f"kubectl get deploy {SIZING_AUTOSCALER_DEPLOY} -n {SIZING_NAMESPACE} "
        "-o jsonpath='{.spec.replicas}' 2>/dev/null", 30)
    suspended = _sizing_last(susp).lower() == "true"
    replicas = _sizing_last(reps)
    enabled = (not suspended) and replicas not in ("", "0")
    return enabled, replicas


def _sizing_set_autoscaler(r, enable, desc):
    """Flip BOTH the HelmRelease suspend flag and the controller Deployment's
    replica count together - vsp-scale-down.py treats them as one switch
    because a suspended HelmRelease alone leaves an already-running autoscaler
    pod in place, and a scaled-to-0 Deployment alone gets re-scaled to 1 by
    Flux the next time it reconciles the (still-unsuspended) HelmRelease."""
    suspend = "false" if enable else "true"
    replicas = 1 if enable else 0
    r.write(
        f"kubectl patch helmrelease {SIZING_AUTOSCALER_HR} -n {SIZING_NAMESPACE} "
        f"--type=merge -p '{{\"spec\":{{\"suspend\":{suspend}}}}}' && "
        f"kubectl scale deploy {SIZING_AUTOSCALER_DEPLOY} -n {SIZING_NAMESPACE} "
        f"--replicas={replicas}",
        desc, tier="transient", timeout=60)


def _sizing_node_utilization(r, warn_pct):
    """`kubectl top nodes` rows, flagged hot at/above warn_pct on either axis."""
    rc, out = r.read("kubectl top nodes --no-headers 2>/dev/null", 30)
    rows = []
    if rc != 0 or not out.strip():
        return rows, False
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        name, cpu, cpu_pct, mem, mem_pct = parts[:5]
        try:
            cpu_n = int(cpu_pct.rstrip('%'))
            mem_n = int(mem_pct.rstrip('%'))
        except ValueError:
            continue
        rows.append({"node": name, "cpu": cpu, "cpu_pct": cpu_n, "mem": mem,
                     "mem_pct": mem_n, "hot": cpu_n >= warn_pct or mem_n >= warn_pct})
    return rows, True


def _sizing_autoscaler_stuck(r):
    """Detect the documented cluster-autoscaler stuck loop from its own logs."""
    rc, pod = r.read(
        f"kubectl get pods -n {SIZING_NAMESPACE} "
        "-l app.kubernetes.io/name=clusterapi-cluster-autoscaler "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null", 30)
    pod = _sizing_last(pod)
    if not pod:
        return False
    rc, logs = r.read(
        f"kubectl logs {pod} -n {SIZING_NAMESPACE} --tail=200 2>/dev/null "
        "| grep -c 'size increase too large'", 30)
    try:
        return int(_sizing_last(logs) or "0") > 0
    except ValueError:
        return False


def _sizing_poll(desc, check_fn, timeout_sec, interval_sec, dry_run):
    """Bounded poll with visible progress. Skipped entirely under --dry-run,
    since nothing was actually changed for it to converge toward."""
    if dry_run:
        row_verbose(f"[dry-run] would wait for: {desc}")
        return True
    started = time.time()
    while time.time() - started < timeout_sec:
        if check_fn():
            row_verbose(f"{desc}: reached ({int(time.time() - started)}s)")
            return True
        row_verbose(f"waiting for {desc} ... ({int(time.time() - started)}s elapsed)")
        time.sleep(interval_sec)
    row_verbose(f"{desc}: TIMED OUT after {timeout_sec}s")
    return False


def chk_sizing(r, ctx):
    """VSP fleet cluster sizing: CP/worker machine type, worker replica bounds,
    cluster-autoscaler state and node utilization.

    Full functional port of vsp-scale-down.py, folded into the check/action
    model the rest of this tool uses rather than that script's own
    step2b/step3/step4 CLI. Every mutating action still goes through
    Runner.write(), so --dry-run and --mode gating behave identically to every
    other section (SECTION_ACT_MODES restricts this to --mode remediate only -
    a cluster resize is an operator decision, not durable config a --mode tune
    template-prep pass should ever apply on its own).

    Reporting (machine types, replica bounds, autoscaler state, node
    utilization) always runs, in every mode, so `--mode report`/`preflight`
    surfaces current sizing even with no target flags - vsp-scale-down.py
    could not do this at all, since it required a target just to look.
    """
    out = []
    cl = r.cluster

    cluster_name = _sizing_discover(r, "kubernetescluster")
    md_name = _sizing_discover(r, "machinedeployment")
    if not cluster_name or not md_name:
        return [warn("sizing.discover", "sizing: cluster/MachineDeployment discoverable",
                     f"kubernetescluster={cluster_name or '?'} "
                     f"machinedeployment={md_name or '?'}", cluster=cl)]

    values = _sizing_pd_values(r)
    cluster_vals = values.get("cluster", {}) or {}
    worker_vals = cluster_vals.get("worker", {}) or {}
    cp_type = cluster_vals.get("machineType", "")
    worker_type = worker_vals.get("machineType", "")
    worker_size = worker_vals.get("size", "")
    cur_min = worker_vals.get("minReplicas")
    cur_max = worker_vals.get("maxReplicas")

    md = r.read_json(f"kubectl get machinedeployment {md_name} -n {SIZING_NAMESPACE} "
                     "-o json 2>/dev/null", 30) or {}
    md_status = md.get("status", {})
    md_spec_replicas = md.get("spec", {}).get("replicas")
    md_ready = md_status.get("readyReplicas", 0)
    md_updated = md_status.get("updatedReplicas", 0)
    md_replicas = md_status.get("replicas", 0)

    kcp = r.read_json(f"kubectl get kubeadmcontrolplane -n {SIZING_NAMESPACE} "
                      "-o json 2>/dev/null", 30) or {}
    kcp_items = kcp.get("items", [])
    kcp_status = kcp_items[0].get("status", {}) if kcp_items else {}
    kcp_ready = kcp_status.get("readyReplicas", 0)
    kcp_replicas = kcp_status.get("replicas", 0)

    out.append(ok("sizing.cp", "Control Plane: machineType known",
                  f"{_sizing_describe(cp_type)}, {kcp_ready}/{kcp_replicas} ready",
                  cluster=cl) if cp_type else
               warn("sizing.cp", "Control Plane: machineType known",
                    "PackageDeployment spec.values.cluster.machineType not set",
                    cluster=cl))
    out.append(ok("sizing.worker", "Worker MachineDeployment: machineType known",
                  f"{_sizing_describe(worker_type)}, {md_ready}/{md_replicas} ready "
                  f"(updated {md_updated})", cluster=cl) if worker_type else
               warn("sizing.worker", "Worker MachineDeployment: machineType known",
                    "PackageDeployment spec.values.cluster.worker.machineType not set",
                    cluster=cl))
    if cur_min is not None and cur_max is not None:
        out.append(ok("sizing.bounds", "Worker replica bounds known",
                      f"min={cur_min} max={cur_max} (current desired={md_spec_replicas})",
                      cluster=cl))
    else:
        out.append(warn("sizing.bounds", "Worker replica bounds known",
                        "minReplicas/maxReplicas not set on the PackageDeployment",
                        cluster=cl))

    as_enabled, as_replicas = _sizing_autoscaler_state(r)
    out.append(ok("sizing.autoscaler", "cluster-autoscaler: state known",
                  f"{'ENABLED' if as_enabled else 'DISABLED'} (replicas={as_replicas or 0})",
                  cluster=cl))

    warn_pct = ctx.get("cpu_warn_pct", 80)
    util_rows, util_ok = _sizing_node_utilization(r, warn_pct)
    if not util_ok:
        out.append(warn("sizing.utilization", "Node utilization: readable",
                        "kubectl top nodes unavailable (metrics-server not ready?)",
                        cluster=cl))
    else:
        for nr in util_rows:
            label = f"{nr['node']}: utilization below {warn_pct}%"
            detail = f"cpu={nr['cpu']} ({nr['cpu_pct']}%) mem={nr['mem']} ({nr['mem_pct']}%)"
            out.append(warn("sizing.util", label, detail, cluster=cl) if nr["hot"]
                       else ok("sizing.util", label, detail, cluster=cl))

    if not may_act(r, "sizing"):
        return out

    cp_target = ctx.get("cp_machine_type")
    worker_target = ctx.get("worker_machine_type")
    expected_size = worker_target.split(".")[-1] if worker_target and "." in worker_target else worker_target or ""
    min_target = ctx.get("worker_min_replicas")
    max_target = ctx.get("worker_max_replicas")
    if ctx.get("worker_count") is not None:
        min_target = max_target = ctx["worker_count"]
    autoscaler_mode = ctx.get("autoscaler_mode", "auto")
    resize_timeout = ctx.get("resize_timeout_min", 60) * 60
    scale_timeout = ctx.get("scale_timeout_min", 60) * 60
    poll_interval = ctx.get("poll_interval_sec", 5)

    cp_changed = bool(cp_target) and cp_target != cp_type
    worker_changed = bool(worker_target) and (worker_type != worker_target or worker_size != expected_size)
    scale_changed = (min_target is not None and max_target is not None
                     and (min_target != cur_min or max_target != cur_max
                          or md_spec_replicas < min_target or md_spec_replicas > max_target
                          or (min_target == max_target and (md_spec_replicas != max_target or md_replicas != max_target))))
    any_target_given = bool(cp_target or worker_target or
                            (min_target is not None and max_target is not None))

    if not (any_target_given or autoscaler_mode != "auto"):
        return out          # report-only invocation, even under --mode remediate

    if cp_target and not cp_changed:
        out.append(ok("sizing.cp.resize", f"Control Plane machineType == {cp_target}",
                      "already at target", cluster=cl))
    if worker_target and not worker_changed:
        out.append(ok("sizing.worker.resize", f"Worker machineType == {worker_target}",
                      "already at target", cluster=cl))
    if min_target is not None and max_target is not None and not scale_changed:
        out.append(ok("sizing.scale", f"Worker replica bounds == min={min_target} max={max_target}",
                      "already at target", cluster=cl))

    temporarily_enabled = False
    if scale_changed and autoscaler_mode == "auto" and not as_enabled:
        _sizing_set_autoscaler(r, True, "temporarily enable cluster-autoscaler so it "
                                        "can converge the new worker replica bounds")
        temporarily_enabled = True
    elif scale_changed and autoscaler_mode == "disable" and not as_enabled:
        out.append(warn("sizing.autoscaler.conflict",
                        "cluster-autoscaler available to converge the new bounds",
                        "--autoscaler disable requested but the autoscaler is OFF and "
                        "a replica-bound change was requested — nothing will grow or "
                        "drain the MachineDeployment toward the new bounds until it is "
                        "enabled", cluster=cl))

    try:
        if cp_changed:
            res = fail("sizing.cp.resize", f"Control Plane machineType == {cp_target}",
                       f"currently {cp_type or 'unknown'}", cluster=cl)
            r.write(
                f"kubectl patch packagedeployment {SIZING_PACKAGEDEPLOYMENT} "
                f"-n {SIZING_NAMESPACE} --type=merge -p "
                f"'{{\"spec\":{{\"values\":{{\"cluster\":{{\"machineType\":"
                f"\"{cp_target}\"}}}}}}}}'",
                f"set CP machineType {cp_type or '?'} -> {cp_target} via PackageDeployment",
                tier="transient", timeout=60)
            res.action = f"machineType -> {cp_target}"
            if not r.dry_run:
                def _kcp_rolled_out():
                    d = r.read_json(f"kubectl get kubeadmcontrolplane -n {SIZING_NAMESPACE} "
                                    "-o json 2>/dev/null", 30) or {}
                    items = d.get("items", [{}])
                    if not items: return False
                    kcp = items[0]
                    st = kcp.get("status", {})
                    ref_name = kcp.get("spec", {}).get("machineTemplate", {}).get("spec", {}).get("infrastructureRef", {}).get("name", "")
                    if ref_name:
                        tmpl = r.read_json(f"kubectl get vspheremachinetemplate {ref_name} "
                                          f"-n {SIZING_NAMESPACE} -o json 2>/dev/null", 30) or {}
                        cpus = tmpl.get("spec", {}).get("template", {}).get("spec", {}).get("numCPUs")
                        expected_cpus = SIZING_MACHINE_TYPES.get(cp_target, (0, 0))[0]
                        if expected_cpus and cpus != expected_cpus:
                            return False
                    return st.get("replicas", 0) > 0 and \
                        st.get("upToDateReplicas", 0) == st.get("replicas", -1) and \
                        st.get("readyReplicas", 0) == st.get("replicas", -1)
                reached = _sizing_poll(f"CP rollout to {cp_target}", _kcp_rolled_out,
                                       resize_timeout, poll_interval, r.dry_run)
                res.state = "pass" if reached else "warn"
                res.detail = ("rolled out" if reached else
                             f"patch applied; rollout not confirmed within "
                             f"{ctx.get('resize_timeout_min', 60)}m — check manually")
            out.append(res)

        if worker_changed:
            res = fail("sizing.worker.resize", f"Worker machineType == {worker_target}",
                       f"currently {worker_type or 'unknown'}", cluster=cl)
            r.write(
                f"kubectl patch packagedeployment {SIZING_PACKAGEDEPLOYMENT} "
                f"-n {SIZING_NAMESPACE} --type=merge -p "
                f"'{{\"spec\":{{\"values\":{{\"cluster\":{{\"worker\":{{\"machineType\":"
                f"\"{worker_target}\",\"size\":\"{expected_size}\"}}}}}}}}}}'",
                f"set worker machineType {worker_type or '?'} -> {worker_target} (size={expected_size}) "
                f"via PackageDeployment", tier="transient", timeout=60)
            res.action = f"machineType -> {worker_target}"
            if not r.dry_run:
                def _md_rolled_out():
                    d = r.read_json(f"kubectl get machinedeployment {md_name} "
                                    f"-n {SIZING_NAMESPACE} -o json 2>/dev/null", 30) or {}
                    st = d.get("status", {})
                    ref_name = d.get("spec", {}).get("template", {}).get("spec", {}).get("infrastructureRef", {}).get("name", "")
                    if ref_name:
                        tmpl = r.read_json(f"kubectl get vspheremachinetemplate {ref_name} "
                                          f"-n {SIZING_NAMESPACE} -o json 2>/dev/null", 30) or {}
                        cpus = tmpl.get("spec", {}).get("template", {}).get("spec", {}).get("numCPUs")
                        expected_cpus = SIZING_MACHINE_TYPES.get(worker_target, (0, 0))[0]
                        if expected_cpus and cpus != expected_cpus:
                            return False
                    return st.get("replicas", 0) > 0 and \
                        st.get("updatedReplicas", 0) == st.get("replicas", -1) and \
                        st.get("readyReplicas", 0) == st.get("replicas", -1) and \
                        st.get("phase", "") == "Running"
                reached = _sizing_poll(f"worker rollout to {worker_target}", _md_rolled_out,
                                       resize_timeout, poll_interval, r.dry_run)
                res.state = "pass" if reached else "warn"
                res.detail = ("rolled out" if reached else
                             f"patch applied; rollout not confirmed within "
                             f"{ctx.get('resize_timeout_min', 60)}m — check manually")
            out.append(res)

        if scale_changed:
            res = fail("sizing.scale",
                       f"Worker replica bounds == min={min_target} max={max_target}",
                       f"currently min={cur_min} max={cur_max}", cluster=cl)
            r.write(
                f"kubectl patch packagedeployment {SIZING_PACKAGEDEPLOYMENT} "
                f"-n {SIZING_NAMESPACE} --type=merge -p "
                f"'{{\"spec\":{{\"values\":{{\"cluster\":{{\"worker\":{{\"minReplicas\":"
                f"{min_target},\"maxReplicas\":{max_target}}}}}}}}}}}'",
                f"set worker bounds min={cur_min}/max={cur_max} -> "
                f"min={min_target}/max={max_target} via PackageDeployment",
                tier="transient", timeout=60)
            res.action = f"bounds -> min={min_target} max={max_target}"
            if not r.dry_run:
                def _flux_propagated():
                    d = r.read_json(f"kubectl get kubernetescluster {cluster_name} "
                                    f"-n {SIZING_NAMESPACE} -o json 2>/dev/null", 30) or {}
                    workers = d.get("spec", {}).get("workers") or [{}]
                    return workers[0].get("maxReplicas") == max_target
                p1 = _sizing_poll("Flux propagation to KubernetesCluster.spec.workers[0]",
                                  _flux_propagated, SIZING_FLUX_PROPAGATION_TIMEOUT,
                                  poll_interval, r.dry_run)
                # Phase 2: MachineDeployment drain/grow, with direct CAPI
                # MachineDeployment.spec.replicas patching when scaling is needed
                # (safe - CAPI-owned, not Flux-owned).
                p2 = False
                if r.dry_run:
                    p2 = True
                else:
                    target_rep = max_target if (min_target == max_target or md_spec_replicas < min_target) else min_target
                    if md_spec_replicas != target_rep:
                        r.write(
                            f"kubectl patch machinedeployment {md_name} "
                            f"-n {SIZING_NAMESPACE} --type=merge -p "
                            f"'{{\"spec\":{{\"replicas\":{target_rep}}}}}'",
                            f"scale MachineDeployment replicas {md_spec_replicas or '?'} -> {target_rep} directly",
                            tier="transient", timeout=60)
                    deadline = time.time() + scale_timeout
                    while time.time() < deadline:
                        cur = r.read_json(
                            f"kubectl get machinedeployment {md_name} "
                            f"-n {SIZING_NAMESPACE} -o json 2>/dev/null", 30) or {}
                        if cur.get("status", {}).get("replicas") == max_target:
                            p2 = True
                            break
                        if not ctx.get("no_auto_fix_autoscaler") and _sizing_autoscaler_stuck(r):
                            row_verbose("cluster-autoscaler stuck ('size increase too "
                                       "large' in its logs) — patching "
                                       "MachineDeployment.spec.replicas directly "
                                       "(CAPI-owned, not Flux-owned, so this is safe)")
                            r.write(
                                f"kubectl patch machinedeployment {md_name} "
                                f"-n {SIZING_NAMESPACE} --type=merge -p "
                                f"'{{\"spec\":{{\"replicas\":{max_target}}}}}'",
                                f"auto-fix stuck autoscaler: "
                                f"MachineDeployment.spec.replicas -> {max_target}",
                                tier="transient", timeout=60)
                        row_verbose(f"waiting for MachineDeployment drain/grow to "
                                   f"{max_target} ... ({int(deadline - time.time())}s "
                                   f"remaining)")
                        time.sleep(poll_interval)
                reached = p1 and p2
                res.state = "pass" if reached else "warn"
                res.detail = ("propagated and drained" if reached else
                             f"patch applied; propagation/drain not confirmed within "
                             f"the configured timeouts (flux_ok={p1}, drain_ok={p2}) — "
                             f"check manually")
            out.append(res)
    finally:
        if temporarily_enabled:
            _sizing_set_autoscaler(r, False, "restore cluster-autoscaler to its "
                                            "original disabled state")
        elif autoscaler_mode == "enable" and not as_enabled:
            res = fail("sizing.autoscaler.set", "cluster-autoscaler == ENABLED",
                       "was DISABLED", cluster=cl)
            _sizing_set_autoscaler(r, True, "enable cluster-autoscaler (--autoscaler enable)")
            res.action = "enabled"
            if not r.dry_run:
                res.state, res.detail = "warn", "was DISABLED; enabled"
            out.append(res)
        elif autoscaler_mode == "disable" and as_enabled:
            res = fail("sizing.autoscaler.set", "cluster-autoscaler == DISABLED",
                       "was ENABLED", cluster=cl)
            _sizing_set_autoscaler(r, False, "disable cluster-autoscaler (--autoscaler disable)")
            res.action = "disabled"
            if not r.dry_run:
                res.state, res.detail = "warn", "was ENABLED; disabled"
            out.append(res)

    need_verify = cp_changed or worker_changed or scale_changed
    if need_verify and not r.dry_run:
        after_rows, after_ok = _sizing_node_utilization(r, warn_pct)
        if after_ok:
            for nr in after_rows:
                label = f"{nr['node']}: utilization below {warn_pct}% (post-action)"
                detail = f"cpu={nr['cpu']} ({nr['cpu_pct']}%) mem={nr['mem']} ({nr['mem_pct']}%)"
                out.append(warn("sizing.util.after", label, detail, cluster=cl) if nr["hot"]
                           else ok("sizing.util.after", label, detail, cluster=cl))

        final_kc = r.read_json(f"kubectl get kubernetescluster {cluster_name} "
                               f"-n {SIZING_NAMESPACE} -o json 2>/dev/null", 30) or {}
        phase = final_kc.get("status", {}).get("phase", "")
        rc, pending = r.read(
            "kubectl get pods -A --field-selector=status.phase=Pending "
            "--no-headers 2>/dev/null | wc -l", 30)
        pending_n = _sizing_last(pending)
        label = "Final state: cluster Ready, no growing pending-pod backlog"
        detail = f"phase={phase or 'unknown'} pending_pods={pending_n or '?'}"
        out.append(ok("sizing.verify", label, detail, cluster=cl)
                   if phase in ("", "Provisioned", "Ready")
                   else warn("sizing.verify", label, detail, cluster=cl))

    return out


# ─── Footprint & HA reduction (remediate-lab.sh VSP Family-A, non-disruptive) ─
# Right-sized requests, HA controller counts, safe-to-evict annotations,
# CAPI/CAPV leader-election, and a durable autoscaler pin - ported from
# remediate-lab.sh's do_right_size/do_reduce_ha/do_safe_evict/
# do_disable_capi_le/do_pin/do_unpin. All of this is safe on a healthy
# cluster (idempotent patches, at most a rolling restart of the patched pod)
# and exists purely to shrink the fleet's footprint on nested/resource-
# constrained lab hardware - it is not "fixing" anything broken, which is why
# it lives behind --mode remediate rather than something --mode tune's
# template-prep pass would ever apply on its own.
FOOTPRINT_NAMESPACE = "vmsp-platform"
FOOTPRINT_REQUESTS = [
    # (kind, namespace, name, container, cpu, memory) - remediate-lab.sh:2708-2719
    ("statefulset", "vodap", "chi-vcf-obs-vcf-obs-0-0", "clickhouse", "250m", "1Gi"),
    ("statefulset", "ops-logs", "log-store", "opensearch", "250m", "8Gi"),
    ("statefulset", "ops-logs", "log-processor", "vcf-ops-logs", "250m", "2Gi"),
    ("deploy", "vmsp-platform", "ops-logs-gateway", "envoy", "200m", "256Mi"),
    ("deploy", "vodap", "vcf-obs-esx-collector-service",
     "vcf-obs-esx-collector-service", "200m", "1536Mi"),
    ("deploy", "vodap", "vcf-obs-vc-collector-service",
     "vcf-obs-vc-collector-service", "200m", "1Gi"),
    ("deploy", "vodap", "vcf-obs-data-query-service",
     "vcf-obs-data-query-service", "200m", "1Gi"),
    ("deploy", "vodap", "vcf-obs-collector-controller-service",
     "vcf-obs-collector-controller-service", "200m", "1Gi"),
    ("deploy", "vodap", "vcf-obs-netops-collector-service",
     "vcf-obs-netops-collector-service", "200m", "1Gi"),
]
FOOTPRINT_HA_DEPLOYS = [
    ("kube-system", "coredns"),
    ("vmsp-platform", "capi-controller-manager"),
    ("vmsp-platform", "capi-ipam-in-cluster-controller-manager"),
    ("vmsp-platform", "capi-kubeadm-bootstrap-controller-manager"),
    ("vmsp-platform", "capi-kubeadm-control-plane-controller-manager"),
    ("vmsp-platform", "capv-controller-manager"),
    ("vmsp-platform", "ndc-controller-manager"),
    ("vmsp-platform", "vmsp-identity"),
]
FOOTPRINT_CAPI_LE_DEPLOYS = (
    "capi-controller-manager", "capi-ipam-in-cluster-controller-manager",
    "capi-kubeadm-bootstrap-controller-manager",
    "capi-kubeadm-control-plane-controller-manager", "capv-controller-manager",
)


def _footprint_discover_autoscaler_rt(r):
    rc, out = r.read(
        f"kubectl get releasetemplate -n {FOOTPRINT_NAMESPACE} -o name 2>/dev/null "
        "| grep -i autoscaler | head -1", 30)
    name = _sizing_last(out)
    return name.split("/", 1)[1] if "/" in name else name


def chk_footprint(r, ctx):
    """VSP fleet lab-density reduction. See the module comment above this
    section for what each lever does and why it is remediate-only.

    The autoscaler pin here is DELIBERATELY a different lever from the
    `sizing` section's --autoscaler flag: this one patches the
    cluster-autoscaler ReleaseTemplate's spec.helm.values.replicaCount (the
    durable, vmsp-operator-rendered layer remediate-lab.sh uses to freeze the
    worker count indefinitely for cost/capacity reasons), while `sizing`'s
    --autoscaler patches the HelmRelease's spec.suspend + Deployment replicas
    directly (a temporary pause used only to let a replica-bounds change
    converge, then restored). Two different, compatible knobs - not two
    implementations of one.
    """
    out = []
    cl = r.cluster

    for kind, ns, name, container, cpu, mem in FOOTPRINT_REQUESTS:
        rc, cur = r.read(
            f"kubectl get {kind} {name} -n {ns} -o "
            f"jsonpath='{{.spec.template.spec.containers[?(@.name==\"{container}\")]"
            f".resources.requests}}' 2>/dev/null", 30)
        cur = (cur or "").strip()
        label = f"{ns}/{name} [{container}]: requests == cpu={cpu} mem={mem}"
        if not cur:
            out.append(warn("footprint.requests", label,
                            "object or container not found", cluster=cl))
            continue
        squeezed = cur.replace(" ", "")
        if f'"cpu":"{cpu}"' in squeezed and f'"memory":"{mem}"' in squeezed:
            out.append(ok("footprint.requests", label, "already at target", cluster=cl))
            continue
        res = fail("footprint.requests", label, f"currently {cur}", cluster=cl)
        if may_act(r, "footprint"):
            if name == "ops-logs-gateway":
                r.write(
                    f"kubectl patch envoyproxy {name}-config -n {ns} --type=json -p "
                    f"'[{{\"op\":\"replace\",\"path\":\"/spec/provider/kubernetes/envoyDeployment/patch/value/spec/template/spec/containers/0/resources/requests/cpu\",\"value\":\"{cpu}\"}},{{\"op\":\"replace\",\"path\":\"/spec/provider/kubernetes/envoyDeployment/patch/value/spec/template/spec/containers/0/resources/requests/memory\",\"value\":\"{mem}\"}}]'",
                    f"patch EnvoyProxy CR {name}-config requests -> cpu={cpu} mem={mem}",
                    tier="persistent", timeout=60)
            r.write(
                f"kubectl set resources {kind}/{name} -n {ns} --containers={container} "
                f"--requests=cpu={cpu},memory={mem}",
                f"right-size {ns}/{name} [{container}] requests -> cpu={cpu} mem={mem}",
                tier="transient", timeout=60)
            res.action = f"requests -> cpu={cpu} mem={mem}"
            if not r.dry_run:
                res.state, res.detail = "warn", f"was {cur}; set to target"
        out.append(res)

    for ns, name in FOOTPRINT_HA_DEPLOYS:
        rc, reps = r.read(
            f"kubectl get deploy {name} -n {ns} -o jsonpath='{{.spec.replicas}}' 2>/dev/null", 30)
        reps = _sizing_last(reps)
        label = f"{ns}/{name}: replicas == 1"
        if not reps.isdigit():
            out.append(warn("footprint.ha", label, "not found", cluster=cl))
            continue
        if reps == "1":
            out.append(ok("footprint.ha", label, "already 1", cluster=cl))
            continue
        res = fail("footprint.ha", label, f"currently {reps}", cluster=cl)
        if may_act(r, "footprint"):
            prefix = "ndc" if name == "ndc-controller-manager" else name
            r.write(
                f"for rt in $(kubectl get releasetemplate -n {ns} -o name 2>/dev/null | grep -E '{prefix}'); do "
                f"kubectl patch \"$rt\" -n {ns} --type=merge -p '{{\"spec\":{{\"helm\":{{\"values\":{{\"replicaCount\":1}}}}}}}}'; "
                f"done",
                f"patch ReleaseTemplates for {name} replicaCount -> 1",
                tier="persistent", timeout=60)
            r.write(f"kubectl scale deploy {name} -n {ns} --replicas=1",
                    f"reduce {ns}/{name} replicas {reps} -> 1", tier="transient", timeout=60)
            res.action = "replicas -> 1"
            if not r.dry_run:
                res.state, res.detail = "warn", f"was {reps}; scaled to 1"
        out.append(res)

    rc, dep_list = r.read(
        "kubectl get deploy -n vodap --no-headers -o "
        "custom-columns=N:.metadata.name 2>/dev/null", 30)
    for dep in [d.strip() for d in (dep_list or "").splitlines() if d.strip()]:
        rc, hp = r.read(
            f"kubectl get deploy {dep} -n vodap -o "
            "jsonpath='{.spec.template.spec.volumes[*].hostPath.path}' 2>/dev/null", 30)
        hp = (hp or "").strip()
        if not hp:
            continue          # no hostPath volume - nothing for the autoscaler to fear evicting
        rc, ann = r.read(
            f"kubectl get deploy {dep} -n vodap -o jsonpath="
            "'{.spec.template.metadata.annotations.cluster-autoscaler\\.kubernetes\\.io/safe-to-evict}'"
            " 2>/dev/null", 30)
        label = f"vodap/{dep}: safe-to-evict=true (hostPath {hp})"
        if (ann or "").strip() == "true":
            out.append(ok("footprint.evict", label, cluster=cl))
            continue
        res = fail("footprint.evict", label, "annotation missing or false", cluster=cl)
        if may_act(r, "footprint"):
            r.write(
                f"kubectl patch deploy {dep} -n vodap --type=merge -p "
                f"'{{\"spec\":{{\"template\":{{\"metadata\":{{\"annotations\":"
                f"{{\"cluster-autoscaler.kubernetes.io/safe-to-evict\":\"true\"}}}}}}}}}}'",
                f"annotate vodap/{dep} safe-to-evict=true", tier="persistent", timeout=60)
            res.action = "annotated safe-to-evict=true"
            if not r.dry_run:
                res.state, res.detail = "warn", "annotation was missing; added"
        out.append(res)

    for dep in FOOTPRINT_CAPI_LE_DEPLOYS:
        d = r.read_json(f"kubectl get deploy {dep} -n {FOOTPRINT_NAMESPACE} -o json 2>/dev/null", 30)
        label = f"{FOOTPRINT_NAMESPACE}/{dep}: --leader-elect=false"
        if not d:
            out.append(warn("footprint.capi_le", label, "not found", cluster=cl))
            continue
        replicas = d.get("spec", {}).get("replicas")
        if replicas not in (1, None):
            out.append(warn("footprint.capi_le", label,
                            f"replicas={replicas} (>1) — leaving LE on; it provides "
                            "real failover here", cluster=cl))
            continue
        args = []
        for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            args.extend(c.get("args") or [])
        if "--leader-elect=false" in args:
            out.append(ok("footprint.capi_le", label, cluster=cl))
            continue
        if not any(a in ("--leader-elect", "--leader-elect=true") for a in args):
            out.append(warn("footprint.capi_le", label,
                            "no --leader-elect arg on this build", cluster=cl))
            continue
        res = fail("footprint.capi_le", label, "still --leader-elect=true", cluster=cl)
        if may_act(r, "footprint"):
            # Same json-patch-by-index approach as remediate-lab.sh:2756 - shipped as
            # one inline python3 heredoc so no local quoting games are needed; the
            # whole multi-line command is base64-wrapped by the transport already.
            r.write(
                "python3 - <<'PY'\n"
                "import subprocess, json\n"
                "KC=['kubectl']\n"
                f"r=subprocess.run(KC+['get','deploy','{dep}','-n','{FOOTPRINT_NAMESPACE}',"
                "'-o','json'],capture_output=True,text=True)\n"
                "d=json.loads(r.stdout)\n"
                "for ci,c in enumerate(d['spec']['template']['spec']['containers']):\n"
                "    for ai,a in enumerate(c.get('args') or []):\n"
                "        if a in ('--leader-elect','--leader-elect=true'):\n"
                "            p=[{'op':'replace','path':'/spec/template/spec/containers/"
                "%d/args/%d'%(ci,ai),'value':'--leader-elect=false'}]\n"
                f"            subprocess.run(KC+['patch','deploy','{dep}','-n',"
                "'" + FOOTPRINT_NAMESPACE + "','--type=json','-p',json.dumps(p)])\n"
                "PY\n",
                f"set --leader-elect=false on {FOOTPRINT_NAMESPACE}/{dep} (replicas=1, "
                "clusterctl-managed so this sticks)", tier="transient", timeout=60)
            res.action = "--leader-elect=false"
            if not r.dry_run:
                res.state, res.detail = "warn", "was --leader-elect=true; disabled"
        out.append(res)

    rt = _footprint_discover_autoscaler_rt(r)
    if not rt:
        out.append(warn("footprint.autoscaler_pin",
                        "cluster-autoscaler ReleaseTemplate discoverable",
                        "no cluster-autoscaler ReleaseTemplate found", cluster=cl))
    else:
        rc, rt_replicas = r.read(
            f"kubectl get releasetemplate {rt} -n {FOOTPRINT_NAMESPACE} -o jsonpath="
            "'{.spec.helm.values.replicaCount}' 2>/dev/null", 30)
        rt_replicas = _sizing_last(rt_replicas)
        want_pinned = ctx.get("autoscaler_pin")     # True=pin off, False=unpin, None=report only
        label = f"{rt}: replicaCount"
        if rt_replicas == "0":
            out.append(ok("footprint.autoscaler_pin", f"{label} == 0 (pinned off)", cluster=cl))
        elif rt_replicas == "1":
            out.append(ok("footprint.autoscaler_pin", f"{label} == 1 (active)", cluster=cl))
        else:
            out.append(warn("footprint.autoscaler_pin", label,
                            f"unexpected value '{rt_replicas}'", cluster=cl))
        if want_pinned is not None and may_act(r, "footprint"):
            target = "0" if want_pinned else "1"
            if rt_replicas != target:
                r.write(
                    f"kubectl patch releasetemplate {rt} -n {FOOTPRINT_NAMESPACE} "
                    f"--type=merge -p '{{\"spec\":{{\"helm\":{{\"values\":"
                    f"{{\"replicaCount\":{target}}}}}}}}}'",
                    f"{'pin' if want_pinned else 'unpin'} autoscaler: {rt} "
                    f"replicaCount {rt_replicas or '?'} -> {target}",
                    tier="persistent", timeout=60)
                out.append(CheckResult(
                    "footprint.autoscaler_pin.set", f"{label} == {target}",
                    "fail" if r.dry_run else "warn",
                    f"was {rt_replicas}; {'pin' if want_pinned else 'unpin'} applied",
                    cluster=cl, action=f"replicaCount -> {target}"))

    # --envoy-gateway-fix (remediate-lab.sh:2105 eg_apply, Family C). Uses the
    # SAME EG_MEM_LIMIT/EG_MEM_REQUEST constants the drift-keeper already
    # asserts (4Gi/512Mi, the value verified LIVE on this lab) rather than
    # remediate-lab.sh's own 8Gi/1536Mi - the two disagreeing is exactly F2's
    # finding (a keeper and a one-shot fix asserting different memory limits
    # fight each other every reconcile). Single constant, used everywhere.
    eg_rt = _storm_discover_rt(r, "envoyproxy-gateway-")
    if not eg_rt:
        out.append(warn("footprint.envoy_gateway", "envoy-gateway ReleaseTemplate discoverable",
                        "not found", cluster=cl))
    else:
        rc, cur_mem = r.read(
            f"kubectl get releasetemplate/{eg_rt} -n {FOOTPRINT_NAMESPACE} -o jsonpath="
            "'{.spec.helm.values.deployment.envoyGateway.resources.limits.memory}' "
            "2>/dev/null", 30)
        cur_mem = _sizing_last(cur_mem)
        rc, cur_le = r.read(
            f"kubectl get releasetemplate/{eg_rt} -n {FOOTPRINT_NAMESPACE} -o jsonpath="
            "'{.spec.helm.values.config.envoyGateway.provider.kubernetes."
            "leaderElection.disable}' 2>/dev/null", 30)
        cur_le = _sizing_last(cur_le)
        label = (f"{eg_rt}: memory.limit={EG_MEM_LIMIT} + leaderElection.disable=true")
        if cur_mem == EG_MEM_LIMIT and cur_le == "true":
            out.append(ok("footprint.envoy_gateway", label, cluster=cl))
        else:
            res = fail("footprint.envoy_gateway", label,
                       f"currently memory.limit={cur_mem or 'unset'} "
                       f"leaderElection.disable={cur_le or 'unset(false)'}", cluster=cl)
            if may_act(r, "footprint"):
                rc, replicas = r.read(
                    f"kubectl get deployment envoy-gateway -n {FOOTPRINT_NAMESPACE} "
                    "-o jsonpath='{.spec.replicas}' 2>/dev/null", 30)
                replicas = _sizing_last(replicas)
                if replicas and replicas != "1":
                    res.state = "warn"
                    res.detail += (f" — deployment/envoy-gateway is running "
                                  f"{replicas} replicas, not 1; disabling leader "
                                  "election on a genuinely multi-replica HA "
                                  "deployment would be a correctness regression, "
                                  "not a fix — refusing, verify manually")
                else:
                    patch_payload = json.dumps({
                        "spec": {
                            "helm": {
                                "values": {
                                    "deployment": {
                                        "envoyGateway": {
                                            "resources": {
                                                "limits": {"memory": EG_MEM_LIMIT},
                                                "requests": {"memory": EG_MEM_REQUEST},
                                            }
                                        }
                                    },
                                    "config": {
                                        "envoyGateway": {
                                            "provider": {
                                                "kubernetes": {
                                                    "leaderElection": {"disable": True}
                                                }
                                            }
                                        }
                                    },
                                }
                            }
                        }
                    })
                    r.write(
                        f"kubectl patch releasetemplate/{eg_rt} -n {FOOTPRINT_NAMESPACE} --type=merge -p "
                        f"'{patch_payload}'",
                        f"envoy-gateway-fix: {eg_rt} memory.limit -> {EG_MEM_LIMIT}, "
                        "leaderElection.disable -> true", tier="persistent", timeout=60)
                    hr_payload = json.dumps({
                        "spec": {
                            "values": {
                                "deployment": {
                                    "envoyGateway": {
                                        "resources": {
                                            "limits": {"memory": EG_MEM_LIMIT},
                                            "requests": {"memory": EG_MEM_REQUEST},
                                        }
                                    }
                                },
                                "config": {
                                    "envoyGateway": {
                                        "provider": {
                                            "kubernetes": {
                                                "leaderElection": {"disable": True}
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    })
                    r.write(
                        f"kubectl patch helmrelease envoyproxy-gateway -n {FOOTPRINT_NAMESPACE} --type=merge -p "
                        f"'{hr_payload}' 2>/dev/null || true",
                        f"envoy-gateway-fix: HelmRelease envoyproxy-gateway values -> {EG_MEM_LIMIT}", tier="persistent", timeout=60)
                    r.write(
                        f"kubectl set resources deploy/envoy-gateway -n {FOOTPRINT_NAMESPACE} "
                        f"--limits=memory={EG_MEM_LIMIT} --requests=memory={EG_MEM_REQUEST} 2>/dev/null || true",
                        f"envoy-gateway-fix: deploy/envoy-gateway resources -> limits={EG_MEM_LIMIT}", tier="transient", timeout=60)
                    res.action = f"memory -> {EG_MEM_LIMIT}, leaderElection.disable -> true"
                    if not r.dry_run:
                        res.state = "warn"
                        res.detail = (f"was memory.limit={cur_mem or 'unset'} "
                                      f"leaderElection.disable={cur_le or 'unset'}; patched")
            out.append(res)

    return out


CLICKHOUSE_CLIENTS = ("vcf-obs-data-query-service", "vcf-obs-collector-controller-service")
FLUENTD_POD = "logging-operator-fluentd-0"
FLUENTD_NS = "vmsp-platform"
FLUENTD_DISK_PCT = 80
FLUENTD_BUFFER_FILES = 10000


def chk_vodap(r, ctx):
    """ClickHouse serving cert freshness and fluentd buffer growth.

    Two distinct failure modes, both from vodap-fix.py:

    1. ClickHouse holds its TLS cert in MEMORY. cert-manager can renew the Secret
       while the running pod keeps serving the old one, so comparing the Secret's
       dates to itself always looks fine - you have to compare what the socket
       actually presents against what the Secret says. That served-vs-stored
       mismatch is the whole point of the check.
    2. fluentd's /buffers PVC fills with abandoned backup chunks (86,546 files /
       8.1 GB in 89 days, per the vsp-health README). A restart cannot help: the
       PVC persists, which is why the fix is a purge, not a rollout. vodap-fix.py's
       own docstring gets this wrong and claims a StatefulSet restart.
    """
    out = []
    cl = r.cluster

    # --- ClickHouse: served cert vs stored cert ---
    rc, info = r.read(
        "ns=vodap; "
        "stored=$(kubectl -n $ns get secret vcf-obs-clickhouse-cert "
        "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d 2>/dev/null "
        "| openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2); "
        "ip=$(kubectl -n $ns get svc clickhouse-vcf-obs "
        "-o jsonpath='{.spec.clusterIP}' 2>/dev/null); "
        "served=''; "
        "if [ -n \"$ip\" ]; then "
        "served=$(echo | timeout 15 openssl s_client -connect \"$ip:8443\" 2>/dev/null "
        "| openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2); fi; "
        "echo \"STORED=$stored\"; echo \"SERVED=$served\"", 90)
    stored = served = ""
    for line in (info or "").splitlines():
        if line.startswith("STORED="):
            stored = line.split("=", 1)[1].strip()
        elif line.startswith("SERVED="):
            served = line.split("=", 1)[1].strip()

    label = "ClickHouse: serving the current certificate"
    if not stored:
        out.append(ok("vodap.cert", label,
                      "no vcf-obs-clickhouse-cert on this cluster", cluster=cl))
    elif not served:
        out.append(warn("vodap.cert", label,
                        "could not read the served cert (ClusterIP unreachable "
                        "from the node?)", cluster=cl))
    elif stored == served:
        out.append(ok("vodap.cert", label, f"notAfter {stored}", cluster=cl))
    else:
        res = fail("vodap.cert", label,
                   f"stale in memory — serving '{served}' but the Secret says "
                   f"'{stored}'", cluster=cl)
        if may_act(r, "vodap"):
            r.write("kubectl rollout restart statefulset/chi-vcf-obs-vcf-obs-0-0 "
                    "-n vodap",
                    "rollout restart ClickHouse so it reloads its renewed cert",
                    tier="transient", timeout=120)
            res.action = "clickhouse restarted"
            if not r.dry_run:
                res.state = "warn"
                res.detail = "was stale in memory; restarted to reload"
        out.append(res)

    # --- ClickHouse clients ---
    for dep in CLICKHOUSE_CLIENTS:
        rc, reps = r.read(
            f"kubectl -n vodap get deployment {dep} "
            "-o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null", 45)
        val = (reps or "").strip().splitlines()
        val = val[-1].strip() if val else ""
        dlabel = f"vodap/{dep}: ready"
        if "/" not in val:
            out.append(warn("vodap.client", dlabel, "not found", cluster=cl))
            continue
        have, _, want = val.partition("/")
        have = int(have) if have.isdigit() else 0
        want = int(want) if want.isdigit() else 0
        if want == 0:
            out.append(ok("vodap.client", f"{dlabel} (scaled to 0 by design)",
                          cluster=cl))
        elif have >= want:
            out.append(ok("vodap.client", dlabel, f"{have}/{want}", cluster=cl))
        else:
            out.append(fail("vodap.client", dlabel, f"{have}/{want}", cluster=cl))

    # --- fluentd buffers ---
    rc, buf = r.read(
        f"p={FLUENTD_POD}; n={FLUENTD_NS}; "
        "rdy=$(kubectl -n $n get pod $p --no-headers 2>/dev/null | awk '{print $2}'); "
        "pct=$(kubectl -n $n exec $p -c fluentd -- df -h /buffers 2>/dev/null "
        "| tail -1 | awk '{gsub(/%/,\"\",$5); print $5}'); "
        "bak=$(kubectl -n $n exec $p -c fluentd -- sh -c "
        "'find /buffers/backup -type f 2>/dev/null | wc -l' 2>/dev/null); "
        "echo \"RDY=$rdy\"; echo \"PCT=$pct\"; echo \"BAK=$bak\"", 90)
    rdy = pct = bak = ""
    for line in (buf or "").splitlines():
        if line.startswith("RDY="):
            rdy = line.split("=", 1)[1].strip()
        elif line.startswith("PCT="):
            pct = line.split("=", 1)[1].strip()
        elif line.startswith("BAK="):
            bak = line.split("=", 1)[1].strip()

    flabel = f"{FLUENTD_NS}/{FLUENTD_POD}: buffers healthy"
    if not rdy:
        out.append(ok("vodap.fluentd", flabel,
                      "fluentd not present on this cluster", cluster=cl))
    else:
        disk = int(pct) if pct.isdigit() else 0
        files = int(bak) if bak.isdigit() else 0
        bad = disk > FLUENTD_DISK_PCT or files > 0
        if not bad:
            out.append(ok("vodap.fluentd", flabel,
                          f"{disk}% used, {files} backup file(s), ready={rdy}",
                          cluster=cl))
        else:
            res = fail("vodap.fluentd", flabel,
                       f"{disk}% used with {files} abandoned backup file(s) — a "
                       f"restart cannot help, the PVC persists", cluster=cl)
            if may_act(r, "vodap"):
                r.write(f"kubectl -n {FLUENTD_NS} exec {FLUENTD_POD} -c fluentd -- "
                        "sh -c 'rm -rf /buffers/backup/*'",
                        f"purge abandoned fluentd backup chunks ({files} file(s))",
                        tier="transient", timeout=180)
                res.action = "buffers purged"
                if not r.dry_run:
                    res.state = "warn"
                    res.detail = f"purged {files} backup file(s) at {disk}% used"
            out.append(res)

    return out


def chk_gateway(r, ctx):
    """Gateway dataplane services must exist and hold their VIPs.

    Two distinct things: the hashed per-gateway envoy Services, and the
    LoadBalancer Services that own the user-facing VIPs. A missing LB ingress IP
    is what "the UI is down" actually looks like (auto-health.py:674).
    """
    out = []
    cl = r.cluster
    cfg = CLUSTERS[cl]
    for svc, want_ip in (cfg.get("gateway_services") or []):
        rc, got = r.read(
            f"kubectl -n vmsp-platform get svc {svc} "
            "-o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null", 45)
        val = (got or "").strip().splitlines()
        val = val[-1].strip() if val else ""
        label = f"vmsp-platform/{svc}: holds {want_ip}"
        if val == want_ip:
            out.append(ok("gateway.svc", label, cluster=cl))
        elif not val:
            out.append(fail("gateway.svc", label,
                            "no LoadBalancer ingress IP assigned", cluster=cl))
        else:
            out.append(fail("gateway.svc", label, f"holds {val} instead", cluster=cl))

    # Hashed envoy dataplane Services. This is a WARNING, not a failure, matching
    # vcfa-stabilizer.sh:1070 which only fails when
    # STABILIZER_GATEWAY_PREFLIGHT_STRICT=1 is set explicitly.
    #
    # Verified on this build (2026-08-14): NO service matches envoy-vmsp-platform*
    # -- only the envoy-gateway operator Service exists -- while both LoadBalancer
    # VIPs are held and /automation returns HTTP 200. So the naming scheme differs
    # here and absence of that pattern does not mean the gateway is broken.
    # auto-health.py does not check this at all. The LB VIP rows above are the
    # load-bearing ones; treating this as a hard failure produced a false alarm on
    # a demonstrably working gateway.
    rc, hashed = r.read(
        "kubectl -n vmsp-platform get svc -o name 2>/dev/null "
        "| grep -c envoy-vmsp-platform", 45)
    val = (hashed or "").strip().splitlines()
    val = val[-1].strip() if val else "0"
    n = int(val) if val.isdigit() else 0
    hlabel = "vmsp-platform: hashed envoy dataplane Services present"
    if n >= 1:
        out.append(ok("gateway.envoy", hlabel, f"{n} found", cluster=cl))
    else:
        out.append(warn("gateway.envoy", hlabel,
                        "none match envoy-vmsp-platform* — informational: this "
                        "build may name them differently. Judge the gateway by the "
                        "LB VIP rows above and the endpoint section", cluster=cl))
    return out


def chk_edge(r, ctx):
    """Known edge cases with specific, documented signatures.

    Each of these was a real incident whose symptom pointed somewhere unhelpful:
      * support-bundle CronJob runaway - 30 GHz+ of CPU, UI unresponsive.
      * resource-manager self-dial gRPC deadlock - the pod is Running and its
        probe passes, but it never listens on :7710/:7777.
      * RabbitMQ missing its copy-config init container - starts with
        "Config file(s): (none)", no AMQPS listener, yet rabbitmq-diagnostics ping
        (the probe) still passes, so it looks healthy while ~15 prelude
        deployments stall behind ebs-service.
    """
    out = []
    cl = r.cluster

    rc, jobs = r.read(
        "kubectl -n vmsp-platform get jobs "
        "-l app.kubernetes.io/name=support-bundle-cluster-info-dump "
        "--no-headers 2>/dev/null | wc -l", 45)
    val = (jobs or "").strip().splitlines()
    val = val[-1].strip() if val else "0"
    n = int(val) if val.isdigit() else 0
    label = "support-bundle jobs: not runaway (<= 3)"
    if n <= 3:
        out.append(ok("edge.supportbundle", label, f"{n} job(s)", cluster=cl))
    else:
        res = fail("edge.supportbundle", label,
                   f"{n} jobs — this pegs CPU and makes the UI unresponsive",
                   cluster=cl)
        if may_act(r, "edge"):
            r.write("kubectl -n vmsp-platform delete jobs "
                    "-l app.kubernetes.io/name=support-bundle-cluster-info-dump "
                    "--cascade=foreground",
                    f"delete {n} runaway support-bundle jobs",
                    tier="transient", timeout=180)
            r.write("kubectl -n vmsp-platform patch cronjob "
                    "support-bundle-cluster-info-dump "
                    "-p '{\"spec\":{\"concurrencyPolicy\":\"Replace\"}}'",
                    "set the support-bundle CronJob concurrencyPolicy=Replace",
                    tier="persistent", timeout=60)
            res.action = "jobs deleted, concurrency capped"
            if not r.dry_run:
                res.state = "warn"
                res.detail = f"{n} deleted; concurrencyPolicy=Replace"
        out.append(res)

    rc, rm = r.read(
        "pod=$(kubectl -n prelude get pod -l app=resource-manager-server -o name 2>/dev/null | head -1 | cut -d/ -f2); "
        "[ -n \"$pod\" ] || pod=$(kubectl -n prelude get pod -l app=resource-manager-server-app -o name 2>/dev/null | head -1 | cut -d/ -f2); "
        "[ -n \"$pod\" ] || { echo RM_NOT_FOUND; exit 0; }; "
        "kubectl -n prelude exec \"$pod\" -- sh -c "
        "'netstat -tlpn 2>/dev/null | grep -qE \":7710|:7777\" && echo RM_LISTENING "
        "|| echo RM_DEADLOCK_SUSPECT' 2>/dev/null || echo RM_UNKNOWN", 90)
    text = (rm or "")
    rlabel = "prelude/resource-manager-server: listening on its gRPC port"
    if "RM_LISTENING" in text:
        out.append(ok("edge.rm", rlabel, cluster=cl))
    elif "RM_NOT_FOUND" in text:
        out.append(warn("edge.rm", rlabel, "pod not found", cluster=cl))
    elif "RM_DEADLOCK_SUSPECT" in text:
        res = fail("edge.rm", rlabel,
                   "not listening — self-dial gRPC bootstrap deadlock; the pod "
                   "looks Running and its probe passes", cluster=cl)
        if may_act(r, "edge"):
            r.write("kubectl patch service resource-manager-grpc -n prelude "
                    "-p '{\"spec\":{\"publishNotReadyAddresses\":true}}'",
                    "set publishNotReadyAddresses=true on resource-manager-grpc Service",
                    tier="persistent", timeout=60)
            r.write("kubectl delete pod -n prelude -l app=resource-manager-server 2>/dev/null; "
                    "kubectl delete pod -n prelude -l app=resource-manager-server-app 2>/dev/null",
                    "restart resource-manager-server pod to unblock gRPC self-dial bootstrap deadlock",
                    tier="transient", timeout=90)
            res.action = "publishNotReadyAddresses set, pod restarted"
            if not r.dry_run:
                res.state = "warn"
                res.detail = "unblocked gRPC self-dial bootstrap deadlock"
        out.append(res)
    else:
        out.append(warn("edge.rm", rlabel, "could not determine", cluster=cl))

    rc, rmq = r.read(
        "kubectl -n prelude get statefulset rabbitmq-ha "
        "-o jsonpath='{.spec.template.spec.initContainers[?(@.name==\"copy-config\")].name}' "
        "2>/dev/null", 45)
    val = (rmq or "").strip().splitlines()
    val = val[-1].strip() if val else ""
    qlabel = "prelude/rabbitmq-ha: copy-config init container present"
    if "copy-config" in val:
        out.append(ok("edge.rabbitmq", qlabel, cluster=cl))
    else:
        res = fail("edge.rabbitmq", qlabel,
                   "MISSING — RabbitMQ starts with no config and no AMQPS "
                   "listener while its ping probe still passes; ~15 prelude "
                   "deployments stall behind ebs-service", cluster=cl)
        if may_act(r, "edge"):
            rc_img, img = r.read("kubectl -n prelude get statefulset rabbitmq-ha "
                                 "-o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null", 30)
            img = (img or "").strip().splitlines()
            img = img[-1].strip() if img else "rabbitmq:3.11-management"
            patch_json = (
                '[{"op":"add","path":"/spec/template/spec/initContainers/-","value":{'
                '"name":"copy-config","image":"' + img + '","imagePullPolicy":"IfNotPresent",'
                '"command":["sh","-c","set -e; cp -L /config-src/* /etc/rabbitmq/ 2>/dev/null || true; '
                'if [ -f /definitions-src/definitions.json ]; then cp -L /definitions-src/definitions.json /etc/rabbitmq/definitions.json; fi; '
                'chmod 0644 /etc/rabbitmq/* 2>/dev/null || true; echo COPY_CONFIG_DONE"],'
                '"volumeMounts":[{"mountPath":"/etc/rabbitmq","name":"config"},'
                '{"mountPath":"/config-src","name":"configmap","readOnly":true},'
                '{"mountPath":"/definitions-src","name":"definitions","readOnly":true}],'
                '"resources":{},"terminationMessagePath":"/dev/termination-log","terminationMessagePolicy":"File"}}]'
            )
            r.write(f"kubectl patch statefulset rabbitmq-ha -n prelude --type=json -p '{patch_json}' && "
                    f"kubectl delete pod rabbitmq-ha-0 -n prelude --now",
                    "restore copy-config init container and restart rabbitmq-ha-0",
                    tier="persistent", timeout=90)
            res.action = "copy-config init container restored"
            if not r.dry_run:
                res.state = "warn"
                res.detail = "copy-config array-append patch applied"
        out.append(res)

    rc_err, rmq_logs = r.read("kubectl logs rabbitmq-ha-0 -n prelude -c rabbitmq-ha --tail 50 2>/dev/null", 30)
    if "erlang.cookie" in (rmq_logs or "") or "accessible by owner only" in (rmq_logs or ""):
        res_cookie = fail("edge.rabbitmq_cookie", "prelude/rabbitmq-ha: .erlang.cookie permissions valid",
                          "erlang.cookie wrong permissions", cluster=cl)
        if may_act(r, "edge"):
            rc_img, img = r.read("kubectl -n prelude get statefulset rabbitmq-ha "
                                 "-o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null", 30)
            img = (img or "").strip().splitlines()
            img = img[-1].strip() if img else "rabbitmq:3.11-management"
            cookie_patch = (
                '[{"op":"add","path":"/spec/template/spec/initContainers/-","value":{'
                '"name":"fix-cookie","image":"' + img + '",'
                '"command":["sh","-c","chmod 400 /var/lib/rabbitmq/.erlang.cookie && echo FIXED"],'
                '"volumeMounts":[{"name":"rabbit-pvc","mountPath":"/var/lib/rabbitmq"}]}}]'
            )
            r.write(f"kubectl patch statefulset rabbitmq-ha -n prelude --type=json -p '{cookie_patch}' && "
                    f"kubectl delete pod rabbitmq-ha-0 -n prelude --now",
                    "add fix-cookie init container to fix .erlang.cookie permissions",
                    tier="persistent", timeout=90)
            res_cookie.action = "fix-cookie init container added"
            if not r.dry_run:
                res_cookie.state = "warn"
                res_cookie.detail = "fix-cookie patch applied"
        out.append(res_cookie)
    return out


def chk_etcd(r, ctx):
    """etcd database fragmentation. Informational, with a threshold-gated defrag.

    auto-health.py reports slack and explicitly takes no action. Defrag on a
    single-member etcd is a brief apiserver-unavailability window, so it is gated
    on the same 30% threshold vcfa-stabilizer.sh uses rather than run on sight.
    """
    cl = r.cluster
    rc, out_ = r.read(
        "etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt "
        "--cert=/etc/kubernetes/pki/etcd/peer.crt "
        "--key=/etc/kubernetes/pki/etcd/peer.key "
        "--endpoints=https://127.0.0.1:2379 endpoint status -w json 2>/dev/null", 90)
    raw = (out_ or "")
    start = raw.find("[")
    if rc != 0 or start < 0:
        return [warn("etcd", "etcd fragmentation: below 30%",
                     "etcdctl not available or returned nothing", cluster=cl)]
    try:
        data = json.loads(raw[start:])
        st = data[0]["Status"]
        size, in_use = st["dbSize"], st["dbSizeInUse"]
    except (ValueError, KeyError, IndexError):
        return [warn("etcd", "etcd fragmentation: below 30%",
                     "could not parse endpoint status", cluster=cl)]
    slack = int(100 * (size - in_use) / size) if size else 0
    label = "etcd fragmentation: below 30%"
    detail = (f"{slack}% slack (dbSize {size // (1024*1024)}MiB, "
              f"inUse {in_use // (1024*1024)}MiB)")
    if slack < 30:
        return [ok("etcd", label, detail, cluster=cl)]
    res = warn("etcd", label, detail + " — informational; defrag briefly stalls "
                                      "the apiserver on a single-member etcd",
               cluster=cl)
    if may_act(r, "etcd"):
        r.write("etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt "
                "--cert=/etc/kubernetes/pki/etcd/peer.crt "
                "--key=/etc/kubernetes/pki/etcd/peer.key "
                "--endpoints=https://127.0.0.1:2379 defrag --command-timeout=120s && "
                "etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt "
                "--cert=/etc/kubernetes/pki/etcd/peer.crt "
                "--key=/etc/kubernetes/pki/etcd/peer.key "
                "--endpoints=https://127.0.0.1:2379 alarm disarm",
                f"defrag etcd ({slack}% slack) and disarm alarms",
                tier="transient", timeout=200)
        res.action = "defragmented"
    return [res]


# ─── Storm mitigation (vcfa-storm-mitigation.sh port, embedded in remediate-lab.sh) ─
# Durable, idempotent mitigations for the recurring ~10-minute CPU storm on the
# single-node VCF Automation appliance (RCA: in-guest concurrency, NOT
# hypervisor steal and NOT vCenter-slow). Every lever here is the "apply"
# composite from the source script - footprint reduction, prelude probe-
# tolerance relax, kube-vip lease repairs, and the data-plane/UI-tier fixes for
# the ":443 Unable to connect" and "UI takes 15s" symptoms. Two further levers
# from the source script (disable-le, logging) are OPT-IN and DISRUPTIVE
# (logging restarts the tenant-manager cell) and are deliberately NOT part of
# this section's default remediate pass - see --storm-disable-le/--storm-logging.
STORM_NAMESPACE = "vmsp-platform"
STORM_PRELUDE_NAMESPACE = "prelude"
STORM_CAPI_DEPLOYS = ("capi-controller-manager", "capi-ipam-in-cluster-controller-manager",
                     "capi-kubeadm-bootstrap-controller-manager",
                     "capi-kubeadm-control-plane-controller-manager", "capv-controller-manager")
STORM_PROBE_TOLERANCE_SEC = 90         # failureThreshold * periodSeconds must reach this
STORM_PROBE_MIN_TIMEOUT_SEC = 10
STORM_GATEWAY_LIVE_FT = 10             # liveness 10x10s = 100s tolerance
STORM_GATEWAY_READY_FT = 3             # readiness 3x5s = 15s tolerance
STORM_GATEWAY_MIN_TIMEOUT_SEC = 5
STORM_UI_DEPLOY_NAMES = ("nginx-httpd-app", "proxy-service", "health-status-app")
STORM_UI_CPU_REQUEST = "200m"
STORM_UI_MEM_REQUEST = "64Mi"
STORM_LE_ARG_DEPLOYS = ("account-manager-server", "authentication-server",
                        "dataprotection-server")
STORM_LOGBACK_CM = "tenant-manager-logback"
STORM_CELL_STATEFULSET = "tenant-manager"


def _storm_is_ui_deploy(name):
    return name in STORM_UI_DEPLOY_NAMES or name.endswith("-ui-app")


def _storm_discover_rt(r, name_filter, exclude=()):
    rc, out = r.read(f"kubectl get releasetemplate -n {STORM_NAMESPACE} -o name 2>/dev/null", 30)
    for line in (out or "").splitlines():
        name = line.strip().split("/", 1)[-1]
        if name_filter.lower() in name.lower() and not any(x in name for x in exclude):
            return name
    return ""


def _storm_scale_to_one(r, ns, name, cl, deploy_data=None):
    if deploy_data is not None:
        reps = str(deploy_data.get("spec", {}).get("replicas", ""))
    else:
        rc, reps = r.read(f"kubectl get deploy {name} -n {ns} -o jsonpath='{{.spec.replicas}}' 2>/dev/null", 30)
        reps = _sizing_last(reps)
    label = f"{ns}/{name}: replicas == 1"
    if not reps.isdigit():
        return warn("storm.footprint", label, "not found", cluster=cl)
    if reps == "1":
        return ok("storm.footprint", label, "already 1", cluster=cl)
    res = fail("storm.footprint", label, f"currently {reps}", cluster=cl)
    if may_act(r, "storm"):
        r.write(f"kubectl scale deploy {name} -n {ns} --replicas=1",
                f"storm footprint: {ns}/{name} replicas {reps} -> 1",
                tier="transient", timeout=60)
        res.action = "replicas -> 1"
        if not r.dry_run:
            res.state, res.detail = "warn", f"was {reps}; scaled to 1"
    return res


def _storm_capi_le_false(r, dep, cl, deploy_data=None):
    if deploy_data is not None:
        d = deploy_data
    else:
        d = r.read_json(f"kubectl get deploy {dep} -n {STORM_NAMESPACE} -o json 2>/dev/null", 30)
    label = f"{STORM_NAMESPACE}/{dep}: --leader-elect=false"
    if not d:
        return warn("storm.footprint", label, "not found", cluster=cl)
    replicas = d.get("spec", {}).get("replicas")
    if replicas not in (1, None):
        return warn("storm.footprint", label,
                    f"replicas={replicas} (>1) — leaving LE on", cluster=cl)
    args = []
    for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        args.extend(c.get("args") or [])
    if "--leader-elect=false" in args:
        return ok("storm.footprint", label, cluster=cl)
    if not any(a in ("--leader-elect", "--leader-elect=true") for a in args):
        return warn("storm.footprint", label, "no --leader-elect arg on this build", cluster=cl)
    res = fail("storm.footprint", label, "still --leader-elect=true", cluster=cl)
    if may_act(r, "storm"):
        r.write(
            "python3 - <<'PY'\n"
            "import subprocess, json\n"
            "KC=['kubectl']\n"
            f"r=subprocess.run(KC+['get','deploy','{dep}','-n','{STORM_NAMESPACE}','-o',"
            "'json'],capture_output=True,text=True)\n"
            "d=json.loads(r.stdout)\n"
            "for ci,c in enumerate(d['spec']['template']['spec']['containers']):\n"
            "    for ai,a in enumerate(c.get('args') or []):\n"
            "        if a in ('--leader-elect','--leader-elect=true'):\n"
            "            p=[{'op':'replace','path':'/spec/template/spec/containers/"
            "%d/args/%d'%(ci,ai),'value':'--leader-elect=false'}]\n"
            f"            subprocess.run(KC+['patch','deploy','{dep}','-n',"
            f"'{STORM_NAMESPACE}','--type=json','-p',json.dumps(p)])\n"
            "PY\n",
            f"storm footprint: --leader-elect=false on {STORM_NAMESPACE}/{dep} "
            "(replicas=1, clusterctl-managed so this sticks)",
            tier="transient", timeout=60)
        res.action = "--leader-elect=false"
        if not r.dry_run:
            res.state, res.detail = "warn", "was --leader-elect=true; disabled"
    return res


def chk_storm(r, ctx):
    """VCFA CPU-storm mitigation [KB 322724, KB 439264]. See the module comment above this section."""
    out = []
    cl = r.cluster

    # --- footprint: CAPI/CAPV(5) + coredns 2->1, LE-false on the CAPI/CAPV set ---
    storm_deploys_json = r.read_json(f"kubectl get deploy -n {STORM_NAMESPACE} -o json 2>/dev/null", 60) or {}
    storm_deploy_map = {item["metadata"]["name"]: item for item in storm_deploys_json.get("items", [])}

    for dep in STORM_CAPI_DEPLOYS:
        dep_data = storm_deploy_map.get(dep)
        out.append(_storm_scale_to_one(r, STORM_NAMESPACE, dep, cl, deploy_data=dep_data))
        out.append(_storm_capi_le_false(r, dep, cl, deploy_data=dep_data))
    out.append(_storm_scale_to_one(r, "kube-system", "coredns", cl))

    kyverno_rt = _storm_discover_rt(r, "kyverno-", exclude=("policies",))
    if not kyverno_rt:
        out.append(warn("storm.footprint", "kyverno cleanup ReleaseTemplate discoverable",
                        "not found", cluster=cl))
    else:
        rc, resync = r.read(
            f"kubectl get releasetemplate {kyverno_rt} -n {STORM_NAMESPACE} -o jsonpath="
            "'{.spec.helm.values.cleanupController.resyncPeriod}' 2>/dev/null", 30)
        resync = _sizing_last(resync)
        label = f"{kyverno_rt}: cleanupController.resyncPeriod == 1h"
        if resync == "1h":
            out.append(ok("storm.footprint", label, cluster=cl))
        else:
            res = fail("storm.footprint", label, f"currently {resync or 'unset'}", cluster=cl)
            if may_act(r, "storm"):
                r.write(
                    f"kubectl patch releasetemplate {kyverno_rt} -n {STORM_NAMESPACE} "
                    "--type=merge -p "
                    '\'{"spec":{"helm":{"values":{"cleanupController":{"resyncPeriod":"1h"}}}}}\'',
                    f"storm footprint: {kyverno_rt} cleanupController.resyncPeriod -> 1h",
                    tier="persistent", timeout=60)
                res.action = "resyncPeriod -> 1h"
                if not r.dry_run:
                    res.state, res.detail = "warn", f"was {resync or 'unset'}; set to 1h"
            out.append(res)

    # --- prelude probe-tolerance relax: raise-only, skip operator-owned Deployments ---
    prelude = r.read_json(f"kubectl get deploy -n {STORM_PRELUDE_NAMESPACE} -o json 2>/dev/null", 60) or {}
    for item in prelude.get("items", []):
        name = item["metadata"]["name"]
        if item["metadata"].get("ownerReferences"):
            continue                       # operator-generated - never fight the operator
        containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if not containers:
            continue
        c = containers[0]
        lp = c.get("livenessProbe")
        if not lp:
            continue
        ft = lp.get("failureThreshold") or 3
        pe = lp.get("periodSeconds") or 10
        to = lp.get("timeoutSeconds") or 1
        need_ft = max(ft, -(-STORM_PROBE_TOLERANCE_SEC // max(pe, 1)))    # ceil division, raise-only
        need_to = max(to, STORM_PROBE_MIN_TIMEOUT_SEC)
        label = f"{STORM_PRELUDE_NAMESPACE}/{name}: liveness tolerance >= {STORM_PROBE_TOLERANCE_SEC}s"
        if need_ft == ft and need_to == to:
            out.append(ok("storm.probe", label, f"{ft * pe}s", cluster=cl))
            continue
        res = fail("storm.probe", label, f"currently {ft * pe}s (fT={ft} x period={pe}s), timeout={to}s",
                   cluster=cl)
        if may_act(r, "storm"):
            cpatch = {"name": c["name"],
                     "livenessProbe": {"failureThreshold": need_ft, "timeoutSeconds": need_to}}
            rp = c.get("readinessProbe")
            if rp:
                cpatch["readinessProbe"] = {
                    "failureThreshold": max(rp.get("failureThreshold") or 3, 3),
                    "timeoutSeconds": max(rp.get("timeoutSeconds") or 1, STORM_PROBE_MIN_TIMEOUT_SEC)}
            body = json.dumps({"spec": {"template": {"spec": {"containers": [cpatch]}}}})
            r.write(
                f"kubectl patch deploy {name} -n {STORM_PRELUDE_NAMESPACE} "
                f"--type=strategic -p '{body}'",
                f"storm probe-relax: {name} tolerance {ft * pe}s -> {need_ft * pe}s",
                tier="transient", timeout=60)
            res.action = f"tolerance -> {need_ft * pe}s"
            if not r.dry_run:
                res.state, res.detail = "warn", f"was {ft * pe}s; raised to {need_ft * pe}s"
        out.append(res)

    # --- kube-vip static-manifest lease VALIDITY guard (file-only, no VM replace) ---
    # Shared with chk_cp's own copy of this same guard (which now also runs it
    # for vsp) - a single implementation so the two can't drift apart.
    out.append(_kubevip_lease_guard(r, cl, "storm"))

    # --- SERVICE kube-vip: preserve VIP on lease-loss + relax lease (via RT) ---
    kubevip_rt = _storm_discover_rt(r, "kube-vip")
    if not kubevip_rt:
        out.append(warn("storm.harden_vip", "kube-vip service ReleaseTemplate discoverable",
                        "not found", cluster=cl))
    else:
        rc, preserve = r.read(
            f"kubectl get releasetemplate {kubevip_rt} -n {STORM_NAMESPACE} -o jsonpath="
            "'{.spec.helm.values.env.vip_preserve_on_leadership_loss}' 2>/dev/null", 30)
        preserve = _sizing_last(preserve)
        label = f"{kubevip_rt}: vip_preserve_on_leadership_loss == true"
        if preserve == "true":
            out.append(ok("storm.harden_vip", label, cluster=cl))
        else:
            res = fail("storm.harden_vip", label, f"currently '{preserve or 'unset'}'", cluster=cl)
            if may_act(r, "storm"):
                r.write(
                    f"kubectl patch releasetemplate {kubevip_rt} -n {STORM_NAMESPACE} "
                    "--type=merge -p "
                    '\'{"spec":{"helm":{"values":{"env":{"vip_preserve_on_leadership_loss":'
                    '"true","vip_leaseduration":"60","vip_renewdeadline":"40",'
                    '"vip_retryperiod":"6"}}}}}\'',
                    f"storm harden-vip: {kubevip_rt} preserve=true, lease 60/40/6",
                    tier="transient", timeout=60)
                res.action = "preserve=true, lease 60/40/6"
                if not r.dry_run:
                    res.state, res.detail = "warn", f"was '{preserve or 'unset'}'; patched"
            out.append(res)

    # --- DATA-PLANE Envoy proxy: probe tolerance + shutdown-manager /tmp mount ---
    rc, cr_names = r.read(f"kubectl get envoyproxy -n {STORM_NAMESPACE} -o name 2>/dev/null", 30)
    for cr_line in (cr_names or "").splitlines():
        cr = cr_line.strip().split("/", 1)[-1]
        if not cr:
            continue
        cr_obj = r.read_json(f"kubectl get envoyproxy {cr} -n {STORM_NAMESPACE} -o json 2>/dev/null", 30)
        label = f"envoyproxy/{cr}: probe tolerance + shutdown-manager /tmp mount"
        if not cr_obj:
            out.append(warn("storm.gateway", label, "CR not readable", cluster=cl))
            continue
        ed = ((cr_obj.get("spec") or {}).get("provider") or {}).get("kubernetes", {}).get("envoyDeployment")
        if ed is None:
            out.append(warn("storm.gateway", label, "no envoyDeployment stanza", cluster=cl))
            continue
        patch = ed.setdefault("patch", {"type": "StrategicMerge", "value": {}})
        val = patch.setdefault("value", {})
        spec = val.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        cs = spec.get("containers")
        if not cs:
            cs = [{"name": "envoy"}, {"name": "shutdown-manager"}]
            spec["containers"] = cs
        changed = []
        for c in cs:
            nm = c.get("name")
            if nm not in ("envoy", "shutdown-manager"):
                continue
            for pk, ft in (("livenessProbe", STORM_GATEWAY_LIVE_FT),
                          ("readinessProbe", STORM_GATEWAY_READY_FT),
                          ("startupProbe", None)):
                p = c.setdefault(pk, {})
                if ft is not None and (p.get("failureThreshold") or 0) < ft:
                    p["failureThreshold"] = ft
                    changed.append(f"{nm}.{pk}.failureThreshold={ft}")
                if (p.get("timeoutSeconds") or 0) < STORM_GATEWAY_MIN_TIMEOUT_SEC:
                    p["timeoutSeconds"] = STORM_GATEWAY_MIN_TIMEOUT_SEC
                    changed.append(f"{nm}.{pk}.timeoutSeconds={STORM_GATEWAY_MIN_TIMEOUT_SEC}")
            if nm == "shutdown-manager":
                vms = c.setdefault("volumeMounts", [])
                if not any(m.get("mountPath") == "/tmp" for m in vms):
                    vms.append({"name": "shutdown-manager", "mountPath": "/tmp"})
                    changed.append(f"{nm}.volumeMounts+=/tmp")
        if not changed:
            out.append(ok("storm.gateway", label, "already at target", cluster=cl))
            continue
        res = fail("storm.gateway", label, "; ".join(changed) + " needed", cluster=cl)
        if may_act(r, "storm"):
            body = json.dumps({"spec": {"provider": {"kubernetes": {"envoyDeployment": {"patch": patch}}}}})
            r.write(f"kubectl patch envoyproxy {cr} -n {STORM_NAMESPACE} --type=merge -p '{body}'",
                    f"storm harden-gateway: {cr} -> {'; '.join(changed)}",
                    tier="transient", timeout=60)
            res.action = "; ".join(changed)
            if not r.dry_run:
                res.state, res.detail = "warn", "; ".join(changed) + " applied"
        out.append(res)

    # --- USER-FACING UI TIER: lift out of BestEffort QoS ---
    ui_deploys = r.read_json(f"kubectl get deploy -n {STORM_PRELUDE_NAMESPACE} -o json 2>/dev/null", 60) or {}
    for item in ui_deploys.get("items", []):
        name = item["metadata"]["name"]
        if not _storm_is_ui_deploy(name):
            continue
        containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if not containers:
            continue
        c0 = containers[0]
        cur_cpu = (c0.get("resources", {}).get("requests", {}) or {}).get("cpu")
        label = f"{STORM_PRELUDE_NAMESPACE}/{name}: out of BestEffort QoS (has a CPU request)"
        if cur_cpu:
            out.append(ok("storm.uitier", label, f"cpu request={cur_cpu}", cluster=cl))
            continue
        res = fail("storm.uitier", label, "no resources.requests — BestEffort "
                                          "(cpu.weight 1); ~15s stalls under storm", cluster=cl)
        if may_act(r, "storm"):
            body = json.dumps({"spec": {"template": {"spec": {"containers": [
                {"name": c0["name"],
                 "resources": {"requests": {"cpu": STORM_UI_CPU_REQUEST,
                                            "memory": STORM_UI_MEM_REQUEST}}}]}}}})
            r.write(
                f"kubectl patch deploy {name} -n {STORM_PRELUDE_NAMESPACE} "
                f"--type=strategic -p '{body}'",
                f"storm uitier: {name} BestEffort -> requests cpu={STORM_UI_CPU_REQUEST} "
                f"mem={STORM_UI_MEM_REQUEST} (no limits)", tier="transient", timeout=60)
            res.action = f"requests cpu={STORM_UI_CPU_REQUEST} mem={STORM_UI_MEM_REQUEST}"
            if not r.dry_run:
                res.state, res.detail = "warn", "was BestEffort; requests added"
        out.append(res)

    # --- opt-in, disruptive levers: only when explicitly requested ---
    if ctx.get("storm_disable_le") and may_act(r, "storm"):
        for dep in STORM_LE_ARG_DEPLOYS:
            d = r.read_json(f"kubectl get deploy {dep} -n {STORM_PRELUDE_NAMESPACE} -o json 2>/dev/null", 30)
            label = f"{STORM_PRELUDE_NAMESPACE}/{dep}: --enable-leader-election=false (opt-in)"
            if not d or d.get("spec", {}).get("replicas") not in (1, None):
                out.append(warn("storm.disable_le", label,
                                "not found or replicas != 1 (unsafe)", cluster=cl))
                continue
            args = [a for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    for a in (c.get("args") or [])]
            if "--enable-leader-election=false" in args:
                out.append(ok("storm.disable_le", label, cluster=cl))
                continue
            res = fail("storm.disable_le", label, "still enabled", cluster=cl)
            r.write(
                "python3 - <<'PY'\n"
                "import subprocess, json\n"
                "KC=['kubectl']\n"
                f"r=subprocess.run(KC+['get','deploy','{dep}','-n','{STORM_PRELUDE_NAMESPACE}',"
                "'-o','json'],capture_output=True,text=True)\n"
                "d=json.loads(r.stdout)\n"
                "for ci,c in enumerate(d['spec']['template']['spec']['containers']):\n"
                "    for ai,a in enumerate(c.get('args') or []):\n"
                "        if a in ('--enable-leader-election','--enable-leader-election=true'):\n"
                "            p=[{'op':'replace','path':'/spec/template/spec/containers/"
                "%d/args/%d'%(ci,ai),'value':'--enable-leader-election=false'}]\n"
                f"            subprocess.run(KC+['patch','deploy','{dep}','-n',"
                f"'{STORM_PRELUDE_NAMESPACE}','--type=json','-p',json.dumps(p)])\n"
                "PY\n",
                f"storm opt-in disable-le: {dep} --enable-leader-election=false",
                tier="transient", timeout=60)
            res.action = "--enable-leader-election=false"
            if not r.dry_run:
                res.state, res.detail = "warn", "was enabled; disabled (EXPERIMENTAL)"
            out.append(res)

    if ctx.get("storm_logging") and may_act(r, "storm"):
        rc, before = r.read(
            f"kubectl get cm {STORM_LOGBACK_CM} -n {STORM_PRELUDE_NAMESPACE} -o jsonpath="
            "'{.data.logback\\.xml}' 2>/dev/null | grep -c 'level=\"DEBUG\"\\|level=\"TRACE\"'", 30)
        before = _sizing_last(before) or "0"
        label = f"{STORM_PRELUDE_NAMESPACE}/{STORM_LOGBACK_CM}: DEBUG/TRACE loggers == 0 (opt-in, DISRUPTIVE)"
        if before == "0":
            out.append(ok("storm.logging", label, cluster=cl))
        else:
            res = fail("storm.logging", label, f"{before} DEBUG/TRACE loggers", cluster=cl)
            r.write(
                "python3 - <<'PY'\n"
                "import subprocess, json\n"
                "KC=['kubectl']\n"
                f"d=json.loads(subprocess.run(KC+['-n','{STORM_PRELUDE_NAMESPACE}','get','cm',"
                f"'{STORM_LOGBACK_CM}','-o','json'],capture_output=True,text=True).stdout)\n"
                "lb=d['data']['logback.xml'].replace('level=\"DEBUG\"','level=\"INFO\"')."
                "replace('level=\"TRACE\"','level=\"INFO\"')\n"
                "p=[{'op':'replace','path':'/data/logback.xml','value':lb}]\n"
                f"subprocess.run(KC+['-n','{STORM_PRELUDE_NAMESPACE}','patch','cm',"
                f"'{STORM_LOGBACK_CM}','--type=json','-p',json.dumps(p)])\n"
                "PY\n",
                f"storm opt-in logging: {STORM_LOGBACK_CM} DEBUG/TRACE -> INFO", tier="transient", timeout=60)
            r.write(
                f"kubectl -n {STORM_PRELUDE_NAMESPACE} rollout restart statefulset "
                f"{STORM_CELL_STATEFULSET}",
                f"storm opt-in logging: restart {STORM_CELL_STATEFULSET} cell to apply "
                "(DISRUPTIVE)", tier="transient", timeout=90)
            res.action = "DEBUG/TRACE -> INFO; cell restarted"
            if not r.dry_run:
                res.state, res.detail = "warn", f"was {before} DEBUG/TRACE; reset and cell restarted"
            out.append(res)

    return out


def _fix_sds_sni(r, cl):
    """Envoy Gateway v1.5 / Envoy v1.34 SDS SAN-without-CA NACK fix [KB 439264, KB 424402].
    Ensures platform-trust ConfigMap exists in every BackendTLSPolicy namespace
    and applies Kyverno ClusterPolicy vcfa-btp-wellknown-to-carefs. Batched execution.
    """
    out = []
    batch_check = (
        "NAMESPACES=$(kubectl get backendtlspolicy -A -o jsonpath='{range .items[*]}{.metadata.namespace}{\"\\n\"}{end}' 2>/dev/null | sort -u | grep -v '^vmsp-platform$'); "
        "for ns in $NAMESPACES; do "
        "  if [ -n \"$ns\" ]; then "
        "    kubectl get configmap platform-trust -n $ns >/dev/null 2>&1 || "
        "    (kubectl get configmap platform-trust -n vmsp-platform -o yaml | "
        "     sed \"s/namespace: vmsp-platform/namespace: $ns/\" | "
        "     sed '/uid:/d; /resourceVersion:/d; /creationTimestamp:/d; /ownerReferences:/,/^[^ ]/d' | "
        "     kubectl apply -f - && echo \"COPIED:$ns\"); "
        "  fi; "
        "done; "
        "kubectl get clusterpolicy vcfa-btp-wellknown-to-carefs >/dev/null 2>&1 || echo \"NEED_POLICY\""
    )
    rc, raw = r.read(batch_check, 45)
    raw_str = raw or ""
    for line in raw_str.splitlines():
        if line.startswith("COPIED:"):
            ns = line.split(":", 1)[1].strip()
            out.append(ok("sds_sni.cm", f"{ns}/platform-trust ConfigMap copied", cluster=cl))

    if "NEED_POLICY" in raw_str or rc != 0:
        kyverno_yaml = (
            "apiVersion: kyverno.io/v1\n"
            "kind: ClusterPolicy\n"
            "metadata:\n"
            "  name: vcfa-btp-wellknown-to-carefs\n"
            "spec:\n"
            "  rules:\n"
            "  - name: mutate-btp-wellknown\n"
            "    match:\n"
            "      any:\n"
            "      - resources:\n"
            "          kinds:\n"
            "          - gateway.networking.k8s.io/v1alpha3/BackendTLSPolicy\n"
            "          - gateway.networking.k8s.io/v1alpha2/BackendTLSPolicy\n"
            "    mutate:\n"
            "      patchStrategicMerge:\n"
            "        spec:\n"
            "          validation:\n"
            "            caCertificateRefs:\n"
            "            - group: \"\"\n"
            "              kind: ConfigMap\n"
            "              name: platform-trust\n"
            "            (wellKnownCACertificates): null\n"
        )
        r.write(f"cat << 'EOF' | kubectl apply -f -\n{kyverno_yaml}EOF\n",
                "apply Kyverno ClusterPolicy vcfa-btp-wellknown-to-carefs for SDS SAN NACK fix",
                tier="persistent", timeout=60)
        out.append(ok("sds_sni.policy", "vcfa-btp-wellknown-to-carefs ClusterPolicy applied", cluster=cl))

    return out


def _cpu_tune_apply(r, cl):
    """Batched CPU tuning application across Prometheus, FluentBit, Kyverno, and Provisioning [KB 417831]."""
    out = []
    batch_cmd = (
        "kubectl patch prometheus k8s -n vmsp-platform --type merge -p '{\"spec\":{\"scrapeInterval\":\"60s\",\"evaluationInterval\":\"60s\",\"retentionSize\":\"4GiB\"}}' && "
        "kubectl patch fluentbitagent fluent-bit-agent -n vmsp-platform --type merge -p '{\"spec\":{\"flush\":10,\"metrics\":{\"interval\":\"120s\"}}}' && "
        "kubectl scale deploy kyverno-admission-controller -n vmsp-policies --replicas=1 && "
        "(kubectl patch deploy provisioning-service-app -n prelude --type json -p '[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/env/-\",\"value\":{\"name\":\"JAVA_TOOL_OPTIONS\",\"value\":\"-Dmanagement.prometheus.metrics.export.exemplars.enabled=false\"}}]' 2>/dev/null || true)"
    )
    r.write(batch_cmd, "cpu-tune (batched): prometheus, fluentbit, kyverno, provisioning-service", tier="persistent", timeout=90)
    out.append(ok("cpu_tune", "CPU tuning applied across Prometheus, FluentBit, Kyverno, Provisioning", cluster=cl))
    return out


def _cpu_tune_rollback(r, cl):
    """Batched CPU tuning rollback across Prometheus, FluentBit, and Kyverno [KB 417831]."""
    out = []
    batch_cmd = (
        "kubectl patch prometheus k8s -n vmsp-platform --type merge -p '{\"spec\":{\"scrapeInterval\":\"30s\",\"evaluationInterval\":\"30s\",\"retentionSize\":\"8GiB\"}}' && "
        "kubectl patch fluentbitagent fluent-bit-agent -n vmsp-platform --type merge -p '{\"spec\":{\"flush\":5,\"metrics\":{\"interval\":\"60s\"}}}' && "
        "kubectl scale deploy kyverno-admission-controller -n vmsp-policies --replicas=3"
    )
    r.write(batch_cmd, "rollback cpu-tune (batched): prometheus, fluentbit, kyverno", tier="persistent", timeout=90)
    out.append(ok("cpu_tune", "CPU tuning rolled back to default settings", cluster=cl))
    return out


def _recover_gateway_503(r, cl):
    """Gateway 503 recovery via batched SDS NACK fix and atomic rollout restarts [KB 439264, KB 424402]."""
    out = []
    out.extend(_fix_sds_sni(r, cl))
    batch_restart = (
        "kubectl rollout restart deployment/envoy-gateway -n vmsp-platform && "
        "kubectl rollout restart deployment/vmsp-gateway -n vmsp-platform && "
        "kubectl rollout restart deployment/vcfa-gateway -n vmsp-platform && "
        "kubectl rollout restart deployment/encryption-manager -n prelude && "
        "kubectl rollout restart deployment/intent-server -n prelude && "
        "kubectl rollout restart deployment/vcfa-service-manager -n prelude"
    )
    r.write(batch_restart, "recover gateway 503 (batched): rollout restart 6 gateway deployments", tier="transient", timeout=90)
    out.append(ok("recover_gateway_503", "gateway 503 recovery: SDS NACK fix applied + gateway deployments restarted", cluster=cl))
    return out


WCP_SERVICES = ("vapi-endpoint", "trustmanagement", "wcp")


def chk_services(r, ctx):
    """vCenter-side WCP services that must be STARTED for the Supervisor to work [KB 314495, KB 343810].

    vmon's startup data can go missing, leaving a service with
    Starttype: AUTOMATIC that never actually starts. vapi-endpoint being down is
    what makes the CSI controller fail to log in to vCenter, which then stalls
    volume attachment - a long causal chain from a service nobody looked at.

    Batched into a single remote loop query on vCenter.
    """
    out = []
    cl = r.cluster
    vc = ctx.get("vcenter")
    if not vc:
        return []

    svcs_str = " ".join(WCP_SERVICES)
    batch_cmd = (
        f"for s in {svcs_str}; do "
        f"st=$(vmon-cli -s $s 2>/dev/null | grep RunState | sed 's/.*RunState: //' | head -1); "
        f"echo \"$s:$st\"; done"
    )
    rc, raw_res = r.read_on_vcenter(batch_cmd, 60)
    states = {}
    if raw_res:
        for line in raw_res.splitlines():
            if ":" in line:
                s_name, s_state = line.split(":", 1)
                states[s_name.strip()] = s_state.strip()

    for svc in WCP_SERVICES:
        state = states.get(svc, "")
        label = f"{vc}: {svc} STARTED"
        if state == "STARTED":
            out.append(ok("services", label, cluster=cl))
            continue
        res_row = fail("services", label, f"is '{state or 'unknown'}'", cluster=cl)
        if may_act(r, "services"):
            r.write_on_vcenter(f"vmon-cli -i {svc}",
                               f"start {svc} on {vc} via vmon-cli",
                               tier="transient", timeout=120)
            res_row.action = "vmon-cli -i issued"
            if not r.dry_run:
                res_row.state = "warn"
                res_row.detail = f"was '{state}'; start issued"
        out.append(res_row)
    return out


# ─── Content Library Trust & Sync (govc, manager-local; ported from supervisor_stabilizer.py Phase 1) ─
GOVC_DOWNLOAD_URL = (
    "https://github.com/vmware/govmomi/releases/download/"
    "v0.37.1/govc_Linux_x86_64.tar.gz"
)


def ensure_govc():
    """Verify govc binary exists on PATH or standard paths; auto-install to ~/.local/bin if missing."""
    found = shutil.which("govc")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.local/bin/govc"),
        "/home/holuser/govc",
        "/home/holuser/.local/bin/govc",
        "/usr/local/bin/govc",
    ]
    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            bin_dir = os.path.dirname(cand)
            if bin_dir not in os.environ.get("PATH", "").split(":"):
                os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
            return cand

    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    archive = os.path.join(bin_dir, "govc.tgz")
    try:
        rc = subprocess.run(
            f"curl -fsSL -o {archive} {GOVC_DOWNLOAD_URL}",
            shell=True, capture_output=True, timeout=60,
        ).returncode
        if rc != 0:
            return None
        rc = subprocess.run(
            f"tar -xzf {archive} -C {bin_dir} govc",
            shell=True, capture_output=True, timeout=30,
        ).returncode
        if rc != 0:
            return None
        target = os.path.join(bin_dir, "govc")
        os.chmod(target, 0o755)
        if os.path.exists(archive):
            os.unlink(archive)
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        return target if os.path.isfile(target) and os.access(target, os.X_OK) else None
    except Exception:
        return None


def _run_govc(args, env_overrides=None, input_data=None, timeout=60):
    """Run govc subcommand with explicit env overrides. Returns (returncode, output)."""
    govc_path = ensure_govc() or "govc"
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    try:
        proc = subprocess.run(
            [govc_path] + args,
            env=env,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, f"<govc {' '.join(args)} timed out after {timeout}s: {exc}>"
    except Exception as exc:
        return 1, f"<govc execution error: {exc}>"


def _is_upstream_reachable(host, port=443, timeout=3):
    """Return True if host:port accepts TCP connection within timeout seconds."""
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def _fetch_upstream_cert(domain, port=443, timeout=15):
    """Fetch live upstream TLS certificate and return (pem_str, sha1_thumbprint)."""
    if not _is_upstream_reachable(domain, port=port, timeout=min(timeout, 3)):
        return None, None
    try:
        fetch = subprocess.run(
            f"echo | openssl s_client -showcerts -servername {domain} "
            f"-connect {domain}:{port} 2>/dev/null "
            f"| openssl x509 -outform PEM",
            shell=True, capture_output=True, text=True, timeout=timeout,
        )
        pem = (fetch.stdout or "").strip()
        if not pem.startswith("-----BEGIN CERTIFICATE-----"):
            return None, None
        fp = subprocess.run(
            "openssl x509 -noout -fingerprint -sha1",
            shell=True, input=pem, capture_output=True, text=True, timeout=10,
        )
        if fp.returncode != 0:
            return None, None
        m = re.search(r"=\s*([0-9A-Fa-f:]+)", fp.stdout)
        if not m:
            return None, None
        return pem, m.group(1).upper()
    except Exception:
        return None, None


def _get_vcenter_configs_with_users(path="/tmp/config.ini"):
    """Return list of dicts: [{'host': fqdn, 'sso_user': user, 'label': label}, ...]."""
    vcenters = []
    try:
        if os.path.exists(path):
            config = configparser.ConfigParser(allow_no_value=True)
            config.optionxform = str
            config.read(path)
            if config.has_section('RESOURCES') and config.has_option('RESOURCES', 'vCenters'):
                for raw_entry in config.get('RESOURCES', 'vCenters').split('\n'):
                    entry = raw_entry.strip()
                    if not entry or entry.startswith('#'):
                        continue
                    parts = entry.split(':')
                    if len(parts) >= 3:
                        h = parts[0].strip()
                        u = parts[2].strip()
                        if h and "." in h and not any(v["host"] == h for v in vcenters):
                            vcenters.append({"host": h, "sso_user": u, "label": h})
    except Exception:
        pass

    if not vcenters:
        vcenters = [
            {"host": "vc-wld01-a.site-a.vcf.lab", "sso_user": "administrator@wld.sso", "label": "vc-wld01-a.site-a.vcf.lab"},
            {"host": "vc-mgmt-a.site-a.vcf.lab", "sso_user": "administrator@vsphere.local", "label": "vc-mgmt-a.site-a.vcf.lab"},
        ]
    return vcenters


def _list_subscribed_libraries(env, target_domain=None):
    """Query govc library.ls and return list of subscribed libraries."""
    rc, raw = _run_govc(["library.ls", "-json"], env, timeout=60)
    if rc != 0 or not raw or raw.strip() == "null":
        return []
    try:
        libs = json.loads(raw.strip())
    except Exception:
        return []
    if not isinstance(libs, list):
        libs = [libs] if isinstance(libs, dict) else []

    matches = []
    for lib in libs:
        if (lib.get("type") or "").upper() != "SUBSCRIBED":
            continue
        sub = lib.get("subscription") or lib.get("subscription_info") or {}
        url = sub.get("subscriptionUrl") or sub.get("subscription_url") or ""
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if target_domain and host != target_domain.lower():
            continue
        thumb = (sub.get("sslThumbprint") or sub.get("ssl_thumbprint") or "").upper()
        matches.append({
            "id": lib.get("id"),
            "name": lib.get("name", "<unnamed>"),
            "url": url,
            "host": host,
            "thumbprint": thumb,
        })
    return matches


def chk_contentlib(r, ctx):
    """vCenter Subscribed Content Library trust store and synchronization [supervisor_stabilizer.py Phase 1].

    Ensures that external/subscribed Content Libraries have their upstream certificates
    trusted in the vCenter Content Library trust store, updates pinned SSL thumbprints,
    and triggers content synchronization.

    Deferral Guard: Proactively validates upstream depot reachability (e.g.
    fleet-01a.site-a.vcf.lab on port 443) before issuing govc library.sync. If the
    depot VM is not yet online during early boot, synchronization is deferred safely
    to avoid 400 Bad Request (connection_to_vcsp_server_failed) errors and wasted delay.
    """
    out = []
    cl = r.cluster
    password = get_password()
    target_domain = ctx.get("target_domain")

    govc_bin = ensure_govc()
    if not govc_bin:
        return [warn("contentlib.govc", "govc CLI available on manager",
                     "govc not found and auto-install failed — skipping Content Library sync",
                     cluster=cl)]

    vcenters = _get_vcenter_configs_with_users()
    for vc in vcenters:
        vc_host = vc["host"]
        sso_user = vc["sso_user"]
        env = {
            "GOVC_URL": f"https://{vc_host}",
            "GOVC_USERNAME": sso_user,
            "GOVC_PASSWORD": password,
            "GOVC_INSECURE": "true",
        }

        rc, about_out = _run_govc(["about"], env, timeout=20)
        if rc != 0:
            out.append(warn("contentlib.reach", f"{vc_host}: vCenter API reachability",
                            f"govc about failed: {about_out.strip()}", cluster=cl))
            continue

        libs = _list_subscribed_libraries(env, target_domain=target_domain)
        if not libs:
            out.append(ok("contentlib.subscribed", f"{vc_host}: subscribed libraries",
                          "no subscribed libraries to sync (normal for management vCenter)", cluster=cl))
            continue

        for lib in libs:
            lib_id = lib["id"]
            lib_name = lib["name"]
            depot_host = lib["host"]
            current_thumb = lib["thumbprint"]
            label = f"{vc_host}: '{lib_name}' ({depot_host})"

            if not depot_host:
                out.append(warn("contentlib.url", label, "subscription URL missing hostname", cluster=cl))
                continue

            reachable = _is_upstream_reachable(depot_host, port=443, timeout=3)
            if not reachable:
                out.append(warn("contentlib.defer", label,
                                f"upstream depot '{depot_host}' not reachable on port 443 (depot VM starting up) — sync deferred",
                                cluster=cl))
                continue

            pem, live_thumb = _fetch_upstream_cert(depot_host)
            if not pem or not live_thumb:
                out.append(warn("contentlib.cert", label,
                                f"could not retrieve TLS certificate from {depot_host}:443",
                                cluster=cl))
                continue

            if may_act(r, "contentlib"):
                if r.dry_run:
                    res_sync = ok("contentlib.sync", label,
                                  f"[dry-run] would trust cert ({live_thumb[:11]}...) and trigger library.sync",
                                  cluster=cl)
                    res_sync.action = "dry-run"
                    out.append(res_sync)
                else:
                    # 1. Add certificate to global trust store (idempotent)
                    rc_trust, trust_out = _run_govc(["library.trust.create", "-"], env, input_data=pem)
                    # 2. Update thumbprint if library was pinning one
                    if current_thumb and live_thumb and current_thumb != live_thumb:
                        _run_govc(["library.update", "-thumbprint", live_thumb, lib_id], env)
                    # 3. Trigger sync
                    rc_sync, sync_out = _run_govc(["library.sync", lib_id], env, timeout=180)
                    if rc_sync == 0:
                        res_sync = ok("contentlib.sync", label,
                                      f"cert trusted ({live_thumb[:11]}...) & sync triggered",
                                      cluster=cl)
                        res_sync.action = "sync triggered"
                        out.append(res_sync)
                    else:
                        msg = (sync_out or "").strip()
                        if "500" in msg or "Internal Server Error" in msg or "400" in msg:
                            res_sync = ok("contentlib.sync", label,
                                          f"cert trusted; sync returned non-fatal code ({msg[:40]}) — vCenter will auto-sync",
                                          cluster=cl)
                            res_sync.action = "cert trusted"
                            out.append(res_sync)
                        else:
                            out.append(warn("contentlib.sync", label,
                                            f"sync returned {rc_sync}: {msg[:60]}",
                                            cluster=cl))
            else:
                out.append(ok("contentlib.status", label,
                              f"upstream reachable, thumbprint: {live_thumb or 'unknown'}",
                              cluster=cl))

    return out


def chk_webhooks(r, ctx):
    """Webhook caBundle vs the CA cert-manager is configured to inject.

    Failure mode (vcf-troubleshooting #59): when the CA behind a webhook's serving
    cert is regenerated, cert-manager reissues the cert but the webhook's caBundle
    can keep the OLD CA if cainjector has not reconciled. Every PVC and pod create
    then fails admission with 'x509: certificate signed by unknown authority'.

    The source of truth is the object's own
    `cert-manager.io/inject-ca-from: <ns>/<certificate>` annotation, resolved to
    that Certificate's secret. Determined empirically on 2026-08-14 - and this is
    where the legacy implementation goes wrong:

    supervisor_stabilizer.py:2330 selects webhooks whose NAME contains quota/cns
    and syncs them to `vmware-system-cert-manager/storage-quota-root-ca-secret`.
    On this Supervisor both quota webhooks are annotated
    `inject-ca-from: kube-system/storage-quota-serving-cert` - a DIFFERENT CA -
    and their caBundles are correct. An assumption-driven check flagged both as
    stale with zero corroborating admission errors, and "remediating" them would
    have overwritten a working caBundle with the wrong CA, causing the very outage
    the check exists to prevent. Compare against what the annotation names, or do
    not compare at all.

    Also checks EVERY webhook in the object: these carry three each, and looking
    only at webhooks[0] would miss a partially-stale object.
    """
    out = []
    cl = r.cluster
    rc, raw = r.read(
        "kubectl get validatingwebhookconfiguration -o json 2>/dev/null "
        "| python3 -c \""
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "[print('|'.join(["
        "  i['metadata']['name'],"
        "  (i['metadata'].get('annotations') or {}).get('cert-manager.io/inject-ca-from',''),"
        "  ','.join((w.get('clientConfig') or {}).get('caBundle','') for w in i.get('webhooks') or [])"
        "])) for i in d.get('items',[])]\"", 90)
    if rc != 0 or not raw.strip():
        return [warn("webhooks", "webhook caBundles: verifiable",
                     "could not enumerate ValidatingWebhookConfigurations",
                     cluster=cl)]

    ca_cache = {}
    checked = 0
    for line in raw.splitlines():
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, inject_from, bundles_csv = parts[0], parts[1].strip(), parts[2]
        if not inject_from or "/" not in inject_from:
            continue                       # not cainjector-managed; nothing to assert
        checked += 1
        ns, cert = inject_from.split("/", 1)

        if inject_from not in ca_cache:
            rc2, sec = r.read(
                f"secret=$(kubectl -n {ns} get certificate {cert} "
                f"-o jsonpath='{{.spec.secretName}}' 2>/dev/null); "
                f"[ -n \"$secret\" ] || secret={cert}; "
                f"kubectl -n {ns} get secret \"$secret\" "
                f"-o jsonpath='{{.data.ca\\.crt}}' 2>/dev/null", 60)
            val = (sec or "").strip().splitlines()
            ca_cache[inject_from] = val[-1].strip() if val else ""
        expected = ca_cache[inject_from]
        label = f"{name}: caBundle matches {inject_from}"

        if not expected:
            out.append(warn("webhooks", label,
                            f"could not read the CA from {inject_from}", cluster=cl))
            continue

        bundles = [b for b in bundles_csv.split(",") if b]
        stale = [i for i, b in enumerate(bundles) if b != expected]
        if not stale:
            out.append(ok("webhooks", label,
                          f"{len(bundles)} webhook(s) in sync", cluster=cl))
            continue

        res = fail("webhooks", label,
                   f"{len(stale)}/{len(bundles)} webhook(s) stale — PVC/pod creates "
                   f"will fail admission with 'x509: certificate signed by unknown "
                   f"authority'", cluster=cl)
        if may_act(r, "webhooks"):
            patch = ",".join(
                f'{{"op":"replace","path":"/webhooks/{i}/clientConfig/caBundle",'
                f'"value":"{expected}"}}' for i in stale)
            r.write(f"kubectl patch validatingwebhookconfiguration {name} "
                    f"--type=json -p '[{patch}]'",
                    f"sync {len(stale)} caBundle(s) on {name} to {inject_from}",
                    tier="persistent", timeout=90)
            r.write("kubectl -n vmware-system-cert-manager rollout restart "
                    "deploy cert-manager-cainjector",
                    "restart cert-manager-cainjector so it stops re-staling it",
                    tier="transient", timeout=90)
            res.action = "caBundle synced"
            if not r.dry_run:
                res.state = "warn"
                res.detail = f"{len(stale)} synced to {inject_from}"
        out.append(res)

    if not checked:
        return [ok("webhooks", "webhook caBundles: in sync",
                   "no cainjector-managed webhook configurations present",
                   cluster=cl)]
    return out


# ─── ESXi entropy source (govc, manager-local; ported from remediate-lab.sh:3022) ─
# One layer below any Kubernetes cluster - the only section that never runs
# kubectl at all. Config-only and NEVER reboots a host: sets the Configured
# value, which only takes effect on that host's NEXT reboot (not forced here).
# That makes this one of the very few "remediate"-class actions with none of
# the risk that keeps CP/worker VM power-cycling and node consolidation
# deliberately unported (see the sizing/footprint docstrings and
# vcf-lab-tuner.md's coverage audit) - there is no live-state impact until a
# human reboots a host.
GOVC_BIN = "/home/holuser/govc"
GOVC_ENV_FILE = "/home/holuser/.govc-vsp01a.env"
ENTROPY_TARGET = "2"   # RDRAND, vs. the slow RDSEED default on AMD Zen4/5


def _govc(cmd):
    return f". {GOVC_ENV_FILE} && {GOVC_BIN} {cmd}"


def chk_entropy(r, ctx):
    """AMD Zen4/5 (EPYC 9004/9005) esxcli entropySources RDRAND workaround.

    Does not self-stage a missing govc the way remediate-lab.sh does
    (downloading a binary from GitHub on demand) - if govc isn't already
    usable, this reports it and stops rather than silently fetching a binary
    from the internet as a side effect of a health check.
    """
    cl = r.cluster
    rc, probe = r.read_local(
        ["bash", "-c", f"[ -f {GOVC_ENV_FILE} ] && [ -x {GOVC_BIN} ] && "
                       f"{_govc('about')} 2>/dev/null | grep -m1 '^FullName'"], timeout=30)
    if rc != 0 or "FullName" not in (probe or ""):
        return [warn("entropy.govc", "govc usable on the manager",
                     f"{GOVC_ENV_FILE}/{GOVC_BIN} not usable — set up govc "
                     "manually (see remediate-lab.sh's stage_govc for the "
                     "download+env-file pattern this deliberately does not "
                     "automate)", cluster=cl)]

    rc, hosts_out = r.read_local(["bash", "-c", _govc("find / -type h")], timeout=60)
    hosts = [h.strip() for h in (hosts_out or "").splitlines() if h.strip()]
    if not hosts:
        return [ok("entropy.hosts", "ESXi hosts discovered via govc",
                   "none found — nothing to check", cluster=cl)]

    out = []
    for h in hosts:
        fqdn = h.rsplit("/", 1)[-1]
        rc, model = r.read_local(
            ["bash", "-c", _govc(f"host.info -host.dns='{fqdn}' 2>/dev/null "
                                 "| grep -m1 'Processor type:'")], timeout=30)
        model = (model or "").split(":", 1)[-1].strip()
        label = f"{fqdn}: entropySources == {ENTROPY_TARGET} (RDRAND)"
        if "intel" in model.lower():
            out.append(ok("entropy.host", label,
                          f"Intel ({model or 'unknown'}) — AMD Zen4/5-specific "
                          "fix, N/A", cluster=cl))
            continue
        rc, cur = r.read_local(
            ["bash", "-c", _govc(f"host.esxcli -host.dns='{fqdn}' system settings "
                                 "kernel list -o entropySources 2>/dev/null "
                                 "| awk 'NR==3{print $3}'")], timeout=30)
        cur = (cur or "").strip()
        if not cur:
            out.append(warn("entropy.host", label,
                            "could not read entropySources (host unreachable "
                            "via govc?)", cluster=cl))
            continue
        if cur == ENTROPY_TARGET:
            out.append(ok("entropy.host", label, f"Configured={cur}", cluster=cl))
            continue
        res = fail("entropy.host", label, f"Configured={cur}", cluster=cl)
        if may_act(r, "entropy"):
            r.local(["bash", "-c",
                    _govc(f"host.esxcli -host.dns='{fqdn}' system settings kernel "
                          f"set -s entropySources -v {ENTROPY_TARGET}")],
                    f"set entropySources={ENTROPY_TARGET} on {fqdn}", timeout=45)
            res.action = f"entropySources -> {ENTROPY_TARGET}"
            if not r.dry_run:
                res.state = "warn"
                res.detail = (f"was {cur}; set to {ENTROPY_TARGET} — REBOOT "
                             f"REQUIRED on {fqdn} before this takes effect "
                             "(not done by this check)")
        out.append(res)
    return out


HANDLERS = {
    "cp": chk_cp, "nodes": chk_nodes, "pods": chk_pods,
    "certs": chk_certs, "proxy": chk_proxy, "kubeadm": chk_kubeadm,
    "endpoint": chk_endpoint, "deployments": chk_deployments,
    "postgres": chk_postgres, "services": chk_services,
    "contentlib": chk_contentlib, "webhooks": chk_webhooks,
    "vodap": chk_vodap, "vcf": chk_vcf, "redis": chk_redis, "salt": chk_salt,
    "argo": chk_argo, "kyverno": chk_kyverno, "password": chk_password,
    "gateway": chk_gateway, "edge": chk_edge, "etcd": chk_etcd,
    "sizing": chk_sizing, "footprint": chk_footprint, "storm": chk_storm,
    "entropy": chk_entropy,
}

# Which legacy tool still owns a section we have not ported yet.
UNPORTED_OWNER = {}


# ─── Drift keeper ────────────────────────────────────────────────────────────
# The keeper is EMITTED, not embodied: a full pass of this tool takes far longer
# than the 60s cadence the Flux-revert tier needs (vsp-health-monitor.py:298
# measures 212s), so the on-node artifact is a small dependency-free shell script.

KEEPER_BODY = r"""#!/bin/bash
# vcf-lab-keeper - emitted by vcf-lab-tuner.py. Do not edit by hand.
# Re-asserts the live objects Flux/vmsp-operator revert on their ~10 minute
# reconcile. Values are generated from the same constants vcf-lab-tuner.py's
# tune mode uses, so the keeper and the declarative layer cannot disagree -
# a keeper asserting a different envoy-gateway memory than the ReleaseTemplate
# is the documented cause of 60-second rollout churn.
set -u
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
if [ ! -f "$KUBECONFIG" ] && [ -f /etc/kubernetes/node-agent.conf ]; then
    export KUBECONFIG="/etc/kubernetes/node-agent.conf"
fi
KB="kubectl"
log() { logger -t vcf-lab-keeper "$1"; }

PROBE_TARGETS='
vcf-fleet-depot deployment/depot-service download-service 10 {"spec":{"template":{"spec":{"containers":[{"name":"download-service","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60},"resources":{"limits":{"memory":"2Gi"},"requests":{"cpu":"300m","memory":"512Mi"}}},{"name":"file-server","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15}},{"name":"proxy-forwarder","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}
vcf-fleet-lcm deployment/vcf-fleet-build-service-fleetbuild fleetbuild 15 {"spec":{"template":{"spec":{"containers":[{"name":"fleetbuild","livenessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}
vidb-external deployment/vidb-service vidb-service 10 {"spec":{"template":{"spec":{"containers":[{"name":"vidb-service","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6,"periodSeconds":10}}]}}}}
vcf-sddc-lcm deployment/vcf-sddc-build-service-sddcbuild sddcbuild 15 {"spec":{"template":{"spec":{"containers":[{"name":"sddcbuild","livenessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}
vcf-sddc-lcm deployment/vcf-sddc-upgrade-service-sddcupgrade sddcupgrade 15 {"spec":{"template":{"spec":{"containers":[{"name":"sddcupgrade","livenessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"readinessProbe":{"timeoutSeconds":15,"failureThreshold":8,"periodSeconds":15},"startupProbe":{"timeoutSeconds":10,"failureThreshold":60,"periodSeconds":10}}]}}}}
vmsp-platform statefulset/prometheus-kube-prometheus-stack-prometheus prometheus 10 {"spec":{"template":{"spec":{"containers":[{"name":"prometheus","livenessProbe":{"timeoutSeconds":10,"failureThreshold":8,"periodSeconds":10},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":8,"periodSeconds":10},"resources":{"limits":{"memory":"4Gi"},"requests":{"memory":"1Gi"}}}]}}}}
vmsp-platform deployment/kube-prometheus-stack-kube-state-metrics kube-state-metrics 10 {"spec":{"template":{"spec":{"containers":[{"name":"kube-state-metrics","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6}}]}}}}
vmsp-platform daemonset/kube-prometheus-stack-prometheus-node-exporter node-exporter 10 {"spec":{"template":{"spec":{"containers":[{"name":"node-exporter","livenessProbe":{"timeoutSeconds":10,"failureThreshold":6},"readinessProbe":{"timeoutSeconds":10,"failureThreshold":6}}]}}}}
'
printf '%s\n' "$PROBE_TARGETS" | while read -r NS REF CON WANT PF; do
    [ -z "$NS" ] && continue
    CUR=$($KB -n "$NS" get "$REF" -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$CON\")].livenessProbe.timeoutSeconds}" 2>/dev/null || echo "")
    if [ -n "$CUR" ] && [ "$CUR" != "$WANT" ]; then
        $KB -n "$NS" patch "$REF" --type=strategic -p "$PF" >/dev/null 2>&1 \
            && log "drift corrected: $NS/$REF probes (livenessTimeout was '${CUR}')"
    fi
done

if $KB -n vmsp-platform get deploy envoy-gateway >/dev/null 2>&1; then
    EGMEM=$($KB -n vmsp-platform get deploy envoy-gateway \
        -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null || echo "")
    if [ "$EGMEM" != "__EG_LIMIT__" ]; then
        $KB -n vmsp-platform set resources deploy/envoy-gateway \
            --limits=memory=__EG_LIMIT__ --requests=memory=__EG_REQUEST__ >/dev/null 2>&1 \
            && log "drift corrected: envoy-gateway memory -> __EG_LIMIT__ (was ${EGMEM})"
    fi
fi

CPIARGS=$($KB -n kube-system get daemonset vsphere-cpi \
    -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || echo "")
if [ -n "$CPIARGS" ] && ! printf '%s' "$CPIARGS" | grep -q -- '--leader-elect-renew-deadline='; then
    NEW_CPIARGS=$(echo "$CPIARGS" | sed 's/\]$/,"--leader-elect-lease-duration=__LEASE__","--leader-elect-renew-deadline=__RENEW__","--leader-elect-retry-period=__RETRY__"]/')
    $KB -n kube-system patch daemonset vsphere-cpi --type=strategic -p \
      "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"vsphere-cpi\",\"args\":$NEW_CPIARGS}]}}}}" \
      >/dev/null 2>&1 \
      && $KB -n kube-system delete pod -l app=vsphere-cpi --grace-period=0 --force >/dev/null 2>&1 \
      && log "drift corrected: vsphere-cpi leader-election args re-applied"
fi

FP=$($KB get validatingwebhookconfiguration kyverno-cleanup-validating-webhook-cfg \
    -o jsonpath='{.webhooks[0].failurePolicy}' 2>/dev/null || echo "")
if [ -n "$FP" ] && [ "$FP" != "Ignore" ]; then
    $KB patch validatingwebhookconfiguration kyverno-cleanup-validating-webhook-cfg \
      --type=json -p '[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' \
      >/dev/null 2>&1 \
      && log "drift corrected: kyverno cleanup webhook failurePolicy -> Ignore (was ${FP})"
fi

for dep in ndc-controller-manager vmsp-identity; do
    REPS=$($KB -n vmsp-platform get deploy "$dep" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "")
    if [ "$REPS" = "2" ]; then
        for rt in $($KB get releasetemplate -n vmsp-platform -o name 2>/dev/null | grep -E "ndc|vmsp-identity"); do
            $KB patch "$rt" -n vmsp-platform --type=merge -p '{"spec":{"helm":{"values":{"replicaCount":1}}}}' >/dev/null 2>&1
        done
        $KB -n vmsp-platform scale deploy "$dep" --replicas=1 >/dev/null 2>&1 \
            && log "drift corrected: $dep replicas -> 1 (was $REPS)"
    fi
done

if $KB -n vmsp-platform get deploy ops-logs-gateway >/dev/null 2>&1; then
    LOGGW=$($KB -n vmsp-platform get deploy ops-logs-gateway -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null || echo "")
    if [ "$LOGGW" != "200m" ]; then
        $KB patch envoyproxy ops-logs-gateway-config -n vmsp-platform --type=json -p '[{"op":"replace","path":"/spec/provider/kubernetes/envoyDeployment/patch/value/spec/template/spec/containers/0/resources/requests/cpu","value":"200m"},{"op":"replace","path":"/spec/provider/kubernetes/envoyDeployment/patch/value/spec/template/spec/containers/0/resources/requests/memory","value":"256Mi"}]' >/dev/null 2>&1
        $KB -n vmsp-platform set resources deploy/ops-logs-gateway -c envoy --requests=cpu=200m,memory=256Mi >/dev/null 2>&1 \
            && log "drift corrected: ops-logs-gateway requests -> cpu=200m,mem=256Mi (was $LOGGW)"
    fi
fi

EGRT=$($KB get releasetemplate -n vmsp-platform -o name 2>/dev/null | grep -E 'envoyproxy-gateway-' | head -1 || echo "")
if [ -n "$EGRT" ]; then
    EGBG=$($KB -n vmsp-platform get "$EGRT" -o jsonpath='{.spec.helm.values.deployment.envoyGateway.resources.limits.memory}' 2>/dev/null || echo "")
    if [ "$EGBG" != "__EG_LIMIT__" ]; then
        $KB patch "$EGRT" -n vmsp-platform --type=merge -p '{"spec":{"helm":{"values":{"deployment":{"envoyGateway":{"resources":{"limits":{"memory":"__EG_LIMIT__"},"requests":{"memory":"__EG_REQUEST__"}}}},"config":{"envoyGateway":{"provider":{"kubernetes":{"leaderElection":{"disable":true}}}}}}}}}' >/dev/null 2>&1 \
            && log "drift corrected: envoyproxy-gateway ReleaseTemplate patched"
    fi
fi

if $KB -n vmsp-platform get helmrelease envoyproxy-gateway >/dev/null 2>&1; then
    EGHM=$($KB -n vmsp-platform get helmrelease envoyproxy-gateway -o jsonpath='{.spec.values.deployment.envoyGateway.resources.limits.memory}' 2>/dev/null || echo "")
    if [ "$EGHM" != "__EG_LIMIT__" ]; then
        $KB patch helmrelease envoyproxy-gateway -n vmsp-platform --type=merge -p '{"spec":{"values":{"deployment":{"envoyGateway":{"resources":{"limits":{"memory":"__EG_LIMIT__"},"requests":{"memory":"__EG_REQUEST__"}}}},"config":{"envoyGateway":{"provider":{"kubernetes":{"leaderElection":{"disable":true}}}}}}}}' >/dev/null 2>&1 \
            && log "drift corrected: envoyproxy-gateway HelmRelease patched"
    fi
fi

$KB exec -n vmsp-platform logging-operator-fluentd-0 -c fluentd -- sh -c 'rm -rf /buffers/backup/* /buffers/*.bak*' >/dev/null 2>&1

exit 0
"""

KEEPER_SERVICE = """[Unit]
Description=VCF lab drift keeper (emitted by vcf-lab-tuner.py)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/{unit}.sh
"""

KEEPER_TIMER = """[Unit]
Description=VCF lab drift keeper timer (emitted by vcf-lab-tuner.py)

[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s

[Install]
WantedBy=timers.target
"""

# Must match whatever the declarative layer (ReleaseTemplate / tune mode) sets.
# LEASE_TRIPLE is defined once, near chk_cp's own lease-tuning helpers, so the
# keeper and the one-shot fix can never disagree the way F2 documents.
EG_MEM_LIMIT   = "4Gi"
EG_MEM_REQUEST = "512Mi"


def detect_legacy_keepers(r, cfg):
    """Return legacy keeper units that are installed or enabled on the node.

    This is the F2 fix. remediate-lab.sh and vsp-stabilizer.sh both install
    /usr/local/bin/vsp-fleet-depot-keeper.sh and the same systemd unit names with
    DIFFERENT payloads (envoy-gateway 8Gi/1536Mi vs 4Gi/512Mi), so whichever ran
    last wins and there is no ordering that yields both. Installing a third
    keeper on top would make it a three-way race. Refuse instead.
    """
    found = []
    for unit in cfg.get("legacy_keeper_units") or []:
        rc, out = r.read(
            f"systemctl is-enabled {unit}.timer 2>/dev/null; "
            f"systemctl is-active {unit}.timer 2>/dev/null; "
            f"systemctl is-active {unit}.service 2>/dev/null; "
            f"test -f /usr/local/bin/{unit}.sh && echo SCRIPT_PRESENT", 40)
        text = (out or "")
        if any(tok in text for tok in ("enabled", "active", "SCRIPT_PRESENT")):
            state = "enabled/active" if ("enabled" in text or "active" in text) else "script on disk"
            found.append((unit, state))
    return found


def do_keeper(r, cfg, cluster, remove=False, purge_legacy=False):
    """Install or remove the emitted keeper. Mutates -> goes through Runner.write."""
    unit = cfg.get("keeper_unit")
    out = []
    if not unit:
        return [warn("keeper", f"{cfg['label']}: keeper supported",
                     "no keeper defined for this cluster", cluster=cluster)]

    if purge_legacy:
        for legacy_unit in cfg.get("legacy_keeper_units") or []:
            r.write(
                f"systemctl disable --now {legacy_unit}.timer "
                f"{legacy_unit}.service 2>/dev/null; "
                f"rm -f /etc/systemd/system/{legacy_unit}.timer "
                f"/etc/systemd/system/{legacy_unit}.service "
                f"/usr/local/bin/{legacy_unit}.sh; systemctl daemon-reload",
                f"purge legacy keeper {legacy_unit} (--purge-legacy-keepers, "
                "remediate-lab.sh/vsp-stabilizer.sh --remove equivalent)",
                tier="persistent", timeout=90)
            res = ok("keeper.purge_legacy", f"{legacy_unit}: purged", cluster=cluster)
            res.action = "removed"
            out.append(res)

    if remove:
        r.write(f"systemctl disable --now {unit}.timer 2>/dev/null; "
                f"rm -f /etc/systemd/system/{unit}.timer "
                f"/etc/systemd/system/{unit}.service /usr/local/bin/{unit}.sh; "
                f"systemctl daemon-reload",
                f"disable and remove {unit}", tier="persistent", timeout=90)
        out.append(ok("keeper", f"{unit}: removed", cluster=cluster))
        return out

    if not purge_legacy:
        legacy = detect_legacy_keepers(r, cfg)
        if legacy:
            names = ", ".join(f"{u} ({s})" for u, s in legacy)
            out.append(fail("keeper.collision", f"{unit}: safe to install",
                            f"legacy keeper present — {names}", cluster=cluster))
            emit()
            emit(f"  {_YELLOW}Refusing to install {unit}.{_NC}")
            emit(f"  {_DIM}A legacy keeper already asserts some of the same live objects on a{_NC}")
            emit(f"  {_DIM}60s timer. Two keepers with different values fight each other every{_NC}")
            emit(f"  {_DIM}minute — that is the documented cause of envoy-gateway rollout churn{_NC}")
            emit(f"  {_DIM}and VCF Ops UI flapping (see vsp-analysis-report-opus.md F2).{_NC}")
            emit(f"  {_DIM}Remove the legacy unit first, e.g.:{_NC}")
            for u, _ in legacy:
                emit(f"      {_GREEN}Tools/vsp-stabilizer.sh --remove{_NC}   "
                     f"{_DIM}# or: systemctl disable --now {u}.timer{_NC}")
            emit()
            return out

    body = (KEEPER_BODY
            .replace("__EG_LIMIT__", EG_MEM_LIMIT)
            .replace("__EG_REQUEST__", EG_MEM_REQUEST)
            .replace("__LEASE__", LEASE_TRIPLE[0])
            .replace("__RENEW__", LEASE_TRIPLE[1])
            .replace("__RETRY__", LEASE_TRIPLE[2]))
    b64_body = base64.b64encode(body.encode()).decode()
    b64_svc = base64.b64encode(KEEPER_SERVICE.format(unit=unit).encode()).decode()
    b64_tmr = base64.b64encode(KEEPER_TIMER.encode()).decode()

    r.write(
        f"echo {b64_body} | base64 -d > /usr/local/bin/{unit}.sh && "
        f"chmod 0755 /usr/local/bin/{unit}.sh && "
        f"echo {b64_svc} | base64 -d > /etc/systemd/system/{unit}.service && "
        f"echo {b64_tmr} | base64 -d > /etc/systemd/system/{unit}.timer && "
        f"systemctl daemon-reload && systemctl enable --now {unit}.timer && "
        f"/usr/local/bin/{unit}.sh",
        f"install and enable {unit}.timer (60s drift keeper)",
        tier="persistent", timeout=120)

    if r.dry_run:
        out.append(ok("keeper", f"{unit}: installed and enabled",
                      "[dry-run] not applied", cluster=cluster))
    else:
        rc, state = r.read(f"systemctl is-active {unit}.timer 2>/dev/null", 30)
        st = (state or "").strip().splitlines()
        st = st[-1].strip() if st else ""
        out.append(ok("keeper", f"{unit}.timer: active", cluster=cluster) if st == "active"
                   else fail("keeper", f"{unit}.timer: active",
                             f"is '{st or 'unknown'}'", cluster=cluster))
    return out


# ─── Per-cluster run ─────────────────────────────────────────────────────────

def run_cluster(name, args, password):
    global CLUSTERS
    CLUSTERS = get_cluster_configs(args)
    cfg = CLUSTERS[name]
    results = []

    banner(f"VCF Lab Tuner — {cfg['label']}",
           f"mode: {args.mode}" + ("  [DRY-RUN]" if args.dry_run else ""))

    if not cfg["sections"]:
        owner = UNPORTED_OWNER.get(name, "the legacy tooling")
        section(f"{cfg['label']} — NOT YET PORTED")
        res = warn(f"{name}.unported", f"{cfg['label']}: sections ported",
                   f"not implemented in v{VERSION} — still owned by {owner}",
                   cluster=name)
        row(res)
        return [res], None

    vcenter_transport = None
    vcenter_host = None

    if cfg["transport"] == "vcenter_hop":
        # The Supervisor CP is not routable from the manager: go through its
        # vCenter, which is also the only thing that knows the CP's address and
        # password. Try each configured vCenter until one reports a Supervisor;
        # a lab with none is not an error.
        host = None
        tried = []
        for vc in _vcenters_from_config():
            tried.append(vc)
            emit(f"{_DIM}  asking {vc} for its Supervisor ...{_NC}", end="")
            scp_ip, scp_pw, cid = discover_supervisor(vc, password)
            if not scp_ip:
                emit(f" {_DIM}none{_NC}")
                continue
            emit(f" {_OK} {_DIM}{scp_ip} ({cid}){_NC}")
            transport = VCenterHopTransport(vc, password, scp_ip, scp_pw)
            rc, _ = transport.exec("echo PONG", timeout=90)
            if rc != 0:
                emit(f"  {_WARN} {scp_ip} discovered but not reachable through {vc}")
                continue
            host, vcenter_host = scp_ip, vc
            vcenter_transport = VCenterTransport(vc, password)
            break
        if not host:
            section(f"{cfg['label']} — NO SUPERVISOR FOUND")
            res = ok(f"{name}.absent", f"{cfg['label']}: present",
                     "no Supervisor reported by any configured vCenter — normal "
                     "for a lab without one", cluster=name)
            row(res)
            return [res], None
    else:
        host, tried = resolve_entry_point(cfg, args.host, password)
        if not host:
            section(f"{cfg['label']} — UNREACHABLE")
            emit(f"\n{_RED}ERROR:{_NC} Cannot SSH to any {cfg['label']} candidate "
                 f"as {cfg['user']}.")
            emit(f"  Tried: {', '.join(tried) if tried else '(none)'}")
            emit(f"  Specify one directly: python3 vcf-lab-tuner.py "
                 f"--cluster {name} --host <IP>")
            return [], 2
        transport = DirectTransport(host, cfg["user"], password, cfg["sudo"])

    runner = Runner(transport, args.mode, args.dry_run, name)
    runner.vcenter_transport = vcenter_transport

    # Bulk prefetch: one kubectl for data several sections need.
    want = args.section
    ctx = {"host": host, "verbose": args.verbose, "nodes": None,
           "aggressive": args.aggressive, "threshold_days": args.threshold_days,
           "site": getattr(args, "site", None),
           "vcenter": vcenter_host,
           "cp_machine_type": args.cp_machine_type,
           "worker_machine_type": args.worker_machine_type,
           "worker_count": args.worker_count,
           "worker_min_replicas": args.worker_min_replicas,
           "worker_max_replicas": args.worker_max_replicas,
           "autoscaler_mode": args.autoscaler,
           "no_auto_fix_autoscaler": args.no_auto_fix_autoscaler,
           "resize_timeout_min": args.resize_timeout,
           "scale_timeout_min": args.scale_timeout,
           "poll_interval_sec": args.poll_interval,
           "cpu_warn_pct": args.cpu_warn_pct,
           "target_domain": getattr(args, "target_domain", None),
           "autoscaler_pin": (True if args.pin_autoscaler else
                              False if args.unpin_autoscaler else None),
           "storm_disable_le": args.storm_disable_le,
           "storm_logging": args.storm_logging,
           "cp_revert": args.revert,
           "kubelet_reload": args.kubelet_reload,
           "fix_sds_sni": args.fix_sds_sni,
           "cpu_tune": args.cpu_tune,
           "rollback_cpu_tune": args.rollback_cpu_tune,
           "recover_gateway_503": args.recover_gateway_503}
    if want is None or want in SECTIONS_NEEDING_NODES:
        emit(f"{_DIM}  fetching cluster state ...{_NC}")
        ctx["nodes"] = runner.read_json("kubectl get nodes -o json 2>/dev/null", 45)

    for key in cfg["sections"]:
        if want and want != key:
            continue
        title, _ = SECTION_MAP[key]
        section(title)
        started = time.time()
        try:
            rows = HANDLERS[key](runner, ctx)
        except Exception as exc:
            rows = [fail(key, f"{title}: check completed",
                         f"handler raised: {exc}", cluster=name)]
        if not isinstance(rows, list):
            rows = [fail(key, f"{title}: handler contract",
                         "handler did not return list[CheckResult]", cluster=name)]
        for res in rows:
            row(res)
            render_legacy(res)
        results.extend(rows)
        emit(f"  {_DIM}({time.time() - started:.1f}s){_NC}")

    if args.cpu_tune:
        section("CPU TUNING")
        rows = _cpu_tune_apply(runner, name)
        for res in rows: row(res)
        results.extend(rows)
    elif args.rollback_cpu_tune:
        section("CPU TUNING ROLLBACK")
        rows = _cpu_tune_rollback(runner, name)
        for res in rows: row(res)
        results.extend(rows)

    if args.recover_gateway_503:
        section("GATEWAY 503 RECOVERY")
        rows = _recover_gateway_503(runner, name)
        for res in rows: row(res)
        results.extend(rows)
    elif args.fix_sds_sni:
        section("SDS SAN NACK FIX")
        rows = _fix_sds_sni(runner, name)
        for res in rows: row(res)
        results.extend(rows)

    if args.install_keeper or args.remove_keeper or args.purge_legacy_keepers:
        section("DRIFT KEEPER")
        started = time.time()
        try:
            rows = do_keeper(runner, cfg, name, remove=args.remove_keeper,
                             purge_legacy=args.purge_legacy_keepers)
        except RuntimeError as exc:
            # Runner refused: read-only mode. Report it rather than crashing.
            rows = [warn("keeper", f"{cfg['label']}: keeper managed",
                         f"requires --mode tune or remediate ({exc})", cluster=name)]
        for res in rows:
            row(res)
        results.extend(rows)
        emit(f"  {_DIM}({time.time() - started:.1f}s){_NC}")

    return results, None


# ─── Help ────────────────────────────────────────────────────────────────────

def show_help():
    w = 70
    emit()
    emit(f"{_CYAN}╔{'═' * w}╗{_NC}")
    emit(f"{_CYAN}║{_NC}{_BLUE}{'VCF Lab Tuner — unified check / tune / remediate':^{w}}{_NC}{_CYAN}║{_NC}")
    emit(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{w}}{_CYAN}║{_NC}")
    emit(f"{_CYAN}╚{'═' * w}╝{_NC}\n")
    emit(f"{_BOLD}USAGE:{_NC}")
    emit(f"    vcf-lab-tuner.py --cluster NAME [--mode MODE] [--section NAME] [-v] [-j]\n")
    emit(f"{_BOLD}OPTIONS:{_NC}")
    emit(f"    {_GREEN}--cluster{_NC} <name>    vsp | vcfa | supervisor | all   (required)")
    emit(f"    {_GREEN}--mode{_NC} <mode>       preflight | tune | remediate | report  (default: report)")
    emit(f"    {_GREEN}--section{_NC} <name>    Run only the named section (see below)")
    emit(f"    {_GREEN}--host{_NC} <IP>         Override the cluster entry point; skips discovery")
    emit(f"    {_GREEN}--dry-run{_NC}           Preview; structurally cannot mutate")
    emit(f"    {_GREEN}--aggressive{_NC}        Unthresholded sweeps (default is damped)")
    emit(f"    {_GREEN}--install-keeper{_NC}    Emit + enable the 60s on-node drift keeper")
    emit(f"    {_GREEN}--remove-keeper{_NC}     Disable + remove it  (both need --mode tune)")
    emit(f"    {_GREEN}--purge-legacy-keepers{_NC}  With --remove-keeper: also purge legacy_keeper_units")
    emit(f"    {_GREEN}--threshold-days{_NC} N  Cert renewal threshold (default {CERT_THRESHOLD_DAYS})")
    emit(f"    {_GREEN}--target-domain{_NC} DOMAIN Upstream domain filter for content library trust sync")
    emit(f"    {_GREEN}--no-color{_NC}          Plain output (auto-off when not a TTY)")
    emit(f"    {_GREEN}-v, --verbose{_NC}       Raw command output and per-item detail")
    emit(f"    {_GREEN}-j, --json{_NC}          Machine-readable document on stdout (implies --no-color)")
    emit(f"    {_GREEN}-h, --help{_NC}          Show this help")
    emit(f"    {_GREEN}--version{_NC}           Print version and exit\n")
    emit(f"{_BOLD}SIZING OPTIONS:{_NC}  (--cluster vsp --section sizing --mode remediate; vsp-scale-down.py port)")
    emit(f"    {_GREEN}--cp-machine-type{_NC} TYPE       Resize the control plane ({', '.join(SIZING_MACHINE_TYPES)})")
    emit(f"    {_GREEN}--worker-machine-type{_NC} TYPE   Resize workers (same TYPE choices)")
    emit(f"    {_GREEN}--worker-count{_NC} N             Set worker min==max==N")
    emit(f"    {_GREEN}--worker-min-replicas{_NC} N      Pair with --worker-max-replicas")
    emit(f"    {_GREEN}--worker-max-replicas{_NC} N")
    emit(f"    {_GREEN}--autoscaler{_NC} MODE            auto | enable | disable  (default: auto)")
    emit(f"    {_GREEN}--no-auto-fix-autoscaler{_NC}     Don't patch MachineDeployment if the autoscaler is stuck")
    emit(f"    {_GREEN}--resize-timeout{_NC} MIN         CP/worker rollout wait  (default 60)")
    emit(f"    {_GREEN}--scale-timeout{_NC} MIN          Replica-bounds drain/grow wait  (default 60)")
    emit(f"    {_GREEN}--poll-interval{_NC} SEC          Seconds between polls  (default 5)")
    emit(f"    {_GREEN}--cpu-warn-pct{_NC} PCT           Node utilization hot threshold  (default 80)\n")
    emit(f"{_BOLD}FOOTPRINT OPTIONS:{_NC}  (--cluster vsp --section footprint --mode remediate)")
    emit(f"    {_GREEN}--pin-autoscaler{_NC}             Durably freeze worker count (ReleaseTemplate replicaCount=0)")
    emit(f"    {_GREEN}--unpin-autoscaler{_NC}           Reverse it (replicaCount=1)")
    emit(f"    {_DIM}(no target flag = also reports envoy-gateway-fix + CAPI-LE + right-size drift){_NC}\n")
    emit(f"{_BOLD}STORM OPTIONS:{_NC}  (--cluster vcfa --section storm --mode remediate; vcfa-storm-mitigation.sh port)")
    emit(f"    {_GREEN}--storm-disable-le{_NC}           OPT-IN: disable LE on 3 replicas==1 vksm services (EXPERIMENTAL)")
    emit(f"    {_GREEN}--storm-logging{_NC}              OPT-IN, DISRUPTIVE: tenant-manager DEBUG/TRACE->INFO + cell restart\n")
    emit(f"{_BOLD}CP LEASE/ETCD OPTIONS:{_NC}  (--cluster {{vsp,vcfa}} --section cp --mode remediate)")
    emit(f"    {_GREEN}--revert{_NC}                     Restore KCM/scheduler/etcd/kube-vip manifests from the newest backup")
    emit(f"    {_GREEN}--kubelet-reload{_NC}             OPT-IN, DISRUPTIVE: restart kubelet if a manifest edit ran this pass\n")
    emit(f"{_BOLD}SECTION NAMES:{_NC}  (use with --section)")
    for key, (title, desc) in SECTION_MAP.items():
        emit(f"    {_GREEN}{key:<10}{_NC} {desc}")
    emit()
    emit(f"{_BOLD}CLUSTER COVERAGE (v{VERSION}):{_NC}")
    for name, cfg in CLUSTERS.items():
        act = ", ".join(sorted({m for s in cfg["sections"] for m in SECTION_ACT_MODES.get(s, ())})) or "detect-only"
        emit(f"    {_DIM}{cfg['label']:<11} : {', '.join(cfg['sections']) or '(not ported)'}{_NC}")
        emit(f"    {_DIM}{'':<11}   mutating modes: {act}{_NC}")
    emit(f"    {_DIM}--dry-run previews every write; a bare cluster/section pass reports only.{_NC}\n")
    emit(f"{_YELLOW}EXAMPLES:{_NC}")
    emit(f"    {_GREEN}# Read-only health of the VSP cluster{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --mode preflight\n")
    emit(f"    {_GREEN}# One section, verbose{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --section certs -v\n")
    emit(f"    {_GREEN}# Machine-readable for CI{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster all --mode preflight --json\n")
    emit(f"    {_GREEN}# Report current CP/worker sizing, replica bounds, autoscaler, utilization{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --section sizing\n")
    emit(f"    {_GREEN}# Resize workers to cp.large-equivalent management type, dry-run first{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --mode remediate --section sizing \\\n"
         f"        --worker-machine-type management.large --dry-run\n")
    emit(f"    {_GREEN}# Scale worker bounds to a fixed count of 6, letting the autoscaler converge{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --mode remediate --section sizing --worker-count 6\n")
    emit(f"    {_GREEN}# Durably pin the autoscaler off to freeze the fleet's footprint{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --mode remediate --section footprint --pin-autoscaler\n")
    emit(f"    {_GREEN}# Mitigate a VCFA CPU storm (footprint, probes, kube-vip, gateway, UI tier){_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vcfa --mode remediate --section storm\n")
    emit(f"    {_GREEN}# Tune KCM/scheduler/etcd/kube-vip lease timing on both the VSP CP and VCFA{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster all --mode remediate --section cp\n")
    emit(f"    {_GREEN}# AMD Zen4/5 ESXi entropySources workaround (config-only, never reboots){_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --mode remediate --section entropy\n")
    emit(f"    {_GREEN}# Fully remove a keeper, including any legacy unit it refused to install over{_NC}")
    emit(f"    python3 vcf-lab-tuner.py --cluster vsp --mode tune --remove-keeper --purge-legacy-keepers\n")
    emit(f"{_BOLD}EXIT CODES:{_NC}")
    emit(f"    0  All checks passed    1  One or more failed    2  Cannot connect")
    sys.exit(0)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global _QUIET

    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--cluster",  required=True, metavar="NAME",
                   choices=list(CLUSTERS.keys()) + ["all"])
    p.add_argument("--mode",     default="report", metavar="MODE", choices=MODES)
    p.add_argument("--section",  default=None, metavar="NAME",
                   choices=list(SECTION_MAP.keys()))
    p.add_argument("--host",     default=None, metavar="IP")
    p.add_argument("--dry-run",  action="store_true")
    p.add_argument("--aggressive", action="store_true")
    p.add_argument("--install-keeper", action="store_true")
    p.add_argument("--remove-keeper", action="store_true")
    p.add_argument("--threshold-days", type=int, default=CERT_THRESHOLD_DAYS, metavar="N")
    # --section sizing (vsp-scale-down.py port): target values, not detect-and-fix.
    p.add_argument("--cp-machine-type", default=None, metavar="TYPE",
                   choices=list(SIZING_MACHINE_TYPES.keys()))
    p.add_argument("--worker-machine-type", default=None, metavar="TYPE",
                   choices=list(SIZING_MACHINE_TYPES.keys()))
    p.add_argument("--worker-count", type=int, default=None, metavar="N")
    p.add_argument("--worker-min-replicas", type=int, default=None, metavar="N")
    p.add_argument("--worker-max-replicas", type=int, default=None, metavar="N")
    p.add_argument("--autoscaler", default="auto", choices=("auto", "enable", "disable"))
    p.add_argument("--no-auto-fix-autoscaler", action="store_true")
    p.add_argument("--resize-timeout", type=int, default=60, metavar="MIN")
    p.add_argument("--scale-timeout", type=int, default=60, metavar="MIN")
    p.add_argument("--poll-interval", type=int, default=5, metavar="SEC")
    p.add_argument("--cpu-warn-pct", type=int, default=80, metavar="PCT")
    # --section footprint: the durable ReleaseTemplate-based autoscaler pin.
    p.add_argument("--pin-autoscaler", action="store_true")
    p.add_argument("--unpin-autoscaler", action="store_true")
    # --section storm: opt-in, disruptive levers off by default (vcfa-storm-mitigation.sh).
    p.add_argument("--storm-disable-le", action="store_true")
    p.add_argument("--storm-logging", action="store_true")
    # --section cp: Family B lease/etcd tuning extras (remediate-lab.sh --apply-lease
    # / --revert-lease / --kubelet-reload).
    p.add_argument("--revert", action="store_true")
    p.add_argument("--kubelet-reload", action="store_true")
    # vcfa-stabilizer.sh parity flags
    p.add_argument("--fix-sds-sni", action="store_true")
    p.add_argument("--cpu-tune", action="store_true")
    p.add_argument("--rollback-cpu-tune", action="store_true")
    p.add_argument("--recover-gateway-503", action="store_true")
    # --install-keeper / --remove-keeper extra.
    p.add_argument("--purge-legacy-keepers", action="store_true")
    p.add_argument("--site", default=None, metavar="SITE", choices=("a", "b"),
                   help="Target site (a or b) for dynamic resolution")
    p.add_argument("--target-domain", default=None, metavar="DOMAIN",
                   help="Target upstream domain for content library trust sync")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-j", "--json", action="store_true")
    p.add_argument("--version", action="version",
                   version=f"vcf-lab-tuner.py {VERSION} ({DATE})")

    # A bad flag must be an error, not a help screen: auto-health.py:952's
    # _QuietArgumentParser turns a typo into help + exit 0, which hides mistakes
    # in scripted use.
    args = p.parse_args()

    if args.no_color or args.json:
        _set_color(False)
    if args.json:
        _QUIET = True

    if args.host and args.cluster == "all":
        emit(f"{_RED}ERROR:{_NC} --host cannot be combined with --cluster all.",
             stderr=True)
        sys.exit(2)

    if args.install_keeper and args.remove_keeper:
        emit(f"{_RED}ERROR:{_NC} --install-keeper and --remove-keeper are mutually exclusive.",
             stderr=True)
        sys.exit(2)

    if (args.install_keeper or args.remove_keeper) and args.mode in READ_ONLY_MODES:
        emit(f"{_RED}ERROR:{_NC} keeper management mutates the node; use "
             f"--mode tune (add --dry-run to preview).", stderr=True)
        sys.exit(2)

    if args.worker_count is not None and (args.worker_min_replicas is not None
                                          or args.worker_max_replicas is not None):
        emit(f"{_RED}ERROR:{_NC} --worker-count and --worker-min-replicas/"
             f"--worker-max-replicas are mutually exclusive.", stderr=True)
        sys.exit(2)
    if (args.worker_min_replicas is None) != (args.worker_max_replicas is None):
        emit(f"{_RED}ERROR:{_NC} --worker-min-replicas and --worker-max-replicas "
             f"must be given together.", stderr=True)
        sys.exit(2)
    if (args.worker_min_replicas is not None and args.worker_max_replicas is not None
            and args.worker_min_replicas > args.worker_max_replicas):
        emit(f"{_RED}ERROR:{_NC} --worker-min-replicas cannot exceed "
             f"--worker-max-replicas.", stderr=True)
        sys.exit(2)
    if args.pin_autoscaler and args.unpin_autoscaler:
        emit(f"{_RED}ERROR:{_NC} --pin-autoscaler and --unpin-autoscaler are "
             f"mutually exclusive.", stderr=True)
        sys.exit(2)

    _sizing_targets_given = bool(
        args.cp_machine_type or args.worker_machine_type or
        args.worker_count is not None or args.worker_min_replicas is not None or
        args.autoscaler != "auto")
    if _sizing_targets_given and args.section != "sizing":
        emit(f"{_RED}ERROR:{_NC} sizing target flags (--cp-machine-type, "
             f"--worker-machine-type, --worker-count, --worker-min/max-replicas, "
             f"--autoscaler) require --section sizing, so a resize/scale can never "
             f"fire as a side effect of a broader --mode remediate sweep.", stderr=True)
        sys.exit(2)
    if _sizing_targets_given and args.mode in READ_ONLY_MODES:
        emit(f"{_RED}ERROR:{_NC} sizing changes mutate the cluster; use "
             f"--mode remediate (add --dry-run to preview).", stderr=True)
        sys.exit(2)

    if (args.pin_autoscaler or args.unpin_autoscaler) and args.section != "footprint":
        emit(f"{_RED}ERROR:{_NC} --pin-autoscaler/--unpin-autoscaler require "
             f"--section footprint.", stderr=True)
        sys.exit(2)
    if (args.pin_autoscaler or args.unpin_autoscaler) and args.mode in READ_ONLY_MODES:
        emit(f"{_RED}ERROR:{_NC} pinning the autoscaler mutates the cluster; use "
             f"--mode remediate (add --dry-run to preview).", stderr=True)
        sys.exit(2)

    if (args.storm_disable_le or args.storm_logging) and args.section != "storm":
        emit(f"{_RED}ERROR:{_NC} --storm-disable-le/--storm-logging require "
             f"--section storm.", stderr=True)
        sys.exit(2)
    if (args.storm_disable_le or args.storm_logging) and args.mode in READ_ONLY_MODES:
        emit(f"{_RED}ERROR:{_NC} these are opt-in mutating levers; use "
             f"--mode remediate (add --dry-run to preview).", stderr=True)
        sys.exit(2)
    if args.storm_logging and not args.dry_run:
        emit(f"{_YELLOW}WARNING:{_NC} --storm-logging restarts the tenant-manager "
             f"cell (DISRUPTIVE, opt-in). Proceeding in 5s — Ctrl-C to abort.",
             stderr=True)
        time.sleep(5)

    if (args.revert or args.kubelet_reload) and args.section != "cp":
        emit(f"{_RED}ERROR:{_NC} --revert/--kubelet-reload require --section cp.",
             stderr=True)
        sys.exit(2)
    if (args.revert or args.kubelet_reload) and args.mode in READ_ONLY_MODES:
        emit(f"{_RED}ERROR:{_NC} --revert/--kubelet-reload mutate the node; use "
             f"--mode remediate (add --dry-run to preview).", stderr=True)
        sys.exit(2)
    if args.kubelet_reload and not args.dry_run:
        emit(f"{_YELLOW}WARNING:{_NC} --kubelet-reload restarts kubelet "
             f"(DISRUPTIVE, opt-in — a brief apiserver blip). Proceeding in "
             f"5s — Ctrl-C to abort.", stderr=True)
        time.sleep(5)

    if args.purge_legacy_keepers and not (args.remove_keeper or args.install_keeper):
        emit(f"{_RED}ERROR:{_NC} --purge-legacy-keepers requires --install-keeper or --remove-keeper.",
             stderr=True)
        sys.exit(2)

    if args.site:
        args.host = None # Force dynamic resolution if site is provided

    targets = list(CLUSTERS.keys()) if args.cluster == "all" else [args.cluster]

    if args.section:
        eligible = [t for t in targets if args.section in CLUSTERS[t]["sections"]]
        if not eligible:
            emit(f"{_RED}ERROR:{_NC} section '{args.section}' is not available for "
                 f"cluster '{args.cluster}' in v{VERSION}.", stderr=True)
            sys.exit(2)

    password = get_password()
    started = datetime.now()
    all_results = []
    exit_code = 0

    for name in targets:
        if args.section and args.section not in CLUSTERS[name]["sections"]:
            continue
        results, hard = run_cluster(name, args, password)
        all_results.extend(results)
        if hard is not None:
            exit_code = max(exit_code, hard)

    total = len(all_results)
    acted = sum(1 for r in all_results if r.action)
    failed = sum(1 for r in all_results if r.state == "fail")
    warned = sum(1 for r in all_results if r.state == "warn")
    elapsed = (datetime.now() - started).total_seconds()

    if failed:
        exit_code = max(exit_code, 1)

    if not args.json:
        color = _GREEN if failed == 0 else _RED
        emit(f"\n{_CYAN}{'─' * 64}{_NC}")
        action_note = f", actions: {acted}" if acted else ""
        emit(f"  {color}{_BOLD}RESULT: {total - failed}/{total} checks passed{_NC}"
             f"  {_DIM}(warn: {warned}{action_note}, total: {elapsed:.1f}s){_NC}")
        if failed:
            emit(f"  {_RED}  {failed} check(s) require attention — see {_FAIL} rows above{_NC}")
        elif total:
            emit(f"  {_GREEN}  No failing checks{_NC}")
        emit(f"{_CYAN}{'─' * 64}{_NC}\n")
    else:
        # Exclusive, labelled JSON on stdout - not appended after human output
        # with positional keys, which is what makes vsp-health.py:1569
        # unparseable in practice.
        json.dump({
            "tool": "vcf-lab-tuner.py",
            "version": VERSION,
            "timestamp": started.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": args.mode,
            "dry_run": args.dry_run,
            "clusters": targets,
            "section_filter": args.section,
            "checks_total": total,
            "checks_failed": failed,
            "checks_warned": warned,
            "actions_taken": acted,
            "healthy": failed == 0,
            "elapsed_seconds": round(elapsed, 1),
            "results": [r.as_dict() for r in all_results],
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
