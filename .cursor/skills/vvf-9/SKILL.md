---
name: VVF 9.1 Lab Support
description: >
  Guide for VMware Validated Foundation (VVF) 9.1 dual-site lab environments on
  Holodeck nested virtualization. Covers component inventory, startup/shutdown flows,
  VSP cluster management, power ordering rules, vcf_services_runtime_shutdown.sh
  integration, confighol-9.1.py VVF detection, and VVF-specific pitfalls.
  Use when working with VVF labs, VVF startup/shutdown scripts (VVF.py, VVFfinal.py,
  VVFshutdown.py), license server power ordering, VSP 5480 API health checks,
  Fleet endpoint verification, or any dual-site VVF 9.1 lab operation.
keywords:
  - VVF
  - VMware Validated Foundation
  - VVF 9.1
  - dual-site
  - license server
  - VSP cluster
  - VVF shutdown
  - VVF startup
  - vvfmgmtcluster
  - vvfpostedgevms
  - vspcontrolplaneips
  - vcf_services_runtime_shutdown
  - govc VVF
  - VVFfinal
  - VVFshutdown
  - fleet-01a
  - fleet-01b
version: "1.0"
date: 2026-06-01
---

# VVF 9.1 Lab Support Skill

## Overview

VVF (VMware Validated Foundation) 9.1 is a leaner VCF derivative. It includes:

- **vCenter** (one per site, management only)
- **VCF Operations** (ops-a / ops-b, one per site)
- **VSP Platform** cluster (hosts Fleet LCM, depot, SDDC build components)
- **License servers** (one per site — critical power ordering constraint)
- **ESXi hosts** (4 per site — vSAN management cluster)

VVF does **NOT** include: SDDC Manager, NSX, VCF Automation (Aria Automation),
Tanzu Kubernetes Grid, or Workload Domain NSX Edges.

---

## Component Inventory (Dual-Site)

### Site A

| Role | Hostname / IP | Notes |
| --- | --- | --- |
| vCenter | `vc-mgmt-a.site-a.vcf.lab` | SSO: `administrator@vsphere.local` |
| VCF Operations | `ops-a.site-a.vcf.lab` | SSH: root |
| License Server | `license-a` (VM display name) | Must be on before vCenter |
| VSP VMs | `vsp-01a-*` (regex) | Hosts Fleet K8s cluster |
| VSP Control Plane VIP | `10.1.1.142` | Port 5480 management API |
| Fleet endpoint | `fleet-01a.site-a.vcf.lab` | Fleet LCM REST API |
| ESXi hosts | `esx-01a` – `esx-04a.site-a.vcf.lab` | vSAN cluster `cluster-mgmt-01a` |
| vSAN datastore | `vsan-mgmt-01a` | |

### Site B

| Role | Hostname / IP | Notes |
| --- | --- | --- |
| vCenter | `vc-mgmt-b.site-b.vcf.lab` | SSO: `administrator@vsphere.local` |
| VCF Operations | `ops-b.site-b.vcf.lab` | SSH: root |
| License Server | `license-b` (VM display name) | Must be on before vCenter |
| VSP VMs | `vsp-01b-*` (regex) | Hosts Fleet K8s cluster |
| VSP Control Plane VIP | `10.2.1.142` | Port 5480 management API |
| Fleet endpoint | `fleet-01b.site-b.vcf.lab` | Fleet LCM REST API |
| ESXi hosts | `esx-01b` – `esx-04b.site-b.vcf.lab` | vSAN cluster `cluster-mgmt-01b` |
| vSAN datastore | `vsan-mgmt-01b` | |

### VSP Cluster K8s Components (per site)

| Namespace | Resource | Kind |
| --- | --- | --- |
| `telemetry` | `telemetry-acceptor` | Deployment |
| `vcf-fleet-depot` | `depot-service` | Deployment |
| `vcf-fleet-depot` | `distribution-service` | Deployment |
| `vcf-fleet-lcm` | `vcf-fleet-build-service-fleetbuild` | Deployment |
| `vcf-fleet-lcm` | `vcf-fleet-upgrade-service-fleetupgrade` | Deployment |
| `vcf-fleet-lcm` | `vcf-fleet-lcm-db` | StatefulSet |
| `vcf-sddc-lcm` | `vcf-sddc-build-service-sddcbuild` | Deployment |
| `vcf-sddc-lcm` | `vcf-sddc-upgrade-service-sddcupgrade` | Deployment |
| `vcf-sddc-lcm` | `vcf-sddc-lcm-db` | StatefulSet |

---

## Config File

The base config for VVF 9.1 dual-site is `holodeck/VVF-91-ALL.ini`.
Key sections:

- `[VVF]` — infrastructure components (replaces `[VCF]` in VCF configs)
- `[VVFFINAL]` — final startup tasks (replaces `[VCFFINAL]`)
- `[SHUTDOWN]` — vSAN and host shutdown settings (shared structure with VCF)

### VVF vs VCF Detection Pattern (Python)

```python
is_vcf = lsf.config.has_section('VCF')
is_vvf = lsf.config.has_section('VVF') and not is_vcf
```

---

## Power Ordering Rule (CRITICAL)

**License servers are tied to vCenter licensing.** Violating this order causes
vCenter to become unlicensed or fail to start.

| Event | Order |
| --- | --- |
| Startup | License server ON → vCenter ON |
| Shutdown | vCenter OFF → License server OFF |

**Implementation:**

- Startup: `VVF.py` Task 4b powers on license VMs via **ESXi direct connect**
  before vCenter starts (Task 5).
- Shutdown: `VVFshutdown.py` Phase 5 shuts down vCenter via ESXi direct, then
  Phase 6 shuts down license VMs also via ESXi direct — **never via vCenter API**.

---

## Startup Flow

```plain
ESXi.py → VVF.py → vSphere.py → pings.py → services.py → Kubernetes.py → VVFfinal.py → final.py
```

### VVF.py Tasks

| Task | Description |
| --- | --- |
| Task 1 | Connect to ESXi hosts directly; fail lab if all fail |
| Task 2 | Verify vSAN datastores (`vvfmgmtdatastore`) |
| Task 3 | Exit maintenance mode (all ESXi hosts) |
| Task 4b | Power on license VMs (`vvfpostedgevms`) via ESXi direct — BEFORE vCenter |
| Task 5 | Power on vCenter VMs (`vvfvCenter`) via ESXi direct |
| Skip | NSX Manager / NSX Edges: skipped for VVF (marked in dashboard) |

### VVFfinal.py Tasks

| Task | Description |
| --- | --- |
| Task 1 | Verify/power on VSP Platform VMs (`vvfvspvms`) via vCenter |
| Task 2 | Poll `https://<vip>:5480/api/v1/system/inventory/nodes` until healthy |
| Task 3 | K8s cert check/renewal (non-fatal, streams `vsp_cert_renewer.py`) |
| Task 4 | Verify component URLs (`vcfcomponenturls`) — HTTP 200 or 401 accepted |

---

## Shutdown Flow (8 Phases)

Implemented in `Shutdown/VVFshutdown.py`. Auto-selected by `Shutdown.py` when
`[VVF]` section is present and `[VCF]` is absent.

```plain
Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7 → Phase 8
```

| Phase | Description |
| --- | --- |
| 1 | Call `vcf_services_runtime_shutdown.sh` per site VIP (graceful K8s drain + VSP VM power-off via govc) |
| 2 | Connect to vCenters (still running; VSP nodes already off) |
| 3 | Shutdown VCF Operations VMs (`ops-a`, `ops-b`) via vCenter |
| 4 | Establish ESXi **direct** connections (BEFORE vCenter shutdown) |
| 5 | Shutdown vCenter VMs via ESXi direct (NOT via vCenter API) |
| 6 | Shutdown license VMs via ESXi direct — **ONLY AFTER vCenter is off** |
| 7 | vSAN Elevator operations (OSA only; ESA auto-detected and skipped) |
| 8 | Shutdown ESXi hosts |

---

## VSP Shutdown Script Integration

Uses Broadcom-provided `Tools/vcf_services_runtime_shutdown.sh`. Called once per
site VIP. Handles graceful K8s workload drain **and** VSP VM power-off via `govc`.

### Invocation Pattern

```python
import subprocess, os

password = open('/home/holuser/creds.txt').read().strip()
env = os.environ.copy()
env['VMSP_PASSWORD'] = password           # VSP node breakglass password
env['VCENTER_USERNAME'] = 'administrator@vsphere.local'
env['VCENTER_PASSWORD'] = password        # same lab password
# GOVC_URL is auto-discovered by the script from VSP component config

result = subprocess.run(
    [
        '/home/holuser/hol/Tools/vcf_services_runtime_shutdown.sh',
        '--node-ip', vsp_vip,           # 10.1.1.142 (site-a) or 10.2.1.142 (site-b)
        '--skip-snapshot-check',        # lab environments commonly have snapshots
    ],
    env=env, text=True
)
```

### Key Notes

- `govc` v0.37.1+ is installed at `/home/holuser/.local/bin/govc`.
- **No `--skip-poweroff` flag** — script handles VSP VM power-off via govc directly.
- Script sets a **power-off-marker** on shutdown → components auto-recover on next VSP boot.
- This eliminates the need for a separate "scale-up deployments" step in `VVFfinal.py`.
- The script is called with `VCENTER_USERNAME` / `VCENTER_PASSWORD` env vars;
  `GOVC_URL` is discovered automatically from the VSP component config.

---

## VSP 5480 Management API

Used by `VVFfinal.py` Task 2 to verify VSP cluster health after startup.

```python
import urllib.request, ssl, json, base64

vsp_vip = '10.1.1.142'   # or 10.2.1.142 for site-b
password = open('/home/holuser/creds.txt').read().strip()

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

token = base64.b64encode(f'admin:{password}'.encode()).decode()
req = urllib.request.Request(
    f'https://{vsp_vip}:5480/api/v1/system/inventory/nodes',
    headers={'Authorization': f'Basic {token}'}
)
with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
    nodes = json.loads(resp.read())
```

Health indicator: all nodes in `nodes` list have `status == 'Ready'`.
The port-5480 API returns HTTP 200 when the cluster is up.

---

## confighol-9.1.py VVF Support

`Tools/confighol-9.1.py` v2.21+ detects VCF vs VVF and guards VCF-only steps:

| Step | VCF | VVF |
| --- | --- | --- |
| 0a/0b/0c — Vault CA, vCenter CA | ✅ | ✅ |
| 1 — ESXi hosts | ✅ | ✅ |
| 2 — vCenters | ✅ | ✅ |
| 3 — NSX (Managers and Edges) | ✅ | ⛔ skipped |
| 4 — SDDC Manager | ✅ | ⛔ skipped |
| 5 — VCF Automation VMs | ✅ | ⛔ skipped |
| 6 — Operations VMs | ✅ | ✅ |
| 7 — SDDC Manager auto-rotate disable | ✅ | ⛔ skipped |
| 8 — Fleet Password Policy | ✅ | ✅ |
| 9 — Proxy / NO_PROXY config | ✅ | ✅ |
| 10 — K8s cert pre-provisioning | ✅ | ✅ |
| 11 — Spherelet cert pre-provisioning | ✅ | ✅ |
| 12 — Final cleanup | ✅ | ✅ |

Detection code (in `main()` after `lsf.init()`):

```python
is_vcf = lsf.config.has_section('VCF')
is_vvf = lsf.config.has_section('VVF') and not is_vcf
```

---

## Status Dashboard Integration

`Tools/status_dashboard.py` includes a `vvffinal` task group:

| Task ID | Label |
| --- | --- |
| `vsp_vms` | Verify/Start VSP Platform VMs |
| `vsp_api_health` | Wait for VSP API Health (port 5480) |
| `k8s_certs` | K8s Certificate Check/Renewal |
| `vcf_component_urls` | Verify VCF Component URLs |

VVF startup phase `('vvf', '4. VVF Startup (VVF.py)')` includes:

- `exit_maintenance` — Exit maintenance mode

---

## Script Locations

| Script | Purpose |
| --- | --- |
| `Startup/VVF.py` | VVF infrastructure startup (ESXi, license VMs, vCenter) |
| `Startup/VVFfinal.py` | VSP cluster startup and component health verification |
| `Shutdown/VVFshutdown.py` | 8-phase graceful VVF shutdown |
| `Shutdown/Shutdown.py` | Orchestrator — auto-routes to VVFshutdown when `[VVF]` detected |
| `Tools/confighol-9.1.py` | HOLification — VCF+VVF aware (v2.21+) |
| `Tools/vcf_services_runtime_shutdown.sh` | Broadcom script: graceful K8s drain + VSP VM power-off |
| `holodeck/VVF-91-ALL.ini` | Base config for VVF 9.1 dual-site labs |

---

## Critical Pitfalls

1. **License server before vCenter**: License VMs MUST be running before vCenter is started, and MUST remain running until vCenter is fully shut down. Power operations always use direct ESXi connections — never vCenter API.

2. **VVF has no SDDC Manager**: Do not call SDDC Manager APIs (`/v1/credentials`, `/v1/tokens`, etc.) in VVF scripts. The `confighol-9.1.py` detection guards these automatically.

3. **VVF has no NSX**: NSX Manager REST API, NSX Edge SSH, and NSX trust-management calls are VCF-only. The `confighol-9.1.py` Step 3 is skipped for VVF.

4. **VSP 5480 API credentials**: Use `admin:<creds.txt-password>` Basic Auth against `https://<vip>:5480`. This is the VSP cluster management API, not the K8s API server.

5. **govc is required for `vcf_services_runtime_shutdown.sh`**: The script uses `govc` to power off VSP VMs. Confirm `govc` is installed at `/home/holuser/.local/bin/govc`. Pass `VCENTER_USERNAME` and `VCENTER_PASSWORD` as env vars.

6. **Power-off-marker enables auto-recovery**: `vcf_services_runtime_shutdown.sh` writes a marker file that triggers automatic K8s component recovery when VSP VMs boot. `VVFfinal.py` only needs to wait for the port-5480 API to respond — no manual `kubectl scale` commands needed.

7. **`[VVF]` section — not labtype**: VVF is not a lab delivery type (HOL, DISCOVERY, etc.) and must not be added to `LABTYPE_INFO` in `labtypes.py`. Detection is via config section presence only.

8. **Dual-site VIPs both need shutdown script**: Call `vcf_services_runtime_shutdown.sh` once for `10.1.1.142` (site-a) and once for `10.2.1.142` (site-b). Both calls are independent.

9. **`VVFfinal` in labtypes.py `_DEFAULT_SEQUENCE`**: `VVFfinal` must appear in `_DEFAULT_SEQUENCE` after `VCFfinal` so it runs during normal startup. For VCF labs without `[VVFFINAL]` in config, `VVFfinal.main()` exits immediately.
