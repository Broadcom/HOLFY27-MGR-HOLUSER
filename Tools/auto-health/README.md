# auto-health.py — VCF Automation (VCFA) Health Check

Read-only diagnostic sweep of the VCF Automation (VCFA) appliance. Run this
first — before reaching for `vcfa-stabilizer.sh` — to understand what's
actually wrong.

VCFA runs as a single-node Kubernetes cluster (the "auto-platform-a"
appliance) hosting `vmsp-platform` (platform infra: gateways, kube-vip,
cert-manager, CAPI), `vmsp-policies` (Kyverno), and `prelude` (the ~50-70 VCF
Automation microservices: authentication, resource-manager, account-manager,
encryption-manager, intent-server, vcfa-service-manager, etc).

Connects via SSH using `sshpass` and the lab password from
`/home/holuser/creds.txt`, the same way `vcfa-stabilizer.sh` does (sudo on the
VCFA node requires a password on VCF 9.1, unlike the NOPASSWD sudo on 9.0).

## Sections checked

| Section | What is checked |
|---------|-----------------|
| `cp` | VIP pinning (`.69`/`.70`/`.72` on eth0, non-deprecated), `vcfa-vip-watchdog.service`, API server `/healthz`, `plndr-cp-lock` death-spiral check (`<10s` only — see below) |
| `nodes` | Node Ready state, SchedulingDisabled |
| `pods` | All pods in ALL namespaces — one line per namespace (`ready/total`), unhealthy pods listed inline with reason |
| `core` | VCFA core components in `vmsp-platform` + `vmsp-policies` (gateways, kube-vip dataplane, CAPI IPAM, cert-manager/trust-manager, kyverno) |
| `auth` | Authentication/identity microservices in `prelude` (authentication-server, resource-manager-server, account-manager-server, encryption-manager, intent-server, vcfa-service-manager) |
| `gateway` | kube-vip LoadBalancer Services (`vcfa-gateway-configuration`, `vmsp-gateway`) exist with the expected VIP ingress IP |
| `endpoint` | `/automation` HTTP probe run from the VCFA node itself, expect HTTP 200 |
| `certs` | cert-manager `Certificate` resources — Ready condition + days to expiry |
| `argo` | Stale `system-shutdown-*` Argo Workflows in `vmsp-platform` |
| `etcd` | Defrag slack % — informational only, this tool never defrags |

## Usage

```bash
# Full health check — all sections (~10-20s)
python3 auto-health.py

# Single section for faster targeted checks
python3 auto-health.py --section cp
python3 auto-health.py --section pods
python3 auto-health.py --section certs

# Verbose — per-item detail and raw command output
python3 auto-health.py --verbose
python3 auto-health.py --section pods --verbose

# JSON output for scripting (checks/summary are appended after the normal
# printed output, same convention as vsp-health.py)
python3 auto-health.py --json 2>/dev/null

# Specify an alternate VCFA node IP
python3 auto-health.py --host 10.1.1.73
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed |
| 1 | One or more checks failed |
| 2 | Cannot connect to the VCFA node |

## Known deviations from vcfa-stabilizer.sh's assumptions

Verified against the live pod (2026-07-14):

- `kyverno-admission-controller`/`kyverno-background-controller`/
  `kyverno-cleanup-controller` live in **`vmsp-policies`**, not
  `vmsp-platform` (the stabilizer's `get_system_status()` greps them
  alongside `vmsp-platform` pods).
- There is **no separate `cert-manager` namespace** on this build —
  `cert-manager`, `cert-manager-cainjector`, `cert-manager-webhook`, and
  `trust-manager` all run inside `vmsp-platform`.
- `trust-manager-sds-server` was **not found** on this build/pod at all (no
  such pod/Deployment in any namespace). The check degrades to a WARN
  ("not found — may not be deployed") rather than a FAIL, since other VCFA
  builds may still ship it.
- The `prelude` authentication Deployment is named `authentication-server`
  (not bare `authentication`).

## v1.1 — plndr-cp-lock lease-duration check removed

v1.0 flagged `plndr-cp-lock` Lease `leaseDurationSeconds != 120` as a WARN
("stuck at chart default, not yet hardened"). Live investigation on this pod
(2026-07-14) showed the manifest correctly had `vip_leaseduration="120"` set
and *unchanged* for over an hour, with zero `leaseTransitions` and the same
Lease `uid`/`creationTimestamp` surviving 3 kube-vip pod restarts — yet
`spec.leaseDurationSeconds` stayed at `15` the entire time. Running
`vcfa-stabilizer.sh`'s old force-patch-to-120 step "succeeded" but reverted
within one kube-vip renewal cycle (~10-15s), because kube-vip v1.0.2's
leaderelection renewal path re-writes its own hardcoded 15s default into the
Lease object on every renewal — it never reads `vip_leaseduration` for that
field. On this single-node control plane (nothing else ever contends for
`plndr-cp-lock`), `15s` vs `120s` has zero operational consequence — it isn't
a real problem, so v1.1 stopped reporting it as one.

The tool (and `vcfa-stabilizer.sh` v2.11+) now only checks for the genuine
`<10s` "death-spiral" signature (originally observed as `=1` during the Apr
2026 control-plane overload incident) as a FAIL — a real corruption signal,
distinct from the harmless chart-default steady state. `vip_renewdeadline`/
`vip_retryperiod`/`vip_preserve_on_leadership_loss` are not known to have the
same ignored-env-var problem and are still actively hardened/checked.

Remediation for anything flagged: `bash vcfa-stabilizer.sh`
