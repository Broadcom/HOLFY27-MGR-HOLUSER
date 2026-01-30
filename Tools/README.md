# HOLFY27 Tools

Version 2.0 - January 2026

This folder contains utility scripts and tools for managing HOLFY27 lab environments. These tools support lab development, debugging, automation, and operational tasks.

---

## Table of Contents

- [HOLFY27 Tools](#holfy27-tools)
  - [Table of Contents](#table-of-contents)
  - [Python Scripts](#python-scripts)
    - [confighol.py](#configholpy)
    - [checkfw.py](#checkfwpy)
    - [dns\_checks.py](#dns_checkspy)
    - [labtypes.py](#labtypespy)
    - [status\_dashboard.py](#status_dashboardpy)
    - [tdns\_import.py](#tdns_importpy)
    - [vpodchecker.py](#vpodcheckerpy)
  - [Shell Scripts](#shell-scripts)
    - [fwoff.sh / fwon.sh](#fwoffsh--fwonsh)
    - [proxyfilteroff.sh / proxyfilteron.sh](#proxyfilteroffsh--proxyfilteronsh)
    - [holpwgen.sh](#holpwgensh)
    - [lkill.sh](#lkillsh)
    - [odyssey-launch.sh](#odyssey-launchsh)
    - [restart\_k8s\_webhooks.sh](#restart_k8s_webhookssh)
    - [runautocheck.sh](#runautochecksh)
    - [VLPagent.sh](#vlpagentsh)
    - [watchvcfa.sh](#watchvcfash)
    - [vcfapwcheck.sh](#vcfapwchecksh)
  - [Expect Scripts](#expect-scripts)
    - [sddcmgr.exp](#sddcmgrexp)
    - [vcfapass.sh](#vcfapasssh)
    - [vcshell.exp](#vcshellexp)
  - [Configuration Files](#configuration-files)
    - [VMware.config](#vmwareconfig)
    - [launch\_odyssey.desktop](#launch_odysseydesktop)
  - [Dependencies](#dependencies)
  - [Support](#support)

---

## Python Scripts

### confighol.py

**vApp HOLification Tool:**

Comprehensive automation tool for "HOLifying" vApp templates after the Holodeck factory build process. This script consolidates and replaces the previous `esx-config.py` and `configvsphere.ps1` scripts into a single, unified tool.

**Execution Order:**

0. **Vault Root CA Import** (with SKIP/RETRY/FAIL options)
1. Pre-checks and environment setup
2. ESXi host configuration
3. vCenter configuration
4. NSX configuration
5. SDDC Manager configuration
6. Operations VMs configuration
7. Final cleanup

**Features:**

1. **Vault CA Import (Step 0):**
   - Runs first, before any other configuration steps
   - Checks if Vault is accessible with a 5-second timeout
   - Downloads and imports the Vault root CA certificate
   - Interactive options when Vault is unavailable:
     - **[S]kip** - Continue without importing (Firefox will show certificate warnings)
     - **[R]etry** - Check Vault again after fixing the issue
     - **[F]ail** - Exit the script with an error
   - Imports the CA into Firefox's certificate store on the console VM

2. **ESXi Host Configuration:**
   - Enables SSH service via vSphere API
   - Configures SSH to auto-start on boot
   - Copies holuser public keys for passwordless SSH access
   - Disables session timeout
   - Detailed success/failure logging for each operation
   - **Note:** Password expiration is not configured on ESXi (chage command not valid)

3. **vCenter Configuration:**
   - Enables bash shell for root user
   - Configures SSH authorized_keys
   - Enables browser support and MOB (Managed Object Browser)
   - Sets password policies (9999 days expiration)
   - Disables HA Admission Control
   - Configures DRS to PartiallyAutomated
   - Creates new vSphere connection if session is invalid
   - Clears ARP cache

4. **NSX Configuration:**
   - Enables SSH via REST API on NSX Managers
   - Configures SSH start-on-boot
   - Removes password expiration for admin, root, audit users

5. **SDDC Manager Configuration:**
   - Configures SSH authorized_keys
   - Sets non-expiring passwords for vcf, backup, root accounts

6. **Operations VMs Configuration:**
   - Checks host reachability (ping) before attempting SSH
   - Checks if SSH port 22 is open
   - Sets non-expiring passwords for root
   - Configures SSH authorized_keys
   - Detailed success/failure logging with error messages
   - Reports authentication failures with username/password info

**Vault Accessibility Check:**

When Vault is not accessible, users see:

```bash
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
WARNING: Vault PKI CA Certificate Not Accessible
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

Connection timeout - Vault server not responding at http://10.1.1.1:32000

The Vault root CA certificate is used to establish trust
for VCF component certificates in Firefox on the console VM.

Options:
  [S]kip  - Continue without importing Vault CA
            (Firefox will show certificate warnings)
  [R]etry - Check Vault again (if you have fixed the issue)
  [F]ail  - Exit the script with an error

Enter choice [S/R/F]:
```

**New Functions:**

| Function | Description |
| ---------- | ------------- |
| `check_vault_accessible(vault_url, ca_path, timeout)` | Returns (bool, str) tuple indicating if Vault is reachable |
| `prompt_vault_unavailable(message)` | Interactive prompt for user choice when Vault is unavailable |

**Usage:**

```bash
# Full interactive HOLification
python3 confighol.py

# Preview what would be done (no changes)
python3 confighol.py --dry-run

# Skip vCenter shell configuration
python3 confighol.py --skip-vcshell

# Skip NSX configuration
python3 confighol.py --skip-nsx

# Only configure ESXi hosts
python3 confighol.py --esx-only
```

**Options:**

| Option | Description |
| -------- | ------------- |
| `--dry-run` | Preview what would be done without making changes |
| `--skip-vcshell` | Skip vCenter shell configuration |
| `--skip-nsx` | Skip NSX configuration |
| `--esx-only` | Only configure ESXi hosts |

**Prerequisites:**

- Complete successful LabStartup reaching Ready state
- Valid `/tmp/config.ini` with all resources defined
- `expect` utility installed (`/usr/bin/expect`)

**Note:** Some NSX operations require manual steps first. See `HOLIFICATION.md` for complete instructions.

---

### checkfw.py

**Firewall Connectivity Check:**

A simple utility to test if the firewall is blocking external connectivity. Tests TCP connection to `www.broadcom.com` on port 443.

**Usage:**

```bash
python3 checkfw.py
```

**Output:**

- `Good` - Firewall is blocking external access (expected in production)
- `Bad` - External access is available (unexpected in production)

---

### dns_checks.py

**DNS Health Check Module:**

Performs DNS resolution checks for Site A, Site B, and external DNS to verify the lab's DNS infrastructure is working correctly.

**Features:**

- Checks DNS resolution against the Holorouter DNS server (10.1.10.129)
- Validates Site A, Site B, and external DNS resolution
- Retries with timeout for lab startup integration
- Triggers lab failure if DNS checks don't pass within timeout

**Usage:**

```bash
# Run all checks with default timeout (5 minutes)
python3 dns_checks.py

# Run specific check
python3 dns_checks.py --check site_a

# Custom timeout
python3 dns_checks.py --timeout 10

# Use different DNS server
python3 dns_checks.py --dns-server 10.1.10.130
```

**Options:**

| Option | Description |
| -------- | ------------- |
| `-c, --check` | Which check to run: `site_a`, `site_b`, `external`, or `all` |
| `-t, --timeout` | Timeout in minutes (default: 5) |
| `-s, --dns-server` | DNS server to use |
| `-v, --verbose` | Verbose output |

---

### labtypes.py

**Lab Type Execution Path Manager:**

Manages different startup sequences for different lab types (HOL, Discovery, VXP, ATE, EDU). Handles module loading priority and lab-type specific configurations.

**Lab Types:**

| Type | Name | Firewall | Proxy Filter | Description |
|------|------|----------|--------------|-------------|
| HOL | Hands-on Labs | Yes | Yes | Full production labs |
| DISCOVERY | Discovery Labs | No | No | Simplified labs, no firewall restrictions |
| VXP | VCF Experience | Yes | Yes | Demo environments |
| ATE | Advanced Technical Enablement | Yes | No | Instructor-led Livefire labs |
| EDU | Education | Yes | Yes | Training environments |

**Module Loading Priority:**

1. `/vpodrepo/20XX-labs/XXXX/Startup/{module}.py` (vpodrepo Startup override)
2. `/vpodrepo/20XX-labs/XXXX/{module}.py` (vpodrepo root override)
3. `/home/holuser/hol/Startup.{labtype}/{module}.py` (LabType-specific core)
4. `/home/holuser/hol/Startup/{module}.py` (Default core module)

**Usage:**

```python
from Tools.labtypes import LabTypeLoader

loader = LabTypeLoader(
    labtype='HOL',
    holroot='/home/holuser/hol',
    vpod_repo='/vpodrepo/2027-labs/2701'
)

# Get startup sequence
sequence = loader.get_startup_sequence()

# Check if firewall is required
if loader.requires_firewall():
    # Enable firewall
    pass
```

---

### status_dashboard.py

**Lab Startup Status Dashboard:**

Generates an auto-refreshing HTML status page for monitoring lab startup progress. Provides real-time visibility into which startup phases are complete, running, or pending.

**Features:**

- Auto-refreshing HTML dashboard (every 30 seconds)
- Color-coded status indicators (pending, running, complete, failed, skipped)
- Progress bars for overall and per-group progress
- Collapsible task groups
- State persistence across module calls
- Failure banner for critical errors

**Usage:**

```bash
# Generate demo dashboard
python3 status_dashboard.py --sku HOL-2701 --demo

# Initialize fresh dashboard
python3 status_dashboard.py --sku HOL-2701 --init

# Clear dashboard completely
python3 status_dashboard.py --clear
```

**Programmatic Usage:**

```python
from Tools.status_dashboard import StatusDashboard, TaskStatus

dashboard = StatusDashboard('HOL-2701')

# Update task status
dashboard.update_task('prelim', 'dns', TaskStatus.RUNNING)
dashboard.update_task('prelim', 'dns', TaskStatus.COMPLETE)

# Skip an entire group
dashboard.skip_group('vvf', 'VCF lab - VVF not applicable')

# Mark as failed
dashboard.set_failed('DNS resolution failed')

# Generate HTML
dashboard.generate_html()
```

**Output Location:** `/lmchol/home/holuser/startup-status.htm`

---

### tdns_import.py

**DNS Record Import Module:**

Imports custom DNS records into the lab's DNS server using the `tdns-mgr` command-line tool. Supports both inline config.ini records and CSV file imports.

**Features:**

- Reads DNS records from config.ini `[VPOD] new-dns-records`
- Supports CSV file import (`new-dns-records.csv`)
- Automatic PTR record creation with `--ptr` flag
- Login retry logic with configurable attempts

**CSV Format:**

```csv
zone,name,type,value
site-a.vcf.lab,gitlab,A,10.1.10.210
site-a.vcf.lab,harbor,A,10.1.10.212
site-a.vcf.lab,registry,CNAME,gitlab.site-a.vcf.lab
```

**Config.ini Format:**

```ini
[VPOD]
new-dns-records = site-a.vcf.lab,gitlab,A,10.1.10.211
    site-a.vcf.lab,harbor,A,10.1.10.212
```

**Usage:**

```bash
# Auto-detect and import records
python3 tdns_import.py

# Import from specific CSV file
python3 tdns_import.py --csv /path/to/records.csv

# Show what would be imported
python3 tdns_import.py --dry-run

# Show records found in config.ini
python3 tdns_import.py --show-config
```

**Reference:** [tdns-mgr GitHub](https://github.com/burkeazbill/tdns-mgr)

---

### vpodchecker.py

**Lab Validation Tool:**

Validates lab configuration against HOL standards. Checks SSL certificates, vSphere licenses, NTP configuration, and VM settings.

**Checks Performed:**

- **SSL Certificates:** Expiration dates for all HTTPS endpoints, including:
  - URLs from `[RESOURCES]` section
  - ESXi hosts from `[RESOURCES] ESXiHosts` (extracts FQDN before `:`)
  - NSX Managers from `[VCF] vcfnsxmgr`
  - VRA URLs from `[VCFFINAL] vraurls`
- **vSphere Licenses:** Validity and expiration with time-based status:
  - ✅ Green checkmark: License expires >= 9 months from now
  - ⚠️ Warning: License expires < 9 months but >= 3 months from now
  - ❌ Red X: License expires < 3 months from now
  - All status messages include the expiration date
- **NTP Configuration:** ESXi host NTP service and server settings
- **VM Configuration:** `uuid.action`, `typematicMinDelay`, `autolock` settings
- **VM Resources:** Reservations and shares (optional)

**Skipped System VMs:**

The following system VMs are automatically skipped for VM configuration checks (they cannot be modified):

- `vCLS-*` - vSphere Cluster Services VMs
- `vcf-services-platform-template-*` - VCF Services Platform Template VMs
- `SupervisorControlPlaneVM*` - Tanzu Supervisor Control Plane VMs

**Usage:**

```bash
# Run all checks and fix issues
python3 vpodchecker.py

# Report only (no fixes)
python3 vpodchecker.py --report-only

# Output as JSON
python3 vpodchecker.py --json

# Generate HTML report
python3 vpodchecker.py --html /tmp/vpodchecker-report.html

# Verbose output
python3 vpodchecker.py --verbose
```

**Options:**

| Option | Description |
| -------- | ------------- |
| `--report-only` | Don't fix issues, just report |
| `--json` | Output as JSON |
| `--html FILE` | Generate HTML report to specified file |
| `-v, --verbose` | Verbose output |

---

## Shell Scripts

### fwoff.sh / fwon.sh

**Firewall Control (Development Only):**

Toggle the lab firewall on or off. **Only works in development cloud environments** - blocked in production.

**Usage:**

```bash
# Disable firewall (dev only)
./fwoff.sh

# Re-enable firewall (dev only)
./fwon.sh
```

**How it Works:**

- Creates a flag file in `/tmp/holorouter/` that the router watcher processes
- The firewall change is applied on the next router watcher cycle
- Firewall is automatically re-enabled on router reboot

---

### proxyfilteroff.sh / proxyfilteron.sh

**Proxy Filter Control (Development Only):**

Toggle the proxy filter on or off. **Only works in development cloud environments** - blocked in production.

**Usage:**

```bash
# Disable proxy filter (dev only)
./proxyfilteroff.sh

# Re-enable proxy filter (dev only)
./proxyfilteron.sh
```

**How it Works:**

- Creates a flag file in `/tmp/holorouter/` that the router watcher processes
- Works similar to firewall control scripts

---

### holpwgen.sh

**HOL Password Generator:**

Generates a strong, random password that meets HOL complexity requirements.

**Password Requirements:**

- 16 characters long
- At least one lowercase letter
- At least one uppercase letter
- At least one digit
- At least one special character (`!` or `-`)

**Usage:**

```bash
# Generate a password
./holpwgen.sh
# Output: xK7mP2bN!qR5tY9w
```

---

### lkill.sh

**LabStartup Kill Script:**

Terminates running labstartup.py processes. Useful for stopping a lab startup in progress.

**Usage:**

```bash
./lkill.sh
```

**What it Does:**

1. Finds and kills the parent `labstartup.py` process
2. Finds and kills any child Startup module processes

---

### odyssey-launch.sh

**Odyssey Client Launcher:**

Launches the Odyssey desktop client in the background.

**Usage:**

```bash
./odyssey-launch.sh
```

---

### restart_k8s_webhooks.sh

**Kubernetes Webhook Restart Script:**

Deletes expired certificates and restarts Kubernetes webhooks on Supervisor clusters. Required for labs with Tanzu/vSphere with Kubernetes when certificates have expired.

**What it Does:**

1. SSH to vCenter and extract Supervisor credentials using `decryptK8Pwd.py`
2. Delete expired certificate secrets
3. Restart webhook deployments
4. Scale up CCI, ArgoCD, and Harbor replicas

**Usage:**

```bash
# Default vCenter
./restart_k8s_webhooks.sh

# Specify vCenter
./restart_k8s_webhooks.sh vc-wld01-a.site-a.vcf.lab
```

---

### runautocheck.sh

**AutoCheck Runner:**

Executes the lab's AutoCheck validation suite. Searches for AutoCheck scripts in multiple locations.

**Search Order:**

1. `${VPOD_REPO}/autocheck.py` (Python, preferred)
2. `${VPOD_REPO}/autocheck.ps1` (PowerShell)
3. `/media/cdrom0/autocheck.ps1` (Legacy CD-based)

**Usage:**

```bash
./runautocheck.sh
```

**Log File:** `/home/holuser/hol/autocheck.log`

---

### VLPagent.sh

**VLP Agent Management:**

Manages the VLP VM Agent installation and event handling. The VLP Agent enables communication with the lab platform.

**Features:**

- Installs VLP Agent (version 1.0.10)
- Cleans up old agent versions
- Handles prepop and lab start events
- Runs event loop watching for trigger files

**Usage:**

```bash
./VLPagent.sh
```

**Event Triggers:**

- `/tmp/prepop.txt` - Prepop start notification
- `/tmp/labstart.txt` - Lab start notification

---

### watchvcfa.sh

**VCF Automation Watcher:**

Monitors and remediates VCF Automation appliance issues during lab startup. Fixes common problems with containerd, kube-scheduler, and seaweedfs pods.

**Issues Remediated:**

- Stale containerd nodes with `Ready,SchedulingDisabled` status
- Stuck `kube-scheduler` pods (0/1 Running)
- Old `seaweedfs-master-0` pods (over 1 hour old)

**Usage:**

```bash
./watchvcfa.sh
```

---

### vcfapwcheck.sh

**VCF Automation Password Check:**

Checks if the VCF Automation appliance password has expired and resets it if necessary.

**Features:**

- Attempts SSH connection up to 10 times (5 minutes total)
- Detects password expiration prompt
- Automatically runs password reset script if needed

**Usage:**

```bash
./vcfapwcheck.sh
```

---

## Expect Scripts

### sddcmgr.exp

**SDDC Manager Password Expiration Reset:**

Expect script to disable password expiration on SDDC Manager accounts.

**Usage:**

```bash
./sddcmgr.exp <sddc_manager_fqdn> <password>
```

**What it Does:**

- SSH to SDDC Manager as `vcf` user
- Switch to root
- Set password max age to -1 (never expire) for: `vcf`, `root`, `backup`

---

### vcfapass.sh

**VCF Automation Password Reset:**

Expect script to reset the VCF Automation appliance password when it expires.

**Usage:**

```bash
./vcfapass.sh <old_password> <new_password>
```

---

### vcshell.exp

**vCenter Shell Configuration:**

Expect script to enable bash shell for the root user on vCenter Server.

**Usage:**

```bash
./vcshell.exp <vcenter_fqdn> <password>
```

**What it Does:**

- SSH to vCenter as root
- Enter shell from Command prompt
- Change root's shell to `/bin/bash`

---

## Configuration Files

### VMware.config

**Conky Desktop Widget Configuration:**

Configuration file for the Conky desktop widget that displays lab status on the main console.

**Displays:**

- Lab name (HOL-####)
- Hostname and username
- IP address
- Lab startup status

**Location:** Displayed on the bottom-left of the desktop

---

### launch_odyssey.desktop

**Odyssey Desktop Launcher:**

Desktop entry file for launching the Odyssey client from the desktop environment.

---

## Dependencies

Most tools require:

- Python 3.x
- `lsfunctions.py` - Core lab functions
- `pyVmomi` - VMware vSphere API
- `requests` - HTTP API calls
- `sshpass` - SSH password authentication
- `expect` - Interactive command automation

---

## Support

For issues with these tools, contact the HOL Core Team.
