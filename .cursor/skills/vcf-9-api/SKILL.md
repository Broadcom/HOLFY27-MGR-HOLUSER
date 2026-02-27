---
name: vcf-9-api
description: Guide API interactions with VMware Cloud Foundation (VCF) 9.0 and 9.1 environments running on Holodeck nested virtualization. Provides correct endpoints, credentials, authentication flows, and SSH access for SDDC Manager, VCF Operations (Aria), NSX Manager, NSX Edges, vCenter, VCF Automation, and Supervisor clusters. Use when the user mentions VCF, SDDC Manager, Aria Operations, NSX, vCenter API, VCF Automation, Supervisor, Tanzu, password policies, fleet management, or any VMware Cloud Foundation operations.
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
- **Startup**: `hol/Startup/VCFfinal.py` Task 2e (scale up + unsuspend postgres)
- **Config**: `/tmp/config.ini` `[VCFFINAL] vcfcomponents` (format: `namespace:resource_type/name`)
- **Standalone**: `python3 Shutdown.py --phase 2b` (scale down only)
- **Dry run**: `python3 Shutdown.py --phase 2b --dry-run`

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

## Critical Pitfalls Discovered

1. **NSX user IDs are numeric**: `/api/v1/node/users/admin` = 404. Use `/api/v1/node/users/10000`.
2. **SDDC Manager rotates passwords**: Always check `GET /v1/credentials` if standard password fails.
3. **NSX CLI password-expiration broken on 9.1**: `set user admin password-expiration 729` silently resets to 0. Use REST API.
4. **VCF Operations internal APIs need special header**: `X-vRealizeOps-API-use-unsupported: true`.
5. **vCenter SSO domain differs**: Management = `@vsphere.local`, Workload = `@wld.sso`.
6. **VCF Automation sudo changed in 9.1**: No longer NOPASSWD. Use `echo 'pw' | sudo -S -i`.
7. **NSX Edge SSH requires -T flag**: PTY allocation causes connection drops for inline commands.
8. **vCenter services may not autostart**: `vapi-endpoint` and `trustmanagement` frequently need manual start.
9. **Component CRDs are cluster-scoped**: `components.api.vmsp.vmware.com` is NOT namespaced. Using `-A` produces `<none>` namespace columns; using `-n` on annotate silently fails. Omit both.
10. **SSH escaping breaks kubectl custom-columns**: Dotted annotation keys (e.g., `component\\.vmsp\\.vmware\\.com/...`) get mangled through SSH+sudo+bash-c layers. Use `-o json` and parse locally in Python instead.
11. **VSP worker kubeconfig path differs**: Workers use `/etc/kubernetes/node-agent.conf`; only the control plane has `super-admin.conf`.
12. **Postgres suspension is two-step**: Must set label `database.vmsp.vmware.com/suspended=true` AND patch Zalando `postgresqls.acid.zalan.do` `numberOfInstances` to 0. Both are needed for clean shutdown matching the startup unsuspend.
13. **ClickHouse in vodap managed by operator**: The `chi-vcf-obs-*` and `chk-vcf-obs-keeper-*` statefulsets are managed by clickhouse-operator (in vmsp-metrics-store). Scaling down the operator alone does not stop ClickHouse pods — must scale the statefulsets directly.

For detailed endpoint reference and troubleshooting, see [VCF_9x_Endpoints.md](../../Documents/git/cursor/VCF_9x_Endpoints.md).
