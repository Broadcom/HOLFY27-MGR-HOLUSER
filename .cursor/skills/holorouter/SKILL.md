---
description: Guide interactions with the Holorouter - the Photon OS 5.0 router/gateway running Kubernetes (kubeadm v1.27), nginx reverse proxy, Technitium DNS, HashiCorp Vault PKI, Authentik identity provider, GitLab EE, MSADCS Proxy, FRR-K8s BGP routing, and Squid proxy. Use when the user mentions holorouter, router, DNS, nginx, reverse proxy, Vault certificates, Authentik, GitLab, certsrv, FRR, BGP, Squid, or any holorouter service management.
globs: 
  - "**/Tools/holorouter/**"
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
| Kubernetes | kubeadm v1.27.16, single control-plane node (`holorouter`), Flannel CNI |
| CPU / RAM | 4 vCPU / 7.8 GB |
| Disk | 74 GB total (sda3), ~53 GB free |
| Container Runtime | containerd 1.6.17 |
| Package Manager | `tdnf` (Photon), `helm` v3.17 |
| Node Name | `holorouter` |
| Node IP (K8s/eth0) | 192.168.0.2 |
| Reverse Proxy | nginx (systemd), ports 80/443 on all interfaces |

### Network Interfaces

The router has ~30 VLAN interfaces spanning two sites:

- **eth0** (192.168.0.2/24) - External/management (default gateway 192.168.0.1)
- **eth1** (10.1.10.129/25) - Site A trunk
- **eth1.10** (10.1.1.1/24) - Site A management VLAN
- **eth1.11-25** (10.1.2.x - 10.1.9.x) - Site A VLANs
- **eth2** - Site B trunk
- **eth2.40-58** (10.2.1.x - 10.2.9.x) - Site B VLANs
- **cni0/flannel.1** (10.244.0.x) - Kubernetes pod network

IP forwarding is enabled. MASQUERADE rules handle NAT.

### DNS Records

All service FQDNs resolve to `192.168.0.2` (eth0 IP) via Technitium DNS in the `vcf.lab` zone:

| Record | Type | Target |
| --- | --- | --- |
| `vault.vcf.lab` | A | 192.168.0.2 |
| `technitium.vcf.lab` | A | 192.168.0.2 |
| `authentik.vcf.lab` | A | 192.168.0.2 |
| `gitlab.vcf.lab` | A | 192.168.0.2 |
| `gitlab-registry.vcf.lab` | A | 192.168.0.2 |
| `ca.vcf.lab` | A | 192.168.0.2 |
| `dns.vcf.lab` | CNAME | technitium.vcf.lab |
| `auth.vcf.lab` | CNAME | authentik.vcf.lab |

## Service Inventory

### nginx Reverse Proxy (systemd)

| Item | Detail |
| --- | --- |
| Deployment | systemd service (`nginx.service`), enabled at boot |
| Config | `/etc/nginx/nginx.conf` |
| Ports | 80 (HTTP) and 443 (HTTPS) on `0.0.0.0` |
| Version | nginx 1.26.x |

nginx acts as the host-based reverse proxy for all web services. It routes by `server_name`:

| server_name | Port | Backend | SSL Cert |
| --- | --- | --- | --- |
| `vault.vcf.lab` | 80 | `http://localhost:32000` (Vault NodePort) | — |
| `vault.vcf.lab` | 443 | `http://localhost:32000` (Vault NodePort) | `/root/nginx-certs/vault.{crt,key}` (IPs: 192.168.0.2, 10.1.1.1) |
| `technitium.vcf.lab dns.vcf.lab` | 80 | 301 redirect to HTTPS | — |
| `technitium.vcf.lab dns.vcf.lab` | 443 | `http://localhost:5380` (Technitium) | `/root/nginx-certs/technitium.{crt,key}` (SANs: dns.vcf.lab, 192.168.0.2, 10.1.1.1) |
| `auth.vcf.lab authentik.vcf.lab` | 80 | `http://localhost:31080` (Authentik, no redirect) | — |
| `auth.vcf.lab authentik.vcf.lab` | 443 | `http://localhost:31080` (Authentik) | `/root/nginx-certs/authentik.{crt,key}` (SANs: authentik.vcf.lab, 192.168.0.2) |
| `gitlab.vcf.lab` | 80 | 301 redirect to HTTPS | — |
| `gitlab.vcf.lab` | 443 | `https://localhost:30443` (GitLab NodePort) | `/holodeck-runtime/gitlab/ssl/gitlab.{crt,key}` |
| `gitlab-registry.vcf.lab` | 80 | 301 redirect to HTTPS | — |
| `gitlab-registry.vcf.lab` | 443 | `https://localhost:30005` (GitLab registry) | `/holodeck-runtime/gitlab/ssl/gitlab-registry.{crt,key}` |
| `ca.vcf.lab` | 80 | 301 redirect to HTTPS | — |
| `ca.vcf.lab` | 443 | `http://localhost:8900` (certsrv-proxy) | `/root/certsrv-proxy/ca.{crt,key}` |

```bash
# Reload after config changes
nginx -t && nginx -s reload
```

### Technitium DNS (namespace: `default`)

| Item | Detail |
| --- | --- |
| Deployment | DaemonSet with hostNetwork |
| Ports | 53 (DNS), 5380 (Web UI) |
| Web UI | `https://technitium.vcf.lab` or `https://dns.vcf.lab` (via nginx SSL), also `http://10.1.1.1:5380` directly |
| Admin | `admin` / creds.txt password |
| Data | `/holodeck-runtime/dns/` on host |
| Zones | `vcf.lab`, `site-a.vcf.lab`, `site-b.vcf.lab` + reverse zones |

**Technitium API pattern:**

*(Note to Agent: If modifying the token extraction or JSON payloads, consider writing a full python `requests` script to avoid bash/JSON escaping hell).*

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
# Login and get token
TOKEN=$(curl -s "http://localhost:5380/api/user/login?user=admin&pass=${PASSWORD}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Add an A record
curl -s "http://localhost:5380/api/zones/records/add?token=${TOKEN}&domain=newhost.vcf.lab&zone=vcf.lab&type=A&ipAddress=192.168.0.2&ttl=3600"

# Add a CNAME record
curl -s "http://localhost:5380/api/zones/records/add?token=${TOKEN}&domain=alias.vcf.lab&zone=vcf.lab&type=CNAME&cname=target.vcf.lab&ttl=3600"

# List zones
curl -s "http://localhost:5380/api/zones/list?token=${TOKEN}&pageNumber=1&zonesPerPage=100"
```

### HashiCorp Vault (namespace: `vault-pki-lab`)

| Item | Detail |
| --- | --- |
| Deployment | Helm chart `vault` v0.32.0 (Vault v1.21.2), StatefulSet |
| Mode | Standalone with file-based persistent storage (after `configure_holorouter.py`) |
| Web UI | `http://vault.vcf.lab` (via nginx), also `http://192.168.0.2:32000` (NodePort) |
| Root Token | creds.txt password (stored in `/root/vault-keys/init.json`) |
| Unseal Key | `/root/vault-keys/init.json` |
| PKI Engine | Mounted at `pki/`, CA: "vcf.lab Root Authority" (10-year TTL) |
| PKI Role | `holodeck` - allows `*.vcf.lab` subdomains, max TTL 720h (30 days) |
| Storage | File backend at `/opt/vault-data` (local-storage PV) |
| Auto-unseal | `/root/unseal_vault.sh` via cron `@reboot` |

**CRITICAL:** The holorouter ships with Vault in dev mode (`inmem` storage, root token `holodeck`). Run `configure_holorouter.py` or `/root/config_vault.sh` to migrate to standalone persistent mode with the correct creds.txt token.

**Issue a TLS certificate:**

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
curl -s -H "X-Vault-Token: ${PASSWORD}" \
  -X POST http://localhost:32000/v1/pki/issue/holodeck \
  -d '{"common_name":"newhost.vcf.lab","ttl":"720h"}' | python3 -m json.tool
```

**Extract cert/key from JSON:**

```python
import json
with open("cert.json") as f:
    data = json.load(f)
cert = data["data"]["certificate"] + "\n" + data["data"]["issuing_ca"]
key = data["data"]["private_key"]
```

### GitLab EE (namespace: `default`)

| Item | Detail |
| --- | --- |
| Deployment | K8s Deployment (`gitlab`), single replica |
| Image | `gitlab/gitlab-ee:latest` |
| Web UI | `https://gitlab.vcf.lab` (via nginx HTTPS) |
| Registry | `https://gitlab-registry.vcf.lab` (via nginx HTTPS, port 30005) |
| SSH | Port 30022 (NodePort) |
| Admin | `root` / creds.txt password |
| SSL Certs | `/holodeck-runtime/gitlab/ssl/` and `/holodeck-runtime/gitlab/config/ssl/` |
| Data | `/holodeck-runtime/gitlab/config/`, `data/`, `logs/` (hostPath) |
| Manifests | `/holodeck-runtime/gitlab/gitlab_deployment.yaml`, `gitlab_service.yaml` |
| Install script | `/holodeck-runtime/gitlab/install_and_configure_gitlab.sh` |

**CRITICAL:** The holorouter ships with GitLab using a self-signed cert and a random auto-generated root password. Run `configure_holorouter.py` to fix both.

**GitLab API access:**

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
TOKEN=$(curl -sk -X POST "https://gitlab.vcf.lab/oauth/token" \
  -F grant_type=password -F username=root -F "password=${PASSWORD}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# List projects
curl -sk -H "Authorization: Bearer ${TOKEN}" "https://gitlab.vcf.lab/api/v4/projects"
```

### Authentik (namespace: `default`)

| Item | Detail |
| --- | --- |
| Deployment | Helm chart `authentik` v2026.2.1 in `default` namespace |
| Components | authentik-server (Deployment), authentik-worker (Deployment), authentik-postgresql (StatefulSet) |
| Web UI | `https://auth.vcf.lab` (preferred) or `https://authentik.vcf.lab` (via nginx SSL) |
| Admin | `akadmin` / creds.txt password |
| API Token | `holodeck` (bootstrap token) |
| Data (DB) | `/holodeck-runtime/authentik/postgres/` on host (PV `authentik-postgres-pv`) |
| Data (Media) | `/holodeck-runtime/authentik/media/` on host (PV `authentik-media-pv`) |
| Helm values | `/holodeck-runtime/authentik/authentik-values.yaml` |
| Storage manifests | `/holodeck-runtime/authentik/authentik-storage.yaml` |

**Users:** akadmin, dev-user, dev-admin, dev-readonly, approver, requestor, prod-user, prod-admin, prod-readonly, vcadmin, demouser, backup, audit, configadmin (all use creds.txt password)

**Groups:** dev-users, dev-admins, dev-readonly, approvers, prod-users, prod-admins, prod-readonly

**API pattern:**

```bash
API="http://192.168.0.2:31080/api/v3"

# List users
curl -sf -H "Authorization: Bearer holodeck" "${API}/core/users/"

# Create a user
curl -sf -X POST "${API}/core/users/" \
  -H "Authorization: Bearer holodeck" \
  -H "Content-Type: application/json" \
  -d '{"username":"newuser","email":"new@vcf.lab","name":"New User","is_active":true}'
```

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

### Certsrv Proxy - Microsoft CA for Vault PKI (namespace: `default`)

| Item | Detail |
| --- | --- |
| Deployment | K8s DaemonSet with hostNetwork (python:3.11-slim) |
| Purpose | Impersonates Microsoft ADCS certsrv, forwards CSR signing to Vault PKI |
| Listen Port | 8900 (behind nginx HTTPS termination) |
| Hostname | `ca.vcf.lab` -> 192.168.0.2 (via nginx :443 -> localhost:8900) |
| Auth | Basic Auth (any username, password = creds.txt) |
| Vault Role | `pki/sign/holodeck` on http://127.0.0.1:32000 |
| Files on Router | `/root/certsrv-proxy/` (script, Dockerfile, creds.txt) |
| Source | `/home/holuser/hol/Tools/holorouter/certsrv_proxy.py` on manager VM |

**Endpoints implemented:**

| Path | Method | Description |
| --- | --- | --- |
| `/certsrv/` | GET | Credential check (200 OK / 401) |
| `/certsrv/certrqxt.asp` | GET | Template list (Option Value format with OID;Name) |
| `/certsrv/certfnsh.asp` | POST | CSR submission (returns HTML with ReqID) |
| `/certsrv/certnew.cer` | GET | Certificate retrieval (issued or CA cert) |
| `/certsrv/certcarc.asp` | GET | CA renewal count (`nRenewals=0`) |
| `/certsrv/certnew.p7b` | GET | CA chain (PKCS#7 DER format) |

**SDDC Manager integration:**

```bash
curl -sk -X PUT "https://sddcmanager-a.site-a.vcf.lab/v1/certificate-authorities" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"microsoftCertificateAuthoritySpec":{"username":"admin@vcf.lab","secret":"<password>","serverUrl":"https://ca.vcf.lab/certsrv","templateName":"VCFWebServer"}}'
```

**Key implementation details:**
- Template validation by SDDC Manager requires `<Option Value="OID;TemplateName">` format in `certrqxt.asp`
- PKCS#7 built using `cryptography.hazmat.primitives.serialization.pkcs7.serialize_certificates()` (pyOpenSSL PKCS7 class removed in newer versions)
- DNS record must be in a `vcf.lab` zone (not `site-a.vcf.lab`) -- the installer creates this zone if needed
- In-memory cert store -- certificates are lost on pod restart

## Common Operations

### Add a New HTTPS Service Behind nginx

1. **Create DNS record** in Technitium:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
TOKEN=$(curl -s "http://localhost:5380/api/user/login?user=admin&pass=${PASSWORD}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s "http://localhost:5380/api/zones/records/add?token=${TOKEN}&domain=newservice.vcf.lab&zone=vcf.lab&type=A&ipAddress=192.168.0.2&ttl=3600"
```

2. **Issue TLS certificate** from Vault:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
curl -s -H "X-Vault-Token: ${PASSWORD}" \
  -X POST http://localhost:32000/v1/pki/issue/holodeck \
  -d '{"common_name":"newservice.vcf.lab","ttl":"720h"}' > /root/certs/newservice.json
python3 -c "
import json
with open('/root/certs/newservice.json') as f: data = json.load(f)
with open('/root/certs/newservice.crt','w') as f: f.write(data['data']['certificate']+'\n'+data['data']['issuing_ca']+'\n')
with open('/root/certs/newservice.key','w') as f: f.write(data['data']['private_key']+'\n')
"
```

3. **Add nginx server block** to `/etc/nginx/nginx.conf`:

```nginx
server {
    listen 80;
    server_name newservice.vcf.lab;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name newservice.vcf.lab;
    ssl_certificate     /root/certs/newservice.crt;
    ssl_certificate_key /root/certs/newservice.key;
    location / {
        proxy_pass http://localhost:<service-port>;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

4. **Reload nginx:**

```bash
nginx -t && nginx -s reload
```

### Renew TLS Certificates

Certificates expire after 30 days (720h). To renew:

1. Re-issue from Vault (same curl command as initial issuance)
2. Overwrite the cert/key files
3. Reload nginx: `nginx -s reload`

### Normalization Script

Run `configure_holorouter.py` from the manager VM to fix all common post-import issues:

```bash
cd /home/holuser/hol/Tools/holorouter
python3 configure_holorouter.py
```

This script fixes: Vault dev->standalone migration, PKI setup, GitLab certs + root password, nginx config, certsrv proxy deployment, Authentik users/groups/icons, and shutdown.sh.

## Console VM (Firefox)

- **Profile path:** `~/snap/firefox/common/.mozilla/firefox/hu6lbvyx.default/`
- **Vault CA trust:** Imported as "vcf.lab Root Authority" (CT,C,C)
- **User-Agent override:** macOS Safari (`general.useragent.override` in `user.js`)
- **Proxy:** PAC or manual proxy at `10.0.0.1:3128`

## Troubleshooting

### nginx port conflict with another service

Check what is listening on ports 80/443:

```bash
ss -tlnp | grep -E ':80 |:443 '
```

### Vault is sealed after reboot

The `unseal_vault.sh` cron job should auto-unseal. If it fails:

```bash
kubectl exec -n vault-pki-lab vault-0 -- vault operator unseal $(python3 -c "import json; print(json.load(open('/root/vault-keys/init.json'))['unseal_key'])")
```

### Vault still in dev mode (inmem storage)

Check with `curl -s http://localhost:32000/v1/sys/seal-status | python3 -m json.tool | grep storage_type`. If `inmem`, run:

```bash
bash /root/config_vault.sh
```

Or from the manager VM:

```bash
python3 /home/holuser/hol/Tools/holorouter/configure_holorouter.py
```

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

### GitLab shows Vault UI on HTTP

If `http://gitlab.vcf.lab` shows the Vault web interface instead of GitLab, the nginx config is missing the HTTP redirect block. Run `configure_holorouter.py` or manually add:

```nginx
server {
    listen 80;
    server_name gitlab.vcf.lab;
    return 301 https://$host$request_uri;
}
```

Then reload: `nginx -t && nginx -s reload`

### Graceful shutdown

Always use the shutdown script to prevent stale pods and corrupted state:

```bash
/root/shutdown.sh              # Shutdown K8s + nginx + squid, then poweroff
/root/shutdown.sh --no-poweroff # Shutdown services only
/root/shutdown.sh --reboot     # Shutdown services, then reboot
```
