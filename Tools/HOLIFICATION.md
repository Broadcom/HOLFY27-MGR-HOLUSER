# HOLification Guide

Version 2.0 - January 2026

This document describes the complete HOLification process for preparing vApp templates for VMware Hands-on Labs. It covers both automated steps (handled by `confighol-9.0.py`) and manual steps that cannot be automated.

> **Script Naming Convention:** The confighol script is named according to the VCF version it was developed and tested against. The current version `confighol-9.0.py` is written and tested for VCF 9.0.1. Future VCF versions may require a new script (e.g., `confighol-9.1.py` for VCF 9.1.x).

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Automated Steps (confighol-9.0.py)](#automated-steps-confighol-90py)
- [Manual Steps Required](#manual-steps-required)
  - [Enable SSH on NSX Managers](#enable-ssh-on-nsx-managers)
  - [Enable SSH on NSX Edges](#enable-ssh-on-nsx-edges)
- [Why Some Steps Cannot Be Automated](#why-some-steps-cannot-be-automated)
- [Complete HOLification Procedure](#complete-holification-procedure)
- [Troubleshooting](#troubleshooting)

---

## Overview

The HOL team leverages the Holodeck factory build process (documented elsewhere) and adjusts ("HOLifies") the deliverable for HOL use. The `confighol-9.0.py` script automates as much of this process as possible, while some operations require manual intervention due to security architecture constraints.

### What Gets Configured

| Component | Configuration | Automated? |
| ----------- | --------------- | ------------ |
| ESXi Hosts | SSH enabled, auto-start, passwordless auth, password expiration | ✅ Yes |
| vCenters | Shell enabled, MOB, password policies, DRS/HA settings | ✅ Yes |
| NSX Managers | SSH enabled, start-on-boot, passwordless auth, password expiration | ⚠️ Partial |
| NSX Edges | SSH enabled, start-on-boot, passwordless auth, password expiration | ❌ No (Manual) |
| SDDC Manager | SSH keys, password expiration | ✅ Yes |
| Operations VMs | SSH keys, password expiration | ✅ Yes |

---

## Prerequisites

Before beginning HOLification:

1. **Complete a successful LabStartup reaching Ready state**
   - All VMs must be running and accessible
   - Network connectivity verified

2. **Edit vPod configuration**
   - Edit `/hol/vPod.txt` on the Console
   - Set to an appropriate test vPod_SKU

3. **Verify config.ini**
   - Ensure `/tmp/config.ini` is accurate and complete
   - All ESXi hosts, vCenters, NSX components must be listed

4. **Install expect utility**
   - Verify `/usr/bin/expect` is installed on the Manager

---

## Automated Steps (confighol-9.0.py)

The following operations are fully automated by running `confighol-9.0.py`:

### ESXi Host Configuration

- Enable SSH service via vSphere API
- Configure SSH to start automatically with host
- Copy holuser public keys for passwordless SSH access
- Set password expiration to 9999 days (non-expiring)
- Disable session timeout

### vCenter Configuration

- Enable bash shell for root user (via expect script)
- Configure SSH authorized_keys
- Enable browser support warning dismissal
- Enable Managed Object Browser (MOB)
- Set local account password expiration to 9999 days
- Configure DRS to PartiallyAutomated mode
- Disable HA Admission Control
- Clear ARP cache

### NSX Manager Configuration (Partial Automation)

- Enable SSH via NSX REST API (if already accessible)
- Configure SSH to start on boot (via SSH after manual enable)
- Copy authorized_keys for passwordless access
- Remove password expiration for admin, root, audit users

### SDDC Manager Configuration

- Copy LMC SSH key for vcf user
- Set non-expiring passwords for vcf, root, backup accounts

### Operations VMs Configuration

- Set non-expiring password for root
- Configure SSH authorized_keys

### Final Cleanup

- Clear ARP cache on console and router
- Run vpodchecker.py to update L2 VM settings

---

## Manual Steps Required

The following steps **must be performed manually** before `confighol-9.0.py` can complete NSX configuration:

### Enable SSH on NSX Managers

SSH must be enabled manually on each NSX Manager via the vSphere Remote Console before `confighol-9.0.py` can configure it further.

**Applies to:**

- `nsx-mgmt-01a` (and all NSX Managers for Site A)
- `nsx-wld-01a` (and all NSX Managers for Workload Domains)
- Repeat for Site B if applicable

**Procedure:**

1. **Launch Firefox Browser** on the console

2. **Connect to Management vCenter:**
   - Bookmarks Toolbar → Region A → vc-mgmt-a Client
   - Login: `administrator@vsphere.local`
   - Password: `VMware123!VMware123!` (or lab password)

3. **Open Remote Console to NSX Manager:**
   - Menu → Inventory → vc-mgmt-a.site-a.vcf.lab
   - Navigate: dc-a → cluster-mgmt-01a
   - Right-click `nsx-mgmt-01a` → Launch Remote Console
   - Login: `admin` / `VMware123!VMware123!`

4. **Enable SSH service:**

   ```bash
   start service ssh
   set service ssh start-on-boot
   get service ssh
   ```

5. **Verify output shows:**

   ```bash
   Service name: ssh
   Service state: running
   Start on boot: True
   ```

6. **Close the Remote Console and Firefox**

7. **Repeat for all NSX Managers** (nsx-mgmt-01a, nsx-wld-01a, etc.)

---

### Enable SSH on NSX Edges

SSH must be enabled manually on each NSX Edge via the vSphere Remote Console. The NSX-T API cannot be used to enable SSH on Edge nodes remotely.

**Applies to:**

- `edge-wld01-01a`
- `edge-wld01-02a`
- All additional Edge nodes for each Workload Domain
- Repeat for Site B if applicable

**Procedure for edge-wld01-01a:**

1. **Launch Firefox Browser** on the console

2. **Connect to Workload vCenter:**
   - Bookmarks Toolbar → Region A → vc-wld01-a Client
   - Login: `administrator@wld.sso`
   - Password: `VMware123!VMware123!` (or lab password)

3. **Open Remote Console to NSX Edge:**
   - Menu → Inventory → vc-wld01-a.site-a.vcf.lab
   - Navigate: dc-a → cluster-wld01-01a
   - Find the VCF-edge resource pool (may have a long name like `VCF-edge_edgecl-wkld-a_ResourcePool_...`)
   - Right-click `edge-wld01-01a` → Launch Remote Console
   - Login: `admin` / `VMware123!VMware123!`

4. **Enable SSH service:**

   ```bash
   start service ssh
   set service ssh start-on-boot
   get service ssh
   ```

5. **Verify output shows:**

   ```bash
   Service name: ssh
   Service state: running
   Start on boot: True
   ```

6. **Close the Remote Console and Firefox**

7. **Repeat for edge-wld01-02a and all other Edge nodes**

---

## Why Some Steps Cannot Be Automated

### NSX Edge SSH Configuration

The NSX-T REST API has limited scope for SSH service management:

| Operation | API Support | Reason |
| ----------- | ------------- | -------- |
| Start SSH on Manager | ✅ Available | `/api/v1/node/services/ssh?action=start` |
| Set start-on-boot | ❌ Not Available | CLI-only: `set service ssh start-on-boot` |
| Configure Edge SSH | ❌ Not Available | Edge nodes don't expose the appliance API |

**Technical Details:**

1. **NSX Edges don't expose the `/api/v1/node/` endpoints**
   - The appliance management API (`/api/v1/node/services/ssh`) is only available on NSX Manager appliances
   - Edge nodes are managed through the Manager but don't have their own REST API for service control

2. **Start-on-boot requires CLI access**
   - The `set service ssh start-on-boot` command can only be run via the NSX CLI
   - This requires SSH access, creating a chicken-and-egg problem: you need SSH to enable SSH on boot
   - The only way to initially enable SSH is via the console

3. **Security by design**
   - NSX is designed so that SSH is disabled by default for security
   - Enabling it requires physical/console access as a security boundary
   - This prevents remote attackers from enabling SSH even with API credentials

### Alternative Approaches Considered

| Approach | Result |
| ---------- | -------- |
| vSphere Guest Operations API (VM Tools) | Not available - NSX appliances don't support guest operations |
| Serial console automation | Too fragile and environment-dependent |
| Custom NSX plugin | Would require VMware engineering support |
| Pre-built images with SSH enabled | Would require changes to Holodeck factory |

**Conclusion:** The manual console steps for NSX Edges are required by NSX's security architecture and cannot be bypassed.

---

## Complete HOLification Procedure

Follow these steps in order for complete HOLification:

### Step 1: Pre-HOLification (Manual)

1. Complete successful LabStartup to Ready state
2. Verify `/hol/vPod.txt` is set correctly
3. Verify `/tmp/config.ini` is accurate

### Step 2: Enable SSH on NSX Components (Manual)

1. Enable SSH on all NSX Managers (see procedure above)
2. Enable SSH on all NSX Edges (see procedure above)
3. Verify SSH is working:

   ```bash
   ssh admin@nsx-mgmt-01a.site-a.vcf.lab
   ssh admin@edge-wld01-01a.site-a.vcf.lab
   ```

### Step 3: Run confighol-9.0.py (Automated)

```bash
cd ~/hol/Tools
python3 confighol-9.0.py
```

The script will:

- Configure all ESXi hosts
- Configure all vCenters (with interactive prompts for shell configuration)
- Configure NSX components (with interactive prompts to confirm SSH is enabled)
- Configure SDDC Manager
- Configure Operations VMs
- Perform final cleanup

### Step 4: Verify Configuration

1. Verify passwordless SSH to all ESXi hosts:

   ```bash
   ssh root@esx-01a.site-a.vcf.lab hostname
   ```

2. Verify vCenter MOB is accessible:
   - Browse to `https://vc-mgmt-a.site-a.vcf.lab/mob`

3. Verify NSX SSH works:

   ```bash
   ssh admin@nsx-mgmt-01a.site-a.vcf.lab
   ```

### Step 5: Site B (If Applicable)

Repeat Steps 2-4 for Site B / Region B components.

---

## Troubleshooting

### SSH Connection Refused on ESXi

**Symptom:** `ssh: connect to host esx-01a... port 22: Connection refused`

**Solution:** The SSH service may not have started. Check via vSphere client:

1. Select the host
2. Configure → Services
3. Find SSH and click Start
4. Enable "Start and stop with host"

### NSX API Returns 401 Unauthorized

**Symptom:** `confighol-9.0.py` fails with authentication errors for NSX

**Solution:** Verify credentials:

- Default user: `admin`
- Verify password matches lab password in `/home/holuser/creds.txt`

### vCenter Shell Already Changed Error

**Symptom:** `vcshell.exp` fails with error

**Solution:** This is normal if HOLification was run before. The shell can only be changed once. The error can be safely ignored.

### SDDC Manager SSH Key Copy Fails

**Symptom:** `scp` to SDDC Manager fails

**Solution:**

1. Verify SDDC Manager is reachable: `ping sddcmanager-a.site-a.vcf.lab`
2. Only the LMC key is supported (Manager key doesn't work due to SDDC Manager restrictions)
3. Verify the vcf user account is not locked

### vpodchecker.py Errors

**Symptom:** Final cleanup step shows vpodchecker errors

**Solution:** This is a separate tool with its own documentation. Run manually for detailed output:

```bash
python3 ~/hol/Tools/vpodchecker.py --verbose
```

---

## Support

For issues with the HOLification process, contact the HOL Core Team.
