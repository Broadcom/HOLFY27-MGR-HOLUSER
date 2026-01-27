# HOL Lab Shutdown Scripts

Version 1.0 - January 2026

## Overview

This folder contains the graceful shutdown orchestration scripts for HOLFY27 lab environments. The scripts ensure an orderly shutdown of all VCF components in the **reverse order** of startup to properly handle dependencies.

## Scripts

### Shutdown.py (Main Orchestrator)

The main entry point for lab shutdown. Coordinates all shutdown phases and provides command-line options.

```bash
# Full shutdown
python3 Shutdown.py

# Preview mode (no changes made)
python3 Shutdown.py --dry-run

# Quick shutdown (skip vSAN wait)
python3 Shutdown.py --quick

# Shutdown VMs only, leave hosts running
python3 Shutdown.py --no-hosts

# Show help
python3 Shutdown.py --help
```

### VCFshutdown.py (VCF Module)

Handles VCF-specific shutdown tasks including:

- Fleet Operations (Aria Suite) via SDDC Manager API
- WCP (Workload Control Plane) shutdown
- Tanzu/Kubernetes workload VMs
- Management VMs (vCenter, SDDC Manager, Aria)
- NSX Edges and Manager
- vSAN elevator operations
- ESXi host shutdown

Can be run standalone:

```bash
python3 VCFshutdown.py --dry-run
```

### fleet.py (Fleet Operations Module)

Provides integration with SDDC Manager Fleet Operations API for graceful shutdown of Aria Suite products:

- vra (Aria Automation)
- vrni (Aria Operations for Networks)
- vrops (Aria Operations)
- vrli (Aria Operations for Logs)

Can be tested standalone:

```bash
python3 fleet.py --fqdn opslcm-a.site-a.vcf.lab --password PASSWORD --action list
python3 fleet.py --fqdn opslcm-a.site-a.vcf.lab --password PASSWORD --action shutdown
```

## Shutdown Order

The shutdown follows the **reverse** of the startup order:

| Startup Order | Shutdown Order |
| -------------- | ---------------- |
| 1. Preliminary | 10. ESXi Hosts |
| 2. ESXi Hosts | 9. vSAN Elevator |
| 3. vSphere | 8. Advanced Settings |
| 4. VCF (NSX, vCenter) | 7. NSX Manager |
| 5. Services | 6. NSX Edges |
| 6. Kubernetes | 5. Management VMs |
| 7. VCF Final (Aria) | 4. Workload VMs |
| 8. Final | 3. WCP Services |
| | 2. Connect to Infrastructure |
| | 1. Fleet Operations (Aria) |

## Process Diagram

The following Mermaid diagrams illustrate the complete shutdown process flow.

### Main Orchestrator (Shutdown.py)

```mermaid
flowchart TD
    START([🚀 Start Shutdown])
    START --> INIT[Initialize Logging]
    INIT --> BANNER[Print Banner & Config]
    
    BANNER --> P0[/"Phase 0: Pre-Shutdown Checks"/]
    P0 --> CHECK_CONFIG[Check config.ini exists]
    CHECK_CONFIG --> DETECT_LAB[Detect Lab Type]
    
    DETECT_LAB --> P1[/"Phase 1: Docker Containers"/]
    P1 --> DOCKER_CHECK{Docker Host Reachable?}
    DOCKER_CHECK -->|Yes| DOCKER_STOP[Stop Containers]
    DOCKER_CHECK -->|No| DOCKER_SKIP[Skip Docker]
    DOCKER_STOP --> P2_START
    DOCKER_SKIP --> P2_START
    
    P2_START[/"Phase 2: VCF Shutdown"/] --> VCF_CHECK{Lab Type in VCF_LAB_TYPES?}
    VCF_CHECK -->|Yes| VCF_MODULE[Call VCFshutdown.py]
    VCF_CHECK -->|No| VCF_DEFAULT[Use Default Shutdown]
    VCF_MODULE --> P3_START
    VCF_DEFAULT --> P3_START
    
    P3_START[/"Phase 3: Final Cleanup"/] --> DISCONNECT[Disconnect vSphere Sessions]
    DISCONNECT --> SUMMARY[Print Summary]
    SUMMARY --> FINISH([✅ Shutdown Complete])

    style START fill:#90EE90
    style FINISH fill:#90EE90
    style P0 fill:#E6F3FF
    style P1 fill:#FFF3E6
    style P2_START fill:#F3E6FF
    style P3_START fill:#E6FFE6
```

### VCF Shutdown Module (VCFshutdown.py)

```mermaid
flowchart TD
    VCF_START([🔧 VCF Shutdown Start])
    
    VCF_START --> P1[/"Phase 1: Fleet Operations"/]
    P1 --> FLEET_CHECK{Fleet Mgmt Reachable?}
    FLEET_CHECK -->|Yes| FLEET_AUTH[Authenticate to SDDC Manager]
    FLEET_AUTH --> FLEET_SYNC[Sync Inventory]
    FLEET_SYNC --> FLEET_OFF[Shutdown: vra → vrni → vrops → vrli]
    FLEET_CHECK -->|No| FLEET_SKIP[Skip Fleet]
    FLEET_OFF --> P2
    FLEET_SKIP --> P2
    
    P2[/"Phase 2: Connect Infrastructure"/]
    P2 --> CONNECT[Connect to vCenters & Hosts]
    
    CONNECT --> P3[/"Phase 3: Stop WCP"/]
    P3 --> WCP[SSH: vmon-cli -k wcp]
    
    WCP --> P4[/"Phase 4: Workload VMs"/]
    P4 --> VM_FIND[Find VMs by Pattern]
    VM_FIND --> VM_OFF[Shutdown Workload VMs]
    
    VM_OFF --> P5[/"Phase 5: Management VMs"/]
    P5 --> MGMT_OFF["Shutdown in order:<br/>Aria → SDDC Mgr → vCenters"]
    
    MGMT_OFF --> P6[/"Phase 6: NSX Edges"/]
    P6 --> EDGE_OFF[Shutdown Edge VMs]
    
    EDGE_OFF --> P7[/"Phase 7: NSX Manager"/]
    P7 --> NSX_OFF[Shutdown NSX Manager]
    
    NSX_OFF --> P8[/"Phase 8: Host Settings"/]
    P8 --> ADV[Set AllocGuestLargePage=1]
    
    ADV --> P9[/"Phase 9: vSAN Elevator"/]
    P9 --> VSAN_CHECK{vSAN Enabled?}
    VSAN_CHECK -->|Yes| VSAN_ON[Enable plogRunElevator]
    VSAN_ON --> VSAN_WAIT[⏳ Wait 45 minutes]
    VSAN_WAIT --> VSAN_OFF[Disable plogRunElevator]
    VSAN_CHECK -->|No| VSAN_SKIP[Skip vSAN]
    VSAN_OFF --> P10
    VSAN_SKIP --> P10
    
    P10[/"Phase 10: ESXi Hosts"/]
    P10 --> HOST_CHECK{Shutdown Hosts?}
    HOST_CHECK -->|Yes| HOST_OFF[ShutdownHost_Task]
    HOST_CHECK -->|No| HOST_SKIP[Skip Hosts]
    HOST_OFF --> VCF_END
    HOST_SKIP --> VCF_END
    
    VCF_END([✅ VCF Shutdown Complete])

    style VCF_START fill:#90EE90
    style VCF_END fill:#90EE90
    style P1 fill:#FFE6E6
    style P2 fill:#E6F3FF
    style P3 fill:#FFF3E6
    style P4 fill:#F3E6FF
    style P5 fill:#E6FFE6
    style P6 fill:#FFFDE6
    style P7 fill:#E6FFFF
    style P8 fill:#FFE6F3
    style P9 fill:#F0F0F0
    style P10 fill:#E6E6FF
```

### Fleet Operations Detail

```mermaid
flowchart LR
    subgraph FLEET["fleet.py - Fleet Operations"]
        F_START([Start]) --> F_AUTH[Get Encoded Token<br/>base64 credentials]
        F_AUTH --> F_ENV[Get All Environments<br/>from SDDC Manager]
        F_ENV --> F_SYNC[Trigger Inventory Sync<br/>for each product]
        F_SYNC --> F_WAIT1[Wait for Sync<br/>max 5 min]
        F_WAIT1 --> F_POWER[Trigger power-off<br/>for each product]
        F_POWER --> F_WAIT2[Wait for Power Op<br/>max 30 min]
        F_WAIT2 --> F_END([Complete])
    end

    style F_START fill:#90EE90
    style F_END fill:#FFB6C1
```

### VM Shutdown Logic

```mermaid
flowchart TB
    subgraph VM_SHUTDOWN["VM Graceful Shutdown Logic"]
        VS_START([Shutdown VM]) --> VS_STATE{Current<br/>Power State?}
        
        VS_STATE -->|Powered Off| VS_SKIP[Already Off - Skip]
        VS_STATE -->|Suspended| VS_FORCE_SUSPEND[Force Power Off]
        VS_STATE -->|Powered On| VS_TOOLS{VMware Tools<br/>Running?}
        
        VS_TOOLS -->|Yes| VS_GUEST[ShutdownGuest]
        VS_TOOLS -->|No| VS_FORCE_NO_TOOLS[Force Power Off]
        
        VS_GUEST --> VS_WAIT{Wait for<br/>Power Off<br/>max 5 min}
        VS_WAIT -->|Powered Off| VS_SUCCESS[Success]
        VS_WAIT -->|Timeout| VS_FORCE_TIMEOUT[Force Power Off]
        
        VS_SKIP --> VS_END([Done])
        VS_FORCE_SUSPEND --> VS_END
        VS_FORCE_NO_TOOLS --> VS_END
        VS_SUCCESS --> VS_END
        VS_FORCE_TIMEOUT --> VS_END
    end

    style VS_START fill:#90EE90
    style VS_END fill:#FFB6C1
    style VS_SUCCESS fill:#90EE90
```

### Sequence Diagram

```mermaid
sequenceDiagram
    participant User
    participant Shutdown.py
    participant VCFshutdown.py
    participant fleet.py
    participant SDDC Manager
    participant vCenter
    participant ESXi Hosts

    User->>Shutdown.py: python3 Shutdown.py
    Shutdown.py->>Shutdown.py: Initialize logging
    Shutdown.py->>Shutdown.py: Phase 0: Pre-checks
    Shutdown.py->>Shutdown.py: Phase 1: Docker containers
    
    Shutdown.py->>VCFshutdown.py: Phase 2: VCF Shutdown
    
    VCFshutdown.py->>fleet.py: Phase 1: Fleet Operations
    fleet.py->>SDDC Manager: Get environments
    SDDC Manager-->>fleet.py: Environment list
    fleet.py->>SDDC Manager: Shutdown vra, vrni, vrops, vrli
    SDDC Manager-->>fleet.py: Request IDs
    fleet.py->>SDDC Manager: Poll for completion
    fleet.py-->>VCFshutdown.py: Fleet shutdown complete
    
    VCFshutdown.py->>vCenter: Phase 2: Connect
    VCFshutdown.py->>vCenter: Phase 3: Stop WCP (vmon-cli -k wcp)
    VCFshutdown.py->>vCenter: Phase 4: Shutdown workload VMs
    VCFshutdown.py->>vCenter: Phase 5: Shutdown management VMs
    VCFshutdown.py->>vCenter: Phase 6: Shutdown NSX Edges
    VCFshutdown.py->>vCenter: Phase 7: Shutdown NSX Manager
    
    VCFshutdown.py->>ESXi Hosts: Phase 8: Set advanced settings
    VCFshutdown.py->>ESXi Hosts: Phase 9: Enable vSAN elevator
    VCFshutdown.py->>VCFshutdown.py: Wait 45 minutes
    VCFshutdown.py->>ESXi Hosts: Disable vSAN elevator
    VCFshutdown.py->>ESXi Hosts: Phase 10: ShutdownHost_Task
    
    VCFshutdown.py-->>Shutdown.py: VCF shutdown complete
    Shutdown.py->>Shutdown.py: Phase 3: Final cleanup
    Shutdown.py-->>User: Shutdown complete
```

## Configuration

Shutdown behavior can be customized via the `[SHUTDOWN]` section in `/tmp/config.ini`:

```ini
[SHUTDOWN]
# Fleet Operations (SDDC Manager)
fleet_fqdn = opslcm-a.site-a.vcf.lab
fleet_username = admin@local
fleet_products = vra,vrni,vrops,vrli

# Docker containers
shutdown_docker = true
docker_host = docker.site-a.vcf.lab
docker_user = holuser
docker_containers = gitlab,ldap,poste.io,flask

# WCP vCenters to stop WCP service
wcp_vcenters = vc-mgmt-a.site-a.vcf.lab
    vc-wld01-a.site-a.vcf.lab
    vc-wld02-a.site-a.vcf.lab

# VM patterns to find and shutdown (regex)
vm_patterns = ^kubernetes-cluster-.*$
    ^dev-project-.*$
    ^cci-service-.*$
    ^SupervisorControlPlaneVM.*$

# Specific workload VMs to shutdown
workload_vms = core-a
    core-b
    hol-ubuntu-001

# Management VMs (in shutdown order)
mgmt_vms = o11n-02a
    o11n-01a
    opslogs-01a
    sddcmanager-a
    vc-wld01-a
    vc-mgmt-a

# NSX components
nsx_edges = edge-wld01-01a
    edge-wld01-02a
nsx_mgr = nsx-mgmt-01a

# ESXi hosts
esx_hosts = esx-01a.site-a.vcf.lab
    esx-02a.site-a.vcf.lab
    esx-03a.site-a.vcf.lab
    esx-04a.site-a.vcf.lab
esx_username = root

# vSAN settings
vsan_enabled = true
vsan_timeout = 2700  # 45 minutes

# Host shutdown
shutdown_hosts = true
```

## vSAN Considerations

Before ESXi hosts can be safely shut down, the vSAN cluster must complete all pending I/O operations. This is done via the "vSAN elevator" process:

1. Enable `plogRunElevator` on all hosts (flushes write cache)
2. Wait 45 minutes for vSAN to complete all I/O
3. Disable `plogRunElevator` on all hosts
4. Proceed with host shutdown

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
- `/home/holuser/hol/labstartup.log` (appended)
- Console output

## Troubleshooting

### Fleet Operations fails

- Verify SDDC Manager is reachable: `ping opslcm-a.site-a.vcf.lab`
- Check credentials in `/home/holuser/creds.txt`
- Test with: `python3 fleet.py --fqdn ... --action list`

### VMs not shutting down

- Check VMware Tools status in vCenter
- VMs without Tools will be force powered off
- Check VM power state in vCenter

### vSAN timeout too long

- Use `--quick` flag for faster (but less safe) shutdown
- Or set `vsan_timeout = 0` in config

### Hosts not shutting down

- Verify ESXi SSH is enabled
- Check root password in `/home/holuser/creds.txt`
- Use `--no-hosts` to skip host shutdown

## Support

For issues with the shutdown scripts, contact the HOL Core Team.
