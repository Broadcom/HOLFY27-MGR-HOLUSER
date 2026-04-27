#!/bin/bash
# Re-issue nginx TLS certificates on holorouter from Vault PKI (holodeck role).
# Run as root on the router when auth.vcf.lab / vault.vcf.lab / dns.vcf.lab certs
# expire (~30–397d TTL depending on role). Safe to re-run; reloads nginx.
#
# Startup/prelim.py queues this via the holorouter NFS share (renew_nginx_tls.request);
# Tools/doupdate.sh runs it from /mnt/manager when auth.vcf.lab is near expiry
# (see Tools/holorouter_nginx_tls_prelim.py).
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"

PASSWORD="$(tr -d '[:space:]' </root/creds.txt)"
VAULT_NS="vault-pki-lab"
CERT_DIR="/root/nginx-certs"
# nginx ssl_certificate paths (must match /etc/nginx/nginx.conf — NOT only ${CERT_DIR})
GITLAB_SSL_DIR="/holodeck-runtime/gitlab/ssl"
CERTSRV_SSL_DIR="/root/certsrv-proxy"
TTL="${CERT_TTL:-9528h}"

issue_cert() {
  local json="$1"
  local base="$2"
  python3 -c "
import json
with open('${json}') as f:
    data = json.load(f)
open('${CERT_DIR}/${base}.crt', 'w').write(
    data['data']['certificate'] + '\n' + data['data']['issuing_ca'] + '\n')
open('${CERT_DIR}/${base}.key', 'w').write(data['data']['private_key'] + '\n')
"
  rm -f "${json}"
}

mkdir -p "${CERT_DIR}" "${GITLAB_SSL_DIR}" "${CERTSRV_SSL_DIR}"

echo "Issuing authentik (auth.vcf.lab)..."
kubectl exec -n "${VAULT_NS}" vault-0 -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write -format=json pki/issue/holodeck \
   common_name=auth.vcf.lab alt_names=authentik.vcf.lab ip_sans=192.168.0.2 ttl=${TTL}" \
  >"${CERT_DIR}/_authentik.json"
issue_cert "${CERT_DIR}/_authentik.json" authentik

echo "Issuing technitium (dns.vcf.lab)..."
kubectl exec -n "${VAULT_NS}" vault-0 -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write -format=json pki/issue/holodeck \
   common_name=technitium.vcf.lab alt_names=dns.vcf.lab ip_sans=192.168.0.2,10.1.1.1 ttl=${TTL}" \
  >"${CERT_DIR}/_technitium.json"
issue_cert "${CERT_DIR}/_technitium.json" technitium

echo "Issuing vault (vault.vcf.lab)..."
kubectl exec -n "${VAULT_NS}" vault-0 -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write -format=json pki/issue/holodeck \
   common_name=vault.vcf.lab ip_sans=192.168.0.2,10.1.1.1 ttl=${TTL}" \
  >"${CERT_DIR}/_vault.json"
issue_cert "${CERT_DIR}/_vault.json" vault

echo "Issuing gitlab (gitlab.vcf.lab)..."
kubectl exec -n "${VAULT_NS}" vault-0 -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write -format=json pki/issue/holodeck \
   common_name=gitlab.vcf.lab ip_sans=192.168.0.2,10.1.1.1 ttl=${TTL}" \
  >"${CERT_DIR}/_gitlab.json"
issue_cert "${CERT_DIR}/_gitlab.json" gitlab
echo "  Installing gitlab TLS to ${GITLAB_SSL_DIR}/ (nginx ssl_certificate path)"
install -m 644 "${CERT_DIR}/gitlab.crt" "${GITLAB_SSL_DIR}/gitlab.crt"
install -m 600 "${CERT_DIR}/gitlab.key" "${GITLAB_SSL_DIR}/gitlab.key"

echo "Issuing gitlab-registry (gitlab-registry.vcf.lab)..."
kubectl exec -n "${VAULT_NS}" vault-0 -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write -format=json pki/issue/holodeck \
   common_name=gitlab-registry.vcf.lab ip_sans=192.168.0.2,10.1.1.1 ttl=${TTL}" \
  >"${CERT_DIR}/_gitlab-registry.json"
issue_cert "${CERT_DIR}/_gitlab-registry.json" gitlab-registry
echo "  Installing gitlab-registry TLS to ${GITLAB_SSL_DIR}/"
install -m 644 "${CERT_DIR}/gitlab-registry.crt" "${GITLAB_SSL_DIR}/gitlab-registry.crt"
install -m 600 "${CERT_DIR}/gitlab-registry.key" "${GITLAB_SSL_DIR}/gitlab-registry.key"

echo "Issuing ca (ca.vcf.lab)..."
kubectl exec -n "${VAULT_NS}" vault-0 -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write -format=json pki/issue/holodeck \
   common_name=ca.vcf.lab ip_sans=192.168.0.2,10.1.1.1 ttl=${TTL}" \
  >"${CERT_DIR}/_ca.json"
issue_cert "${CERT_DIR}/_ca.json" ca
echo "  Installing ca TLS to ${CERTSRV_SSL_DIR}/ (nginx ssl_certificate path)"
install -m 600 "${CERT_DIR}/ca.crt" "${CERTSRV_SSL_DIR}/ca.crt"
install -m 600 "${CERT_DIR}/ca.key" "${CERTSRV_SSL_DIR}/ca.key"

chmod 640 "${CERT_DIR}"/*.crt "${CERT_DIR}"/*.key 2>/dev/null || true
chown root:root "${CERT_DIR}"/*.crt "${CERT_DIR}"/*.key 2>/dev/null || true

nginx -t && nginx -s reload
echo "Done. Certificate end dates (authoritative paths nginx serves):"
for c in authentik technitium vault; do
  openssl x509 -in "${CERT_DIR}/${c}.crt" -noout -subject -enddate
done
for f in "${GITLAB_SSL_DIR}/gitlab.crt" "${GITLAB_SSL_DIR}/gitlab-registry.crt" "${CERTSRV_SSL_DIR}/ca.crt"; do
  openssl x509 -in "$f" -noout -subject -enddate
done
