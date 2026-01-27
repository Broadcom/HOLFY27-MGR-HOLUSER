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
