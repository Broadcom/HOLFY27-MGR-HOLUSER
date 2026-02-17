# HOLFY27-MGR-HOLUSER

VMware Hands-on Labs (HOL) FY27 Manager holuser Repository

This repository contains the core lab startup, shutdown, and configuration scripts for VMware Hands-on Labs environments.

## Overview

The HOL lab startup system orchestrates the boot sequence of VMware Cloud Foundation (VCF) components, ensuring services come online in the correct order with proper health checks.

## VM Boot Order

The lab startup system boots VMs in a carefully orchestrated sequence to ensure dependencies are met. The boot order is managed by the `VCF.py` module.

### VCF Boot Sequence

| Order | Task | Config Key | Description | Wait Time |
| ------- | ------ | ------------ | ------------- | ----------- |
| 1 | Management Cluster | `vcfmgmtcluster` | Connect to ESXi hosts, exit maintenance mode | Variable |
| 2 | Datastore Check | `vcfmgmtdatastore` | Verify VSAN/storage is accessible | Until ready |
| 3 | NSX Manager | `vcfnsxmgr` | Start NSX Manager VMs | 30 seconds |
| 4 | NSX Edges | `vcfnsxedges` | Start NSX Edge VMs | 5 minutes |
| 4b | Post-Edge VMs | `vcfpostedgevms` | Start VMs that need early boot (e.g., VCF Automation) | 30 seconds |
| 5 | vCenter | `vcfvCenter` | Start vCenter Server | Continues |

### Complete Startup Module Sequence

After VCF component startup, additional modules run in order:

1. **prelim** - DNS checks, network validation
2. **ESXi** - ESXi host configuration
3. **VCF** - VCF component startup (see table above)
4. **VVF** - VVF-specific startup (if applicable)
5. **vSphere** - vSphere cluster configuration, DRS settings
6. **pings** - Network connectivity verification
7. **services** - Windows/Linux service checks
8. **Kubernetes** - K8s cluster health checks
9. **urls** - URL availability verification
10. **VCFfinal** - VCF Automation, Tanzu, final VCF tasks
11. **final** - Cleanup, ready signal
12. **odyssey** - Odyssey client installation (if enabled)

### Configuration Example

```ini
[VCF]
# ESXi hosts in management cluster
vcfmgmtcluster = esx-01a.site-a.vcf.lab:esx
 esx-02a.site-a.vcf.lab:esx
 esx-03a.site-a.vcf.lab:esx
 esx-04a.site-a.vcf.lab:esx

# VSAN datastore name
vcfmgmtdatastore = vsan-mgmt-01a

# NSX Manager VMs (format: vmname:esxhost)
vcfnsxmgr = nsx-mgmt-01a:esx-01a.site-a.vcf.lab

# NSX Edge VMs
vcfnsxedges = edge-mgmt-01a:esx-02a.site-a.vcf.lab
 edge-mgmt-02a:esx-02a.site-a.vcf.lab

# Post-Edge VMs - boot immediately after NSX Edges
# Use for VMs that need extra boot time but don't require vCenter
vcfpostedgevms = auto-a:esx-02a.site-a.vcf.lab

# vCenter Server VM
vcfvCenter = vc-mgmt-a:esx-02a.site-a.vcf.lab
```

### Post-Edge VMs (vcfpostedgevms)

The `vcfpostedgevms` configuration option allows you to boot VMs immediately after NSX Edges are started, before vCenter. This is useful for:

- **VCF Automation (auto-a)**: Requires significant boot time
- **Large appliances**: VMs that take 10+ minutes to fully initialize
- **Dependency-free VMs**: VMs that don't need vCenter but need early boot

These VMs start in parallel with subsequent startup tasks, maximizing overall boot efficiency.

## Lab Types

The system supports multiple lab types with different configurations:

| Lab Type | Description |
| ---------- |  ------------- |
| HOL | Full production Hands-on Labs |
| Discovery | Simplified discovery environments |
| VXP | VCF Experience Program demos |
| ATE | Advanced Technical Enablement (Livefire) |
| EDU | Education/training environments |

## Key Configuration Files

### /tmp/config.ini

The main runtime configuration file, copied from `/home/holuser/hol/holodeck/{SKU}.ini` during startup if no vpodrepo is pulled with its custom config.ini.

### /home/holuser/creds.txt

Contains the lab password used for SSH/API authentication.

### /home/holuser/hol/holodeck/{SKU}.ini

Lab-specific configuration template containing all VM lists, URL checks, and feature flags.

## Tools

### confighol-9.0.py

HOLification tool that prepares a vApp for HOL deployment. The script is named according to the VCF version it was developed and tested against - `confighol-9.0.py` is written and tested for VCF 9.0.1.

- Imports Vault root CA to Firefox
- Imports vCenter CA certificates to Firefox
- Configures SSH access on ESXi hosts
- Sets non-expiring passwords
- Configures NSX SSH access

```bash
python3 Tools/confighol-9.0.py --dry-run    # Preview changes
python3 Tools/confighol-9.0.py              # Full HOLification
```

> **Note:** Future VCF versions may require a new script version (e.g., `confighol-9.1.py` for VCF 9.1.x).

### cert-replacement.py

Manages SSL certificates for VCF components using HashiCorp Vault PKI:

- Generates CSRs via SDDC Manager API
- Signs certificates with Vault PKI (2-year TTL)
- Replaces certificates on VCF components

```bash
python3 Tools/cert-replacement.py --dry-run
python3 Tools/cert-replacement.py --targets sddcmanager-a.site-a.vcf.lab
```

### vpodchecker.py

Validates vPod configuration and updates L2 VM settings.

## NFS Communication with Router

The manager VM exports `/tmp/holorouter` via NFS to the router VM (`10.1.10.129`). This directory contains:

- `iptablescfg.sh` - Firewall rules
- `squid.conf` - Proxy configuration
- `allowlist` - Allowed domains for proxy
- `gitdone` - Signal file indicating git pull complete
- `ready` - Signal file indicating lab is ready

The router mounts this share at `/mnt/manager` and applies configurations from these files.

## Troubleshooting

### Proxy Not Working

Check Squid configuration on router:

```bash
ssh root@router "grep 'acl whitelist' /etc/squid/squid.conf"
# Should show: acl whitelist url_regex "/etc/squid/allowlist"
```

### NFS Mount Failing

Verify directory exists and NFS is exported:

```bash
ls -la /tmp/holorouter
showmount -e localhost
```

### VM Not Booting

Check if VM is in the correct config section:

```bash
grep -A10 '\[VCF\]' /tmp/config.ini
```

## Analysis: What Happens Today Without Internet?

### Current Failure Points and Timing

When a lab boots without access to GitHub, the following happens:

1. **Root cron** (`@reboot`) runs `[HOLFY27-MGR-ROOT/gitpull.sh](2027-labstartup/HOLFY27-MGR-ROOT/gitpull.sh)`:

   - `wait_for_proxy()` loops up to **60 attempts x 5 seconds = 5 minutes** waiting to reach `https://github.com` through the proxy
   - After timeout, it logs a WARNING but **continues** (returns 1, does not exit)
   - `do_git_pull()` runs but fails; logs "Git pull failed - continuing with existing code"
   - It also tries to `curl` download `tdns-mgr` and `oh-my-posh` from GitHub (lines 74-115) -- these will fail silently
   - **Result: Does NOT hard-fail. Takes ~5 minutes delay.**

2. **Holuser cron** (`@reboot`) runs `[HOLFY27-MGR-HOLUSER/gitpull.sh](2027-labstartup/HOLFY27-MGR-HOLUSER/gitpull.sh)`:

   - Same `wait_for_proxy()` loop: **5 minutes timeout**
   - Git pull fails but logs "Git pull failed - continuing with existing code"
   - Still creates the `gitdone` signal file for the router
   - **Result: Does NOT hard-fail. Takes ~5 minutes delay.**

3. **Holuser cron** then runs `[HOLFY27-MGR-HOLUSER/labstartup.sh](2027-labstartup/HOLFY27-MGR-HOLUSER/labstartup.sh)`:

   - The `git_clone()` function (lines 95-155) retries **10 attempts x 5 seconds = ~50 seconds per attempt**, meaning up to **~10 minutes** of waiting
   - For **HOL SKUs**: `git_clone` exits with `FAIL - Could not clone GIT Project` if the repo doesn't already exist locally
   - For **non-HOL SKUs**: Falls back to local `holodeck/*.ini` files
   - If `git_pull()` is used (repo already exists locally): retries **30 attempts x 5 seconds = 2.5 minutes**, then logs "Could not perform git pull. Will attempt LabStartup with existing code." and **continues**
   - **Result: If `/vpodrepo` already has a cloned repo, lab startup WILL eventually succeed after ~7-12 minutes of wasted time. If no local repo exists, HOL SKUs HARD FAIL.**

4. **Router** runs `[27XX-HOLOROUTER/getrules.sh](2027-labstartup/27XX-HOLOROUTER/getrules.sh)`:

   - Waits for NFS mount from manager, then waits for `gitdone` signal
   - The gitdone signal IS created by holuser's gitpull.sh even on failure, so this works
   - **Result: No failure here.**

## Version

- **Version**: 3.0
- **Updated**: January 2026
- **Authors**: Burke Azbill and HOL Core Team
