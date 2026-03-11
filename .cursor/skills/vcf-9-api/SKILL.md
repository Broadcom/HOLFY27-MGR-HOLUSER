---
name: vcf-9-api
description: Guide API interactions with VMware Cloud Foundation (VCF) 9.0 and 9.1 environments running on Holodeck nested virtualization. Provides correct endpoints, credentials, authentication flows, SSH access, dir-cli SSO management, CSI secret structure, SDDC Manager auto-rotate API, credential remediation, resource lock management, PostgreSQL direct access, and version-specific differences for SDDC Manager, VCF Operations (Aria), NSX Manager, NSX Edges, vCenter, VCF Automation, and Supervisor clusters. Use when the user mentions VCF, SDDC Manager, Aria Operations, NSX, vCenter API, VCF Automation, Supervisor, Tanzu, password policies, fleet management, CSI driver, dir-cli, service accounts, auto-rotate, credential remediation, password reset, resource locks, or any VMware Cloud Foundation operations.
---

# VCF 9.x API Interaction Guide

This environment is a **Holodeck nested virtualization lab** running VCF 9.0 or 9.1 on the `site-a.vcf.lab` domain. All passwords default to the contents of `/home/holuser/creds.txt` . SDDC Manager may rotate passwords — always verify via the credentials API if standard password fails.

## Quick Reference: Credentials & Auth

| Component | Hostname | Username | Auth Method |
| --- | --- | --- | --- |
| SDDC Manager | `sddcmanager-a.site-a.vcf.lab` | `vcf` | Basic Auth |
| VCF Operations | `ops-a.site-a.vcf.lab` | `admin` (authSource: `localItem`) | OpsToken |
| NSX Manager (Mgmt) | `nsx-mgmt-01a.site-a.vcf.lab` | `admin` | Basic Auth |
| NSX Manager (WLD) | `nsx-wld01-01a.site-a.vcf.lab` | `admin` | Basic Auth |
| vCenter (Mgmt) | `vc-mgmt-a.site-a.vcf.lab` | `administrator@vsphere.local` | Session Token |
| vCenter (WLD) | `vc-wld01-a.site-a.vcf.lab` | `administrator@wld.sso` | Session Token |
| VCF Automation | `auto-a.site-a.vcf.lab` | `vmware-system-user` | SSH + sudo |
| ESXi Hosts | `esx-01a` through `esx-07a.site-a.vcf.lab` | `root` | Basic Auth |
| Supervisor (SCP) | `10.1.1.188` (9.1) / `10.1.1.85` VIP | Via `decryptK8Pwd.py` | kubeconfig |
| VSP Cluster (CP) | `10.1.1.142` (VIP) | `vmware-system-user` | SSH + sudo |

## Quick Reference: SSH Access

| Target | SSH User | Notes |
| --- | --- | --- |
| vCenter (Mgmt/WLD) | `root` | Password from creds.txt; appliance shell by default, use `shell` to get bash |
| SDDC Manager | `vcf` | No root SSH. Use `su - root` via expect for root access |
| NSX Managers | `admin` (CLI), `root` (bash) | Root may be rotated by SDDC Manager. Check `/v1/credentials` |
| NSX Edges | `admin` (CLI), `root` (bash) | Use `-T` flag (no PTY) for inline commands |
| VCF Automation | `vmware-system-user` | `sudo -S -i` (9.1 requires password). DNS: `auto-a.site-a.vcf.lab` resolves to `10.1.1.70` |
| Operations VMs | `root` | SSH disabled by default. Enable via vSphere Guest Operations API |
| VSP Worker Nodes | `vmware-system-user` | `sudo -S -i` with password from creds.txt |
| Console VM | `holuser` | Ubuntu 24.04 Gnome desktop. `su` for root |

## 1. SDDC Manager API

```bash
# All requests use Basic Auth
SDDC="sddcmanager-a.site-a.vcf.lab"
PASSWORD=$(cat /home/holuser/creds.txt)

# List credentials (check for rotated passwords)
curl -sk -u "vcf:${PASSWORD}" "https://${SDDC}/v1/credentials" | python3 -m json.tool

# List domains
curl -sk -u "vcf:${PASSWORD}" "https://${SDDC}/v1/domains"

# List hosts
curl -sk -u "vcf:${PASSWORD}" "https://${SDDC}/v1/hosts"
```

### Key Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/credentials` | List managed creds (check for rotated passwords!) |
| GET | `/v1/domains` | List VCF domains |
| GET | `/v1/clusters` | List clusters |
| GET | `/v1/hosts` | List ESXi hosts |
| PUT | `/v1/credentials/{id}` | Update/rotate a credential |

## 2. VCF Operations (Aria Operations) API

**Critical**: Internal endpoints require `X-vRealizeOps-API-use-unsupported: true` header.

```python
import requests, urllib3
urllib3.disable_warnings()

OPS = "ops-a.site-a.vcf.lab"
PASSWORD = open("/home/holuser/creds.txt").read().strip()

# Step 1: Acquire OpsToken
token_resp = requests.post(
    f"https://{OPS}/suite-api/api/auth/token/acquire",
    json={"username": "admin", "password": PASSWORD, "authSource": "localItem"},
    headers={"X-vRealizeOps-API-use-unsupported": "true"},
    verify=False
)
token = token_resp.json()["token"]

# Step 2: Use token for API calls
headers = {
    "Authorization": f"OpsToken {token}",
    "X-vRealizeOps-API-use-unsupported": "true"
}

# Query password policies
policies = requests.post(
    f"https://{OPS}/suite-api/internal/passwordmanagement/policies/query",
    json={"page": 0, "pageSize": 100},
    headers=headers, verify=False
)

# Get policy constraints (valid expiry range)
constraints = requests.get(
    f"https://{OPS}/suite-api/internal/passwordmanagement/policies/constraint",
    headers=headers, verify=False
)

# Create a password policy
new_policy = requests.post(
    f"https://{OPS}/suite-api/internal/passwordmanagement/policies",
    json={"name": "MyPolicy", "description": "...", "expiration_days": 729},
    headers=headers, verify=False
)

# Assign policy to VCF Management
requests.post(
    f"https://{OPS}/suite-api/internal/passwordmanagement/policies/{policy_id}/assign",
    json={"assignmentGroup": ["MANAGEMENT"]},
    headers=headers, verify=False
)
```

### Inaccessible Endpoints (Do NOT attempt)

| Path | Why |
| --- | --- |
| `/vcf-operations/rest/ops/internal/*` | Requires browser CSRF/Session — not proxied through suite-api |
| `/suite-api/api/fleet-management/*` | Returns 401 — auth mechanism differs from OpsToken |

## 3. NSX Manager API

**Critical**: User management requires **numeric IDs**, not usernames.

| User | Numeric ID |
| --- | --- |
| root | `0` |
| admin | `10000` |
| audit | `10002` |

```bash
NSX="nsx-mgmt-01a.site-a.vcf.lab"
PASSWORD=$(cat /home/holuser/creds.txt)

# Get user details (MUST use numeric ID)
curl -sk -u "admin:${PASSWORD}" "https://${NSX}/api/v1/node/users/10000"

# Set password expiration via API (CLI may silently reset to 0 on 9.1!)
curl -sk -u "admin:${PASSWORD}" -X PUT \
  "https://${NSX}/api/v1/node/users/10000" \
  -H "Content-Type: application/json" \
  -d '{"password_change_frequency": 9999}'

# List transport nodes (edges)
curl -sk -u "admin:${PASSWORD}" "https://${NSX}/api/v1/transport-nodes"

# Enable SSH on an Edge via transport node API
curl -sk -u "admin:${PASSWORD}" -X POST \
  "https://${NSX}/api/v1/transport-nodes/{node-id}/node/services/ssh?action=start"

# Set Edge user password expiration via transport node API
curl -sk -u "admin:${PASSWORD}" -X PUT \
  "https://${NSX}/api/v1/transport-nodes/{node-id}/node/users/10000" \
  -H "Content-Type: application/json" \
  -d '{"password_change_frequency": 9999}'
```

### Version-Specific Notes

| Behavior | 9.0 | 9.1 |
| --- | --- | --- |
| NSX CLI `set user admin password-expiration 729` | Works | **Silently resets to 0** — use REST API instead |
| Edge SSH enablement | Guest Ops (`systemctl`) | Transport Node API (preferred) |
| Edge SSH CLI commands | `ssh admin@edge "cmd"` | Must use `-T` flag (no PTY allocation) |

## 4. vCenter API

```bash
VC="vc-mgmt-a.site-a.vcf.lab"
PASSWORD=$(cat /home/holuser/creds.txt)

# Get session token (Management vCenter)
SESSION=$(curl -sk -X POST "https://${VC}/api/session" \
  -u "administrator@vsphere.local:${PASSWORD}" | tr -d '"')

# Workload vCenter uses different SSO domain
WLD_SESSION=$(curl -sk -X POST "https://vc-wld01-a.site-a.vcf.lab/api/session" \
  -u "administrator@wld.sso:${PASSWORD}" | tr -d '"')

# List Supervisor clusters
curl -sk -H "vmware-api-session-id: ${SESSION}" \
  "https://${VC}/api/vcenter/namespace-management/clusters"

# Check local account password expiry (root only, NOT SSO users)
curl -sk -H "vmware-api-session-id: ${SESSION}" \
  "https://${VC}/rest/appliance/local-accounts/root"

# vCenter version check via SSH
ssh root@${VC} "com.vmware.appliance.version1.system.version.get"
# Parse: grep '^ *Version: *[0-9]' to get e.g. "9.1.0.0"

# List/start vCenter services via SSH
ssh root@${VC} "vmon-cli --list"
ssh root@${VC} "vmon-cli --status trustmanagement"
ssh root@${VC} "vmon-cli --start trustmanagement"
```

### Known Issues

- `administrator@vsphere.local` is an **SSO user**, not a local account. `/rest/appliance/local-accounts/administrator` returns 404.
- Workload vCenter uses SSO domain `wld.sso`, not `vsphere.local`.
- `vapi-endpoint` and `trustmanagement` services frequently fail to autostart on boot — always verify and start them.

## 5. VCF Automation (Aria Automation)

```bash
# SSH access
VCFA="auto-a.site-a.vcf.lab"  # resolves to 10.1.1.70
PASSWORD=$(cat /home/holuser/creds.txt)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no vmware-system-user@${VCFA}

# Kubernetes API (9.0: 10.1.1.71, 9.1: 10.1.1.72)
# Auto-detect from kubeconfig:
K8S_API=$(sshpass -p "${PASSWORD}" ssh vmware-system-user@${VCFA} \
  "echo '${PASSWORD}' | sudo -S -i grep server /etc/kubernetes/super-admin.conf" \
  2>/dev/null | awk '{print $2}')

# sudo behavior differs by version
# 9.0: sudo -i (NOPASSWD)
# 9.1: echo 'password' | sudo -S -i (password required)
```

### VCF Automation Password Expiry

- `vmware-system-user`: Use `sudo -S chage -M -1 vmware-system-user`
- `root`: Use `sudo -S chage -M -1 root`
- Both require `sudo -S` (pipe password) on the Automation appliance

## 6. VCF Services Runtime (VSP Cluster)

### Architecture

The VSP cluster runs VCF component services as K8s workloads. Gateway LoadBalancer
services expose the cluster endpoints:

| Service | External IP | FQDN | Role |
| --- | --- | --- | --- |
| vmsp-gateway | 10.1.1.131 | `vsp-01a.site-a.vcf.lab` | VSP cluster main |
| vmsp-gateway-0 | 10.1.1.132 | `instance-01a.site-a.vcf.lab` | Instance components |
| vmsp-gateway-1 | 10.1.1.36 | `fleet-01a.site-a.vcf.lab` | Fleet components |
| Control Plane VIP | 10.1.1.142 | — | K8s API (port 6443) |

IP pool: 10.1.1.141-10.1.1.160. VSP VMs are named `vsp-01a-*` (5 nodes: 1 control-plane, 4 workers).

### SSH Access

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# SSH to VSP worker
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no vmware-system-user@vsp-01a.site-a.vcf.lab

# Discover control plane IP from worker's kubeconfig
# IMPORTANT: workers have node-agent.conf, NOT super-admin.conf
echo "${PASSWORD}" | sudo -S -i grep server /etc/kubernetes/node-agent.conf
# Output: server: https://10.1.1.142:6443

# SSH to control plane directly
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no vmware-system-user@10.1.1.142
```

### Component CRDs (CRITICAL: cluster-scoped, NOT namespaced)

```bash
# components.api.vmsp.vmware.com is cluster-scoped (NAMESPACED=false)
# Do NOT use -A flag (produces <none> namespace)
# Do NOT use -n flag on annotate/get (silent failure)
echo "${PASSWORD}" | sudo -S -i kubectl get components.api.vmsp.vmware.com

# Check annotation status — use -o json parsed locally (see SSH Escaping below)
echo "${PASSWORD}" | sudo -S -i kubectl get components.api.vmsp.vmware.com -o json

# Annotate component (NO -n flag — cluster-scoped)
echo "${PASSWORD}" | sudo -S -i kubectl annotate components.api.vmsp.vmware.com salt \
  component.vmsp.vmware.com/operational-status=Running --overwrite
```

### Component Namespaces

| Namespace | Workloads | Component CRD |
| --- | --- | --- |
| salt | salt-master, salt-minion | salt |
| salt-raas | raas, redis, pgdatabase | salt-raas |
| telemetry | telemetry-acceptor | telemetry-acceptor |
| vcf-fleet-depot | depot-service, distribution-service | vcf-fleet-depot |
| vcf-fleet-lcm | fleet-build, fleet-upgrade, fleet-lcm-db | vcf-fleet-lcm |
| vcf-sddc-lcm | sddc-build, sddc-upgrade, sddc-lcm-db | vcf-sddc-lcm |
| vidb-external | vidb-service, vidb-postgres-instance | vidb |
| ops-logs | log-processor, log-store | ops-logs |
| vodap | vcf-obs-* collectors, ClickHouse (chi-*, chk-*) | vcf-obs-data-platform |
| vmsp-metrics-store | clickhouse-operator, vsp-metrics-store-operator | vcfms-metrics-store |

### Scale Up Stopped Components

```bash
# Unsuspend postgres (two-step: remove label + restore numberOfInstances)
echo "${PASSWORD}" | sudo -S -i kubectl label postgresinstances.database.vmsp.vmware.com \
  --all -n salt-raas database.vmsp.vmware.com/suspended-
echo "${PASSWORD}" | sudo -S -i kubectl label postgresinstances.database.vmsp.vmware.com \
  --all -n vidb-external database.vmsp.vmware.com/suspended-

# Scale up deployments/statefulsets
echo "${PASSWORD}" | sudo -S -i kubectl scale deployment/salt-master -n salt --replicas=1

# Annotate component CRD (cluster-scoped — NO -n flag)
echo "${PASSWORD}" | sudo -S -i kubectl annotate components.api.vmsp.vmware.com salt \
  component.vmsp.vmware.com/operational-status=Running --overwrite
```

### SSH Escaping Pitfall (lsf.ssh + vsp_kubectl)

**CRITICAL**: `lsf.ssh()` wraps commands in double quotes: `sshpass -p pw ssh opts target "command"`.
When combined with `vsp_kubectl`'s `bash -c '...'` wrapper, kubectl `custom-columns` with
dotted annotation keys (`component\\.vmsp\\.vmware\\.com`) gets mangled through 4 escaping
layers (Python → subprocess → SSH → bash -c). The annotation column silently returns `<none>`.

**Fix**: Use `-o json` and parse the JSON response locally in Python (`json.loads()`) instead of
trying to use `custom-columns` or `jsonpath` with dotted keys through SSH. Strip the SSH banner
(`Welcome to Photon...`) by finding the first `{` in stdout before parsing.

### Shutdown Scripts

Graceful shutdown/startup of VSP components is managed by:
- **Shutdown**: `hol/Shutdown/VCFshutdown.py` Phase 2b (scale down) + Phase 19b (power off VSP VMs)
- **Supervisor Workloads**: Phase 3b dynamically discovers and shuts down VKS clusters and Supervisor Services (Harbor, etc.) via the SCP K8s API before WCP is stopped
- **Dynamic VM Discovery**: Phase 4 discovers Supervisor-managed workload VMs from WLD vCenter in addition to regex pattern matching
- **Startup**: `hol/Startup/VCFfinal.py` Task 2e (scale up + unsuspend postgres)
- **Config**: `/tmp/config.ini` `[VCFFINAL] vcfcomponents` (format: `namespace:resource_type/name`)
- **Standalone**: `python3 Shutdown.py --phase 2b` (scale down only)
- **Dry run**: `python3 Shutdown.py --phase 3b --dry-run` (preview Supervisor workload shutdown)

## 7. Operations VMs (SSH Enablement)

SSH is disabled by default on Operations VMs. Enable via vSphere Guest Operations API:

```python
from pyVim.connect import SmartConnect
from pyVmomi import vim
import ssl

si = SmartConnect(host="vc-mgmt-a.site-a.vcf.lab",
                  user="administrator@vsphere.local",
                  pwd=PASSWORD, sslContext=ssl._create_unverified_context())

vm = si.content.searchIndex.FindByDnsName(None, "ops-a.site-a.vcf.lab", True)
creds = vim.vm.guest.NamePasswordAuthentication(username="root", password=PASSWORD)
pm = si.content.guestOperationsManager.processManager

spec = vim.vm.guest.ProcessManager.ProgramSpec(
    programPath="/usr/bin/systemctl",
    arguments="enable --now sshd"
)
pm.StartProgramInGuest(vm, creds, spec)
```

## 8. vCenter SSO Service Account Management (dir-cli)

The `dir-cli` utility on vCenter (`/usr/lib/vmware-vmafd/bin/dir-cli`) manages SSO service accounts. These accounts (e.g., `svc-vcfsp-vc-*@vsphere.local`) are used by CSI drivers, VCF Automation, and other components.

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Find a service account
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli user find-by-name --account svc-vcfsp-vc-zna836 \
   --login administrator@vsphere.local --password '${PASSWORD}'"

# Reset a service account password (user modify does NOT support password changes)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli password reset --account svc-vcfsp-vc-zna836 \
   --new 'NewPassword123!' --login administrator@vsphere.local --password '${PASSWORD}'"

# Set password to never expire
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli user modify --account svc-vcfsp-vc-zna836 \
   --password-never-expires --login administrator@vsphere.local --password '${PASSWORD}'"
```

### CSI Secret Structure on VCF Automation

The vSphere CSI driver on VCF Automation (`auto-a`, K8s API at `10.1.1.71`) uses two secrets in `kube-system`:

| Secret | Key | Content |
| --- | --- | --- |
| `vsphere-config-secret` | `csi-vsphere.conf` | Full CSI config with user, password, thumbprint, datacenter |
| `vsphere-cloud-secret` | `vc-mgmt-a.site-a.vcf.lab.password` | Just the password |
|  | `vc-mgmt-a.site-a.vcf.lab.username` | Just the username |

Both must be updated when the service account password is reset.

## 9. SDDC Manager Auto-Rotate API

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
TOKEN=$(curl -sk -X POST "https://sddcmanager-a.site-a.vcf.lab/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('accessToken',''))")

# List credentials with auto-rotate
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://sddcmanager-a.site-a.vcf.lab/v1/credentials" | python3 -c "
import json,sys
for el in json.load(sys.stdin).get('elements', []):
    ar = el.get('autoRotatePolicy', {})
    if ar.get('frequencyInDays'):
        print(f'{el[\"username\"]} ({el[\"credentialType\"]}): rotate every {ar[\"frequencyInDays\"]}d')
"

# Disable auto-rotate (requires resource in ACTIVE state)
curl -sk -X PATCH -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "https://sddcmanager-a.site-a.vcf.lab/v1/credentials" \
  -d '{"operationType":"UPDATE","elements":[{
    "resourceName":"vc-mgmt-a.site-a.vcf.lab",
    "resourceType":"VCENTER",
    "credentials":[{"credentialType":"SSH","username":"root",
      "autoRotatePolicy":{"frequencyInDays":0,"enableAutoRotatePolicy":false}}]
  }]}'
```

**Limitation**: SDDC Manager requires the resource to be in ACTIVE state. In partially-started labs, resources may show ERROR status, causing the PATCH to fail with `RESOURCE_IS_NOT_IN_ACTIVE_STATE`.

## 10. SDDC Manager Credential Remediation API

SDDC Manager's `PATCH /v1/credentials` endpoint supports three operation types:
- **UPDATE**: Changes the password on both SDDC Manager's database AND the target component. Does NOT work for service accounts.
- **ROTATE**: Generates a new password and applies it to the target component. Used for service accounts (`svc-*`).
- **REMEDIATE**: Tells SDDC Manager "the password on the target is already X, update your records." Used when you've already set the password externally.

### Authentication (Bearer Token, NOT Basic Auth)

Credential operations require a Bearer token, not Basic Auth:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
SDDC="sddcmanager-a.site-a.vcf.lab"

TOKEN=$(curl -sk -X POST "https://${SDDC}/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('accessToken',''))")
```

### List Credentials and Their Status

```bash
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/credentials" | python3 -c "
import json,sys
for el in json.load(sys.stdin).get('elements', []):
    print(f\"{el['resource']['resourceName']:40s} {el['username']:50s} {el['credentialType']:15s} {el.get('accountStatus','UNKNOWN')}\")
"
```

### REMEDIATE a Single Resource

```bash
curl -sk -X PATCH -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "https://${SDDC}/v1/credentials" \
  -d '{
    "operationType": "REMEDIATE",
    "elements": [{
      "resourceName": "esx-01a.site-a.vcf.lab",
      "resourceType": "ESXI",
      "credentials": [{
        "credentialType": "SSH",
        "username": "root",
        "password": "'"${PASSWORD}"'"
      }]
    }]
  }'
```

### Credential Resource Types

| resourceType | Example Resources |
| --- | --- |
| `ESXI` | `esx-01a.site-a.vcf.lab` through `esx-07a` |
| `VCENTER` | `vc-mgmt-a.site-a.vcf.lab`, `vc-wld01-a.site-a.vcf.lab` |
| `NSXT_MANAGER` | `nsx-mgmt-01a.site-a.vcf.lab`, `nsx-wld01-01a.site-a.vcf.lab` |
| `NSXT_EDGE` | `edge-mgmt-01a.site-a.vcf.lab`, `edge-wld01-01a.site-a.vcf.lab` |
| `PSC` | Same FQDNs as vCenters (SSO admin credentials) |
| `BACKUP` | Same FQDNs (backup credentials — skip these in bulk ops) |

### Concurrency Limit

SDDC Manager enforces a maximum of **10 concurrent credential operations**. If you see `HTTP 403` with "you have reached the maximum number of 10 concurrent update/rotate passwords operations", you must wait for existing tasks to complete or clear them.

### Monitor Credential Task Progress

```bash
TASK_ID="<from PATCH response>"
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/tasks/${TASK_ID}" | python3 -c "
import json,sys
t = json.load(sys.stdin)
print(f\"Status: {t['status']}\")
for sub in t.get('subTasks', []):
    print(f\"  {sub['name']}: {sub['status']} {sub.get('description','')}\")
"
```

## 11. SDDC Manager PostgreSQL Direct Access

When API-level operations are blocked by stale locks or inconsistent resource statuses, direct PostgreSQL access on SDDC Manager can be used as a last resort.

### SSH + PostgreSQL Connection

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# SSH as vcf, then su to root via expect (su requires a TTY)
# The PostgreSQL password is in /root/.pgpass (base64-encoded format)
# Connection: TCP on 127.0.0.1:5432, user postgres, database platform

sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vcf@sddcmanager-a.site-a.vcf.lab \
  "export PGPASSWORD='iHk0JKypNFrR9C5iOI2PmBmUCfSbdrjFxaGoxEEFz3w='; \
   /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform -c \"SELECT 1\""
```

**Note**: The `PGPASSWORD` value above was discovered in `/root/.pgpass`. It may differ per deployment.

### Clear Stale Resource Locks

```bash
# Resource locks prevent credential operations when previous tasks failed/hung
/usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform \
  -c "DELETE FROM lock"
```

### Fix Resource Statuses

Resources stuck in `ERROR` or `ACTIVATING` state block credential operations with "Resources [...] are not available/ready."

```bash
/usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform \
  -c "UPDATE host SET status = 'ACTIVE'" \
  -c "UPDATE nsxt SET status = 'ACTIVE'" \
  -c "UPDATE vcenter SET status = 'ACTIVE'" \
  -c "UPDATE nsxt_edge_cluster SET status = 'ACTIVE'" \
  -c "UPDATE domain SET status = 'ACTIVE'"
```

### Restart SDDC Manager Services After DB Changes

```bash
# Must restart services to pick up DB changes
systemctl restart operationsmanager commonsvcs domainmanager
```

### Key Database Tables

| Database | Table | Purpose |
| --- | --- | --- |
| `platform` | `lock` | Resource-level locks for credential operations |
| `platform` | `host` | ESXi host records (status column) |
| `platform` | `nsxt` | NSX Manager records (status column) |
| `platform` | `vcenter` | vCenter records (status column) |
| `platform` | `nsxt_edge_cluster` | NSX Edge cluster records (status column) |
| `platform` | `domain` | VCF domain records (status column) |
| `platform` | `credential` | Credential records (encrypted secrets with IV) |
| `operationsmanager` | `task` | Credential operation task records |
| `operationsmanager` | `processing_task` | Active task tracking |
| `operationsmanager` | `execution` | Task execution history |

## 12. SDDC Manager Service Account Dependencies

When SDDC Manager performs credential operations, it authenticates to vCenter using SSO service accounts. If these service account passwords are wrong on the vCenter SSO side, all credential operations fail with "Cannot complete login due to incorrect credentials."

### Identify Service Accounts

Service accounts follow the pattern `svc-sddcmanager-a-vc-<vcenter>-<id>@<sso-domain>` and `svc-nsx-<domain>-vc-<vcenter>-<id>@<sso-domain>`.

```bash
# List service accounts from SDDC Manager credentials
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/credentials" | python3 -c "
import json,sys
for el in json.load(sys.stdin).get('elements', []):
    if el['username'].startswith('svc-'):
        print(f\"{el['resource']['resourceName']:40s} {el['username']}\")
"
```

### Reset Service Account Passwords on vCenter SSO

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Management vCenter (SSO domain: vsphere.local)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli password reset \
   --account svc-sddcmanager-a-vc-mgmt-a-9382 \
   --new '${PASSWORD}' \
   --login administrator@vsphere.local --password '${PASSWORD}'"

# Workload vCenter (SSO domain: wld.sso)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-wld01-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli password reset \
   --account svc-sddcmanager-a-vc-wld01-a-7530 \
   --new '${PASSWORD}' \
   --login administrator@wld.sso --password '${PASSWORD}'"
```

**Note**: The service account IDs (e.g., `-9382`, `-7530`) are unique per deployment. Query `/v1/credentials` to find the exact names.

## 13. VCF Operations Version Differences

| Behavior | VCF 9.0 | VCF 9.1 |
| --- | --- | --- |
| OpsToken authSource | `"local"` | `"localItem"` |
| Password management API | Not available (404) | `/suite-api/internal/passwordmanagement/policies/*` |
| Fleet CA certificate name | `VCF Operations Fleet Management Locker CA` | `Broadcom, Inc CA` |
| suite-api lifecycle actions | HTTP 500 (not supported) | HTTP 500 (not supported) — use fleet-lcm direct API |
| VCF Automation `sudo` | NOPASSWD | Password required (`echo pw \| sudo -S -i`) |

## 14. VSP & Supervisor Proxy Configuration

### Supervisor Proxy (via vCenter API)

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
WLD_VC="vc-wld01-a.site-a.vcf.lab"

# Get session
SESSION=$(curl -sk -X POST "https://${WLD_VC}/api/session" \
  -u "administrator@wld.sso:${PASSWORD}" | tr -d '"')

# Find Supervisor cluster ID
CLUSTER_ID=$(curl -sk -H "vmware-api-session-id: ${SESSION}" \
  "https://${WLD_VC}/api/vcenter/namespace-management/clusters" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['cluster'])")

# Configure proxy (both HTTP and HTTPS in one call)
curl -sk -X PATCH -H "vmware-api-session-id: ${SESSION}" \
  -H "Content-Type: application/json" \
  "https://${WLD_VC}/api/vcenter/namespace-management/clusters/${CLUSTER_ID}" \
  -d '{
    "cluster_proxy_config": {
      "proxy_settings_source": "CLUSTER_CONFIGURED",
      "http_proxy_config": "http://10.1.1.1:3128",
      "https_proxy_config": "http://10.1.1.1:3128"
    }
  }'
```

### VSP Node Proxy (Photon OS)

VSP nodes are Photon OS 5.0 VMs. Configure proxy at four levels:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
PROXY="http://10.1.1.1:3128"
NO_PROXY="localhost,127.0.0.1,10.1.1.0/24,10.96.0.0/12,172.16.0.0/12,.site-a.vcf.lab,.svc,.cluster.local,.svc.cluster.local,10.1.0.0/24,registry.vmsp-platform.svc.cluster.local"

# Discover VSP node IPs from control plane
VSP_NODES=$(sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vmware-system-user@10.1.1.142 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}'" 2>/dev/null)

# For each node, configure:
# 1. /etc/sysconfig/proxy (Photon proxy framework)
# 2. /etc/environment (system-wide env vars)
# 3. /etc/systemd/system/containerd.service.d/http-proxy.conf
# 4. /etc/systemd/system/kubelet.service.d/http-proxy.conf
# 5. systemctl daemon-reload
```

### Proxy Settings Reference

| Setting | Value |
| --- | --- |
| Proxy server | `http://10.1.1.1:3128` (holorouter Squid) |
| NO_PROXY | `localhost,127.0.0.1,10.1.1.0/24,10.96.0.0/12,172.16.0.0/12,.site-a.vcf.lab,.svc,.cluster.local,.svc.cluster.local,10.1.0.0/24,registry.vmsp-platform.svc.cluster.local` |
| Supervisor source | `CLUSTER_CONFIGURED` (overrides `VC_INHERITED` default) |
| Automated by | `confighol-9.1.py` v2.9+ (Step 9) |

## 15. VCF Operations Certificate Management API

> **Consolidated reference**: For the complete certificate management guide (MSADCS proxy, SDDC Manager cert workflow, Vault signing, PKCS#7 ordering, troubleshooting), see the `vcf-certs` skill.

The VCF Operations internal API manages TLS certificates for fleet-managed components (VCF Automation, Log Management, Operations for Networks, Identity Broker, VCF services runtimes).

### Authentication

```python
import requests
requests.packages.urllib3.disable_warnings()

OPS = "ops-a.site-a.vcf.lab"
PASSWORD = open("/home/holuser/creds.txt").read().strip()

# Acquire OpsToken (try "local" first, then "localItem")
resp = requests.post(f"https://{OPS}/suite-api/api/auth/token/acquire",
    json={"username": "admin", "authSource": "local", "password": PASSWORD},
    verify=False)
token = resp.json()["token"]

session = requests.Session()
session.verify = False
session.headers.update({
    "Authorization": f"OpsToken {token}",
    "Content-Type": "application/json",
    "X-vRealizeOps-API-use-unsupported": "true"
})

# CRITICAL: Initialize the cert management session by calling the internal endpoint
session.post(f"https://{OPS}/vcf-operations/rest/ops/internal/certificatemanagement/certificates/query",
    json={"vcfComponent": "VCF_MANAGEMENT", "vcfComponentType": "ARIA"})
```

### Query Certificates

```python
resp = session.post(
    f"https://{OPS}/suite-api/internal/certificatemanagement/certificates/query",
    json={"vcfComponent": "VCF_MANAGEMENT", "vcfComponentType": "ARIA"})
# Response key: "vcfCertificateModels" (list of all cert types)
certs = resp.json()["vcfCertificateModels"]
tls_certs = [c for c in certs if c["category"] == "TLS_CERT"]
# Key fields: certificateResourceKey, applianceIp, issuedToCommonName, issuedBy,
#   displayApplianceType, displayStatus, appliance (enum like ARIA_AUTOMATION)
```

### Generate CSR

```python
payload = {
    "commonCsrData": {
        "country": "US", "email": "", "keySize": "KEY_2048",
        "keyAlgorithm": "RSA", "locality": "Palo Alto",
        "organization": "Broadcom", "orgUnit": "vcfms", "state": "CA"
    },
    "componentCsrData": [{
        "certificateId": "<certificateResourceKey>",
        "commonName": "target-fqdn",
        "subjectAltNames": {"dns": ["target-fqdn"], "ip": ["10.1.1.X"]}
    }]
}
resp = session.post(f"https://{OPS}/suite-api/internal/certificatemanagement/csrs", json=payload)
# CRITICAL: keySize must be enum: UNKNOWN, KEY_2048, KEY_3072, KEY_4096
```

### List CSRs

```python
resp = session.get(f"https://{OPS}/suite-api/internal/certificatemanagement/csrs")
# Response key: "certificateSignatureInfo" (NOT "csrDetails")
# CSR PEM is in "csr" field (NOT "csrContent")
# CSR uses spaces instead of newlines — MUST normalize before Vault signing
```

### Import Signed Certificate

```python
payload = {
    "certificates": [{
        "name": "vault-target-timestamp",
        "source": "PASTE",
        "certificate": server_cert_pem + "\n" + ca_cert_pem
    }]
}
resp = session.put(
    f"https://{OPS}/suite-api/internal/certificatemanagement/repository/certificates/import",
    json=payload)
```

### List Repository Certificates

```python
# CRITICAL: page and pageSize params are REQUIRED, otherwise returns HTTP 500
resp = session.get(
    f"https://{OPS}/suite-api/internal/certificatemanagement/repository/certificates",
    params={"page": 0, "pageSize": 100})
# Response key: "vcfRepositoryCertificates" (NOT "repositoryCertificateModels")
# Cert ID field: "certId" (NOT "certificateId")
# Key fields: certId, name, commonName, issuer, inUse, caType
```

### Replace Certificate

```python
payload = {
    "caType": "EXTERNAL_CA",
    "certificatesMapping": [{
        "certificateId": "<certificateResourceKey from query>",
        "importedCertificateId": "<certId from repo>"
    }]
}
resp = session.put(
    f"https://{OPS}/suite-api/internal/certificatemanagement/certificates/replace",
    json=payload)
# Response includes subTasksDetails with orchestratorType (VROPS or VRSLCM)
```

### Orchestrator Types and Limitations

| Component | Orchestrator | Behavior |
| --- | --- | --- |
| VCF services runtime (fleet-01a, instance-01a, vsp-01a) | VROPS | Replacement completes within minutes |
| Identity broker (vidb-a) | VROPS | Replacement completes within minutes |
| Log management (opslogs-a) | VROPS | Replacement completes within minutes |
| VCF Ops for networks (opsnet-a) | VROPS | Replacement completes within minutes |
| VCF Automation (auto-a, auto-platform-a) | VRSLCM | Depends on fleet-upgrade-service health |
| VCF Operations (ops-a) | VRSLCM | Depends on fleet-upgrade-service health |

**VRSLCM orchestrator limitation**: The VRSLCM orchestrator delegates to the `fleet-upgrade-service` running on the VSP cluster. If this service is unhealthy (returns `VCF_LCM_500_INTERNAL_SERVER_ERROR`), replacement tasks stay at `NOT_STARTED` indefinitely. The CSR/sign/import steps succeed but the final replacement never executes.

### Task Status API (Unreliable)

```python
# Task status endpoint returns HTTP 500 for most tasks
resp = session.get(f"https://{OPS}/suite-api/internal/certificatemanagement/tasks/{task_id}")
# Workaround: Poll the certificate query endpoint and check issuedBy field change
```

### Fleet-Managed Targets and Certificate Keys

| Target | Component | Certificate Key |
| --- | --- | --- |
| auto-a.site-a.vcf.lab | ARIA_AUTOMATION | e10c3710-b85f-32b8-bdfb-6185932903f1 |
| auto-platform-a.site-a.vcf.lab | ARIA_AUTOMATION | a3ac49bf-a649-3232-9b17-8cab0fda02f5 |
| ops-a.site-a.vcf.lab | ARIA_OPERATION | 8a3c3ddc-ce65-35ff-a3a1-7fc5005134f3 |
| opslogs-a.site-a.vcf.lab | ARIA_LOGS | e0cc9b82-b58e-3200-9baf-5ca052a80128 |
| vidb-a.site-a.vcf.lab | V_IDB | a5348e66-3817-3507-925e-03f6efc2b5ad |
| fleet-01a.site-a.vcf.lab | VMSP_PLATFORM | 869fe28e-d4c8-36cd-b811-bf819f1416e3 |
| instance-01a.site-a.vcf.lab | VMSP_PLATFORM | 9885f72c-b252-38bf-a4fe-c2c86a8212fa |
| vsp-01a.site-a.vcf.lab | VMSP_PLATFORM | f054476f-d26e-3279-b46c-4dfcc1f0f12e |
| opsnet-a (10.1.1.60) | ARIA_NETWORK | 29cfd82e-dec4-30f1-83a2-2c68ab59dac5 |

## Critical Pitfalls Discovered

1. **NSX user IDs are numeric**: `/api/v1/node/users/admin` = 404. Use `/api/v1/node/users/10000`.
2. **SDDC Manager rotates passwords**: Always check `GET /v1/credentials` if standard password fails.
3. **NSX CLI password-expiration broken on 9.1**: `set user admin password-expiration 729` silently resets to 0. Use REST API.
4. **VCF Operations internal APIs need special header**: `X-vRealizeOps-API-use-unsupported: true`.
5. **vCenter SSO domain differs**: Management = `@vsphere.local`, Workload = `@wld.sso`.
6. **VCF Automation sudo changed in 9.1**: No longer NOPASSWD. Use `echo 'pw' | sudo -S -i`.
7. **NSX Edge SSH requires -T flag**: PTY allocation causes connection drops for inline commands.
8. **vCenter services may not autostart**: `vapi-endpoint` and `trustmanagement` frequently need manual start after cold boot.
9. **Component CRDs are cluster-scoped**: `components.api.vmsp.vmware.com` is NOT namespaced. Using `-A` produces `<none>` namespace columns; using `-n` on annotate silently fails. Omit both.
10. **SSH escaping breaks kubectl custom-columns**: Dotted annotation keys (e.g., `component\\.vmsp\\.vmware\\.com/...`) get mangled through SSH+sudo+bash-c layers. Use `-o json` and parse locally in Python instead.
11. **VSP worker kubeconfig path differs**: Workers use `/etc/kubernetes/node-agent.conf`; only the control plane has `super-admin.conf`.
12. **Postgres suspension is two-step**: Must set label `database.vmsp.vmware.com/suspended=true` AND patch Zalando `postgresqls.acid.zalan.do` `numberOfInstances` to 0. Both are needed for clean shutdown matching the startup unsuspend.
13. **ClickHouse in vodap managed by operator**: The `chi-vcf-obs-*` and `chk-vcf-obs-keeper-*` statefulsets are managed by clickhouse-operator (in vmsp-metrics-store). Scaling down the operator alone does not stop ClickHouse pods — must scale the statefulsets directly.
14. **dir-cli `user modify` does NOT change passwords**: Use `password reset` subcommand instead. `user modify --password-never-expires` only controls expiration policy.
15. **vCenter REST API `/api/session` returns 201 not 200**: The POST to create a session returns HTTP 201 (Created) on success, not 200. Always accept both `200` and `201` when checking session creation.
16. **SCP VMs are EAM-managed on VCF 9.0.x**: `PowerOnVM_Task()` via vCenter returns `vim.fault.NoPermission`. Must connect directly to the hosting ESXi host as `root` to power on/off Supervisor Control Plane VMs.
15. **VCF upgrade rotates CSI service account passwords**: After VCF upgrades (e.g., 9.0.0 to 9.0.1), the `svc-vcfsp-vc-*@vsphere.local` password in vCenter SSO may no longer match the K8s secrets. Both `vsphere-config-secret` and `vsphere-cloud-secret` in kube-system namespace must be updated.
16. **vCenter SSH host keys change on upgrade**: VCF upgrades regenerate vCenter SSH host keys. Use `ssh-keygen -R` to remove old keys and `-o StrictHostKeyChecking=accept-new` for all SSH commands.
17. **VCF Automation microservices don't auto-scale after shutdown**: The ~50 deployments in the `prelude` namespace stay at 0 replicas. The `vcfa-service-manager` reconciles addons/CRDs but does not restore replica counts. Must be manually scaled to 1.
18. **NSX Edge `/root/.ssh/` directory missing**: NSX Edges don't have `/root/.ssh/` by default. Must `mkdir -p /root/.ssh && chmod 700 /root/.ssh` before copying authorized_keys.
19. **VCF Operations Fleet CA extraction**: The Fleet Operations root CA can be extracted from the TLS chain at `ops-a:443` using `openssl s_client -showcerts`. It's the last cert in the chain (self-signed, O=VMware, CN=VCF Operations Fleet Management Locker CA).
20. **SDDC Manager credential `UPDATE` vs `REMEDIATE`**: `UPDATE` changes the password on the target component AND SDDC Manager's DB. `REMEDIATE` only updates SDDC Manager's stored credential (assumes you already changed it externally). `UPDATE` does NOT work for service accounts — use `ROTATE` instead.
21. **SDDC Manager resource locks block credential ops**: Failed or hung credential tasks leave stale entries in the `platform.lock` table. The `/v1/resource-locks` API does NOT support DELETE. Must clear locks via direct PostgreSQL access (`DELETE FROM lock`).
22. **SDDC Manager resource status must be ACTIVE**: Credential operations fail with "Resources [...] are not available/ready" if the host/nsxt/vcenter/domain status is `ERROR` or `ACTIVATING`. Fix via direct DB update, not API.
23. **SDDC Manager PostgreSQL uses TCP, not socket**: `psql` must connect via `-h 127.0.0.1`, not the default Unix socket (which doesn't exist). Password is in `/root/.pgpass`.
24. **SDDC Manager `su - root` requires TTY**: SSH to SDDC Manager as `vcf` then `su - root` fails with "must be run from a terminal" unless `ssh -t` or `expect` is used to allocate a PTY.
25. **SDDC Manager service account dependencies are circular**: Credential remediation for ESXi/NSX/vCenter requires SDDC Manager to authenticate to vCenter via SSO service accounts (`svc-sddcmanager-a-vc-*`). If those service account passwords are wrong in vCenter SSO, ALL credential operations fail — even for unrelated resources like ESXi hosts. Fix the service accounts first via `dir-cli password reset` on each vCenter.
26. **SDDC Manager 10-task concurrency limit**: Maximum 10 concurrent credential update/rotate operations. Failed/cancelled tasks count against this limit until they expire. Clear by restarting `operationsmanager` service or waiting.
27. **NSX `set service ssh start-on-boot` fails if already set**: The CLI command may return a non-zero exit code when the setting is already enabled. Always check with `get service ssh start-on-boot` first, and verify state after setting. Treat `true`/`enabled` in the output as success.
28. **NSX Manager `/root/.ssh/` may also be missing**: Not just Edges — NSX Managers can also lack `/root/.ssh/`. Always `mkdir -p /root/.ssh && chmod 700 /root/.ssh` before SCP of authorized_keys.
29. **vCenter SSH/shell can be enabled via REST API**: Use `PUT /api/appliance/access/ssh` (json body: `true`) and `PUT /api/appliance/access/shell` (json body: `{"enabled": true, "timeout": 0}`) to enable SSH and bash shell access without needing SSH first. Useful in labstartup when confighol hasn't run yet.
30. **Broadcom, Inc CA not present in all VCF 9.1 labs**: The VCF Operations Fleet CA name `Broadcom, Inc CA` (documented for 9.1) may not be imported into Firefox in all deployments. Do not hard-code it as an expected/required CA.
31. **CCI Kubernetes API returns HTTP 500 on unauthenticated requests in VCF 9.1 C2**: The CCI endpoint (`/cci/kubernetes/apis/project.cci.vmware.com/v1alpha2/projects`) returns 500 (not 401) when accessed without authentication. Treat 401, 403, and 500 from CCI URLs as evidence the service is alive — any HTTP response confirms the service is running.
32. **CCI 503 "no healthy upstream" has two root causes**: The `/cci/kubernetes/` path routes to `ccs-k3s` service in prelude namespace via HTTPRoute. The `ccs-k3s-app` has a deep init-container dependency chain: `ccs-k3s` → `ccs-infra-eas` → `provisioning-service` → `project-service` → `ebs-app` → `rabbitmq-ha`. If `rabbitmq-ha-0` is in CrashLoopBackOff (check for `.erlang.cookie` permissions 0660 vs required 0400), fix cookie permissions and restart. If `provisioning-service-app` is stuck at 0/1 Running with no ports listening, check for Spring Boot deadlock in `PrometheusExemplarsAutoConfiguration` via `jcmd 1 Thread.print` — fix by adding `-Dmanagement.prometheus.metrics.export.exemplars.enabled=false` to JAVA_OPTS.
33. **RabbitMQ `.erlang.cookie` permissions broken by fsGroup**: VCF Automation's `rabbitmq-ha` StatefulSet uses `fsGroup: 200` pod security context. This causes Kubernetes to set group-read/write (0660) on all PVC files including `.erlang.cookie`, but Erlang requires owner-only (0400). Fix by running a temporary root pod to `chmod 400 /var/lib/rabbitmq/.erlang.cookie` on the PVC, then deleting `rabbitmq-ha-0` to restart.
34. **VCF Automation VIP (10.1.1.70) drops after cold boot**: kube-vip manages both the control plane VIP and LoadBalancer service VIPs on VCF Automation. It watches the `istio-ingressgateway` service endpoints. After cold boot, Antrea CNI takes time to initialize, breaking ClusterIP routing. The istio-ingressgateway pod goes into ImagePullBackOff (can't reach registry ClusterIP). kube-vip detects no endpoints and releases the VIP, then loses its own leader lease and crashes. Fix: manually `ip addr add 10.1.1.70/32 dev eth0`, wait for Antrea to ready, delete ImagePullBackOff pods. `VCFfinal.py` Task 4b automates this.
35. **VCF Automation kube-scheduler stuck after VIP flap**: When kube-vip releases and re-acquires the VIP rapidly, the kube-scheduler may start with stale RBAC caches (errors like `clusterrole "system:kube-scheduler" not found`) even though the ClusterRoles exist. Pods get stuck in Pending with no events. Fix: restart containerd and kubelet (`systemctl restart containerd && sleep 3 && systemctl restart kubelet`), then re-add VIP if needed.
36. **VSP nodes are Photon OS 5.0 with proxy framework**: VSP cluster nodes use `/etc/sysconfig/proxy` (Photon standard) and `/etc/profile.d/proxy.sh` for shell proxy env. Proxy is disabled by default (`PROXY_ENABLED="no"`). To enable: set `PROXY_ENABLED="yes"` with `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` in `/etc/sysconfig/proxy`, add vars to `/etc/environment`, and create systemd drop-in files for containerd and kubelet at `/etc/systemd/system/{containerd,kubelet}.service.d/http-proxy.conf`. Run `systemctl daemon-reload` after.
37. **Supervisor proxy configured via vCenter namespace-management API**: Use `PATCH /api/vcenter/namespace-management/clusters/{cluster_id}` with `cluster_proxy_config.proxy_settings_source: "CLUSTER_CONFIGURED"` and `http_proxy_config`/`https_proxy_config` fields. The Supervisor inherits from vCenter by default (`VC_INHERITED`). The `no_proxy_config` field is NOT supported in the PATCH — no_proxy is auto-populated in the `wcp-proxy-config` K8s secret on the SCP. Setting both HTTP and HTTPS proxy must be done in a single PATCH call.
38. **VSP node images are all internal**: All container images on VSP nodes come from `registry.vmsp-platform.svc.cluster.local:5000`. The proxy NO_PROXY list must include this registry to avoid routing internal pulls through the proxy. The containerd `config_path` is `/etc/containerd/certs.d` with entries for `127.0.0.1:30000`, `localhost:5000`, and the internal registry.
39. **VSP service CIDR `198.18.128.0/17` must be in NO_PROXY**: The VSP cluster uses service CIDR `198.18.128.0/17` (not the standard `10.96.0.0/12`). The internal registry ClusterIP is `198.18.128.16`. Containerd resolves hostnames to IPs before checking NO_PROXY, so hostname-only entries like `registry.vmsp-platform.svc.cluster.local` are not sufficient — the CIDR `198.18.0.0/16` must be in NO_PROXY for containerd and kubelet.
40. **Fleet LCM direct API component types differ from suite-api proxy types**: The `/fleet-lcm/v1/components` endpoint uses different type names than `/suite-api/internal/components/`. Key differences: `NI` → `OPS_NETWORKS`, `LI` → `OPS_LOGS`, `FLEET_LCM` → `VCF_FLEET_LCM`, `SDDC_LCM` → `VCF_SDDC_LCM`. The fleet-lcm API also exposes additional types: `VCF_FLEET_DEPOT`, `OPS_DATA_PLATFORM`, `VCFMS_METRICS_STORE`, `TELEMETRY_ACCEPTOR`, `SALT` (vs `SALT_MASTER` in suite-api).
41. **Fleet LCM shutdown action returns HTTP 202**: `POST /fleet-lcm/v1/components/{id}?action=shutdown` returns HTTP 202 Accepted with a `taskId` in the response body. Poll task status via `GET /fleet-lcm/v1/tasks/{taskId}`. Task goes through stages: `INITIATED` → `RUNNING` → `COMPLETED`/`FAILED`. The shutdown workflow is `SHUTDOWN_COMPONENT_WORKFLOW`.
42. **OpsToken authSource varies in VCF 9.1 builds**: The `suite-api/api/auth/token/acquire` endpoint may accept `authSource: "local"` or `authSource: "localItem"` depending on the VCF 9.1 build. Always try both (local first, then localItem) for robustness.
43. **VSP Identity Service JWT for Fleet LCM**: The direct fleet-lcm API accepts a JWT Bearer token obtained from VSP Identity Service at `https://ops-a.site-a.vcf.lab/api/v1/identity/token` (POST with JSON `{"username":"admin","password":"...","source":"localItem"}`). This is an alternative to OpsToken for authenticating to fleet-lcm endpoints.
44. **Vault root token is creds.txt password**: In Holodeck labs, the HashiCorp Vault root token is the same as the creds.txt password (not `"holodeck"`). Verify with `curl -sk -H "X-Vault-Token: $(cat /home/holuser/creds.txt)" http://10.1.1.1:32000/v1/auth/token/lookup-self`. The root token from `/root/vault-keys/init.json` on the router also equals creds.txt password.
45. **Vault PKI role `holodeck` max_ttl must be updated for long-lived certs**: The default Vault PKI role `holodeck` has `max_ttl: 2592000` (720h/30 days). For 2-year certificates, update with `POST /v1/pki/roles/holodeck` setting `max_ttl: "17520h"`. The PKI mount itself allows up to 87600h (10 years).
46. **VCF 9.1 C4 OpsToken authSource is `"local"`**: In VCF 9.1 Cycle 4 build (version 9.1.0.0.25262080), the VCF Operations suite-api auth endpoint requires `authSource: "local"` (not `"localItem"`). Always try `"local"` first for this build.
47. **SDDC Manager Bearer token auth uses `admin@local`**: The SDDC Manager `/v1/tokens` endpoint uses `admin@local` as the username (not `administrator@vsphere.local` or `vcf`). Basic Auth with `vcf:password` works for GET endpoints but Bearer token is required for credential operations and CSR/cert replacement.
48. **SDDC Manager resource certificates span domains**: Resources from both management and workload domains are accessible via `/v1/domains/{domain_id}/resource-certificates`. The management domain contains SDDC Manager, mgmt vCenter, mgmt NSX, VSP, VCFA-platform, and ESXi hosts 01-04. The workload domain contains WLD vCenter, WLD NSX, and ESXi hosts 05-07.
49. **auto-platform-a.site-a.vcf.lab (10.1.1.69)**: VCF Automation Platform node is a separate VM from auto-a (10.1.1.70). SSH user is `vmware-system-user`. It appears as resource type `VSP` in SDDC Manager resource certificates. Its SSL cert has CN=`auto-platform-a.site-a.vcf.lab`.
50. **opslogs-a.site-a.vcf.lab SSH user is `vmware-system-user`**: Unlike ops-a which uses `root` for SSH, opslogs-a (VCF Operations for Logs) uses `vmware-system-user`. SSH as `root` or `admin` is rejected.
51. **opsnet-a.site-a.vcf.lab has no SSH access**: VCF Operations for Networks (opsnet-a) does not accept SSH connections from any user (root, admin, vmware-system-user all fail). It is accessible only via HTTPS. Certificate is managed via the VSP cluster Ops Locker.
52. **NSX Edge VMs (vna-wld01-*) are unreachable on port 443**: In VCF 9.1 C4, the NSX Edge transport nodes are named `vna-wld01-01a` and `vna-wld01-02a` (not `edge-*`). They do not expose HTTPS on port 443 from the management network.
53. **VCF 9.1 C4 VSP cluster has 6 nodes**: The VSP cluster in this build has 6 nodes (IPs: 10.1.1.141, 10.1.1.143, 10.1.1.144, 10.1.1.145, 10.1.1.146, 10.1.1.147) with control plane VIP at 10.1.1.142.
54. **SDDC Manager Microsoft CA proxy (certsrv)**: See the `vcf-certs` skill for complete proxy documentation, deployment, PKCS#7 ordering, and SDDC Manager cert workflow. Source files in `Tools/CertsrvProxy/`.
55. **SDDC Manager certsrv template validation requires `OID;Name` format**: The `MicrosoftCaService.java` class uses regex `<Option Value="(.+)">.+?</Option>` (case-insensitive) to extract templates from `certrqxt.asp`, then splits the Value by `;` and takes `[1]` as the template name. Template options MUST use `<Option Value="1.3.6.1.4.1.311.21.8.X;TemplateName">Display Name</Option>` format.
56. **pyOpenSSL `PKCS7` class removed in v25+**: `OpenSSL.crypto.PKCS7()` raises `AttributeError` on pyOpenSSL 25.x+ (bundled with cryptography 46.x). Use `cryptography.hazmat.primitives.serialization.pkcs7.serialize_certificates()` instead.
57. **Technitium DNS `vcf.lab` zone must be created for `ca.vcf.lab`**: Only `site-a.vcf.lab` and `site-b.vcf.lab` zones exist by default. A `vcf.lab` zone must be created via `api/zones/create?zone=vcf.lab&type=Primary` before adding `ca.vcf.lab` A records. This does not affect subzone delegation.
58. **VCF Operations Fleet Management CA validation does NOT follow 301 redirects**: When configuring a Microsoft CA, VCF Operations sends `GET /certsrv` (without trailing slash). If the server responds with `301 Moved Permanently` to `/certsrv/`, VCF Operations' Java HTTP client does not follow the redirect and reports "Failed to update certificate authorities". The certsrv proxy must serve content directly (HTTP 200) for both `/certsrv` and `/certsrv/` — no redirects.
59. **SDDC Manager CA `secret` field required for PUT**: The `PUT /v1/certificate-authorities` body requires `microsoftCertificateAuthoritySpec.secret` in addition to `password`. Omitting `secret` returns HTTP 400 "Secret in Microsoft certificate authority spec cannot be null or blank". Both fields should be set to the same password value.
60. **SDDC Manager cert generation uses PKCS#7 retrieval**: After `POST /certsrv/certfnsh.asp`, SDDC Manager parses the HTML for `certnew.cer\?ReqID=(\d+)&amp` (expects HTML entity `&amp;`, not raw `&`), then fetches the signed cert chain via `GET /certsrv/certnew.p7b?ReqID=<id>&Enc=b64` (PKCS#7 format). The proxy must support ReqID-based lookups on the `/certnew.p7b` endpoint, not just `CACert`.
61. **SDDC Manager certfnsh.asp POST uses Apache HttpClient `UrlEncodedFormEntity`**: The POST body uses proper URL encoding where `+` in base64 CSR data is encoded as `%2B`. The proxy must use `urllib.parse.parse_qs()` (not manual `split('&')` + `unquote_plus()`) to preserve `+` characters in CSR data. `unquote_plus()` converts `+` to space, corrupting base64.
62. **Python `.format()` unsafe on HTML with CSS**: Pre-built HTML templates containing CSS `{...}` braces (e.g., `:root { --bg: ... }`) will crash Python's `str.format()` with `KeyError`. Use `str.replace()` with a safe placeholder like `{{ERROR}}` instead.
60. **VCF Operations cert mgmt CSR response uses `certificateSignatureInfo` key**: The `GET .../csrs` endpoint returns CSRs under `certificateSignatureInfo` (not `csrDetails`). CSR PEM is in the `csr` field (not `csrContent`). CSR PEM uses spaces instead of newlines — must normalize (replace spaces with newlines, fix BEGIN/END markers) before sending to Vault for signing.
61. **VCF Operations cert mgmt repo requires `page`/`pageSize` params**: The `GET .../repository/certificates` endpoint returns HTTP 500 without query parameters. Must pass `?page=0&pageSize=100`. Response uses `vcfRepositoryCertificates` key (not `repositoryCertificateModels`). Cert ID field is `certId` (not `certificateId`).
62. **VCF Operations cert replace orchestrator types**: VROPS orchestrator handles VCF services runtimes (fleet-01a, instance-01a, vsp-01a), Identity broker, Log management, and Ops for Networks — these complete in minutes. VRSLCM orchestrator handles VCF Automation and VCF Operations — depends on `fleet-upgrade-service` health. If unhealthy, tasks remain `NOT_STARTED` indefinitely.
63. **VCF Operations cert mgmt task status API returns HTTP 500**: The `GET .../tasks/{taskId}` endpoint consistently returns HTTP 500 "Internal Server error, cause unknown" for most tasks. Workaround: poll `certificates/query` and check `issuedBy` field for issuer change, or poll `repository/certificates` to confirm import.
64. **VCF Operations cert mgmt CSR generation `keySize` is enum**: The `commonCsrData.keySize` field must be an enum value (`KEY_2048`, `KEY_3072`, or `KEY_4096`), not a plain number string like `"4096"`. Using `"4096"` returns HTTP 400 "Could not map [4096] to KeySize".
65. **Vault PKI role `holodeck` needs `allow_any_name` and `enforce_hostnames: false`**: To sign CSRs with arbitrary CNs (like `VCFA`, `OPS_LOGS`, `VIDB`), the Vault PKI role must have `allow_any_name: true` and `enforce_hostnames: false`. Default role rejects non-hostname CNs with "common name ... not allowed by this role".
66. **VCF Operations cert mgmt query requires POST with body**: The query certificates endpoint is POST (not GET) and requires body `{"vcfComponent": "VCF_MANAGEMENT", "vcfComponentType": "ARIA"}`. An empty body returns 0 results.
67. **PKCS#7 DER encoding reorders certificates**: Python's `cryptography.hazmat.primitives.serialization.pkcs7.serialize_certificates()` uses strict DER which sorts SET OF elements by encoded byte value, destroying certificate order. SDDC Manager's `CertificateOperationOrchestratorImpl` takes `certs[0]` as signed cert and `certs[1..]` as CA chain from the PKCS#7 response. Use a custom ASN.1 builder (`build_ordered_pkcs7()`) that constructs the `[0] IMPLICIT` certificates field directly from concatenated DER bytes to preserve leaf-first ordering. Java's `CertificateFactory.generateCertificates()` preserves the order from the raw ASN.1 structure.
68. **SDDC Manager `sign-verbatim` required for full subject DN**: Vault's `pki/sign/{role}` strips subject DN fields (O, OU, C, L, ST) when the role has empty arrays for those fields. Use `pki/sign-verbatim/{role}` instead to preserve the full CSR subject DN. Also add `ext_key_usage: ['ServerAuth', 'ClientAuth']` and `key_usage: ['DigitalSignature', 'KeyAgreement', 'KeyEncipherment']` to the payload.
69. **SDDC Manager PostgreSQL password location**: On SDDC Manager, use `su` to root (requires TTY via `pty.openpty()` in Python) then read `/root/.pgpass`. Format: `localhost:*:*:postgres:<password>`. Database `operationsmanager` contains task/execution tables but cert data is in-memory during workflows. Use `PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d operationsmanager` for queries.
