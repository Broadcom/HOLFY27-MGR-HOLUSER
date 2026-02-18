# HOLFY27 Tools

Version 2.1 - January 2026

This folder contains utility scripts and tools for managing HOLFY27 lab environments. These tools support lab development, debugging, automation, and operational tasks.

---

## Table of Contents

- [HOLFY27 Tools](#holfy27-tools)
  - [Table of Contents](#table-of-contents)
  - [Python Scripts](#python-scripts)
    - [cert-replacement.py](#cert-replacementpy)
    - [confighol-9.0.py](#confighol-90py)
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
    - [check\_wcp\_vcenter.sh](#check_wcp_vcentersh)
    - [check\_fix\_wcp.sh](#check_fix_wcpsh)
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
  - [Partner Export](#partner-export)
    - [offline-ready.py](#offline-readypy)
  - [Dependencies](#dependencies)
  - [Support](#support)

---

## Python Scripts

### cert-replacement.py

**VCF Certificate Management Script:**

Manages SSL certificates for VCF infrastructure components using HashiCorp Vault PKI as the Certificate Authority. This tool automates CSR generation, certificate signing, and replacement across VCF components. This is only applicable to vPods using an updated holorouter with vault installed.

**Certificate Replacement Workflow:**

For SDDC Manager-managed resources (SDDC Manager, vCenter, NSX Manager):

1. Generate CSR on the component via SDDC Manager API
2. Retrieve CSR from SDDC Manager
3. Sign CSR with HashiCorp Vault PKI (2-year TTL by default)
4. Upload signed certificate chain via SDDC Manager API
5. SDDC Manager applies the certificate to the component

For non-SDDC-managed resources (VCF Operations, VCF Automation):

1. Generate CSR locally
2. Sign CSR with HashiCorp Vault PKI
3. Replace certificate via component-specific method (SSH/API)

**VCF Components Managed:**

| Component | FQDN | Method | Status |
| ----------- | ------ | -------- | -------- |
| SDDC Manager | sddcmanager-a.site-a.vcf.lab | SDDC Manager API | Automated |
| vCenter | vc-mgmt-a.site-a.vcf.lab | SDDC Manager API | Automated |
| NSX Manager | nsx-mgmt-a.site-a.vcf.lab | SDDC Manager API | Automated |
| VCF Operations | ops-a.site-a.vcf.lab | SSH replacement | Manual |
| VCF Automation | auto-a.site-a.vcf.lab | VCF Operations Manager | Manual |
| VCF Operations for Networks | opsnet-a.site-a.vcf.lab | SSH (TBD) | Manual |

**Default Credentials by Component:**

| Target | API User | SSH User |
| -------- | ---------- | ---------- |
| sddcmanager-a | <administrator@vsphere.local> | vcf / root |
| vc-mgmt-a | <administrator@vsphere.local> | root |
| nsx-mgmt-a | admin | admin |
| ops-a | admin | root |
| auto-a | admin | vmware-system-user |

**Usage:**

```bash
# Replace certificates on all components
python3 cert-replacement.py

# Replace certificate on specific component
python3 cert-replacement.py --target sddcmanager-a.site-a.vcf.lab

# Check certificate expiration only
python3 cert-replacement.py --check-only

# Use custom Vault server
python3 cert-replacement.py --vault-url http://vault.example.com:8200

# Dry run - show what would be done
python3 cert-replacement.py --dry-run
```

**Options:**

| Option | Description |
| -------- | ------------- |
| `--target HOST` | Replace certificate on specific component only |
| `--check-only` | Check certificate expiration without replacing |
| `--dry-run` | Show what would be done without making changes |
| `--vault-url URL` | HashiCorp Vault server URL |
| `--ttl DAYS` | Certificate TTL in days (default: 730 = 2 years) |
| `-v, --verbose` | Verbose output |

**Environment Variables:**

| Variable | Description |
| ---------- | ------------- |
| `VCF_PASS` | VCF password (fallback: `/home/holuser/creds.txt`) |
| `VAULT_TOKEN` | Vault authentication token |

**Prerequisites:**

- HashiCorp Vault PKI engine configured with intermediate CA
- Network connectivity to Vault server
- Valid credentials for VCF components

---

### confighol-9.0.py

**vApp HOLification Tool for VCF 9.0.1:**

> **Naming Convention:** This script is named according to the VCF version it was developed and tested against. The current version `confighol-9.0.py` is written and tested for VCF 9.0.1. Future VCF versions may require a new script (e.g., `confighol-9.1.py` for VCF 9.1.x).

Comprehensive automation tool for "HOLifying" vApp templates after the Holodeck factory build process. This script consolidates and replaces the previous `esx-config.py` and `configvsphere.ps1` scripts into a single, unified tool.

**Execution Order:**

0a. **Vault Root CA Import** (with SKIP/RETRY/FAIL options)

0b. **vCenter CA Import** (with SKIP/RETRY/FAIL options)

1. Pre-checks and environment setup
2. ESXi host configuration
3. vCenter configuration
4. NSX configuration
5. SDDC Manager configuration
6. Operations VMs configuration
7. Final cleanup (vpodchecker)

**Features:**

1. **Vault CA Import (Step 0a):**
   - Runs first, before any other configuration steps
   - Checks if Vault is accessible with a 5-second timeout
   - Downloads and imports the Vault root CA certificate
   - Interactive options when Vault is unavailable:
     - **[S]kip** - Continue without importing (Firefox will show certificate warnings)
     - **[R]etry** - Check Vault again after fixing the issue
     - **[F]ail** - Exit the script with an error
   - Imports the CA into Firefox's certificate store on the console VM

2. **vCenter CA Import (Step 0b):**
   - Runs after Vault CA import
   - Reads vCenter list from `/tmp/config.ini` `[RESOURCES] vCenters`
   - Downloads CA certificates from each vCenter's `/certs/download.zip` endpoint
   - Extracts and imports each CA into Firefox's certificate store
   - Interactive options when vCenter is unavailable (same as Vault)
   - Requires: `libnss3-tools` package (provides `certutil`)

3. **ESXi Host Configuration:**
   - Enables SSH service via vSphere API
   - Configures SSH to auto-start on boot
   - Copies holuser public keys for passwordless SSH access
   - Disables session timeout
   - Detailed success/failure logging for each operation
   - **Note:** Password expiration is not configured on ESXi (chage command not valid)

4. **vCenter Configuration:**
   - Enables bash shell for root user
   - Configures SSH authorized_keys
   - Enables browser support and MOB (Managed Object Browser)
   - Sets password policies (9999 days expiration)
   - Disables HA Admission Control
   - Configures DRS to PartiallyAutomated
   - Creates new vSphere connection if session is invalid
   - Clears ARP cache

5. **NSX Configuration:**
   - Enables SSH via REST API on NSX Managers
   - Configures SSH start-on-boot
   - Removes password expiration for admin, root, audit users

6. **SDDC Manager Configuration:**
   - Configures SSH authorized_keys
   - Sets non-expiring passwords for vcf, backup, root accounts

7. **Operations VMs Configuration:**
   - Checks host reachability (ping) before attempting SSH
   - Checks if SSH port 22 is open
   - Sets non-expiring passwords for root
   - Configures SSH authorized_keys
   - Detailed success/failure logging with error messages
   - Reports authentication failures with username/password info

8. **Final Steps (vpodchecker):**
   - Runs `vpodchecker.py` to configure L2 VMs
   - Sets `uuid.action`, `typematicMinDelay`, `autolock` settings
   - Clears ARP cache on console and router

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
python3 confighol-9.0.py

# Preview what would be done (no changes)
python3 confighol-9.0.py --dry-run

# Skip vCenter shell configuration
python3 confighol-9.0.py --skip-vcshell

# Skip NSX configuration
python3 confighol-9.0.py --skip-nsx

# Only configure ESXi hosts
python3 confighol-9.0.py --esx-only
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
| ------ | ------ | ---------- | -------------- | ------------- |
| HOL | Hands-on Labs | Yes | Yes | Full production labs |
| VXP | VCF Experience | Yes | Yes | Demo environments |
| ATE | Advanced Technical Enablement | Yes | No | Instructor-led Livefire labs |
| EDU | Education | Yes | Yes | Training environments |
| DISCOVERY | Discovery Labs | No | No | Simplified labs, no firewall restrictions |

**Override Priority (applies to all subfolders: Startup, Shutdown, Tools, console, holodeck, holorouter):**

1. `/vpodrepo/20XX-labs/XXXX/{subfolder}/{file}` (lab-specific override)
2. `/vpodrepo/20XX-labs/XXXX/{file}` (lab root override)
3. `/home/holuser/{labtype}/{subfolder}/{file}` (external team override repo)
4. `/home/holuser/hol/{labtype}/{subfolder}/{file}` (in-repo labtype override)
5. `/home/holuser/hol/{subfolder}/{file}` (default core)

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
- Collapsible task groups (auto-collapse completed groups)
- State persistence across module calls
- Failure banner for critical errors
- **Item count tracking** with success/failure breakdown for array-based checks

**Item Count Tracking:**

Tasks can now display detailed counts for operations involving multiple items (VMs, URLs, services, etc.):

```bash
URL Checks ✅
Verify all configured web interfaces
8 items: 8 succeeded
```

```bash
Ping Targets ❌
Verify IP connectivity to configured hosts
12 items: 11 succeeded, 1 failed
Failed: 10.1.10.99
```

Counts are color-coded: green for success, red for failures, purple for skipped.

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

# Update with item counts
dashboard.update_task('urls', 'url_checks', TaskStatus.COMPLETE,
                      total=8, success=8, failed=0)

# Update with partial failures
dashboard.update_task('pings', 'ping_targets', TaskStatus.FAILED,
                      message='Some targets unreachable',
                      total=12, success=11, failed=1)

# Skip an entire group
dashboard.skip_group('vvf', 'VCF lab - VVF not applicable')

# Mark as failed
dashboard.set_failed('DNS resolution failed')

# Generate HTML
dashboard.generate_html()
```

**update_task() Parameters:**

| Parameter | Type | Description |
| ----------- | ------ | ------------- |
| `group_id` | str | Group identifier (e.g., 'urls', 'pings') |
| `task_id` | str | Task identifier within group |
| `status` | TaskStatus/str | Task status |
| `message` | str | Optional status message |
| `total` | int | Total items processed |
| `success` | int | Items that succeeded |
| `failed` | int | Items that failed |
| `skipped` | int | Items that were skipped |

**Output Location:** `/lmchol/home/holuser/startup-status.htm`

---

### tdns_import.py

**DNS Record Import Module:**

Imports custom DNS records into the lab's DNS server using the `tdns-mgr` command-line tool. Supports both inline config.ini records.

**Features:**

- Reads DNS records from config.ini `[VPOD] new-dns-records`
- Automatic PTR record creation with `--ptr` flag
- Login retry logic with configurable attempts

**Config.ini Format:**

```ini
# zone,hostname,record_type,content(IP for A record, FQDN for CNAME record)
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

Validates lab configuration against HOL standards. Checks SSL certificates, vSphere licenses, NTP configuration, VM settings, and password expirations.

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
- **Password Expiration:** User account password expiration for all lab components

**Password Expiration Checks:**

Checks password expiration for user accounts on all lab infrastructure:

| Component | Users Checked | Method |
| ----------- | --------------- | -------- |
| ESXi Hosts | root | `chage -l` via SSH |
| vCenter | `root (Linux), administrator@vsphere.local` | SSH + REST API |
| NSX Manager | admin, root, audit | REST API |
| SDDC Manager | vcf, root, backup | `chage -l` via SSH |
| VCF Automation | vmware-system-user, root | `chage -l` via SSH |
| VCF Operations | admin, root | `chage -l` via SSH |

**Password Expiration Status:**

| Status | Condition |
| -------- | ----------- |
| ✅ PASS | Password never expires, or expires in > 3 years (1095+ days) |
| ✅ PASS | Password expires in > 2 years (730+ days) |
| ❌ FAIL | Password expires in < 2 years (< 730 days) |
| ❌ FAIL | Password already expired |
| ⚠️ WARN | Could not check (connection/auth issues) |

**Lab Year Extraction:**

The tool uses robust pattern matching to extract the lab year from various SKU formats for license expiration validation:

| SKU Format | Example | Extracted Year |
| ------------ | --------- | ---------------- |
| Standard | HOL-2701, ATE-2705 | 27 |
| BETA/Testing | BETA-901-TNDNS | 27 (default) |
| Named | Discovery-Demo | 27 (default) |

The license expiration window is calculated as December 30 of the lab year through January 31 of the following year (e.g., 2027-12-30 to 2028-01-31 for HOLFY27 labs).

**Skipped System VMs:**

The following system VMs are automatically skipped for VM configuration checks (they cannot be modified):

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

### check_wcp_vcenter.sh

**WCP vCenter Services Check Script:**

Verifies that critical vCenter services required for Workload Control Plane (WCP) / Supervisor clusters are running. This script should be run BEFORE starting Supervisor Control Plane VMs.

**Critical Services Checked:**

| Service | Description |
| ------- | ----------- |
| `vapi-endpoint` | vCenter API endpoint service |
| `trustmanagement` | Encryption key delivery to SCP VMs (critical!) |
| `wcp` | Workload Control Plane service |

**What it Does:**

1. Verifies vCenter is reachable
2. Checks each service status via `vmon-cli`
3. Attempts to start any stopped services
4. Returns exit code 5 if services cannot be started

**Usage:**

```bash
# Default vCenter (vc-wld01-a.site-a.vcf.lab)
./check_wcp_vcenter.sh

# Specify vCenter
./check_wcp_vcenter.sh vc-wld01-a.site-a.vcf.lab
```

**Exit Codes:**

| Code | Meaning |
| ---- | ------- |
| 0 | All services running |
| 1 | vCenter not reachable |
| 5 | Could not start required services |

**Note:** This script is also called by `VCFfinal.py` during lab startup when Tanzu/Supervisor is configured.

---

### check_fix_wcp.sh

**WCP Certificate Fix Script:**

Fixes Kubernetes certificates and webhooks on Supervisor Control Plane VMs. This script should be run AFTER Supervisor VMs are started. Replaces the old `restart_k8s_webhooks.sh`.

**What it Does:**

1. SSH to vCenter and extract Supervisor credentials using `decryptK8Pwd.py`
2. Verify Supervisor Control Plane is accessible (with VIP fallback logic)
3. Check hypercrypt and kubelet services on SCP VM
4. Delete expired certificate secrets
5. Restart webhook deployments
6. Scale up CCI, ArgoCD, and Harbor replicas

**Usage:**

```bash
# Default vCenter
./check_fix_wcp.sh

# Specify vCenter
./check_fix_wcp.sh vc-wld01-a.site-a.vcf.lab
```

**Exit Codes:**

| Code | Meaning |
| ---- | ------- |
| 0 | Success |
| 1 | General error |
| 2 | SCP not running (hypercrypt/encryption issue) |
| 3 | Cannot connect to Supervisor |
| 4 | kubectl commands failed |

**Note:** This script is called by `VCFfinal.py` after starting Supervisor VMs.

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

Monitors and remediates VCF Automation appliance issues during lab startup. Fixes common problems with the Kubernetes environment including containerd, kube-scheduler, CSI controller, volume attachments, and seaweedfs pods.

**Issues Remediated:**

| Issue | Symptom | Resolution |
| ----- | ------- | ---------- |
| Stale containerd | Node shows `Ready,SchedulingDisabled` status | Restarts containerd service, uncordons node |
| Stuck kube-scheduler | Pod shows `0/1 Running` | Restarts containerd service |
| Stale seaweedfs-master-0 | Pod older than 1 hour from captured template | Deletes pod to trigger recreation |
| Stuck volume attachments | Pods stuck in `ContainerCreating` with "volume attachment is being deleted" errors | Removes finalizers from stuck VolumeAttachment objects |
| vCenter vAPI endpoint stopped | vCenter REST API returns 503, CSI controller crashes | Starts `vmware-vapi-endpoint` service on vCenter |
| CSI controller issues | vsphere-csi-controller in `CrashLoopBackOff` | Cleans up stale leader leases and restarts controller |

**Stuck Volume Attachments:**

This is a common issue after the VCFA appliance is restored from a template. Volume attachments that existed when the template was captured may have `deletionTimestamp` set but still be marked as attached. The CSI controller can't clean them up because the finalizer (`external-attacher/csi-vsphere-vmware-com`) requires the attacher to detach the volume first.

The script detects VolumeAttachment objects with `deletionTimestamp != null` and removes their finalizers, allowing Kubernetes to delete them and create fresh attachments for the new pods.

**vCenter vAPI Endpoint Check:**

The vsphere-csi-controller requires vCenter's REST API to be available. The script checks the vAPI endpoint by making a request to `https://vc-mgmt-a.site-a.vcf.lab/rest/com/vmware/cis/session`:

- **HTTP 404** - Service is running (expected for unauthenticated request)
- **HTTP 503** - Service Unavailable, `vmware-vapi-endpoint` is stopped

If 503 is detected, the script SSHs to vCenter and runs `service-control --start vmware-vapi-endpoint` to start the service.

**CSI Controller Recovery:**

The vsphere-csi-controller can get stuck in CrashLoopBackOff for several reasons:

1. **vCenter REST API unavailable** - The controller expects the vCenter vAPI endpoint to be running. If `vmware-vapi-endpoint` is stopped, the controller crashes. (Checked and fixed by the vAPI endpoint check above)
2. **Stale leader leases** - When a CSI controller pod is deleted, its leader election leases may persist. The new pod can't become leader and its sidecar containers (csi-attacher, csi-provisioner, etc.) fail.

The script checks for stale leases held by non-existent pods and deletes them, then restarts the CSI controller to pick up leadership.

**Usage:**

```bash
./watchvcfa.sh
```

**Log Files:**

- `/home/holuser/hol/labstartup.log` (main log)
- `/lmchol/hol/labstartup.log` (console log)

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

## Partner Export

### offline-ready.py

**Offline Lab Export Preparation Tool:**

> **Location:** This script is located at `HOLFY27-MGR-AUTOCHECK/Tools/offline-ready.py` and is deployed to `/home/holuser/hol/HOLFY27-MGR-AUTOCHECK/Tools/` on the Manager VM.

Prepares a lab environment for export to a third-party partner who will run the lab without internet access. This is a one-time preparation tool run manually on the Manager VM before exporting the lab.

**What it Does:**

| Step | Description |
| ---- | ----------- |
| 1 | Creates offline-mode marker files to skip git operations on boot |
| 2 | Creates testing flag files (both NFS and local) to skip git clone/pull in labstartup.sh and gitpull.sh |
| 3 | Sets `lockholuser = false` in config.ini and holodeck/*.ini files |
| 4 | Removes external URLs from config.ini and holodeck/*.ini URL checks |
| 5 | Sets passwords on Manager, Router, and Console from creds.txt |
| 6 | Disables VLP Agent startup (creates persistent `.vlp-disabled` marker) |
| 7 | Verifies that /vpodrepo has a local copy of the lab repository |

**Usage:**

```bash
# Preview changes (dry-run)
python3 ~/hol/HOLFY27-MGR-AUTOCHECK/Tools/offline-ready.py --dry-run

# Apply all changes
python3 ~/hol/HOLFY27-MGR-AUTOCHECK/Tools/offline-ready.py

# Apply without confirmation prompt
python3 ~/hol/HOLFY27-MGR-AUTOCHECK/Tools/offline-ready.py --yes
```

**Options:**

| Option | Description |
| -------- | ------------- |
| `--dry-run` | Preview changes without applying them |
| `--yes, -y` | Skip confirmation prompt |
| `--verbose, -v` | Verbose output |

**Prerequisites:**

- Lab should have completed a successful startup at least once
- `/vpodrepo` should contain a valid local copy of the lab repository
- Console and Router VMs should be running and accessible via SSH

See the [HOLFY27-MGR-AUTOCHECK README](../HOLFY27-MGR-AUTOCHECK/README.md#offline-lab-export-partner-preparation) for full documentation.

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
