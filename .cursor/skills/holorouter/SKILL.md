---
description: Guide interactions with the Holorouter - the Photon OS 5.0 router/gateway running Kubernetes (kubeadm v1.27), Traefik reverse proxy, Technitium DNS, HashiCorp Vault PKI, Authentik identity provider, FRR-K8s BGP routing, and Squid proxy. Use when the user mentions holorouter, router, DNS, Traefik, reverse proxy, Vault certificates, Authentik, forward-auth, FRR, BGP, Squid, or any holorouter service management.
globs: 
  - "**/Tools/Holorouter/**"
  - "**/Tools/Authentik/**"
---

# Holorouter Operations Skill

## SSH Access

```bash
sshpass -f /home/holuser/creds.txt ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no root@router "<command>"
```

Password is always the contents of `/home/holuser/creds.txt` .

The holorouter hostname is `router` from the console/manager, IP `10.1.10.129` on the trunk interface, and `10.1.1.1` on VLAN 10 (management).

## Architecture

| Component | Detail |
| --- | --- |
| OS | VMware Photon OS 5.0 |
| Kubernetes | kubeadm v1.27.16, single control-plane node, Flannel CNI |
| CPU / RAM | 4 vCPU / 7.8 GB |
| Disk | 74 GB total (sda3), ~53 GB free |
| Container Runtime | containerd 1.6.17 |
| Package Manager | `tdnf` (Photon), `helm` v3.17 |
| Node IP (K8s) | 192.168.0.2 |

### Network Interfaces

The router has ~30 VLAN interfaces spanning two sites:

- **eth0** (192.168.0.2/24) - External/management (default gateway 192.168.0.1)
- **eth1** (10.1.10.129/25) - Site A trunk
- **eth1.10** (10.1.1.1/24) - Site A management VLAN (used for Traefik)
- **eth1.11-25** (10.1.2.x - 10.1.9.x) - Site A VLANs
- **eth2** - Site B trunk
- **eth2.40-58** (10.2.1.x - 10.2.9.x) - Site B VLANs
- **cni0/flannel.1** (10.244.0.x) - Kubernetes pod network

IP forwarding is enabled. MASQUERADE rules handle NAT.

## Service Inventory

### Traefik Reverse Proxy (namespace: `traefik`)

| Item | Detail |
| --- | --- |
| Deployment | Helm chart `traefik/traefik` v39.x (Traefik v3.6.x) |
| Network | hostNetwork, ports 80 (HTTP) and 443 (HTTPS) on all interfaces |
| Dashboard | `https://traefik.vcf.lab/dashboard/` (protected by Authentik forward-auth) |
| TLS Secrets | `traefik-vcf-lab-tls`, `dns-vcf-lab-tls`, `vault-vcf-lab-tls`, `auth-vcf-lab-tls` in `traefik` namespace |
| Config files | `/root/traefik-values.yaml`, `/root/traefik-dashboard-ingressroute.yaml` |

**Helm values location:** `/root/traefik-values.yaml`

```bash
# Upgrade Traefik after values change
helm upgrade traefik traefik/traefik -n traefik -f /root/traefik-values.yaml
```

### Technitium DNS (namespace: `default`)

| Item | Detail |
| --- | --- |
| Deployment | DaemonSet with hostNetwork |
| Ports | 53 (DNS), 5380 (Web UI) |
| Web UI | `https://dns.vcf.lab` (via Traefik), also `http://10.1.1.1:5380` directly |
| Admin | `admin` / creds.txt password |
| Data | `/holodeck-runtime/dns/` on host |
| Zones | `vcf.lab`, `site-a.vcf.lab`, `site-b.vcf.lab` + reverse zones |

**Technitium API pattern:**

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
# Login and get token
TOKEN=$(curl -s "http://localhost:5380/api/user/login?user=admin&pass=${PASSWORD}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Add an A record
curl -s "http://localhost:5380/api/zones/records/add?token=${TOKEN}&domain=newhost.vcf.lab&zone=vcf.lab&type=A&ipAddress=10.1.1.100&ttl=3600"

# List zones
curl -s "http://localhost:5380/api/zones/list?token=${TOKEN}&pageNumber=1&zonesPerPage=100"
```

### HashiCorp Vault (namespace: `vault-pki-lab`)

| Item | Detail |
| --- | --- |
| Deployment | Helm chart `vault` v0.32.0 (Vault v1.21.2), StatefulSet |
| Web UI | `https://vault.vcf.lab` (via Traefik), also `http://10.1.1.1:32000` (NodePort) |
| Root Token | creds.txt password  |
| Unseal Key | `/root/vault-keys/init.json` |
| PKI Engine | Mounted at `pki/`, CA: "vcf.lab Root Authority" (valid until 2036) |
| PKI Role | `holodeck` - allows `*.vcf.lab` subdomains, max TTL 9528h (397 days) |
| Storage | File backend at `/vault/data` inside pod |

**Issue a TLS certificate:**

```bash
kubectl exec -n vault-pki-lab vault-0 -- sh -c '
  export VAULT_ADDR=http://127.0.0.1:8200
  export VAULT_TOKEN="$(cat /home/holuser/creds.txt)"
  vault write -format=json pki/issue/holodeck \
    common_name=newhost.vcf.lab \
    ttl=9528h'
```

**Extract cert/key from JSON:**

```python
import json
with open("cert.json") as f:
    data = json.load(f)
cert = data["data"]["certificate"] + "\n" + data["data"]["issuing_ca"]
key = data["data"]["private_key"]
```

**Create K8s TLS secret from cert:**

```bash
kubectl create secret tls newhost-vcf-lab-tls \
  --cert=newhost.crt --key=newhost.key \
  -n traefik
```

### Authentik (namespace: `authentik`)

| Item | Detail |
| --- | --- |
| Deployment | K8s manifests (server + worker + PostgreSQL) |
| Version | 2025.12.4 |
| Web UI | `https://auth.vcf.lab` (via Traefik) |
| Admin | `akadmin` / creds.txt password |
| API Token | Stored in `/root/authentik/bootstrap-token.txt` on router |
| Manifests | `/root/authentik/` on router |
| Data (DB) | `/opt/authentik-data/postgres/` on host |
| Data (Files) | `/opt/authentik-data/files/` on host, mounted at `/data` in server + worker pods |

**Users:** akadmin, provider-admin, tenant-admin, tenant-user, holuser (all use creds.txt password)

**Groups:** provider-admins, tenant-admins, tenant-users, app-users

**API pattern:**

```bash
TOKEN=$(cat /root/authentik/bootstrap-token.txt | cut -d: -f2)
SVC_IP=$(kubectl get svc -n authentik authentik-server -o jsonpath='{.spec.clusterIP}')
API="http://${SVC_IP}:9000/api/v3"

# List users
curl -sf -H "Authorization: Bearer ${TOKEN}" "${API}/core/users/"

# Create a user
curl -sf -X POST "${API}/core/users/" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"username":"newuser","email":"new@vcf.lab","name":"New User","is_active":true}'
```

**Forward-Auth Configuration:**
- Proxy Provider: `traefik-forward-auth` (PK=1, mode=forward_single)
- Application: `traefik-dashboard`
- Outpost: Embedded outpost with `authentik_host=https://auth.vcf.lab`
- Traefik Middleware: `authentik-forward-auth` in `traefik` namespace

### FRR-K8s (namespace: `default`)

| Item | Detail |
| --- | --- |
| Deployment | Helm chart `frr-k8s` v0.0.18, DaemonSet with hostNetwork |
| Services | BGP (port 179), Zebra, BFD, StaticD |
| Config | Managed via FRR K8s CRDs |

### Squid Proxy

| Item | Detail |
| --- | --- |
| Deployment | systemd service (`squid.service`) |
| Port | 3128 |
| Config | `/etc/squid/squid.conf` |
| Allowlist | `/etc/squid/allowlist` |

### NFS Server

| Item | Detail |
| --- | --- |
| Export | `/opt/nfs-share *(rw,sync)` |
| Port | 2049 |
| Mount point | Used by manager at `/mnt/manager` |

## Common Operations

### Add a New HTTPS Service Behind Traefik

1. **Create DNS record** in Technitium:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
TOKEN=$(curl -s "http://localhost:5380/api/user/login?user=admin&pass=${PASSWORD}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s "http://localhost:5380/api/zones/records/add?token=${TOKEN}&domain=newservice.vcf.lab&zone=vcf.lab&type=A&ipAddress=10.1.1.1&ttl=3600"
```

2. **Issue TLS certificate** from Vault:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
kubectl exec -n vault-pki-lab vault-0 -- sh -c \
  'VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN="${PASSWORD}" vault write -format=json pki/issue/holodeck common_name=newservice.vcf.lab ttl=9528h' > /root/traefik-certs/newservice.json
```

3. **Extract and store as K8s secret:**

```bash
cd /root/traefik-certs
python3 -c "
import json
with open('newservice.json') as f: data = json.load(f)
with open('newservice.crt','w') as f: f.write(data['data']['certificate']+'\n'+data['data']['issuing_ca']+'\n')
with open('newservice.key','w') as f: f.write(data['data']['private_key']+'\n')
"
kubectl create secret tls newservice-vcf-lab-tls --cert=newservice.crt --key=newservice.key -n traefik
```

4. **Create IngressRoute:**

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: newservice
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`newservice.vcf.lab`)
      kind: Rule
      services:
        - name: my-service
          namespace: my-namespace
          port: 8080
  tls:
    secretName: newservice-vcf-lab-tls
```

### Renew TLS Certificates

Certificates expire after 397 days. To renew:

1. Re-issue from Vault (same command as initial issuance)
2. Update the K8s TLS secret:

```bash
kubectl create secret tls SERVICE-vcf-lab-tls \
  --cert=SERVICE.crt --key=SERVICE.key \
  -n traefik --dry-run=client -o yaml | kubectl apply -f -
```

3. Traefik picks up the new secret automatically (no restart needed)

### Protect a Service with Authentik Forward-Auth

Add the middleware reference to an IngressRoute:

```yaml
middlewares:
  - name: authentik-forward-auth
    namespace: traefik
```

And add the outpost callback route (higher priority):

```yaml
- match: Host(`myservice.vcf.lab`) && PathPrefix(`/outpost.goauthentik.io/`)
  kind: Rule
  priority: 15
  services:
    - name: authentik-server
      namespace: authentik
      port: 9000
```

## Console VM (Firefox)

- **Profile path:** `~/snap/firefox/common/.mozilla/firefox/hu6lbvyx.default/`
- **Vault CA trust:** Imported as "vcf.lab Root Authority" (CT,C,C)
- **User-Agent override:** macOS Safari (`general.useragent.override` in `user.js`)
- **Proxy:** PAC or manual proxy at `10.0.0.1:3128`

## Troubleshooting

### Traefik pod won't start (port conflict)

Check if old pods still hold ports 80/443. Force-delete stale pods:

```bash
kubectl delete pod <old-pod> -n traefik --force --grace-period=0
```

### Vault is sealed after reboot

```bash
kubectl exec -n vault-pki-lab vault-0 -- vault operator unseal $(cat /root/vault-keys/init.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["unseal_key"])')
```

### Authentik outpost returns 404 for forward-auth

The embedded outpost needs time to reload providers after configuration changes. Restart the server:

```bash
kubectl rollout restart deployment/authentik-server -n authentik
```

Wait ~30 seconds for the outpost to register providers.

### DNS changes not resolving

Technitium API tokens expire. Re-login to get a fresh token before making API calls.

### Certificate errors in Firefox

Verify the Vault CA is trusted:

```bash
certutil -L -d sql:~/snap/firefox/common/.mozilla/firefox/hu6lbvyx.default/ | grep vcf
```

If missing, re-import:

```bash
certutil -A -n "vcf.lab Root Authority" -t "CT,C,C" -i /tmp/vcf-lab-ca.crt -d sql:~/snap/firefox/common/.mozilla/firefox/hu6lbvyx.default/
```
