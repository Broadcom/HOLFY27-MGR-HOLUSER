# HOL Lab Shutdown Scripts

Version 2.3 - 2026-04-27

## Overview

This folder contains the graceful shutdown orchestration scripts for HOLFY27 lab environments. Based on/inspired by the great work of Christopher Lewis on the FY26 Shutdown scripts. The scripts ensure an orderly shutdown of all VCF components following the **official Broadcom VCF 9.0/9.1 documentation**.

**Reference**: [VCF 9.0 Shutdown Operations](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/fleet-management/vcf-shutdown-and-startup/vcf-shutdown.html)

## Quick-start to Shutdown pod

### Shutdown procedure

1) Shutdown nested environment:

    ```bash
    ssh holuser@manager
    cd hol
    python3 Shutdown/Shutdown.py
    ```

    Wait until complete.

2) Shutdown router as follows:

   ```bash
    # login as root and run 
    /root/shutdown.sh
    # This was written to perform a graceful shutdown of 
    # the kubernetes environment, services, etc...
    ```

3) Shutdown manager

4) Shutdown console

## Scripts

### Shutdown.py (Main Orchestrator)

The main entry point for lab shutdown. Coordinates all shutdown phases and provides command-line options.

```bash
# Full shutdown (all phases)
python3 Shutdown.py

# Preview mode (no changes made)
python3 Shutdown.py --dry-run

# Quick shutdown (skip vSAN elevator)
python3 Shutdown.py --quick

# Shutdown VMs only, leave hosts running
python3 Shutdown.py --no-hosts

# Run a single VCF shutdown phase
python3 Shutdown.py --phase 1

# Preview a single phase
python3 Shutdown.py --phase 13 --dry-run

# Multiple VCF phases in one process (prerequisites such as Phase 2 / 17b auto-inserted)
python3 Shutdown.py --phases 2,8

# Phase 1 with only selected suite products (overrides [SHUTDOWN] fleet_products for this run)
python3 Shutdown.py --phase 1 --fleet-products vra
python3 Shutdown.py --phase 1 --fleet-products vra,vrni
python3 Shutdown.py --phase 1 --fleet-products vrops,vrli
python3 Shutdown.py --phase 1 --fleet-products vra,vrni,vrops,vrli,vrlcm

# Show help (includes full phase list)
python3 Shutdown.py --help
```

### VCFshutdown.py (VCF Module)

Handles VCF-specific shutdown tasks including:

- Fleet Operations (VCF Operations Suite) via suite-api internal components API
- WCP (Workload Control Plane) shutdown
- Tanzu/Kubernetes workload VMs
- Management VMs (vCenter, SDDC Manager, VCF Operations Suite)
- NSX Edges and Manager
- vSAN elevator operations
- ESXi host shutdown

Can be run standalone:

```bash
# Full shutdown
python3 VCFshutdown.py

# Preview all phases
python3 VCFshutdown.py --dry-run

# Run a single phase
python3 VCFshutdown.py --phase 1

# Preview a single phase
python3 VCFshutdown.py --phase 1 --dry-run
```

### fleet.py (Fleet Operations Module)

Provides integration with VCF Operations Manager APIs for component discovery and graceful shutdown of VCF Operations Suite products.

Supports two API versions:

| Version | Endpoint | Auth | API Style |
| ------- | -------- | ---- | --------- |
| VCF 9.1 (primary) | `fleet-01a` via `/fleet-lcm/v1/` | JWT (VSP Identity) | Component types: `VCFA`, `OPS_NETWORKS`, `OPS`, `OPS_LOGS`, `VCF_FLEET_LCM` |
| VCF 9.1 (fallback) | `ops-a` via `/suite-api/internal/components/` | OpsToken | Same logical products; shutdown action may return HTTP 500 |
| VCF 9.0 | `opslcm-a` via `/lcm/lcops/api/v2/` | Basic (base64) | Environment/product-based (vra, vrni) |

VCF 9.1 product → component type mapping:

| Product | suite-api `componentType` | fleet-lcm `componentType` | Description |
| ------- | --------------------------- | ---------------------------- | ----------- |
| vra | VCFA | VCFA | VCF Automation |
| vrni | NI | OPS_NETWORKS | Operations for Networks |
| vrops | OPS | OPS | VCF Operations |
| vrli | LI | OPS_LOGS | Operations for Logs |
| vrlcm | FLEET_LCM | VCF_FLEET_LCM | Fleet Lifecycle Manager |

Can be tested standalone:

```bash
# Probe for VCF 9.1 API availability
python3 fleet.py --fqdn ops-a.site-a.vcf.lab --password PASSWORD --action probe

# List components (VCF 9.1)
python3 fleet.py --fqdn ops-a.site-a.vcf.lab --password PASSWORD --version 9.1 --action list

# Shutdown products (VCF 9.1)
python3 fleet.py --fqdn ops-a.site-a.vcf.lab --password PASSWORD --version 9.1 --action shutdown --products vra vrni

# List environments (VCF 9.0)
python3 fleet.py --fqdn opslcm-a.site-a.vcf.lab --password PASSWORD --action list
```

## Shutdown Order

The shutdown follows the **official Broadcom VCF 9.0/9.1 documentation**.

### VCF 9.0 Workload Domain Order

Per [VCF 9.0 Workload Domain Shutdown](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/fleet-management/vcf-shutdown-and-startup/vcf-shutdown/shut-down-the-virtual-infrastructure-workload-domain.html):

| VCF 9.0 Order | Component |
| ------------- | --------- |
| 1 | Virtualized customer workloads |
| 2 | VMware Live Recovery (if applicable) |
| 4 | NSX Edge nodes |
| 5 | NSX Manager nodes |
| 7 | ESX hosts |
| 8 | vCenter Server (LAST for workload domain) |

### VCF 9.0 Management Domain Order

Per [VCF 9.0 Management Domain Shutdown](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/fleet-management/vcf-shutdown-and-startup/vcf-shutdown/shut-down-the-management-domain.html):

| VCF 9.0 Order | Component |
| ------------- | --------- |
| 1 | VCF Automation (VCF Automation / vra) |
| 2 | VCF Operations for Networks (vrni) |
| 3 | VCF Operations collector |
| 4 | VCF Operations for logs (vrli) |
| 5 | VCF Identity Broker |
| 6 | VCF Operations fleet management (VCF Operations Manager) |
| 7 | VCF Operations (vrops, orchestrator) |
| 8 | VMware Live Site Recovery (if applicable) |
| 9 | NSX Edge nodes |
| 10 | NSX Manager |
| 11 | SDDC Manager |
| 12 | vSAN and ESX Hosts (includes vCenter shutdown) |

### Implementation Phases

| Phase | Description | Notes |
| ----- | ----------- | ----- |
| **Main Orchestrator (Shutdown.py)** | | |
| Phase 0 | Pre-Shutdown Checks | Check config, detect lab type |
| Phase 0b | Docker Containers | Optional remote Docker stop |
| (module) | VCF Environment Shutdown | Invokes `VCFshutdown.py` (internal phases 1–20) |
| Phase 21 | Final Cleanup | Disconnect vSphere sessions |
| Phase 22 | Wait for Host Power Off | Ping monitoring (15s interval, 30min max) |
| **VCF Shutdown (VCFshutdown.py)** | | |
| Phase 1 | Fleet Operations | VCF Ops Suite via API (vra, vrni, vrops, vrli, vrlcm) |
| Phase 1b | VCF Automation VM fallback | Only if Fleet API failed |
| Phase 2 | Connect to vCenters | vCenters first (while available) |
| Phase 2b | Scale Down VCF Components | K8s workloads on VSP |
| Phase 3b | Supervisor workload drain | VKS/Harbor etc. before WCP stop |
| Phase 3 | Stop WCP | Workload Control Plane services |
| Phase 4 | Workload VMs | Tanzu, K8s, Supervisor VMs |
| Phase 5 | Workload NSX Edges | Workload domain NSX Edges |
| Phase 6 | Workload NSX Manager | Workload domain NSX Manager |
| Phase 7 | Workload vCenters | Workload vCenters (LAST per VCF 9.0) |
| Phase 8 | VCF Ops Networks VMs | VCF Operations for Networks VMs |
| Phase 9 | VCF Ops Collector VMs | VCF Operations Collector VMs |
| Phase 10 | VCF Ops Logs VMs | VCF Operations for Logs VMs |
| Phase 11 | VCF Identity Broker VMs | VCF Identity Broker VMs |
| Phase 12 | VCF Fleet Mgmt VMs | VCF Operations Fleet Management VMs |
| Phase 13 | VCF Operations VMs | VCF Operations (vrops) VMs |
| Phase 14 | Mgmt NSX Edges | Management NSX Edges |
| Phase 15 | Mgmt NSX Manager | Management NSX Manager |
| Phase 16 | SDDC Manager | SDDC Manager |
| Phase 17 | Mgmt vCenter | Management domain vCenter |
| Phase 17b | Connect to ESXi | Direct ESXi connections (vCenters now down) |
| Phase 17c | Post-Edge VMs | Optional patterns from `[VCF] vcfpostedgevms` |
| Phase 18 | Host Settings | Set ESXi advanced settings |
| Phase 19 | vSAN Elevator | Enable elevator, poll until flush complete, disable (OSA only) |
| Phase 19b | VSP Platform VMs | Shutdown VSP VMs |
| Phase 19c | Pre-ESXi Audit | Find and shutdown straggler VMs |
| Phase 20 | ESXi Hosts | Shutdown ESXi hosts |

### Key Design Decisions

1. **vCenters connected first (Phase 2)**: While vCenters are still running, VM shutdown operations go through vCenter. For selective runs (`--phase` / `--phases`), **Phase 2 (or Phase 17b for ESXi-only inventory)** is auto-inserted when a phase needs vCenter or ESXi vim inventory but you did not list it.

2. **Phase 1b follows Phase 1**: If the Fleet API cannot shut down VCF Automation, the fallback VM shutdown (Phase 1b) runs immediately after Phase 1; Phase 1b can connect to vCenters on its own when needed.

3. **VCF Components vs VMs**: In VCF 9.1, many services run as Kubernetes workloads on VSP. These are handled by Phase 2b (K8s scale-down) and Phase 1 (Fleet API). VM-name phases (8–13, etc.) only target VMs that exist for your build.

4. **Fleet shutdown (VCF 9.1)**: Phase 1 prefers the **fleet-lcm direct API** on the Fleet LCM gateway (JWT via VSP Identity Service) so `POST .../components/{id}?action=shutdown` works. It falls back to suite-api internal components, then VCF 9.0 legacy LCM. Components that already appear inactive are **skipped** with a `SKIP:` log line.

5. **Progress and ETA**: Long waits (Fleet task polling, vSAN elevator, host power-off wait, guest shutdown) emit **at least one line every ~90 seconds** to the console and `shutdown.log`. At startup, an **approximate** total time budget is logged; each phase logs remaining budget (not a SLA).

6. **Idempotent VM shutdown**: VM phases skip guests that are **already powered off** (`SKIP:` in logs).

## Command-Line Options

### Shutdown.py Options

| Option | Short | Description |
| ------ | ----- | ----------- |
| `--dry-run` | `-n` | Preview mode - show what would be done without making changes |
| `--quick` | `-q` | Skip the vSAN elevator entirely (faster but less safe) |
| `--no-hosts` | | Skip ESXi host shutdown (leave hosts running) |
| `--phase PHASE` | `-p` | Run only one VCF shutdown phase (mutually exclusive with `--phases`) |
| `--phases LIST` | | Comma-separated VCF phases in canonical order (auto prerequisites) |
| `--fleet-products` | | Comma-separated product keys for Phase 1 only (overrides config) |
| `--version` | `-v` | Show version number |
| `--debug` | `-d` | Enable debug logging |

### VCFshutdown.py Options

| Option | Short | Description |
| ------ | ----- | ----------- |
| `--dry-run` | | Preview mode |
| `--standalone` | | Run in standalone test mode |
| `--skip-init` | | Skip lsf.init() call |
| `--phase PHASE` | `-p` | Run only one phase (mutually exclusive with `--phases`) |
| `--phases LIST` | | Comma-separated phases (same expansion as `Shutdown.py`) |
| `--fleet-products` | | Phase 1 product list override |

### Selective shutdown (`--phase`, `--phases`, `--fleet-products`)

- **`--phase`**: one internal VCF phase (e.g. `8` for Ops for Networks VMs).
- **`--phases`**: several phases in **one process**, merged with **auto-prerequisites**:
  - VM inventory on vCenter → inserts **`2`** if missing.
  - VM inventory on ESXi (e.g. `19b`, `17c`) → inserts **`17b`** if missing.
  - Phases always run in **canonical VCF order** (not the order tokens appear on the CLI).
- **`--fleet-products`**: applies only to **Phase 1** Fleet shutdown. Standard keys:

| Key | VCF 9.1 suite-api type | fleet-lcm direct type | Component |
| --- | ---------------------- | ---------------------- | --------- |
| `vra` | VCFA | VCFA | VCF Automation |
| `vrni` | NI | OPS_NETWORKS | Operations for Networks |
| `vrops` | OPS | OPS | VCF Operations (vROps) |
| `vrli` | LI | OPS_LOGS | Operations for Logs |
| `vrlcm` | FLEET_LCM | VCF_FLEET_LCM | Fleet / LCM |

```bash
# Fleet Phase 1: one run shuts down the products you list together
python3 Shutdown.py --phase 1 --fleet-products vra
python3 Shutdown.py --phase 1 --fleet-products vrni
python3 Shutdown.py --phase 1 --fleet-products vra,vrni
python3 Shutdown.py --phase 1 --fleet-products vrops
python3 Shutdown.py --phase 1 --fleet-products vrli
python3 Shutdown.py --phase 1 --fleet-products vrlcm
python3 Shutdown.py --phase 1 --fleet-products vra,vrni,vrops,vrli,vrlcm

# VM phases: connect + shutdown Ops for Networks VMs (Phase 2 auto-inserted before 8)
python3 Shutdown.py --phases 8
python3 Shutdown.py --phases 2,8

# Preview Phase 13
python3 Shutdown.py --phase 13 --dry-run

python3 Shutdown.py --phase 2b
python3 Shutdown.py --phase 16
python3 Shutdown.py --phase 19
python3 Shutdown.py --phase 19c

# Full lab shutdown (default)
python3 Shutdown.py
```

When **`--phase`** or **`--phases`** is set:

- Only the listed VCF phases run (plus any auto-inserted prerequisites), then **vSphere sessions are disconnected** inside `VCFshutdown.py` for selective runs.
- `Shutdown.py` skips Docker (0b), orchestrator cleanup (21), and host ping wait (22).
- Config is still loaded so each phase has required settings.

## Configuration

Shutdown behavior can be customized via the `[SHUTDOWN]` section in `vPodRepo/config.ini`:

```ini
[SHUTDOWN]
# Fleet Operations - VCF 9.1 (internal components API via ops-a)
ops_fqdn = ops-a.site-a.vcf.lab
ops_username = admin

# Fleet Operations - VCF 9.0 legacy (SDDC Manager LCM API via opslcm-a)
fleet_fqdn = opslcm-a.site-a.vcf.lab
fleet_username = admin@local
fleet_products = vra,vrni,vrops,vrli

# Docker containers
shutdown_docker = true
docker_host = docker.site-a.vcf.lab
docker_user = holuser
docker_containers = gitlab,ldap,poste.io,flask

# VM regex patterns to find and shutdown (regex)
vm_patterns = ^kubernetes-cluster-.*$
    ^dev-project-.*$
    ^cci-service-.*$
    ^SupervisorControlPlaneVM.*$

# VCF Operations for Networks VMs (vrni) - only actual VMs
vcf_ops_networks_vms = ops_networks-platform-10-1-1-60
    ops_networks-collector-10-1-1-62

# VCF Operations Collector VMs - only actual VMs
vcf_ops_collector_vms = opscollector-01a

# VCF Operations for Logs VMs (vrli) - only actual VMs
# NOTE: opslogs-a is a VCF Component on VSP, NOT a VM. Do not list it here.
#vcf_ops_logs_vms = 

# VCF Identity Broker VMs - only actual VMs
# NOTE: Not deployed as VMs in VCF 9.1
#vcf_identity_broker_vms =

# VCF Operations Fleet Management VMs - only actual VMs
# NOTE: opslcm-a runs on VSP/K8s in VCF 9.1, NOT a standalone VM
#vcf_ops_fleet_vms =

# VCF Operations VMs (vrops) - only actual VMs
vcf_ops_vms = ops-a

# SDDC Manager VMs
sddc_manager_vms = sddcmanager-a

# ESXi settings
esx_username = root
vsan_enabled = true
vsan_timeout = 2700
shutdown_hosts = true
```

### Important: VCF Components vs VMs

In VCF 9.1, many services run as Kubernetes workloads on VSP rather than as standalone VMs. The `[SHUTDOWN]` VM lists should only contain **actual VM names** that exist in vCenter inventory. VCF Component services are managed by:

| Service | How it's managed | NOT a VM |
| ------- | ---------------- | -------- |
| opslogs-a (VCF Ops for Logs) | VCF Component on VSP (Phase 2b) | Do not list in vcf_ops_logs_vms |
| opslcm-a (Fleet LCM) | VCF Component on VSP (Phase 2b) | Do not list in vcf_ops_fleet_vms |
| opsproxy-01a | Service/VIP, not a VM | Do not list in vcf_ops_collector_vms |
| Identity Broker | VCF Component on VSP | Do not list in vcf_identity_broker_vms |
| o11n-01a, o11n-02a | Not deployed in VCF 9.1 | Do not list in vcf_ops_vms |

## vSAN Considerations

Before ESXi hosts can be safely shut down, the vSAN cluster must complete all pending I/O operations. This is done via the "vSAN elevator" process:

1. Enable `plogRunElevator` on all hosts (starts flushing write cache to capacity tier)
2. Poll `/storage/lsom/elevatorRunning` on each host every 30 seconds
3. Once all hosts report `0` (flush complete), disable `plogRunElevator`
4. Proceed with host shutdown

The 45-minute timeout is retained as a safety ceiling, but the script finishes as soon as all hosts complete the flush. In a quiesced lab environment (all VMs already shut down), this typically takes **2-10 minutes** instead of the full 45.

**vSAN ESA** (Express Storage Architecture) does NOT use the plog mechanism and the elevator wait is automatically skipped.

The `--quick` flag skips the elevator entirely but may result in data loss if vSAN has pending operations.

## Dependencies

The shutdown scripts rely on:

- `lsfunctions.py` - Core lab functions
- `pyVmomi` - VMware vSphere API
- `requests` - HTTP API calls
- Standard Python 3.x libraries

## Logging

Shutdown logs are written to:

- `/home/holuser/hol/shutdown.log`
- `/home/holuser/hol/labstartup.log` (Reset)
- Console output (real-time with detailed per-operation progress)

## Troubleshooting

### Fleet Operations fails (VCF 9.1)

- Verify VCF Operations Manager is reachable: `ping ops-a.site-a.vcf.lab`
- Test the components API: `python3 fleet.py --fqdn ops-a.site-a.vcf.lab --password PASSWORD --version 9.1 --action list`
- The suite-api internal components endpoint requires OpsToken auth with `X-vRealizeOps-API-use-unsupported: true`
- If the API returns HTML instead of JSON, the auth header format may be wrong

### Fleet Operations fails (VCF 9.0)

- Verify SDDC Manager LCM is reachable: `ping opslcm-a.site-a.vcf.lab`
- Check credentials
- Test with: `python3 fleet.py --fqdn opslcm-a.site-a.vcf.lab --password PASSWORD --action list`

### VMs not shutting down

- Check VMware Tools status in vCenter
- VMs without Tools will be force powered off after timeout
- Use `--phase 19c` to run the pre-ESXi audit and find straggler VMs

### vSAN timeout too long

- The elevator now uses active polling and finishes as soon as the flush completes (typically 2-10 minutes in a quiesced lab)
- If it still takes too long, use `--quick` flag to skip the elevator entirely (less safe)
- Or set `vsan_timeout = 0` in config to skip

### Hosts not shutting down

- Verify ESXi SSH is enabled
- Use `--no-hosts` to skip host shutdown

### SSH host key errors

The scripts use `StrictHostKeyChecking=no` and `UserKnownHostsFile=/dev/null` to handle host key changes common in lab environments.

## Support

For issues with the shutdown scripts, contact the HOL Core Team.
