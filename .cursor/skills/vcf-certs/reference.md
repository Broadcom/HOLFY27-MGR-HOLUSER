# VCF Certificate Management — Detailed Reference

## SDDC Manager Java Code Path (Decompiled)

Understanding these Java classes is critical for debugging certificate failures:

### MicrosoftCaService.generateAndFetchCertificateChain()

1. POSTs CSR to `{serverUrl}/certfnsh.asp` with URL-encoded body:
   - `Mode=newreq`
   - `CertRequest=<base64 PEM CSR>`
   - `CertAttrib=CertificateTemplate:<templateName>`
   - `TargetStoreFlags=0&SaveCert=yes`
2. Parses HTML response for `certnew.cer\?ReqID=(\d+)&amp` (note: `&amp;` HTML entity)
3. Fetches chain via `GET {serverUrl}/certnew.p7b?ReqID={reqID}&Enc=b64`
4. Decodes base64 PKCS#7 response
5. Returns `X509Certificate[]` via `CertificateConverter.convertPkcs7ToX509Certificate()`

### CertificateConverter.convertPkcs7ToX509Certificate()

```java
CertificateFactory cf = CertificateFactory.getInstance("X.509", "SUN");
InputStream is = new ByteArrayInputStream(pkcs7DerBytes);
Collection<? extends Certificate> certs = cf.generateCertificates(is);
return certs.toArray(new X509Certificate[0]);
```

Certificate order matches the ASN.1 structure order in the PKCS#7 blob.

### CertificateOperationOrchestratorImpl

```
certs[0] → setSignedCertificate()
certs[1..n] → setCaChain() via Arrays.copyOfRange(certs, 1, certs.length)
```

### SslCertValidator.validateCSRContent()

Compares `csr.getPublicKey()` against `certs[0].getPublicKey()`. This is why PKCS#7 ordering is critical — if the CA cert is `certs[0]`, the public keys won't match.

## PKCS#7 DER vs BER Encoding

**DER (Distinguished Encoding Rules)** requires `SET OF` elements to be sorted by their encoded byte values. Python's `cryptography` library strictly follows DER.

**BER (Basic Encoding Rules)** preserves insertion order for `SET OF` elements. Java's `CertificateFactory` accepts BER.

The PKCS#7 `SignedData` structure has a `certificates [0] IMPLICIT SET OF Certificate OPTIONAL` field. Under DER, certificates with shorter DER encoding sort first, which typically puts the CA cert (shorter subject DN) before the leaf cert.

### ASN.1 Structure Built by `build_ordered_pkcs7()`

```
ContentInfo {
  contentType: 1.2.840.113549.1.7.2 (signedData)
  content [0] {
    SignedData {
      version: 1
      digestAlgorithms: {} (empty SET)
      encapContentInfo {
        contentType: 1.2.840.113549.1.7.1 (data)
      }
      certificates [0] IMPLICIT {
        cert_der_list[0] (leaf cert)
        cert_der_list[1] (CA cert)
        ... (order preserved)
      }
      signerInfos: {} (empty SET)
    }
  }
}
```

## SDDC Manager certfnsh.asp POST Encoding

The POST body uses `application/x-www-form-urlencoded` via Apache HttpClient's `UrlEncodedFormEntity`. Critical: `+` in base64 CSR data is encoded as `%2B`.

**Correct parsing** (Python):
```python
from urllib.parse import parse_qs
params = parse_qs(body_str)
csr_pem = params['CertRequest'][0]
```

**Wrong parsing** (corrupts data):
```python
from urllib.parse import unquote_plus
# unquote_plus converts + to space, breaking base64
```

## certrqxt.asp Template Format

SDDC Manager's Java regex: `<Option Value="(.+)">.+?</Option>` (case-insensitive).
After extraction, splits the Value by `;` and takes `[1]` as template name.

**Required format**:
```html
<Option Value="1.3.6.1.4.1.311.21.8.X;VCFWebServer">VCF Web Server Template</Option>
```

## pyOpenSSL PKCS7 Migration

**pyOpenSSL v25+** removed `OpenSSL.crypto.PKCS7()` (bundled with `cryptography` v46+). The `serialize_certificates()` replacement reorders certs (DER sorting).

**Migration path**:
- `OpenSSL.crypto.PKCS7()` → removed, cannot use
- `cryptography.hazmat.primitives.serialization.pkcs7.serialize_certificates()` → reorders certs
- `build_ordered_pkcs7()` → custom builder, preserves order (the correct solution)

## Proxy File Locations

### On the manager VM (source)

```
/home/holuser/hol/Tools/CertsrvProxy/
├── certsrv_proxy-beta.py          # Standalone (TLS :443)
├── certsrv_proxy.py               # Traefik (:8900 HTTP)
├── install_certsrv_proxy-beta.sh  # Standalone installer
├── install_certsrv_proxy.sh       # Traefik installer
├── README.md                      # Full documentation
└── docker/
    ├── certsrv_proxy_docker.py    # Docker variant
    ├── Dockerfile
    ├── docker-compose.yml
    └── sample.env
```

### On the holorouter (deployed)

```
/root/certsrv-proxy/
├── certsrv_proxy-beta.py          # Active script
├── ca-vcf-lab.crt                 # TLS cert (Vault-issued)
├── ca-vcf-lab.key                 # TLS private key
└── creds.txt                      # Password (Vault token + Basic Auth)
```

## Vault PKI Endpoints Used by Proxy

| Operation | Method | Path | Body |
| --- | --- | --- | --- |
| Sign CSR (preserves DN) | POST | `/v1/pki/sign-verbatim/holodeck` | `{csr, common_name, ttl, ext_key_usage, key_usage}` |
| Issue cert + key | POST | `/v1/pki/issue/holodeck` | `{common_name, ttl}` |
| Get CA cert | GET | `/v1/pki/ca/pem` | — |
| List all serials | LIST | `/v1/pki/certs` | — |
| Read cert by serial | GET | `/v1/pki/cert/{serial}` | — |
| Revoke cert | POST | `/v1/pki/revoke` | `{serial_number}` |

## VCF Operations Certificate Management API Summary

### Authentication

```python
resp = requests.post(f"https://ops-a.site-a.vcf.lab/suite-api/api/auth/token/acquire",
    json={"username": "admin", "authSource": "local", "password": password},
    headers={"X-vRealizeOps-API-use-unsupported": "true"}, verify=False)
token = resp.json()["token"]
```

### Query certs → Generate CSR → Import signed cert → Replace

| Step | Method | Endpoint | Key Gotcha |
| --- | --- | --- | --- |
| Query | POST | `.../certificates/query` | Body required: `{"vcfComponent":"VCF_MANAGEMENT","vcfComponentType":"ARIA"}` |
| CSR | POST | `.../csrs` | `keySize` is enum: `KEY_2048`, not `"2048"` |
| List CSRs | GET | `.../csrs` | Response key: `certificateSignatureInfo`. CSR PEM uses spaces not newlines |
| Import | PUT | `.../repository/certificates/import` | Source: `"PASTE"`, cert = leaf + CA PEM concatenated |
| List repo | GET | `.../repository/certificates` | REQUIRES `?page=0&pageSize=100`. Key: `vcfRepositoryCertificates`, ID: `certId` |
| Replace | PUT | `.../certificates/replace` | Returns task. VRSLCM orchestrator depends on fleet-upgrade-service |
| Task status | GET | `.../tasks/{id}` | Returns HTTP 500 for most tasks — poll cert query instead |

### Fleet-Managed Certificate Keys

| Target FQDN | Component Type | Certificate Key |
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

## Known Issues Quick Reference

| # | Issue | Root Cause | Fix |
| --- | --- | --- | --- |
| 1 | Public key mismatch during install | PKCS#7 DER sorts CA before leaf | `build_ordered_pkcs7()` |
| 2 | "No certificate data found" | PKCS#7 response missing PEM headers | Wrap in BEGIN/END PKCS7 |
| 3 | Subject DN stripped by Vault | `pki/sign/` discards O/OU/C/L/ST | Use `pki/sign-verbatim/` |
| 4 | VCF Ops rejects CA (301) | Java client doesn't follow redirects | Serve 200 for `/certsrv` |
| 5 | NSX ReTrust failure post-install | Transient timing issue | Retry install; cosmetic |
| 6 | certsrv port 443 in use | Force-deleted pod left orphan process | Kill PID, restart pod |
| 7 | CSR "contains no data" at Vault | Malformed PEM (no newlines) | `normalize_csr_pem()` |
| 8 | VRSLCM replace NOT_STARTED | fleet-upgrade-service down | Use VCF Ops UI instead |
| 9 | Template validation fails | Option Value missing `OID;Name` format | Fix certrqxt.asp HTML |
| 10 | Python `.format()` crash on HTML | CSS braces conflict with format() | Use `.replace()` placeholder |
