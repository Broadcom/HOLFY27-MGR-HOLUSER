# HOL Lab Shutdown Scripts

Version 2.0 - February 2026

## Overview

This folder contains the graceful shutdown orchestration scripts for HOLFY27 lab environments. The scripts ensure an orderly shutdown of all VCF components following the **official Broadcom VCF 9.0/9.1 documentation**.

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
On the router, login as root and run the /root/shutdown.sh script - This was written to perform a graceful shutdown of the kubernetes environment, services, etc...

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

# Quick shutdown (skip vSAN wait)
python3 Shutdown.py --quick

# Shutdown VMs only, leave hosts running
python3 Shutdown.py --no-hosts

# Run a single VCF shutdown phase
python3 Shutdown.py --phase 1

# Preview a single phase
python3 Shutdown.py --phase 13 --dry-run

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
| VCF 9.1 | `ops-a` via `/suite-api/internal/components/` | OpsToken | Component-based (VCFA, NI, OPS, LI) |
| VCF 9.0 | `opslcm-a` via `/lcm/lcops/api/v2/` | Basic (base64) | Environment/product-based (vra, vrni) |

VCF 9.1 component type mapping:

| Product | Component Type | Description |
| ------- | -------------- | ----------- |
| vra | VCFA | VCF Automation |
| vrni | NI | Operations for Networks |
| vrops | OPS | VCF Operations |
| vrli | LI | Operations for Logs |
| vrlcm | FLEET_LCM | Fleet Lifecycle Manager |

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
| Phase 1 | Docker Containers | Stop Docker containers |
| Phase 2 | VCF Environment Shutdown | Calls VCFshutdown.py |
| Phase 3 | Final Cleanup | Disconnect vSphere sessions |
| Phase 4 | Wait for Host Power Off | Ping monitoring (15s, 30min max) |
| **VCF Shutdown (VCFshutdown.py)** | | |
| Phase 1 | Fleet Operations | VCF Ops Suite via API (vra, vrni, vrops, vrli) |
| Phase 1b | VCF Automation VM fallback | Only if Fleet API failed |
| Phase 2 | Connect to vCenters | vCenters first (while available) |
| Phase 2b | Scale Down VCF Components | K8s workloads on VSP |
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
| Phase 18 | Host Settings | Set ESXi advanced settings |
| Phase 19 | vSAN Elevator | Enable elevator, wait 45min, disable (OSA only) |
| Phase 19b | VSP Platform VMs | Shutdown VSP VMs |
| Phase 19c | Pre-ESXi Audit | Find and shutdown straggler VMs |
| Phase 20 | ESXi Hosts | Shutdown ESXi hosts |

### Key Design Decisions

1. **vCenters connected first (Phase 2)**: While vCenters are still running, all VM shutdown operations (Phases 3-17) go through vCenter for reliable VM discovery and graceful shutdown. ESXi host direct connections only happen after vCenters are shut down (Phase 17b).

2. **Phase 1b follows Phase 1**: If the Fleet API cannot shut down VCF Automation, the fallback VM shutdown (Phase 1b) runs immediately after, before connecting to infrastructure (Phase 2).

3. **VCF Components vs VMs**: In VCF 9.1, many services (opslogs-a, opslcm-a, Identity Broker, etc.) run as Kubernetes workloads on VSP, not as standalone VMs. These are handled by Phase 2b (K8s scale-down) and Phase 1 (Fleet API), not by VM lookup phases. Phases 10-12 only target actual VMs when configured in `config.ini`.

4. **Fleet API uses suite-api internal endpoint**: The VCF 9.1 Fleet LCM plugin API at `/vcf-operations/plug/fleet-lcm/v1/` requires a UI session. Instead, the script uses `/suite-api/internal/components/` with OpsToken authentication (same pattern as the password management API). The shutdown *action* (`POST ?action=shutdown`) returns HTTP 500 through this proxy, so Phase 1 performs component discovery only - actual shutdown is handled by VM power-off in Phase 1b and subsequent phases.

## Command-Line Options

### Shutdown.py Options

| Option | Short | Description |
| ------ | ----- | ----------- |
| `--dry-run` | `-n` | Preview mode - show what would be done without making changes |
| `--quick` | `-q` | Skip the 45-minute vSAN elevator wait (faster but less safe) |
| `--no-hosts` | | Skip ESXi host shutdown (leave hosts running) |
| `--phase PHASE` | `-p` | Run only a specific VCF shutdown phase |
| `--version` | `-v` | Show version number |
| `--debug` | `-d` | Enable debug logging |

### VCFshutdown.py Options

| Option | Short | Description |
| ------ | ----- | ----------- |
| `--dry-run` | | Preview mode |
| `--standalone` | | Run in standalone test mode |
| `--skip-init` | | Skip lsf.init() call |
| `--phase PHASE` | `-p` | Run only a specific phase |

### Using --phase for Selective Shutdown

The `--phase` parameter allows you to run a single shutdown phase at a time. This is useful for:

- **Debugging**: Test a specific phase without running the entire sequence
- **Selective operations**: Shut down only certain components
- **Recovery**: Re-run a failed phase without repeating completed ones

```bash
# Shut down only Fleet Operations (VCF Automation, Networks, etc.)
python3 Shutdown.py --phase 1

# Preview what Phase 13 (VCF Operations) would do
python3 Shutdown.py --phase 13 --dry-run

# Scale down VCF Component Services on VSP
python3 Shutdown.py --phase 2b

# Shut down only SDDC Manager
python3 Shutdown.py --phase 16

# Run the vSAN elevator operations
python3 Shutdown.py --phase 19

# Run the pre-ESXi audit to find straggler VMs
python3 Shutdown.py --phase 19c

# Not passing --phase runs all phases in order (default behavior)
python3 Shutdown.py
```

When `--phase` is specified:

- Only the selected VCF shutdown phase executes
- Infrastructure connections (Phase 2) are set up automatically if needed
- Configuration is always read so the selected phase has all required data
- Docker containers, final cleanup, and host power-off monitoring are skipped

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

1. Enable `plogRunElevator` on all hosts (flushes write cache)
2. Wait 45 minutes for vSAN to complete all I/O
3. Disable `plogRunElevator` on all hosts
4. Proceed with host shutdown

**vSAN ESA** (Express Storage Architecture) does NOT use the plog mechanism and the elevator wait is automatically skipped.

The `--quick` flag skips this wait period but may result in data loss if vSAN has pending operations.

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

- Use `--quick` flag for faster (but less safe) shutdown
- Or set `vsan_timeout = 0` in config

### Hosts not shutting down

- Verify ESXi SSH is enabled
- Use `--no-hosts` to skip host shutdown

### SSH host key errors

The scripts use `StrictHostKeyChecking=no` and `UserKnownHostsFile=/dev/null` to handle host key changes common in lab environments.

## Support

For issues with the shutdown scripts, contact the HOL Core Team.
