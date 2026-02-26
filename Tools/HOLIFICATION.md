# HOLification Guide

Version 2.1 - February 26, 2026

This document describes the complete HOLification process for preparing vApp templates for VMware Hands-on Labs. It covers the automated steps handled by the confighol scripts.

> **Changelog:**
> - **v2.1 (2026-02-26):** NSX Edge SSH automated via Guest Operations; Operations VM SSH automated via Guest Operations; SDDC Manager password rotation recovery for NSX; license checks expanded to all ESXi hosts and VCF Operations; updated procedures to remove manual Edge SSH steps.
> - **v2.0 (2026-01):** Initial version.

> **Script Naming Convention:** The confighol script is named according to the VCF version it was developed and tested against. The current version `confighol-9.1.py` is written and tested for VCF 9.1.x.

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Automated Steps (confighol-9.1.py)](#automated-steps-confighol-91py)
- [Manual Steps Required](#manual-steps-required)
  - [Enable SSH on NSX Managers](#enable-ssh-on-nsx-managers)
- [How NSX Edge and Operations VM SSH Is Automated](#how-nsx-edge-and-operations-vm-ssh-is-automated)
- [Complete HOLification Procedure](#complete-holification-procedure)
- [Troubleshooting](#troubleshooting)

---

## Overview

The HOL team leverages the Holodeck factory build process (documented elsewhere) and adjusts ("HOLifies") the deliverable for HOL use. The `confighol-9.1.py` script automates as much of this process as possible. NSX Edge and Operations VM SSH enablement - previously a manual task - is now fully automated via vSphere Guest Operations.

### What Gets Configured

| Component | Configuration | Automated? |
| ----------- | --------------- | ------------ |
| ESXi Hosts | SSH enabled, auto-start, passwordless auth, password expiration | ✅ Yes |
| vCenters | Shell enabled, MOB, password policies, DRS/HA settings | ✅ Yes |
| NSX Managers | SSH enabled, start-on-boot, passwordless auth, password expiration, SDDC rotated password recovery | ⚠️ Partial |
| NSX Edges | SSH enabled (via Guest Ops), start-on-boot, passwordless auth, password expiration | ✅ Yes |
| SDDC Manager | SSH keys, password expiration | ✅ Yes |
| Operations VMs | SSH enabled (via Guest Ops), SSH keys, password expiration | ✅ Yes |

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

## Automated Steps (confighol-9.1.py)

The following operations are fully automated by running `confighol-9.1.py`:

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
- Automatically recover rotated root passwords from SDDC Manager

### NSX Edge Configuration (Fully Automated)

- Enable SSH via vSphere Guest Operations API (`systemctl enable/start sshd`)
- Configure SSH to start on boot
- Copy authorized_keys for passwordless access
- Remove password expiration for admin, root, audit users

### SDDC Manager Configuration

- Copy LMC SSH key for vcf user
- Set non-expiring passwords for vcf, root, backup accounts

### Operations VMs Configuration

- Enable SSH via vSphere Guest Operations API (if not already running)
- Set non-expiring password for root
- Configure SSH authorized_keys

### Final Cleanup

- Clear ARP cache on console and router
- Run vpodchecker.py to update L2 VM settings

---

## Manual Steps Required

The following steps **must be performed manually** before `confighol-9.1.py` can complete NSX Manager configuration:

### Enable SSH on NSX Managers

SSH must be enabled manually on each NSX Manager via the vSphere Remote Console before `confighol-9.1.py` can configure it further. NSX Managers require an initial SSH enable via the console; the script then configures start-on-boot and passwordless access.

**Applies to:**

- `nsx-mgmt-01a` (and all NSX Managers for Site A)
- `nsx-wld-01a` (and all NSX Managers for Workload Domains)
- Repeat for Site B if applicable

**Procedure:**

1. **Launch Firefox Browser** on the console

2. **Connect to Management vCenter:**
   - Bookmarks Toolbar → Region A → vc-mgmt-a Client
   - Login: `administrator@vsphere.local`
   - Password: `[check in creds.txt]` (or lab password)

3. **Open Remote Console to NSX Manager:**
   - Menu → Inventory → vc-mgmt-a.site-a.vcf.lab
   - Navigate: dc-a → cluster-mgmt-01a
   - Right-click `nsx-mgmt-01a` → Launch Remote Console
   - Login: `admin` / `[check in creds.txt]`

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

> **Note:** NSX Edges no longer require manual SSH enablement. The script enables SSH on Edges automatically via vSphere Guest Operations.

---

## How NSX Edge and Operations VM SSH Is Automated

### vSphere Guest Operations API

The `confighol-9.1.py` script uses the vSphere Guest Operations Manager API to run commands inside VMs via VMware Tools, without needing SSH access first. This solves the chicken-and-egg problem where SSH must be enabled before SSH can be used to enable SSH on boot.

**How it works:**

1. The script connects to the vCenter that manages the target VM
2. Locates the VM by name in the vCenter inventory
3. Authenticates to the guest OS via VMware Tools (using root credentials)
4. Runs `systemctl enable sshd` and `systemctl start sshd` inside the VM
5. Waits for SSH port 22 to become available
6. Proceeds with the remaining configuration (authorized_keys, password expiration)

**Requirements:**

- VMware Tools must be running in the target VM (verified automatically)
- Root credentials must be valid for the guest OS
- The VM must be powered on

### Previously Considered Alternatives (Now Superseded)

| Approach | Previous Status | Current Status |
| ---------- | -------- | -------- |
| vSphere Guest Operations API (VM Tools) | "Not available" | ✅ Works on both NSX Edges and Operations VMs |
| Serial console automation | Too fragile | Not needed |
| Custom NSX plugin | Would require engineering support | Not needed |
| Pre-built images with SSH enabled | Would require factory changes | Not needed |

### SDDC Manager Password Rotation Recovery

SDDC Manager may rotate the root password on NSX components during credential management operations. When this happens, the standard lab password no longer works for root SSH. The `confighol-9.1.py` script now handles this automatically:

1. Detects root SSH authentication failure with the standard password
2. Queries the SDDC Manager credentials API for the actual current password
3. Resets the root password back to the standard lab password via the NSX API
4. Updates the SDDC Manager credential record to match

---

## Complete HOLification Procedure

Follow these steps in order for complete HOLification:

### Step 1: Pre-HOLification (Manual)

1. Complete successful LabStartup to Ready state
2. Verify `/hol/vPod.txt` is set correctly
3. Verify `/tmp/config.ini` is accurate

### Step 2: Enable SSH on NSX Managers (Manual)

1. Enable SSH on all NSX Managers (see procedure above)
2. Verify SSH is working:

```bash
ssh admin@nsx-mgmt-01a.site-a.vcf.lab
```

> **Note:** NSX Edge SSH is now handled automatically by the script in Step 3.

### Step 3: Run confighol-9.1.py (Automated)

```bash
cd ~/hol/Tools
python3 confighol-9.1.py
```

The script will:

- Configure all ESXi hosts
- Configure all vCenters (with interactive prompts for shell configuration)
- Configure NSX Managers (with interactive prompts to confirm SSH is enabled)
- Configure NSX Edges (SSH enabled automatically via Guest Operations)
- Configure SDDC Manager
- Configure VCF Automation VMs
- Configure Operations VMs (SSH enabled automatically via Guest Operations)
- Disable SDDC Manager auto-rotate policies
- Configure VCF Operations Fleet Password Policy
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
ssh root@edge-wld01-01a.site-a.vcf.lab hostname
```

4. Verify Operations VM SSH works:

```bash
ssh root@ops-a.site-a.vcf.lab hostname
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

**Symptom:** `confighol-9.1.py` fails with authentication errors for NSX

**Solution:** Verify credentials:

- Default user: `admin`
- Verify password matches lab password in `/home/holuser/creds.txt`

### NSX Root Password Rotated by SDDC Manager

**Symptom:** Root SSH to NSX Manager/Edge fails with "Permission denied" even though the password is correct for admin.

**Solution:** SDDC Manager may have rotated the root password. The `confighol-9.1.py` script handles this automatically by querying SDDC Manager for the actual password and resetting it. If manual recovery is needed:

1. Query SDDC Manager credentials API for the current root password
2. Reset via NSX API: `PUT /api/v1/node/users/0` with old and new password
3. Update SDDC Manager credential record: `PATCH /v1/credentials`

### Guest Operations Fails on NSX Edge

**Symptom:** "Invalid guest credentials" or "Guest operations unavailable" when enabling Edge SSH

**Solution:**

1. Verify the Edge VM is powered on and VMware Tools is running
2. Check if root password has been rotated (see above)
3. As a fallback, enable SSH manually via vSphere Remote Console (login as admin, run `start service ssh` and `set service ssh start-on-boot`)

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
