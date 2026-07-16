# VSP Cluster Troubleshooting Scripts

Version 1.2 - 2026-07-16
Author: Burke Azbill and HOL Core Team

Standalone tools for diagnosing and remediating VSP (Supervisor) cluster
issues.  Each script is fully self-contained — no imports from `lsfunctions.py`
— **except** `vsp-health-monitor.py`, which runs on the manager VM and does
import `lsfunctions` (see its own section below).

All scripts connect to the VSP cluster via SSH using `sshpass` and the lab
password from `/home/holuser/creds.txt`. The four standalone scripts
(`vsp-health.py`, `kube-fix.py`, `salt-stabilize.py`, `vodap-fix.py`) all
resolve the control-plane host the same way, trying candidates in order
until one answers SSH: explicit `--host <IP>` (if given) → the hardcoded VIP
(`10.1.1.142`) → auto-discovery via `--worker <FQDN>`. Pass `--host` only
when you already know the actual CP node IP (e.g. VIP is down and you found
the node via vCenter console). `vsp-health-monitor.py` has no `--host` flag
(it is unattended/cron-driven) — see its section below for its own
equivalent resolution order.

---

## Scripts at a Glance

| Script | Version | Mode | Purpose |
| ------ | ------- | ---- | ------- |
| `vsp-health.py` | 2.5.0 | Read-only | Full cluster health sweep — diagnose before acting |
| `vsp-health-monitor.py` | 2.3 | Automated (cron) | Detects + optionally remediates 15 recurring failure modes every 5 min |
| `kube-fix.py` | 1.1.0 | Remediation | Control-plane VIP drop + kube-controller-manager/kube-scheduler backoff |
| `salt-stabilize.py` | 1.1.0 | Remediation | Salt RAAS / salt-master / salt-minion crash recovery |
| `vodap-fix.py` | 1.1.0 | Remediation | ClickHouse TLS cert staleness + fluentd buffer disk full |

---

## `vsp-health.py` — Comprehensive Health Check (v2.5.0)

Full read-only diagnostic sweep across all VSP components. Run this first to
understand what's wrong before running any remediation script. Every check
in this script has a corresponding automated fix in `vsp-health-monitor.py`
(see the checks table below) — this script never makes changes itself.

### Sections checked

| Section | What is checked |
| ------- | ---------------- |
| `cp` | VIP reachable, `kube-vip.yaml` manifest (`vip_preserve_on_leadership_loss`), `crictl ps` for etcd / kube-controller-manager / kube-scheduler / kube-vip |
| `nodes` | Every node: Ready state, SchedulingDisabled |
| `pods` | All pods in ALL namespaces — one line per namespace (`ready/total`), unhealthy pods listed inline with reason |
| `vcf` | All VCF-managed workloads (`vcfcomponents`) — `spec.replicas` vs `readyReplicas`, detects scaled-to-0, plus `components.api.vmsp.vmware.com` operational-status |
| `postgres` | All Zalando Spilo instances — container readiness, suspended CRD label, `numberOfInstances` vs. saved original-instances annotation |
| `redis` | Redis pod readiness + `redis-service` endpoint population |
| `salt` | salt-master / salt-minion readiness + log tail for SSL / RAAS stop errors |
| `certs` | cert-manager `Certificate` resources — Ready condition + days to expiry |
| `argo` | Stale `system-shutdown-*` Argo Workflows + power-off-marker ConfigMap (KB 440862 boot-deadlock signature) |
| `vodap` | ClickHouse served-cert-vs-secret mismatch (cert-manager alone can't see this — ClickHouse never hot-reloads) + logging-operator-fluentd buffer backlog |
| `proxy` | VSP node proxy config vs. the lab's canonical proxy URL |
| `kubeadm` | kubeadm's own cert population (separate from cert-manager's Certificate CRDs in `certs`) |

### CP host resolution (vsp-health.py)

Tries, in order, until one answers SSH: explicit `--host <IP>` → hardcoded
VIP (`10.1.1.142`) → auto-discovery via `--worker <FQDN>` (reads the actual
CP IP out of a worker node's kubeconfig).

### Usage

```bash
# Full health check — all sections (~20s)
python3 vsp-health.py

# Single section for faster targeted checks
python3 vsp-health.py --section pods
python3 vsp-health.py --section vcf
python3 vsp-health.py --section salt
python3 vsp-health.py --section certs
python3 vsp-health.py --section vodap
python3 vsp-health.py --section proxy
python3 vsp-health.py --section kubeadm

# Verbose — per-pod issue detail and raw kubectl snippets
python3 vsp-health.py --verbose
python3 vsp-health.py --section pods --verbose

# JSON output for scripting
python3 vsp-health.py --json 2>/dev/null | jq .healthy

# When the VIP (10.1.1.142) is down — specify a CP node IP directly
python3 vsp-health.py --host 10.1.1.143
```

---

## `vsp-health-monitor.py` — Automated Health Monitor & Remediator (v2.3)

Runs on the **manager VM** (not a VSP node — VSP control-plane nodes are
CAPI "cattle" that get rolling-replaced, so a unit installed there would be
lost on the next replacement). Detects and (optionally) remediates the same
recurring failure modes `vsp-health.py` can only report, either once at lab
startup or on a recurring manager-side cron job (default every 5 min).

**Disabled by default** — nothing runs until `[VSPMONITOR] enabled=true` is
set in `config.ini`. The recurring cron job is a manager crontab entry
(`holuser`'s sudoers cannot install systemd units); install/remove it with
`--install-timer` / `--uninstall-timer`. The recurring `--once` pass exits
immediately if the lab has not reached "Ready" (`--ignore-ready` bypasses
this for the intentional pre-Ready startup pass).

### CP host resolution (vsp-health-monitor.py)

Distinct from the other four scripts (no `--host` flag — this is
unattended/cron-driven). On each cycle, tries in order until the CP
responds to ping: the configured `vsp_control_plane_ip` (default
`10.1.1.142`) → the hardcoded default VIP (in case the config value was
overridden to something now stale) → the existing VIP-restore remediation
(`ip addr add` + gratuitous ARP on the auto-discovered node, gated by
`remediate`) → running the cycle directly against the auto-discovered CP
node IP if the VIP still won't come back, instead of skipping the entire
cycle.

### Checks (config.ini `[VSPMONITOR] checks`, comma-separated, all on by default)

| Check | What it fixes |
| ----- | -------------- |
| `host_contention` | Gate — runs first; if 1-min load > nproc×multiplier, downgrades every other check this cycle to detect-only (hypervisor CPU steal, not a config problem) |
| `vsp_size` | Re-asserts the active ComponentVersion size profile's control-plane cpu >= 12 |
| `kvip_manifest` | Patches `vip_preserve_on_leadership_loss=true` in the kube-vip static-pod manifest |
| `cp_pod_crash` | `crictl rm -f` on a crashed kube-controller-manager/kube-scheduler static pod |
| `gateway` | Restarts unhealthy envoy-gateway controller / vmsp-gateway / ops-logs-gateway pods |
| `node_flap` | Sets `node-monitor-grace-period=90s`; bounces etcd/kube-apiserver if a node is currently NotReady |
| `crashloop_pods` | Force-restarts cluster-wide CrashLoopBackOff/Error pods above a restart threshold (capped per cycle) |
| `postgres` | Fixes `pgdatabase-0` pgdata permissions; un-suspends PostgresInstance CRDs; restores Zalando `numberOfInstances` |
| `salt_stack` | Ordered redis→raas→salt-master→salt-minion rollout restart, gated on detected readiness/log-error issues |
| `vodap` | ClickHouse served-cert-vs-secret mismatch → StatefulSet restart; purges fluentd `/buffers/backup` backlog |
| `component_health` | Fixes `NotRunning` Component CRD annotations; scales up any `[VCFFINAL] vcfcomponents` entry below its saved replica count |
| `argo_cleanup` | Deletes stale `system-shutdown-*` Argo Workflows; replays power-off-marker ConfigMap; uncordons non-condemned nodes |
| `proxy_config` | Repairs VSP-node proxy config drift against the lab's canonical values |
| `cert_renewal` | Delegates to `vsp_cert_renewer.py --cluster vsp` (runs once per boot session) |
| `vip` | Detect-only — verifies every LoadBalancer Service VIP is reachable |

### Config (`config.ini [VSPMONITOR]`, all optional)

| Key | Default | Meaning |
| --- | ------- | ------- |
| `enabled` | `false` | Master on/off |
| `remediate` | `true` | `false` = detect/log only |
| `interval_seconds` | `300` | Cron cadence (rounded to whole minutes) |
| `vsp_control_plane_ip` | `10.1.1.142` | CP host tried first each cycle |
| `vsp_worker_fqdn` | `vsp-01a.site-a.vcf.lab` | Worker FQDN for CP-IP discovery when the VIP itself is down |
| `checks` | *(all 15 above)* | Which checks run |
| `crashloop_restart_threshold` | `5` | Min `restartCount` before `crashloop_pods` acts |
| `crashloop_max_restarts_per_cycle` | `15` | Safety cap per run |
| `crashloop_exclude_namespaces` | *(none)* | Extra namespaces to skip (csv) |
| `host_contention_load_multiplier` | `1.5` | 1-min load > nproc×this → downgrade to detect-only |
| `cert_renewal_threshold_days` | `60` | Passed to `vsp_cert_renewer.py --threshold-days` |

### CLI flags

| Flag | Meaning |
| ---- | ------- |
| `--once` | Run one check/remediate pass and exit (default) |
| `--dry-run` | Detect and log only; never remediate this run |
| `--install-timer` | Install/refresh the manager crontab entry (also runs one immediate pass, ignoring the Ready gate) |
| `--uninstall-timer` | Remove the crontab entry |
| `--ignore-ready` | Skip the lab-Ready gate (used by the VCFfinal startup pass) |
| `--version` | Print version and exit |

### Usage

```bash
# One-off manual pass (respects the lab-Ready gate)
python3 vsp-health-monitor.py --once

# Preview without remediating
python3 vsp-health-monitor.py --once --dry-run

# Install the recurring 5-min cron job (also runs one pre-Ready pass)
python3 vsp-health-monitor.py --install-timer

# Remove the cron job
python3 vsp-health-monitor.py --uninstall-timer
```

---

## `kube-fix.py` — Control Plane Fix (v1.1.0)

Remediates Kubernetes control-plane instability caused by kube-vip VIP drops
and kube-controller-manager CrashLoopBackOff after cold boot. Also available
automatically, gated on detection, as `vsp-health-monitor.py`'s
`kvip_manifest` and `cp_pod_crash` checks.

### Root causes addressed

1. **kube-vip VIP drop** — Default `vip_preserve_on_leadership_loss=false`
   causes the VIP to be released when kube-vip panics during leader election
   under API-server load at boot.  Without the VIP, kube-scheduler and
   kube-controller-manager lose their API connection and crash.
2. **kube-controller-manager CrashLoopBackOff** — After losing the VIP, KCM
   enters a 5-minute exponential back-off.  During this window it cannot update
   Endpoints/EndpointSlices, so newly restarted pods are never added to their
   Services — causing the Redis/Salt cert-timing race.
3. **kube-scheduler CrashLoopBackOff** — Same back-off issue.
4. **Dropped VIP** — If `10.1.1.142` is not assigned to the CP node, restore it
   with `ip addr add` and send a gratuitous ARP to update switch MAC tables.

### Steps performed

1. **CP host resolution** — `--host` → hardcoded VIP → auto-discovery via `--worker`
2. **VIP restore** — if `--vip` (default `10.1.1.142`) is unreachable: `ip addr add` + gratuitous ARP
3. **kube-vip manifest patch** — sets `vip_preserve_on_leadership_loss=true`
   (persistent — survives reboots)
4. **kube-controller-manager reset** — `crictl rm -f` clears the 5-minute back-off
5. **kube-scheduler reset** — same `crictl rm -f` approach
6. **Verification** — VIP ping, `crictl ps`, `kubectl get nodes`

### Usage

```bash
python3 kube-fix.py                          # all fixes
python3 kube-fix.py --dry-run                # preview only
python3 kube-fix.py --skip-vip --skip-kcm    # manifest patch only
python3 kube-fix.py --host 10.1.1.143        # force a specific CP node IP
```

---

## `salt-stabilize.py` — Salt Stack Remediation (v1.1.0)

Remediates the Salt infrastructure (salt-raas + salt namespaces) after a cold
boot or an unexpected failure. Mirrors the fix that `VCFfinal.py` v6.3.22+
applies automatically at startup, and that `vsp-health-monitor.py`'s
`salt_stack` and `postgres` checks apply automatically (gated on detection)
every 5 min if enabled.

Run manually any time VCF Operations Security Posture shows
**"Salt infrastructure is down"** or **"Failed to load Benchmarks"**.

### Root causes addressed

1. **pgdatabase-0 permissions** — Postgres data directory permissions != `0700`
   causes Spilo to refuse to start (`FATAL: data directory has invalid permissions`).
2. **Redis TLS cert race** — Redis loads its in-memory TLS cert at pod start.
   If `vsp_cert_renewer` runs ~18s later, the cert in memory is stale/expired.
   RAAS Celery worker sees `SSL CERTIFICATE_VERIFY_FAILED` and crash-loops.
3. **salt-master event-bus broken** — Receives `500/530` from the unhealthy RAAS
   SSE API and stops auto-recovering.
4. **salt-minion permanently stopped** — `"This Minion was scheduled to stop"`
   when it cannot authenticate with a broken master.

### Steps performed

1. **CP host resolution** — `--host` → hardcoded VIP → auto-discovery via `--worker`
2. Check `pgdatabase-0` — if < 3/3 ready, fix pgdata permissions via `walg` sidecar
3. `kubectl rollout restart deployment/redis` (salt-raas) → wait 30 s
4. `kubectl rollout restart deployment/raas`  (salt-raas) → wait 60 s
5. `kubectl rollout restart deployment/salt-master` (salt) → wait 45 s
6. `kubectl rollout restart deployment/salt-minion` (salt)
7. Final readiness check + log scan

### Usage

```bash
python3 salt-stabilize.py            # full remediation
python3 salt-stabilize.py --dry-run  # preview only
python3 salt-stabilize.py --verbose  # show kubectl output
```

---

## `vodap-fix.py` — Vodap ClickHouse TLS + Fluentd Disk Fix (v1.1.0)

Addresses two long-running lab issues that `VCFfinal.py` v6.3.22+ also handles
proactively at startup, and that `vsp-health-monitor.py`'s `vodap` check
applies automatically (gated on detection) every 5 min if enabled.

### Problem 1 — ClickHouse TLS cert staleness

`cert-manager` renews `vcf-obs-clickhouse-cert` every 90 days. ClickHouse
loads its TLS cert at pod startup and **never hot-reloads** it.  If ClickHouse
restarts in the narrow window between the old cert expiring and cert-manager
finishing the secret update, it continues to serve the old expired cert.

The vodap Java services (`vcf-obs-data-query-service`,
`vcf-obs-netops-collector-service`) use BouncyCastle FIPS which strictly
validates certificate expiry and refuses to connect → startup probe fails
(`connection refused` / HTTP 503) → CrashLoopBackOff.

**Symptoms:** vodap pods in CrashLoopBackOff; logs show
`CertificateExpiredException: certificate expired on <date>`.

**Detection:** compares the cert ClickHouse is actually serving (via
`openssl s_client`) with the cert in the Kubernetes secret.

**Fix:** `kubectl rollout restart statefulset/chi-vcf-obs-vcf-obs-0-0 -n vodap`
— forces ClickHouse to reload the now-valid cert from the secret.

### Problem 2 — logging-operator-fluentd buffer PVC disk full

The fluentd readiness probe runs a shell script inside the container that checks:
- `/buffers` disk usage must be **< 80%**
- Number of active `.buffer` files must be **< 10,000**

Over weeks of continuous operation, `/buffers/backup` accumulates old chunk
files (86,546 files / 8.1 GB observed after 89 days), pushing disk usage to
83% and failing the readiness probe (pod shows `1/2` containers Ready).

**A pod restart does NOT fix this** — the buffer PVC persists across pod
recreations, so the new pod immediately remounts the full volume and fails
the same probe.

**Fix:** `kubectl exec` into the running container and `rm -rf /buffers/backup/*`.
Disk usage drops immediately; the readiness probe passes within 15–30 s.

### Steps performed

1. **CP host resolution** — `--host` → hardcoded VIP → auto-discovery via `--worker`
2. Read `vcf-obs-clickhouse-cert` from the Kubernetes secret; decode cert
3. Check cert expiry and whether cert was recently renewed
4. Connect to ClickHouse (`openssl s_client`) and compare served cert vs. secret cert
5. Check whether vodap client deployments are at full readyReplicas
6. If mismatch detected: restart ClickHouse StatefulSet, wait up to 180 s
7. Check `logging-operator-fluentd-0` container readiness
8. If `< 2/2` ready: exec into container, check `/buffers` disk usage and backup file count
9. Purge `/buffers/backup/*` if disk > threshold; poll for readiness probe to pass

### Usage

```bash
python3 vodap-fix.py             # full diagnosis + remediation
python3 vodap-fix.py --dry-run   # preview only (no changes made)
python3 vodap-fix.py --verbose   # show raw kubectl output per step
```

---

## Recommended Runbooks

If `vsp-health-monitor.py` is installed and enabled (`[VSPMONITOR]
enabled=true`, cron job installed via `--install-timer`), most of the
failure modes below are already being detected and remediated every 5
minutes — these manual runbooks are for point-in-time diagnosis, a faster
one-off fix, or labs where the monitor isn't running.

### "Salt infrastructure is down"

Symptom: VCF Operations Security Posture shows **"Salt infrastructure is down"**
or **"Failed to load Benchmarks"**.

```bash
# 1. Diagnose — look for issues in cp, nodes, redis, salt sections
python3 vsp-health.py

# 2. Fix control plane if kube-vip or KCM issues are reported
python3 kube-fix.py

# 3. Stabilize Salt stack
python3 salt-stabilize.py

# 4. Verify all clear
python3 vsp-health.py
```

### Vodap pods in CrashLoopBackOff / fluentd 1/2 Ready

Symptom: `vcf-obs-data-query-service` or `vcf-obs-netops-collector-service`
stuck restarting; or `logging-operator-fluentd-0` shows `1/2` containers Ready.

```bash
# 1. Diagnose
python3 vsp-health.py --section pods
python3 vsp-health.py --section vcf
python3 vsp-health.py --section certs
python3 vsp-health.py --section vodap

# 2. Fix ClickHouse TLS cert + fluentd buffer disk
python3 vodap-fix.py

# 3. Verify all clear
python3 vsp-health.py
```

### General "something is broken"

```bash
# 1. Full sweep — identify what is unhealthy
python3 vsp-health.py

# 2. Run the appropriate remediation script(s) based on output
#    cp / nodes issues     → kube-fix.py
#    salt / redis issues   → salt-stabilize.py
#    vodap / fluentd       → vodap-fix.py
#    proxy / kubeadm certs → not yet standalone; run
#                            vsp-health-monitor.py --once (proxy_config /
#                            cert_renewal checks) or fix manually

# 3. Verify
python3 vsp-health.py
```
