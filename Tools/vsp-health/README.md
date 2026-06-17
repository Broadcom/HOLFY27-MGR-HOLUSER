# VSP Cluster Troubleshooting Scripts

Standalone tools for diagnosing and remediating VSP (Supervisor) cluster
issues.  Each script is fully self-contained — no imports from `lsfunctions.py`.

All scripts connect to the VSP cluster via SSH using `sshpass` and the lab
password from `/home/holuser/creds.txt`.  Control-plane discovery is automatic;
use `--host <IP>` only when the VIP (`10.1.1.142`) is unreachable.

---

## Scripts at a Glance

| Script | Mode | Purpose |
|--------|------|---------|
| `vsp-health.py` | Read-only | Full cluster health sweep — diagnose before acting |
| `kube-fix.py` | Remediation | Control-plane VIP drop + kube-controller-manager backoff |
| `salt-stabilize.py` | Remediation | Salt RAAS / salt-master / salt-minion crash recovery |
| `vodap-fix.py` | Remediation | ClickHouse TLS cert staleness + fluentd buffer disk full |

---

## `vsp-health.py` — Comprehensive Health Check

Full read-only diagnostic sweep across all VSP components. Run this first to
understand what's wrong before running any remediation script.

### Sections checked

| Section | What is checked |
|---------|-----------------|
| `cp` | VIP reachable, `kube-vip.yaml` manifest (`vip_preserve_on_leadership_loss`), `crictl ps` for etcd / kube-controller-manager / kube-scheduler / kube-vip |
| `nodes` | Every node: Ready state, SchedulingDisabled |
| `pods` | All pods in ALL namespaces — one line per namespace (`ready/total`), unhealthy pods listed inline with reason |
| `vcf` | All 25 VCF-managed workloads (`vcfcomponents`) — `spec.replicas` vs `readyReplicas`, detects scaled-to-0 |
| `postgres` | All Zalando Spilo instances — container readiness, suspended CRD label |
| `redis` | Redis pod readiness + `redis-service` endpoint population |
| `salt` | salt-master / salt-minion readiness + log tail for SSL / RAAS stop errors |
| `certs` | cert-manager `Certificate` resources — Ready condition + days to expiry |
| `argo` | Stale `system-shutdown-*` Argo Workflows in `vmsp-platform` |

### Usage

```bash
# Full health check — all sections (~20s)
python3 vsp-health.py

# Single section for faster targeted checks
python3 vsp-health.py --section pods
python3 vsp-health.py --section vcf
python3 vsp-health.py --section salt
python3 vsp-health.py --section certs

# Verbose — per-pod issue detail and raw kubectl snippets
python3 vsp-health.py --verbose
python3 vsp-health.py --section pods --verbose

# JSON output for scripting
python3 vsp-health.py --json 2>/dev/null | jq .healthy

# When the VIP (10.1.1.142) is down — specify a CP node IP directly
python3 vsp-health.py --host 10.1.1.143
```

---

## `kube-fix.py` — Control Plane Fix

Remediates Kubernetes control-plane instability caused by kube-vip VIP drops
and kube-controller-manager CrashLoopBackOff after cold boot.

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

1. **VIP restore** — if `10.1.1.142` is unreachable: `ip addr add` + gratuitous ARP
2. **kube-vip manifest patch** — sets `vip_preserve_on_leadership_loss=true`
   (persistent — survives reboots)
3. **kube-controller-manager reset** — `crictl rm -f` clears the 5-minute back-off
4. **kube-scheduler reset** — same `crictl rm -f` approach
5. **Verification** — VIP ping, `crictl ps`, `kubectl get nodes`

### Usage

```bash
python3 kube-fix.py                          # all fixes
python3 kube-fix.py --dry-run                # preview only
python3 kube-fix.py --skip-vip --skip-kcm   # manifest patch only
python3 kube-fix.py --host 10.1.1.143        # when VIP is down
```

---

## `salt-stabilize.py` — Salt Stack Remediation

Remediates the Salt infrastructure (salt-raas + salt namespaces) after a cold
boot or an unexpected failure. Mirrors the fix that `VCFfinal.py` v6.3.22+
applies automatically at startup.

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

1. Check `pgdatabase-0` — if < 3/3 ready, fix pgdata permissions via `walg` sidecar
2. `kubectl rollout restart deployment/redis` (salt-raas) → wait 30 s
3. `kubectl rollout restart deployment/raas`  (salt-raas) → wait 60 s
4. `kubectl rollout restart deployment/salt-master` (salt) → wait 45 s
5. `kubectl rollout restart deployment/salt-minion` (salt)
6. Final readiness check + log scan

### Usage

```bash
python3 salt-stabilize.py            # full remediation
python3 salt-stabilize.py --dry-run  # preview only
python3 salt-stabilize.py --verbose  # show kubectl output
```

---

## `vodap-fix.py` — Vodap ClickHouse TLS + Fluentd Disk Fix

Addresses two long-running lab issues that `VCFfinal.py` v6.3.22+ also handles
proactively at startup.

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

1. Read `vcf-obs-clickhouse-cert` from the Kubernetes secret; decode cert
2. Check cert expiry and whether cert was recently renewed
3. Connect to ClickHouse (`openssl s_client`) and compare served cert vs. secret cert
4. Check whether vodap client deployments are at full readyReplicas
5. If mismatch detected: restart ClickHouse StatefulSet, wait up to 180 s
6. Check `logging-operator-fluentd-0` container readiness
7. If `< 2/2` ready: exec into container, check `/buffers` disk usage and backup file count
8. Purge `/buffers/backup/*` if disk > threshold; poll for readiness probe to pass

### Usage

```bash
python3 vodap-fix.py             # full diagnosis + remediation
python3 vodap-fix.py --dry-run   # preview only (no changes made)
python3 vodap-fix.py --verbose   # show raw kubectl output per step
```

---

## Recommended Runbooks

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
#    cp / nodes issues   → kube-fix.py
#    salt / redis issues → salt-stabilize.py
#    vodap / fluentd     → vodap-fix.py

# 3. Verify
python3 vsp-health.py
```
