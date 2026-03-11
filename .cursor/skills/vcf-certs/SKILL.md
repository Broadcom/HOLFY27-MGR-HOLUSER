---
name: vcf-certs
description: Manage SSL/TLS certificate lifecycle in VCF 9.x Holodeck labs. Covers the MSADCS Proxy (certsrv→Vault PKI), SDDC Manager certificate generation/installation, VCF Operations Fleet Management cert replacement, Vault PKI configuration, PKCS#7 ordering pitfalls, CSR normalization, and troubleshooting certificate validation failures. Use when working with VCF certificates, certsrv proxy, Vault PKI signing, SDDC Manager Generate CSRs, Generate Signed Certificates, Install Certificates, PKCS#7, certificate replacement, Microsoft CA configuration, or any TLS/SSL cert operations in VCF.
---

# VCF 9.x Certificate Management

Consolidated guide for all certificate operations in the Holodeck VCF lab. Passwords in `/home/holuser/creds.txt`. Proxy source in `Tools/CertsrvProxy/`.

## Architecture Overview

```
VCF Components (SDDC Mgr, VCF Ops)
        |  HTTPS :443 (certsrv protocol)
        v
MSADCS Proxy (K8s DaemonSet, holorouter, hostNetwork)
        |  HTTP :32000 (X-Vault-Token)
        v
HashiCorp Vault PKI (pki/ mount, role: holodeck)
```

- **DNS**: `ca.vcf.lab → 10.1.1.1` (Technitium, zone `vcf.lab`)
- **Vault**: `http://127.0.0.1:32000` on holorouter. Token = creds.txt password
- **Proxy scripts**: `certsrv_proxy-beta.py` (standalone TLS), `certsrv_proxy.py` (Traefik), `certsrv_proxy_docker.py`

## 1. Vault PKI Configuration

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
VAULT="http://10.1.1.1:32000"

# Verify token
curl -sk -H "X-Vault-Token: $PASSWORD" "$VAULT/v1/auth/token/lookup-self"

# Get CA cert
curl -sk "$VAULT/v1/pki/ca/pem"

# Check/update role for long-lived certs (default max_ttl is 720h)
curl -sk -H "X-Vault-Token: $PASSWORD" "$VAULT/v1/pki/roles/holodeck" | python3 -m json.tool
curl -sk -H "X-Vault-Token: $PASSWORD" -X POST "$VAULT/v1/pki/roles/holodeck" \
  -d '{"max_ttl":"17520h","allow_any_name":true,"enforce_hostnames":false}'
```

**Role requirements for VCF CSRs**: `allow_any_name: true`, `enforce_hostnames: false` (some VCF components use non-hostname CNs like `VCFA`, `OPS_LOGS`).

## 2. MSADCS Proxy — Deployment

### Install (Beta/Standalone — current lab mode)

```bash
cd /home/holuser/hol/Tools/CertsrvProxy
bash install_certsrv_proxy-beta.sh
```

Creates DNS record, issues TLS cert from Vault, deploys K8s DaemonSet on holorouter port 443.

### Manual operations

```bash
PASSWORD='VMware123!VMware123!'

# Check proxy pod
sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=accept-new root@10.1.1.1 \
  'kubectl get pods -l app=certsrv-proxy'

# View proxy logs
sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=accept-new root@10.1.1.1 \
  'kubectl logs -l app=certsrv-proxy --tail=50'

# Redeploy after code changes
sshpass -p "$PASSWORD" scp -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password \
  /home/holuser/hol/Tools/CertsrvProxy/certsrv_proxy-beta.py \
  root@10.1.1.1:/root/certsrv-proxy/certsrv_proxy-beta.py
sshpass -p "$PASSWORD" ssh root@10.1.1.1 \
  'kubectl delete pod -l app=certsrv-proxy --grace-period=5'
```

## 3. SDDC Manager — Configure Microsoft CA

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
SDDC="sddcmanager-a.site-a.vcf.lab"

TOKEN=$(curl -sk -X POST "https://$SDDC/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"$PASSWORD\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['accessToken'])")

curl -sk -X PUT "https://$SDDC/v1/certificate-authorities" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "microsoftCertificateAuthoritySpec": {
      "serverUrl": "https://ca.vcf.lab/certsrv",
      "username": "administrator",
      "password": "'"$PASSWORD"'",
      "secret": "'"$PASSWORD"'",
      "templateName": "VCFWebServer"
    }
  }'
```

**Critical**: Both `password` AND `secret` fields required (HTTP 400 without `secret`).

## 4. SDDC Manager — Certificate Workflow (UI)

The full workflow via SDDC Manager UI (`Inventory > Workload Domains > [domain] > Security`):

1. **Select resource** (checkbox) → **Generate CSRs** → fill OU/Org/Locality/State/Country → Next → Next → Generate CSRs
2. **Select resource** → **Generate Signed Certificates** → select "Microsoft" CA → Generate Certificates
3. **Select resource** → **Install Certificates**

Each step creates a task visible in the Tasks panel.

## 5. SDDC Manager — Certificate Workflow (API)

```bash
# List domain certificates
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://$SDDC/v1/domains/{domain_id}/resource-certificates"

# Generate CSR (POST)
curl -sk -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "https://$SDDC/v1/domains/{domain_id}/csrs/generate" \
  -d '{"resources":[{"fqdn":"vc-mgmt-a.site-a.vcf.lab","type":"vcenter"}],
       "csrGenerationSpec":{"keyAlgorithm":"RSA","keySize":"2048",
         "organization":"Broadcom","organizationalUnit":"HOL",
         "locality":"Palo Alto","state":"CA","country":"US"}}'
```

## 6. Vault CSR Signing — Critical Details

### Use `sign-verbatim`, not `sign`

Vault's `pki/sign/{role}` strips subject DN fields (O, OU, C, L, ST) when the role has empty arrays. SDDC Manager validates the full subject DN. Always use `sign-verbatim`:

```python
payload = {
    'csr': csr_pem,
    'common_name': cn,
    'ttl': '17520h',
    'ext_key_usage': ['ServerAuth', 'ClientAuth'],
    'key_usage': ['DigitalSignature', 'KeyAgreement', 'KeyEncipherment'],
}
resp = requests.post(f'{vault_url}/v1/pki/sign-verbatim/holodeck',
    headers={'X-Vault-Token': token}, json=payload, verify=False)
```

### CSR Normalization

SDDC Manager sends CSRs with PEM header glued to base64 (no newline). Normalize before sending to Vault:

```python
def normalize_csr_pem(raw: str) -> str:
    raw = raw.strip()
    header = '-----BEGIN CERTIFICATE REQUEST-----'
    footer = '-----END CERTIFICATE REQUEST-----'
    b64 = raw.split(header, 1)[1].split(footer, 1)[0] if header in raw else raw
    b64 = re.sub(r'\s+', '', b64)
    lines = [b64[i:i+64] for i in range(0, len(b64), 64)]
    return header + '\n' + '\n'.join(lines) + '\n' + footer
```

## 7. PKCS#7 Ordering — The Critical Pitfall

**Problem**: Python's `cryptography.hazmat.primitives.serialization.pkcs7.serialize_certificates()` uses strict DER encoding which **sorts SET OF elements by encoded byte value**. This reorders certificates regardless of input order.

**Impact**: SDDC Manager's `CertificateOperationOrchestratorImpl` takes `certs[0]` as the signed cert and `certs[1..]` as the CA chain from the PKCS#7. When DER sorting puts the CA cert before the leaf, SDDC Manager compares the CA's public key against the CSR and fails with:
> "Public key in CSR and server certificate are not matching."

**Fix**: Use `build_ordered_pkcs7()` which manually constructs the ASN.1 structure preserving insertion order:

```python
def _der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    elif length < 0x100:
        return bytes([0x81, length])
    elif length < 0x10000:
        return bytes([0x82, length >> 8, length & 0xff])
    else:
        return bytes([0x83, length >> 16, (length >> 8) & 0xff, length & 0xff])

def build_ordered_pkcs7(cert_der_list: list[bytes]) -> bytes:
    oid_signed_data = bytes([0x06,0x09,0x2a,0x86,0x48,0x86,0xf7,0x0d,0x01,0x07,0x02])
    oid_data = bytes([0x06,0x09,0x2a,0x86,0x48,0x86,0xf7,0x0d,0x01,0x07,0x01])
    version = bytes([0x02,0x01,0x01])
    digest_algs = bytes([0x31,0x00])
    content_info = bytes([0x30]) + _der_length(len(oid_data)) + oid_data
    certs_content = b''.join(cert_der_list)
    certs_field = bytes([0xa0]) + _der_length(len(certs_content)) + certs_content
    signer_infos = bytes([0x31,0x00])
    sd = version + digest_algs + content_info + certs_field + signer_infos
    signed_data = bytes([0x30]) + _der_length(len(sd)) + sd
    explicit0 = bytes([0xa0]) + _der_length(len(signed_data)) + signed_data
    outer = oid_signed_data + explicit0
    return bytes([0x30]) + _der_length(len(outer)) + outer
```

**Usage**: Pass `[leaf_der, ca_der]` — leaf cert first, CA second.

**Verification**: Test with Java's `CertificateFactory.generateCertificates()` on SDDC Manager to confirm `certs[0]` is the leaf:

```java
CertificateFactory cf = CertificateFactory.getInstance("X.509", "SUN");
FileInputStream fis = new FileInputStream("/tmp/test.p7b");
Collection<? extends Certificate> certs = cf.generateCertificates(fis);
// certs iterator order must match insertion order
```

## 8. SDDC Manager Internal Certificate Flow

Understanding the Java code path is essential for debugging:

1. `CertificateOperationOrchestratorImpl.generateCertificate()` calls `getCertificateChain(csr)` on the CA plugin
2. `MicrosoftCaPlugin.getCertificateChain()` → `MicrosoftCaService.generateAndFetchCertificateChain()`
3. CSR submitted to `POST /certsrv/certfnsh.asp`, HTML response parsed for `certnew.cer?ReqID=(\d+)&amp` (HTML entity `&amp;`, not raw `&`)
4. Cert retrieved via `GET /certsrv/certnew.p7b?ReqID=<id>&Enc=b64`
5. `CertificateConverter.convertPkcs7ToX509Certificate()` uses Java `CertificateFactory.generateCertificates()` to parse PKCS#7 into `X509Certificate[]`
6. `certs[0]` → `setSignedCertificate()`, `certs[1..]` → `setCaChain()` via `Arrays.copyOfRange`
7. During install: `SslCertValidator.validateCSRContent()` compares CSR public key against `certs[0]` public key

### Key proxy protocol requirements

- `certfnsh.asp` POST body: URL-encoded form, use `parse_qs()` not `unquote_plus()` (preserves `+` in base64)
- `certnew.p7b` response must be PEM-wrapped: `-----BEGIN PKCS7-----\n<base64 64-char lines>\n-----END PKCS7-----`
- `certrqxt.asp` template options: `<Option Value="1.3.6.1.4.1.311.21.8.X;TemplateName">Display Name</Option>`

## 9. VCF Operations Fleet Management — Certificate Replacement

For fleet-managed components (auto-a, ops-a, opslogs-a, vidb-a, fleet-01a, etc.). See `vcf-9-api` skill Section 15 for the full API reference.

**Key orchestrator split**:

| Orchestrator | Components | Behavior |
| --- | --- | --- |
| VROPS | vidb-a, opslogs-a, opsnet-a, fleet-01a, instance-01a, vsp-01a | Completes in minutes |
| VRSLCM | auto-a, auto-platform-a, ops-a | Depends on fleet-upgrade-service health |

VRSLCM tasks remain `NOT_STARTED` indefinitely if `fleet-upgrade-service` is down. Workaround: use VCF Operations UI manually.

## 10. Troubleshooting Reference

### Certificate Generation Failed — "No certificate data found"

PKCS#7 response was raw base64 without PEM headers. Proxy must wrap in `-----BEGIN PKCS7-----` / `-----END PKCS7-----` with 64-char line breaks.

### Certificate Validation Failed — "Public key mismatch"

PKCS#7 DER ordering put CA cert as `certs[0]`. Fix: use `build_ordered_pkcs7()` (Section 7).

### Certificate Validation Failed — "Failed to validate the certificate"

Vault `pki/sign/{role}` stripped subject DN fields. Fix: use `pki/sign-verbatim/{role}` (Section 6).

### NSX ReTrust Failed After Install

Transient — NSX tries to re-trust vCenter while services restart. Retry the install. If cert dates updated, cert is installed; only trust needs refresh.

### VCF Operations Rejects CA — "Failed to update certificate authorities"

Proxy returned 301 for `/certsrv` (no trailing slash). Fix: serve HTTP 200 directly for both `/certsrv` and `/certsrv/`.

### Proxy "Address already in use" on restart

Orphaned Python process holds port 443 after force pod delete. Fix: `kill -9 <pid>` on holorouter, then delete pod with `--grace-period=5`.

### Vault CSR Signing Returns 400 "csr contains no data"

CSR PEM malformed (no newlines). Apply `normalize_csr_pem()` (Section 6).

## 11. Diagnostic Commands

```bash
# SDDC Manager cert operation logs
sshpass -p "$PASSWORD" ssh vcf@sddcmanager-a.site-a.vcf.lab \
  'grep -i "validat\|Public key\|CERTIFICATE_VALIDATION\|Failed to validate\|replaceCert" \
   /var/log/vmware/vcf/operationsmanager/operationsmanager.log | tail -20'

# Proxy logs on holorouter
sshpass -p "$PASSWORD" ssh root@10.1.1.1 'kubectl logs -l app=certsrv-proxy --tail=50'

# Test PKCS#7 ordering (openssl)
curl -sk -u "admin:$PASSWORD" "https://ca.vcf.lab/certsrv/certnew.p7b?ReqID=1&Enc=b64" \
  | openssl pkcs7 -print_certs | grep "subject="
# certs[0] MUST be the leaf cert, certs[1] the CA

# SDDC Manager PostgreSQL access (for lock clearing)
sshpass -p "$PASSWORD" ssh vcf@sddcmanager-a.site-a.vcf.lab \
  "python3 -c \"
import subprocess, pty, os, time
master, slave = pty.openpty()
p = subprocess.Popen(['su', '-', 'root', '-c', 'cat /root/.pgpass'], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
os.close(slave); time.sleep(1)
os.write(master, b'$PASSWORD\n'); time.sleep(1)
print(p.stdout.read().decode()); os.close(master); p.wait()
\""
```

## 12. Cross-References

- **Vault PKI setup & Traefik TLS**: See `holorouter` skill (Vault section)
- **SDDC Manager credentials & PostgreSQL**: See `vcf-9-api` skill (Sections 10-12)
- **Certificate troubleshooting issues 27-30**: See `vcf-troubleshooting` skill
- **Proxy source code & deployment docs**: See `Tools/CertsrvProxy/README.md`
- **VCF Operations cert mgmt API**: See `vcf-9-api` skill (Section 15)
