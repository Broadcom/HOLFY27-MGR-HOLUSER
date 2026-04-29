---
name: vcf-9-api
description: Guide API interactions with VMware Cloud Foundation (VCF) 9.0 and 9.1 environments running on Holodeck nested virtualization. Provides correct endpoints, credentials, authentication flows, SSH access, dir-cli SSO management, CSI secret structure, SDDC Manager auto-rotate API, credential remediation, resource lock management, PostgreSQL direct access, Authentik REST API (OAuth2/SCIM), vCenter OIDC identity provider federation, VCF Operations VIDB auth source, and version-specific differences for SDDC Manager, VCF Operations (Aria), NSX Manager, NSX Edges, vCenter, VCF Automation, and Supervisor clusters. Use when the user mentions VCF, SDDC Manager, Aria Operations, NSX, vCenter API, VCF Automation, Supervisor, Tanzu, password policies, fleet management, CSI driver, dir-cli, service accounts, auto-rotate, credential remediation, password reset, resource locks, Authentik, OIDC, SCIM, VCF SSO, or any VMware Cloud Foundation operations.
---

# VCF 9.x API Interaction Guide

This environment is a **Holodeck nested virtualization lab** running VCF 9.0 or 9.1 on the `site-a.vcf.lab` domain. All passwords default to the contents of `/home/holuser/creds.txt` . SDDC Manager may rotate passwords — always verify via the credentials API if standard password fails.

## Quick Reference: Credentials & Auth

| Component | Hostname | Username | Auth Method |
| --- | --- | --- | --- |
| SDDC Manager | `sddcmanager-a.site-a.vcf.lab` | `vcf` | Basic Auth |
| VCF Operations | `ops-a.site-a.vcf.lab` | `admin` (authSource: try `local` then `localItem`) | OpsToken |
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
| Operations VMs | varies | `root` on ops-a/opslcm-a; `vmware-system-user` on opslogs-a; no SSH on opsnet-a |
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
    json={"username": "admin", "password": PASSWORD, "authSource": "local"},  # try "localItem" if 401
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

### VIDB auth source (external OIDC for VCF Operations UI)

Register an OpenID Connect IdP so Operations users can authenticate via the same issuer used for VCF SSO (e.g. Authentik). Uses **public** `suite-api` (OpsToken), not the internal `vcf-operations/rest/ops/internal/*` browser session.

```python
import requests
import urllib3
urllib3.disable_warnings()
OPS, PASSWORD = "ops-a.site-a.vcf.lab", open("/home/holuser/creds.txt").read().strip()
token = requests.post(
    f"https://{OPS}/suite-api/api/auth/token/acquire",
    json={"username": "admin", "password": PASSWORD, "authSource": "local"},
    headers={"X-vRealizeOps-API-use-unsupported": "true"}, verify=False,
).json()["token"]
headers = {"Authorization": f"OpsToken {token}", "X-vRealizeOps-API-use-unsupported": "true",
           "Content-Type": "application/json", "Accept": "application/json"}
payload = {
    "name": "VCF-Auth-authentik",
    "sourceType": {"id": "VIDB", "name": "VIDB"},
    "property": [
        {"name": "display-name", "value": "VCF Auth"},
        {"name": "issuer-url", "value": "https://auth.vcf.lab/application/o/vcf/"},
        {"name": "client-id", "value": "<from IdP>"},
        {"name": "client-secret", "value": "<from IdP>"},
    ],
    "certificates": [],
}
r = requests.post(f"https://{OPS}/suite-api/api/auth/sources", json=payload, headers=headers, verify=False)
# HTTP 500 with "VCF SSO authentication source test failed" if issuer unreachable or client invalid
```

**Auth source type metadata**: `GET /suite-api/api/auth/sourcetypes/VIDB` returns allowed property names (`issuer-url` + `client-id` + `client-secret` mode, or legacy `host`/`port`/`tenant`/`user-name`/`password`).

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

### Identity provider federation (OIDC on management vCenter)

Embedded **VCF Identity Broker** / federation uses the vSphere Automation API. **Create** an external OIDC provider:

- `POST https://{vc}/api/vcenter/identity/providers`
- Body shape: `{"config_tag": "Oidc", "oidc": { ... }}`
- Required OIDC fields: `client_id`, `client_secret`, `discovery_endpoint` (full URL to `/.well-known/openid-configuration` — **not** a separate `issuer_url` key on this API version).
- **`claim_map`**: must be a **JSON object** (Map). Use `{}` for defaults (Cycle 7 wizard “unique identifier `sub`”). Plain string values per claim (e.g. `{"sub": "sub"}`) produce **`parsePropertyException` / “Unable to parse property with name spec”** — values must be list-structured map-entries, not strings; simplest automation path is **`"claim_map": {}`**.
- vCenter fetches the discovery document through an **internal HTTP proxy** (`InvalidArgumentException` mentioning `localhost:1080/external-vecs/...`). If the IdP TLS chain is not trusted, errors appear as **HTTP 526**, **503**, etc. on that fetch — align with manual workflow: trust the CA that signs the IdP (e.g. `auth.vcf.lab` via `https://ca.vcf.lab` or Vault PKI distribution).

**Redirect URI** (Holodeck / embedded tenant): `https://vc-mgmt-a.site-a.vcf.lab/federation/t/CUSTOMER/auth/response/oauth2` — `CUSTOMER` is the literal path segment, not a variable.

**SCIM base URL** for Authentik (and manual sync): `https://vc-mgmt-a.site-a.vcf.lab/usergroup/t/CUSTOMER/scim/v2`

```python
import requests
VC, PASSWORD = "vc-mgmt-a.site-a.vcf.lab", open("/home/holuser/creds.txt").read().strip()
sid = requests.post(f"https://{VC}/api/session", auth=("administrator@vsphere.local", PASSWORD), verify=False)
sid.raise_for_status()
session_id = sid.text.strip('"')
headers = {"vmware-api-session-id": session_id, "Content-Type": "application/json"}
body = {
    "config_tag": "Oidc",
    "oidc": {
        "claim_map": {},
        "client_id": "<oauth_client_id>",
        "client_secret": "<oauth_client_secret>",
        "discovery_endpoint": "https://auth.vcf.lab/application/o/vcf/.well-known/openid-configuration",
    },
}
r = requests.post(f"https://{VC}/api/vcenter/identity/providers", json=body, headers=headers, verify=False)
# Expect HTTP 200 or 201 on success
```

### VAMI no-proxy (Fleet IAM / VIDB outbound OIDC)

When the management vCenter has a **global HTTP(S) proxy** (Squid), **embedded VIDB** fetches the OIDC discovery URL through that proxy. Hostnames like **`auth.vcf.lab`** must be in the **VAMI** no-proxy list or Squid returns **403** and Fleet IAM `configureIDP` fails with “Failed to connect”.

- **Authoritative API** (not only `/etc/sysconfig/proxy`): `GET` / `PUT` `https://<vc-fqdn>:5480/rest/appliance/networking/noproxy` with Basic **`root:<creds.txt>`** and JSON body **`{"servers":["localhost",...,"auth.vcf.lab",".vcf.lab","192.168.0.2"]}`** on `PUT`.
- **vmware-vmon** injects `NO_PROXY` into **vsphere-ui** at service start. After changing VAMI no-proxy, run **`systemctl restart vmware-vmon`** on the vCSA (brief UI disruption) so the JVM picks up the new bypass list. Restarting **only** `vsphere-ui` may leave stale env if vmon was not restarted.
- **`Tools/authentik_vcf_integration.py`** merges issuer / `.vcf.lab` / holorouter IP via VAMI and optionally restarts vmware-vmon (disable with `authentik_skip_mgmt_vc_vmon_restart=true` in `[VCFFINAL]`).

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

## 7. Operations VMs (SSH Access)

SSH access varies by Operations VM:

| VM | SSH User | Notes |
| --- | --- | --- |
| `ops-a` | `root` | SSH usually enabled; password from creds.txt |
| `opslcm-a` | `root` | SSH usually enabled |
| `opsdata-01a` | `root` | SSH usually enabled |
| `opslogs-a` | `vmware-system-user` | `root` and `admin` rejected |
| `opsnet-a` | N/A | No SSH access at all — HTTPS API only |

When scripting Operations VM configuration, try multiple users:

```bash
# Try root first, then vmware-system-user
for user in root vmware-system-user; do
    if sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no \
       -o ConnectTimeout=10 $user@$OPSVM "echo SSH_OK" 2>/dev/null | grep -q SSH_OK; then
        echo "SSH works as $user"
        break
    fi
done
```

If SSH is disabled by default, enable via vSphere Guest Operations API:

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

## 9. SDDC Manager Bearer Token & Auto-Rotate API

Credential operations (PATCH, auto-rotate) require a Bearer token from `/v1/tokens`, not Basic Auth. Username is `admin@local`.

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
SDDC="sddcmanager-a.site-a.vcf.lab"

TOKEN=$(curl -sk -X POST "https://${SDDC}/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('accessToken',''))")

# List credentials with auto-rotate
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/credentials" | python3 -c "
import json,sys
for el in json.load(sys.stdin).get('elements', []):
    ar = el.get('autoRotatePolicy', {})
    if ar.get('frequencyInDays'):
        print(f'{el[\"username\"]} ({el[\"credentialType\"]}): rotate every {ar[\"frequencyInDays\"]}d')
"

# Disable auto-rotate (requires resource in ACTIVE state)
curl -sk -X PATCH -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "https://${SDDC}/v1/credentials" \
  -d '{"operationType":"UPDATE","elements":[{
    "resourceName":"vc-mgmt-a.site-a.vcf.lab",
    "resourceType":"VCENTER",
    "credentials":[{"credentialType":"SSH","username":"root",
      "autoRotatePolicy":{"frequencyInDays":0,"enableAutoRotatePolicy":false}}]
  }]}'
```

## 10. SDDC Manager Credential Remediation API

`PATCH /v1/credentials` supports three operation types:
- **UPDATE**: Changes password on target AND SDDC Manager DB. Does NOT work for service accounts.
- **ROTATE**: Generates new password, applies to target. Used for service accounts (`svc-*`).
- **REMEDIATE**: Updates SDDC Manager's stored credential only (assumes you already changed it externally).

**Requires**: Bearer token (Section 9), resource in ACTIVE state.

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

Last resort when API operations are blocked by stale locks or inconsistent resource statuses.

### Connection Details

- **SSH user**: `vcf` (can run `psql` directly; use `su - root` via `pty.openpty()` in Python for `/root/.pgpass`)
- **PostgreSQL**: TCP on `127.0.0.1:5432` (NOT Unix socket), user `postgres`
- **Password**: In `/root/.pgpass` (format: `localhost:*:*:postgres:<password>`). May differ per deployment.
- **Databases**: `platform` (resource/lock/credential tables), `operationsmanager` (task/execution tables)

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vcf@sddcmanager-a.site-a.vcf.lab \
  "export PGPASSWORD='iHk0JKypNFrR9C5iOI2PmBmUCfSbdrjFxaGoxEEFz3w='; \
   /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform -c \"SELECT 1\""
```

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

SDDC Manager authenticates to vCenter via SSO service accounts (`svc-sddcmanager-a-vc-*`, `svc-nsx-*-vc-*`). If these passwords are wrong, ALL credential operations fail. Fix these FIRST before any other remediation. Account IDs (e.g., `-9382`) are unique per deployment — query `/v1/credentials` to discover exact names. See Section 8 for `dir-cli password reset` syntax.

## 13. VCF Operations Version Differences

| Behavior | VCF 9.0 | VCF 9.1 |
| --- | --- | --- |
| OpsToken authSource | `"local"` | `"localItem"` (but 9.1 C4 uses `"local"`) |
| Password management API | 404 | `/suite-api/internal/passwordmanagement/policies/*` |
| Fleet CA name | `VCF Operations Fleet Management Locker CA` | `Broadcom, Inc CA` |
| suite-api lifecycle actions | HTTP 500 | HTTP 500 — use fleet-lcm direct API |
| VCF Automation sudo | NOPASSWD | `echo pw \| sudo -S -i` |
| VSP cluster nodes | 5 nodes | 6 nodes (9.1 C4) |

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

### Orchestrator Types

| Orchestrator | Components | Behavior |
| --- | --- | --- |
| VROPS | vidb-a, opslogs-a, opsnet-a, fleet-01a, instance-01a, vsp-01a | Completes in minutes |
| VRSLCM | auto-a, auto-platform-a, ops-a | Depends on fleet-upgrade-service; stays `NOT_STARTED` if unhealthy |

**Task status API is unreliable** (HTTP 500). Workaround: poll `certificates/query` and check `issuedBy` field for change.

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

## 16. Authentik REST API + VCF SSO automation (Holodeck)

**Human-readable lab procedure**: `HOL_Authentik_Config_Cycle_7.md` (CoreDNS, Authentik UI, VCF Operations wizard, SCIM, roles).

**Automated helper**: `Tools/authentik_vcf_integration.py` (+ `Tools/authentik_fleet_iam.py`) — gated by `[VCFFINAL] authentik_vcf_integration=true`; invoked from `Startup/VCFfinal.py` Task 8b after Vault CA distribution. Reads `/tmp/config.ini`.

**Default (full E2E)**: OpsToken + **Fleet IAM** public suite-api — `GET/POST .../api/fleet-management/iam/vidbs`, `.../ssorealms`, `POST .../identity-providers` (OIDC + **SCIM** + **TLS chain for the IdP host** as a JSON array of **full PEM strings**, not one string per line — see pitfalls §68), `POST .../identity-providers/{id}/directories/{dir}/sync-client` with `{"generateToken":true}` to mint the **SCIM bearer token**; then Authentik SCIM provider, `POST .../providers/scim/{pk}/sync/` (best-effort), `PUT .../ssorealms/{realm}/principals/{groupId}/roles` (`vcf_administrator` + `SSO_REALM` scope), and `POST .../components/auth-sources` (Join SSO for vCenter + VCF Operations + VCF Automation). Authentik users/groups: defaults `prod-admin@vcf.lab` / `dev-admin@vcf.lab` and `prod-admins` / `dev-admins`.

**Legacy** (`authentik_skip_fleet_iam=true`): vCenter `POST /api/vcenter/identity/providers`, VIDB `POST /suite-api/api/auth/sources`, optional `[VCFFINAL] vcf_scim_bearer_token` for Authentik SCIM only.

Optional keys: `authentik_mgmt_vc_fqdn`, `authentik_ops_fqdn`, `authentik_issuer_base`, `authentik_application_slug`, `authentik_directory_users`, `authentik_scim_group_names`, `authentik_scim_domain`, `authentik_fleet_idp_name`, `authentik_fleet_directory_name`, `authentik_fleet_vcf_role`, `authentik_fleet_join_nsx`, `authentik_skip_fleet_iam`, `vcf_scim_bearer_token` (legacy), `authentik_skip_coredns`, `authentik_skip_vcenter`, `authentik_skip_ops_vidb`, `authentik_api_token`, `authentik_signing_key_pk`.

### Authentik API base URL and auth

| Item | Value |
| --- | --- |
| Base | `https://auth.vcf.lab/api/v3` (or `http://192.168.0.2:31080/api/v3` from holorouter) |
| Header | `Authorization: Bearer <token>` |
| Default bootstrap token | `holodeck` (override with env `AUTHENTIK_API_TOKEN` or `[VCFFINAL] authentik_api_token`) |

**Path construction**: the HTTP client base URL must end with `/api/v3`. API paths are **relative** (`flows/instances/...`, `providers/oauth2/`, `core/applications/`). Do **not** prefix paths with `/api/v3` again or requests hit `/api/v3/api/v3/...` (**404**).

### OAuth2/OpenID provider + application (explicit consent)

1. Resolve flow PKs: `GET .../flows/instances/?slug=default-provider-authorization-explicit-consent` and `...slug=default-provider-invalidation-flow`.
2. Resolve signing key: `GET .../crypto/certificatekeypairs/` (first result, or set `authentik_signing_key_pk`).
3. `POST .../providers/oauth2/` with `authorization_flow`, `invalidation_flow`, `client_type: confidential`, `redirect_uris` as a **list of objects** `[{"url": "<vc callback>", "matching_mode": "strict"}]`, `signing_key`, `issuer_mode` (e.g. `per_provider`), `sub_mode`, `include_claims_in_id_token`.
4. `POST .../core/applications/` with `name`, `slug` (e.g. `vcf`), `provider` set to the OAuth2 provider **integer pk**.
5. **Reuse**: `GET .../providers/oauth2/{pk}/` returns `client_id` and `client_secret` (admin API) for idempotent vCenter/VIDB registration.

### SCIM provider + backchannel (Authentik → vCenter)

**Automated (default)**: After Fleet IAM configures the OIDC+SCIM IdP, `POST .../identity-providers/{idpConfigId}/directories/{directoryId}/sync-client` with `{"tokenTtl":262800,"generateToken":true}` returns `accessTokenDetails.accessToken` — use that as the Authentik SCIM provider `token` (`verify_certificates: false` for lab). See `Tools/authentik_fleet_iam.py`.

**Manual / legacy**: Optional `[VCFFINAL] vcf_scim_bearer_token` when `authentik_skip_fleet_iam=true` (wizard popup token).

1. `POST .../providers/scim/` with `name`, `url` (`https://<mgmt-vc>/usergroup/t/CUSTOMER/scim/v2`), `token`, `verify_certificates: false` (lab parity with “uncheck verify” in the doc).
2. Link: `PATCH .../core/applications/<slug>/` with JSON `{"backchannel_providers": [<scim_provider_pk>]}` — URL uses **application slug** (UUID pk for PATCH returned 404 in testing).
3. **Initial directory sync**: `POST .../providers/scim/{pk}/sync/` (script tries this and `.../sync/full/`); worker may still batch — UI Schedules (Play) remains a fallback.

### CoreDNS forwarder (Cycle 7 Step 1)

On **router** as `root`, patch `10.1.1.1` → `10.244.0.1` in `/holodeck-runtime/k8s/coredns_configmap.yaml`, then `kubectl delete` + `kubectl apply` that manifest. The script uses `sshpass -f /home/holuser/creds.txt` and **`subprocess.run(..., shell=True)`** (not `lsf.run_command`, which splits passwords incorrectly).

## Critical Pitfalls

Items below are unique pitfalls NOT already covered in the detailed sections above. For pitfalls covered in detail elsewhere, see the referenced sections or skills.

### NSX
1. **NSX user IDs are numeric**: Use `/api/v1/node/users/10000` (admin), `0` (root). Not usernames.
2. **NSX CLI password-expiration broken on 9.1**: `set user admin password-expiration 729` silently resets to 0. Use REST API.
3. **NSX Edge SSH requires `-T` flag**: PTY allocation causes connection drops for inline commands.
4. **NSX `/root/.ssh/` missing on Edges AND Managers**: Always `mkdir -p /root/.ssh && chmod 700 /root/.ssh` before SCP.
5. **NSX `set service ssh start-on-boot` fails if already set**: Check with `get service ssh start-on-boot` first.
6. **NSX Edge VMs in 9.1 C4 named `vna-wld01-*`** (not `edge-*`), unreachable on port 443.
7. **NSX Edge SR STANDBY `Op_state: down` is normal** in `ACTIVE_STANDBY` mode.

### vCenter
8. **`/api/session` returns HTTP 201** (not 200). Accept both when checking session creation.
9. **SSH/shell can be enabled via REST API**: `PUT /api/appliance/access/ssh` (body: `true`) without needing SSH first.
10. **SSH host keys change on upgrade**: Use `ssh-keygen -R` and `-o StrictHostKeyChecking=accept-new`.
11. **`vapi-endpoint` and `trustmanagement` frequently STOPPED** after cold boot — always verify/start via `vmon-cli`.
12. **Fresh vCenter has VAMI shell + PAM blocking sshpass**: Root login shell is `/opt/vmware/bin/appliancesh` and `/etc/pam.d/sshd` includes `pam_mgmt_cli.so`. Fix via pyVmomi Guest Operations on ESXi: `usermod -s /bin/bash root`, rewrite `/etc/pam.d/sshd`, restart sshd. See vcf-troubleshooting Section 36.

### VCF Automation
12. **Sudo changed in 9.1**: No longer NOPASSWD. Use `echo 'pw' | sudo -S -i`.
13. **Microservices don't auto-scale after shutdown**: ~50 prelude deployments stay at 0 replicas. Must manually scale to 1.
14. **VIP (10.1.1.70) drops after cold boot**: kube-vip releases VIP when istio-ingressgateway has no endpoints. Fix: `ip addr add 10.1.1.70/32 dev eth0`. Automated by `VCFfinal.py` Task 4b.
15. **kube-scheduler stuck after VIP flap**: Fix by restarting containerd + kubelet.
16. **SCP VMs are EAM-managed on 9.0.x**: PowerOn via vCenter fails with `NoPermission`. Connect directly to ESXi hosts as `root`.
17. **CCI returns HTTP 500 on unauthenticated requests (9.1 C2)**: Treat 401, 403, and 500 as evidence the service is alive.
18. **CCI 503 root causes**: RabbitMQ `.erlang.cookie` permissions (fsGroup sets 0660, Erlang needs 0400) and provisioning-service Spring Boot deadlock (`PrometheusExemplarsAutoConfiguration`).
19. **CSI service account passwords rotated during upgrade**: Both `vsphere-config-secret` and `vsphere-cloud-secret` must be updated. Use `dir-cli password reset` (Section 8).

### VSP Cluster
20. **Component CRDs are cluster-scoped**: `components.api.vmsp.vmware.com` — never use `-A` or `-n` flags.
21. **SSH escaping breaks kubectl custom-columns**: Use `-o json` parsed locally in Python instead.
22. **Worker kubeconfig path**: `/etc/kubernetes/node-agent.conf` (not `super-admin.conf`).
23. **Postgres suspension is two-step**: Label `database.vmsp.vmware.com/suspended` AND patch `numberOfInstances`.
24. **ClickHouse in vodap**: Must scale statefulsets directly; stopping the operator alone doesn't stop pods.
25. **VSP service CIDR is `198.18.128.0/17`**: Must include `198.18.0.0/16` in NO_PROXY for containerd/kubelet.
26. **VSP node images are all internal**: From `registry.vmsp-platform.svc.cluster.local:5000`. Containerd resolves to ClusterIP before checking NO_PROXY.
27. **Photon OS 5.0 proxy framework**: Four config points: `/etc/sysconfig/proxy`, `/etc/environment`, containerd drop-in, kubelet drop-in. Run `systemctl daemon-reload` after.
28. **Supervisor proxy via vCenter API**: `PATCH .../namespace-management/clusters/{id}` with `proxy_settings_source: "CLUSTER_CONFIGURED"`. Both HTTP and HTTPS must be set in one PATCH call.

### VCF Operations
29. **Internal APIs need header**: `X-vRealizeOps-API-use-unsupported: true`.
30. **OpsToken authSource varies**: Try `"local"` first, then `"localItem"`. VCF 9.1 C4 uses `"local"`.
31. **Fleet LCM direct API type names differ**: `NI`→`OPS_NETWORKS`, `LI`→`OPS_LOGS`, `FLEET_LCM`→`VCF_FLEET_LCM`, `SDDC_LCM`→`VCF_SDDC_LCM`.
32. **Fleet LCM shutdown returns HTTP 202**: Poll `GET /fleet-lcm/v1/tasks/{taskId}`.
33. **VSP Identity Service JWT**: Alternative auth for fleet-lcm at `https://ops-a.../api/v1/identity/token`.
34. **Fleet CA name varies**: `Broadcom, Inc CA` (9.1) vs `VCF Operations Fleet Management Locker CA` (9.0). Do not hard-code.
35. **Fleet CA extraction**: Last cert in TLS chain at `ops-a:443` via `openssl s_client -showcerts`.

### SDDC Manager
36. **Resource certificates span domains**: Both mgmt and WLD resources accessible via `/v1/domains/{id}/resource-certificates`.
37. **`auto-platform-a` (10.1.1.69)**: Separate VM from auto-a (10.1.1.70). SSH: `vmware-system-user`.
38. **`opslogs-a` SSH user is `vmware-system-user`**: Not `root` or `admin`.
39. **`opsnet-a` has no SSH access**: HTTPS only.
40. **VCF 9.1 C4 credentials API requires Bearer token**: Basic Auth (`-u vcf:password`) returns 0 elements. Use `POST /v1/tokens` with `admin@local` to get `accessToken`, then `Authorization: Bearer <token>`.
41. **VNA Edge root passwords are rotated by SDDC Manager**: VNA edge root passwords differ from the standard lab password. Query `/v1/credentials?resourceType=NSXT_EDGE` with Bearer token to retrieve rotated passwords.
42. **VNA Edge STANDBY has unreachable management IP**: In `ACTIVE_STANDBY` HA, the standby VNA edge's management IP (e.g. 10.1.1.197) is not pingable. SSH operations will fail, but NSX Manager REST API (password expiration, SSH enable) still works.
43. **SDDC Manager API returns HTTP 502/503 after service restart**: After `systemctl restart operationsmanager commonsvcs domainmanager`, the `/v1/tokens` endpoint recovers first (~10s) but `/v1/credentials` may return HTTP 502 for up to 60s. Always implement a retry loop (e.g. 6 attempts, 10s sleep) when querying credentials immediately after a service restart.
44. **Operations VMs SSH user varies by VM**: `ops-a` and `opslcm-a` accept `root`, `opslogs-a` accepts only `vmware-system-user`, `opsnet-a` has no SSH at all. When scripting, try `root` first, then `vmware-system-user`, and skip VMs where both fail.

### lsfunctions Library
45. **`lsf.run_command(cmd)` splits on spaces**: It internally calls `cmd.split()` which mangles quoted passwords. `sshpass -p "MyP@ss!"` becomes `['sshpass', '-p', '"MyP@ss!"']` with literal quote chars in the password arg. Use `subprocess.run(cmd, shell=True)` directly for any command containing passwords.
46. **`lsf.ssh()` / `lsf.scp()` return objects with potentially missing `.stderr`**: On failure, use `getattr(result, 'stderr', '') or ''` instead of `result.stderr` to avoid `AttributeError`.

### Certificate Operations (see `vcf-certs` skill for full details)
43. **PKCS#7 DER encoding reorders certs**: Use custom `build_ordered_pkcs7()`.
44. **Use `sign-verbatim`, not `sign`**: Vault `pki/sign/{role}` strips subject DN fields.
45. **certsrv template format**: `<Option Value="OID;TemplateName">`.
46. **`certfnsh.asp` POST encoding**: Use `parse_qs()`, not `unquote_plus()` (corrupts base64 `+`).
47. **VCF Ops CA validation rejects 301 redirects**: Serve `/certsrv` directly as HTTP 200.
48. **`PUT /v1/certificate-authorities` requires both `password` AND `secret` fields**.
49. **pyOpenSSL `PKCS7` class removed in v25+**: Use `cryptography.hazmat.primitives.serialization.pkcs7.serialize_certificates()`.
50. **Python `.format()` unsafe on HTML with CSS braces**: Use `str.replace()` instead.

### Vault
51. **Root token is creds.txt password** (not `"holodeck"`).
52. **PKI role `holodeck` default max_ttl is 720h**: Update to `17520h` for 2-year certs. Needs `allow_any_name: true`, `enforce_hostnames: false`.
53. **Technitium DNS needs `vcf.lab` zone** for `ca.vcf.lab` records (only `site-a.vcf.lab` exists by default).

### Fleet LCM Shutdown API
54. **OPS component SHUTDOWN returns HTTP 400**: `VCF_LCM_400_INVALID_REQUEST` / `UnsupportedOperation`. VCF Operations is the control-plane itself; shut down via VM power-off in Phase 13 instead.
55. **VCFA shutdown takes ~21 minutes via Fleet LCM**: The `shutdown_component_ref` stage stays PENDING for ~17 min while K8s workloads drain, then `persist_sddc_lcm_components_ref` takes ~4 min.
56. **OPS_NETWORKS (vrni) shutdown takes ~8 minutes**: Progresses through `flip_nodes_status_join_task_ref` then `persist_sddc_lcm_components_ref`.
57. **OPS_LOGS (vrli) shutdown takes ~8 minutes**: Same pattern as vrni.

### SDDC Manager Task Concurrency
58. **Auto-rotate disable HTTP 403 "10 concurrent"**: Stale `task_metadata` + `entity_and_task` + `task_and_entity_type_and_entity` in platform DB, plus `processing_task` in domainmanager DB, consume the 10-task concurrency limit. Must DELETE all these records and restart services. See vcf-troubleshooting Section 37.
59. **`/v1/tasks` vs `/v1/credentials/tasks`**: `/v1/credentials/tasks` shows credential-specific task status. `/v1/tasks` shows ALL SDDC Manager tasks computed from platform DB tables. Stale tasks appear only in `/v1/tasks` and cannot be cancelled via API.
60. **Operations VM passwords may be rotated**: SDDC Manager rotates passwords on Operations VMs (ops-a, opscollector-01a, opslcm-a). Query `/v1/credentials` (no resourceType filter) and match by hostname to retrieve actual passwords.
61. **Holorouter HTTPS leaves expire separately from Vault root CA trust**: `https://auth.vcf.lab` and `https://vault.vcf.lab` use nginx PEMs in `/root/nginx-certs/`. Importing the Vault root into Firefox (or `Tools/vault_firefox_trust.py`) does not renew those leaves. Re-issue with `Tools/holorouter/renew-nginx-tls-from-vault.sh` on the router. See vcf-troubleshooting Section 38.

### Authentik / VCF SSO (API automation)
62. **Authentik API double `/api/v3`**: Base URL is `https://auth.vcf.lab/api/v3`; child paths must be `flows/...`, `providers/oauth2/`, not `/api/v3/flows/...`.
63. **vCenter OIDC `claim_map` is not a string map**: Keys like `{"sub": "sub"}` cause **`parsePropertyException`**. Use **`claim_map: {}`** unless you implement the correct list-valued map-entry structure.
64. **vCenter OIDC discovery fetch uses internal proxy**: Failures show as **526** / **503** / `InvalidArgumentException` with `localhost:1080/external-vecs/...` — fix IdP TLS trust (same class of issue as federation wizard CA upload).
65. **VIDB `POST /suite-api/api/auth/sources` can return HTTP 500** with `VCF SSO authentication source test failed` until the IdP is reachable and client credentials match the live OAuth app.
66. **SCIM bearer token for vCenter (Fleet IAM)**: Mint via `POST /suite-api/api/fleet-management/iam/identity-providers/{idpConfigId}/directories/{directoryId}/sync-client` with `generateToken: true` (after OIDC+SCIM IdP is created). Legacy: `[VCFFINAL] vcf_scim_bearer_token` when `authentik_skip_fleet_iam=true`.
67. **Authentik SCIM “sync now”**: Script calls `POST .../providers/scim/{pk}/sync/` (and `/sync/full/`); some builds return **405** — groups may appear after worker/hourly sync; UI Schedules (Play) is fallback.
68. **Fleet IAM `certificateChain` must be full PEM strings**: The JSON field is an array of **whole certificates** (typically leaf + issuing CA, max three). Splitting a PEM into **one JSON string per line** is parsed as many separate chains → **`idp.pem.certificate.chains.max.exceeded`**. `Tools/authentik_fleet_iam.py` builds the correct shape.
69. **SSO Overview “Prerequisites” ≠ IdP creation**: Checking the five boxes only acknowledges documentation; you must also click **Configure SSO** on **Get Started with SSO** to open the **Configure VCF SSO** wizard (`…/initial-setup`), per `HOL_Authentik_Config_Cycle_7.md` Step 3. **`GET .../fleet-management/iam/ssorealms`** must show a non-null **`idpId`** after `POST .../identity-providers` (Fleet IAM script) or after finishing the IdP step in the UI.
70. **Fleet IAM `directories[0].name` (validName) rejects dots**: Only alphanumeric, space, hyphen, underscore (1–128). Using **`vcf.lab`** as the directory display name (same as SCIM domain) causes **`directories[0].validName`** HTTP 400 — `Tools/authentik_fleet_iam.py` sanitizes (e.g. `vcf_lab`) while **`domains`** / **`defaultDomain`** stay **`vcf.lab`**.
