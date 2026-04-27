#!/bin/bash
set -euo pipefail

###############################################################################
# update-holorouter.sh
#
# Consolidated Holorouter Customization Installer
#
# Deploys and configures the following services on the holorouter Kubernetes
# cluster (kubeadm v1.27, Photon OS 5.0):
#
#   - Traefik reverse proxy (ports 80/443 on 10.1.1.1)
#   - Authentik identity provider (SSO portal + OIDC)
#   - MSADCS Proxy (Microsoft CA Web Enrollment -> Vault PKI)
#   - TLS certificates for all services (issued by Vault PKI)
#   - DNS records for all service hostnames
#
# Run on: root@holorouter (root@router)
# Idempotent: safe to re-run
#
# Usage:
#   bash update-holorouter.sh
#
# Prerequisites:
#   - Technitium DNS running (DaemonSet, port 53/5380)
#   - HashiCorp Vault running (vault-pki-lab namespace, NodePort 32000)
#   - /root/creds.txt with lab password
#   - /root/vault-keys/init.json with unseal key
#   - certsrv_proxy.py in same directory as this script
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AUTHENTIK_VERSION="2026.2.1"
AUTHENTIK_IMAGE="ghcr.io/goauthentik/server:${AUTHENTIK_VERSION}"
POSTGRES_IMAGE="docker.io/library/postgres:17-alpine"
TRAEFIK_IMAGE="docker.io/library/traefik:v3.6.10"

MGMT_IP="10.1.1.1"
EXTERNAL_IP="10.1.10.129"
VAULT_NS="vault-pki-lab"
DNS_PORT="5380"
CERT_TTL="9528h"
CERT_DIR="/root/traefik-certs"
IMAGE_DIR="/root/containerd-images"
AUTHENTIK_NS="authentik"
TRAEFIK_NS="traefik"
CERTSRV_NS="default"
CERTSRV_DIR="/root/certsrv-proxy"
AUTHENTIK_DIR="/root/authentik"
ADMIN_EMAIL="authadmin@vcf.lab"

HOSTNAMES=("traefik" "dns" "vault" "auth" "ca")

PASSWORD=""
BOOTSTRAP_TOKEN=""

echo "============================================================"
echo "  Holorouter Consolidated Installer"
echo "  $(date)"
echo "============================================================"
echo ""

###############################################################################
# Phase 0: Pre-flight checks
###############################################################################
phase_preflight() {
    echo "=== Phase 0: Pre-flight Checks ==="

    if [ ! -f /root/creds.txt ]; then
        echo "  ERROR: /root/creds.txt not found"
        exit 1
    fi

    PASSWORD=$(cat /root/creds.txt | tr -d '[:space:]')
    if [ ${#PASSWORD} -le 1 ]; then
        echo "  ERROR: /root/creds.txt content length must be > 1"
        exit 1
    fi
    echo "  [OK] /root/creds.txt found (length=${#PASSWORD})"

    if [ ! -f /root/vault-keys/init.json ]; then
        echo "  ERROR: /root/vault-keys/init.json not found"
        echo "  Please ensure that /root/creds.txt exists and has the Lab password in it."
        exit 1
    fi
    echo "  [OK] Vault keys found"

    if ! kubectl get pods -n ${VAULT_NS} vault-0 -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Running; then
        echo "  ERROR: Vault pod not running in ${VAULT_NS}"
        exit 1
    fi
    echo "  [OK] Vault pod running"

    local sealed
    sealed=$(kubectl exec -n ${VAULT_NS} vault-0 -- sh -c \
        "VAULT_ADDR=http://127.0.0.1:8200 vault status -format=json 2>/dev/null" | \
        python3 -c 'import json,sys; print(json.load(sys.stdin).get("sealed","true"))' 2>/dev/null || echo "true")
    if [ "${sealed}" = "true" ] || [ "${sealed}" = "True" ]; then
        echo "  Vault is sealed, auto-unsealing..."
        local unseal_key
        unseal_key=$(python3 -c "import json; print(json.load(open('/root/vault-keys/init.json'))['unseal_key'])")
        kubectl exec -n ${VAULT_NS} vault-0 -- sh -c \
            "VAULT_ADDR=http://127.0.0.1:8200 vault operator unseal '${unseal_key}'" > /dev/null 2>&1
        sleep 3
        echo "  [OK] Vault unsealed"
    else
        echo "  [OK] Vault is unsealed"
    fi

    if ! kubectl get daemonset technitium -n default > /dev/null 2>&1; then
        echo "  ERROR: Technitium DNS DaemonSet not found"
        exit 1
    fi
    echo "  [OK] Technitium DNS running"

    if ! command -v helm &> /dev/null; then
        echo "  ERROR: helm not found"
        exit 1
    fi
    echo "  [OK] Helm available ($(helm version --short 2>/dev/null))"

    echo "  All pre-flight checks passed."
    echo ""
}

###############################################################################
# Phase 1: DNS records
###############################################################################
phase_dns() {
    echo "=== Phase 1: DNS Records ==="

    local ENCODED_PASS
    ENCODED_PASS=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${PASSWORD}'))")
    local DNS_API="http://localhost:${DNS_PORT}/api"

    local TOKEN
    TOKEN=$(curl -m 15 -sf "${DNS_API}/user/login?user=admin&pass=${ENCODED_PASS}&includeInfo=false" | \
        python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
    echo "  API token acquired"

    echo "  Creating vcf.lab zone..."
    curl -m 15 -sf "${DNS_API}/zones/create?token=${TOKEN}&zone=vcf.lab&type=Primary" > /dev/null 2>&1 || true

    for NAME in "${HOSTNAMES[@]}"; do
        echo "  Adding A record: ${NAME}.vcf.lab -> ${MGMT_IP}"
        curl -m 15 -sf "${DNS_API}/zones/records/add?token=${TOKEN}&domain=${NAME}.vcf.lab&zone=vcf.lab&type=A&ipAddress=${MGMT_IP}&ttl=3600&overwrite=true" > /dev/null 2>&1 || true
    done

    echo "  Verifying DNS resolution..."
    for NAME in "${HOSTNAMES[@]}"; do
        local RESOLVED
        RESOLVED=$(dig @127.0.0.1 "${NAME}.vcf.lab" +short 2>/dev/null || echo "FAILED")
        echo "    ${NAME}.vcf.lab -> ${RESOLVED}"
    done

    echo ""
}

###############################################################################
# Phase 2: Vault PKI role update
###############################################################################
phase_vault_pki() {
    echo "=== Phase 2: Vault PKI Role Update ==="

    kubectl exec -n ${VAULT_NS} vault-0 -- sh -c \
        "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' vault write pki/roles/holodeck \
            allowed_domains=vcf.lab \
            allow_subdomains=true \
            allow_bare_domains=true \
            max_ttl=34300800 \
            key_usage='DigitalSignature,KeyAgreement,KeyEncipherment' \
            ext_key_usage='ServerAuth,ClientAuth' \
            allow_ip_sans=true" > /dev/null 2>&1

    echo "  Vault PKI role 'holodeck' updated:"
    echo "    allowed_domains: vcf.lab"
    echo "    allow_subdomains: true"
    echo "    allow_bare_domains: true"
    echo "    max_ttl: 34300800s (397 days)"
    echo ""
}

###############################################################################
# Phase 3: TLS certificates
###############################################################################
phase_certs() {
    echo "=== Phase 3: TLS Certificates ==="

    mkdir -p "${CERT_DIR}"
    kubectl create namespace "${TRAEFIK_NS}" --dry-run=client -o yaml | kubectl apply -f - > /dev/null 2>&1

    for SVC in "${HOSTNAMES[@]}"; do
        local CN="${SVC}.vcf.lab"
        echo "  Issuing certificate for ${CN}..."

        kubectl exec -n ${VAULT_NS} vault-0 -- sh -c \
            "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${PASSWORD}' \
             vault write -format=json pki/issue/holodeck \
             common_name=${CN} ip_sans=${MGMT_IP} ttl=${CERT_TTL}" \
            > "${CERT_DIR}/${SVC}.json"

        python3 -c "
import json
with open('${CERT_DIR}/${SVC}.json') as f:
    data = json.load(f)
with open('${CERT_DIR}/${SVC}.crt', 'w') as f:
    f.write(data['data']['certificate'] + '\n')
    f.write(data['data']['issuing_ca'] + '\n')
with open('${CERT_DIR}/${SVC}.key', 'w') as f:
    f.write(data['data']['private_key'] + '\n')
"

        kubectl create secret tls "${SVC}-vcf-lab-tls" \
            --cert="${CERT_DIR}/${SVC}.crt" \
            --key="${CERT_DIR}/${SVC}.key" \
            -n "${TRAEFIK_NS}" \
            --dry-run=client -o yaml | kubectl apply -f - > /dev/null 2>&1

        local EXPIRY
        EXPIRY=$(openssl x509 -in "${CERT_DIR}/${SVC}.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
        echo "    ${CN}: expires ${EXPIRY}"
    done

    python3 -c "
import json
with open('${CERT_DIR}/traefik.json') as f:
    data = json.load(f)
with open('${CERT_DIR}/ca.crt', 'w') as f:
    f.write(data['data']['issuing_ca'] + '\n')
"
    echo "  CA certificate saved to ${CERT_DIR}/ca.crt"
    echo ""
}

###############################################################################
# Phase 4: Container images
###############################################################################
phase_images() {
    echo "=== Phase 4: Container Images ==="

    mkdir -p "${IMAGE_DIR}"

    declare -A IMAGES=(
        ["authentik-server"]="${AUTHENTIK_IMAGE}"
        ["postgres17"]="${POSTGRES_IMAGE}"
        ["traefik"]="${TRAEFIK_IMAGE}"
    )

    for name in "${!IMAGES[@]}"; do
        local image="${IMAGES[$name]}"
        local tarball="${IMAGE_DIR}/${name}.tar"

        if ctr -n k8s.io images check "name==${image}" 2>/dev/null | grep -q "${image}"; then
            echo "  [SKIP] ${image} already loaded"
            continue
        fi

        if [ -f "${tarball}" ]; then
            echo "  [LOAD] Importing ${tarball}..."
            ctr -n k8s.io images import "${tarball}"
        else
            echo "  [PULL] Pulling ${image}..."
            ctr -n k8s.io images pull "${image}"
            echo "  [SAVE] Saving to ${tarball}..."
            ctr -n k8s.io images export "${tarball}" "${image}"
        fi
    done

    echo "  Building certsrv-proxy image with pre-installed dependencies..."
    phase_build_certsrv_image

    echo ""
}

###############################################################################
# Phase 4b: Build certsrv-proxy image
###############################################################################
phase_build_certsrv_image() {
    echo "=== Phase 4b: Building certsrv-proxy Image ==="
    mkdir -p "${CERTSRV_DIR}"

    if [ -f "${SCRIPT_DIR}/certsrv_proxy.py" ]; then
        cp "${SCRIPT_DIR}/certsrv_proxy.py" "${CERTSRV_DIR}/certsrv_proxy.py"
    elif [ -f "${CERTSRV_DIR}/certsrv_proxy.py" ]; then
        echo "  [SKIP] certsrv_proxy.py already in ${CERTSRV_DIR}"
    else
        echo "  ERROR: certsrv_proxy.py not found"
        return 1
    fi
    test -f "${CERTSRV_DIR}/creds.txt" || cp /root/creds.txt "${CERTSRV_DIR}/creds.txt" 2>/dev/null || true

    if [ -f "${SCRIPT_DIR}/Dockerfile.certsrv-proxy" ]; then
        cp "${SCRIPT_DIR}/Dockerfile.certsrv-proxy" "${CERTSRV_DIR}/Dockerfile"
    else
        cat <<'EOF' > "${CERTSRV_DIR}/Dockerfile"
FROM python:3.11-slim
RUN pip install --no-cache-dir cryptography requests urllib3
COPY certsrv_proxy.py /app/certsrv_proxy.py
WORKDIR /app
ENTRYPOINT ["python3", "/app/certsrv_proxy.py"]
EOF
    fi

    echo "  [BUILD] Building certsrv-proxy:latest..."
    if command -v docker >/dev/null 2>&1; then
        if ! systemctl is-active --quiet docker; then
            echo "  [SERVICE] Starting docker daemon..."
            systemctl start docker
        fi

        docker build -t certsrv-proxy:latest -f "${CERTSRV_DIR}/Dockerfile" "${CERTSRV_DIR}"
        echo "  [SAVE] Saving to ${IMAGE_DIR}/certsrv-proxy.tar..."
        docker save certsrv-proxy:latest -o "${IMAGE_DIR}/certsrv-proxy.tar"

        echo "  [SERVICE] Stopping docker daemon..."
        systemctl stop docker

        ctr -n k8s.io images import "${IMAGE_DIR}/certsrv-proxy.tar" >/dev/null
    elif command -v nerdctl >/dev/null 2>&1; then
        nerdctl -n k8s.io build -t certsrv-proxy:latest -f "${CERTSRV_DIR}/Dockerfile" "${CERTSRV_DIR}"
        echo "  [SAVE] Saving to ${IMAGE_DIR}/certsrv-proxy.tar..."
        nerdctl -n k8s.io save certsrv-proxy:latest -o "${IMAGE_DIR}/certsrv-proxy.tar"
    elif command -v buildah >/dev/null 2>&1; then
        buildah bud -t certsrv-proxy:latest -f "${CERTSRV_DIR}/Dockerfile" "${CERTSRV_DIR}"
        echo "  [SAVE] Saving to ${IMAGE_DIR}/certsrv-proxy.tar..."
        buildah push certsrv-proxy:latest "docker-archive:${IMAGE_DIR}/certsrv-proxy.tar"
        ctr -n k8s.io images import "${IMAGE_DIR}/certsrv-proxy.tar" >/dev/null
    else
        echo "  [ERROR] No container build tool (docker/nerdctl/buildah) found."
        return 1
    fi
}

###############################################################################
# Phase 5: Traefik deployment
###############################################################################
phase_traefik() {
    echo "=== Phase 5: Traefik Reverse Proxy ==="

    kubectl create namespace "${TRAEFIK_NS}" --dry-run=client -o yaml | kubectl apply -f - > /dev/null 2>&1

    helm repo add traefik https://traefik.github.io/charts 2>/dev/null || true
    helm repo update traefik > /dev/null 2>&1

    local VALUES_FILE="/root/traefik-values.yaml"
    cat > "${VALUES_FILE}" << 'VALEOF'
deployment:
  replicas: 1
  dnsPolicy: ClusterFirstWithHostNet

hostNetwork: true

securityContext:
  allowPrivilegeEscalation: true
  capabilities:
    drop: [ALL]
    add: [NET_BIND_SERVICE]
  readOnlyRootFilesystem: true

podSecurityContext:
  runAsGroup: 0
  runAsNonRoot: false
  runAsUser: 0

ports:
  web:
    port: 80
    hostPort: 80
    exposedPort: 80
    protocol: TCP
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
  websecure:
    port: 443
    hostPort: 443
    exposedPort: 443
    protocol: TCP
    http:
      tls:
        enabled: true

service:
  enabled: false

ingressRoute:
  dashboard:
    enabled: false

providers:
  kubernetesCRD:
    enabled: true
    allowCrossNamespace: true
    namespaces: []
  kubernetesIngress:
    enabled: true
    namespaces: []

additionalArguments:
  - "--api.dashboard=true"
  - "--api.insecure=false"
  - "--log.level=INFO"

resources:
  requests:
    cpu: "50m"
    memory: "64Mi"
  limits:
    cpu: "200m"
    memory: "128Mi"

tolerations:
  - key: "node-role.kubernetes.io/control-plane"
    operator: "Exists"
    effect: "NoSchedule"

nodeSelector:
  kubernetes.io/hostname: holorouter
VALEOF

    echo "  Installing/upgrading Traefik Helm release..."
    helm upgrade --install traefik traefik/traefik -n "${TRAEFIK_NS}" -f "${VALUES_FILE}" > /dev/null 2>&1

    echo "  Waiting for Traefik pod..."
    kubectl wait --for=condition=Available deployment/traefik -n "${TRAEFIK_NS}" --timeout=120s > /dev/null 2>&1
    echo "  [OK] Traefik running on ports 80/443"
    echo ""
}

###############################################################################
# Phase 6: MSADCS Proxy (certsrv) deployment
###############################################################################
phase_certsrv() {
    echo "=== Phase 6: MSADCS Proxy (certsrv) ==="

    mkdir -p "${CERTSRV_DIR}"

    if [ -f "${SCRIPT_DIR}/certsrv_proxy.py" ] && [ ! -f "${CERTSRV_DIR}/certsrv_proxy.py" ]; then
        cp "${SCRIPT_DIR}/certsrv_proxy.py" "${CERTSRV_DIR}/certsrv_proxy.py"
    fi
    test -f "${CERTSRV_DIR}/creds.txt" || cp /root/creds.txt "${CERTSRV_DIR}/creds.txt" 2>/dev/null || true

    echo "  Deploying certsrv-proxy DaemonSet..."
    kubectl delete daemonset certsrv-proxy -n ${CERTSRV_NS} 2>/dev/null || true
    sleep 2

    cat <<'DSEOF' | kubectl apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: certsrv-proxy
  namespace: default
  labels:
    app: certsrv-proxy
spec:
  selector:
    matchLabels:
      app: certsrv-proxy
  template:
    metadata:
      labels:
        app: certsrv-proxy
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
      - name: certsrv-proxy
        image: certsrv-proxy:latest
        imagePullPolicy: IfNotPresent
        command:
        - python3
        - /app/certsrv_proxy.py
        - --port
        - "8900"
        - --vault-url
        - http://127.0.0.1:32000
        - --creds-file
        - /app/creds.txt
        ports:
        - containerPort: 8900
          hostPort: 8900
          protocol: TCP
        volumeMounts:
        - name: creds-file
          mountPath: /app/creds.txt
          readOnly: true
        - name: script-file
          mountPath: /app/certsrv_proxy.py
          readOnly: true
      volumes:
      - name: creds-file
        hostPath:
          path: /root/creds.txt
          type: File
      - name: script-file
        hostPath:
          path: /root/certsrv-proxy/certsrv_proxy.py
          type: File
DSEOF

    echo "  Creating certsrv-proxy Service..."
    cat <<'SVCEOF' | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: certsrv-proxy
  namespace: default
spec:
  selector:
    app: certsrv-proxy
  ports:
  - port: 8900
    targetPort: 8900
    protocol: TCP
  type: ClusterIP
SVCEOF

    echo "  Creating IngressRoute for ca.vcf.lab (no forward-auth)..."
    cat <<'IREOF' | kubectl apply -f -
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: certsrv-proxy
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`ca.vcf.lab`)
      kind: Rule
      services:
        - name: certsrv-proxy
          namespace: default
          port: 8900
  tls:
    secretName: ca-vcf-lab-tls
IREOF

    echo "  Waiting for certsrv-proxy pod..."
    local WAITED=0
    while [ ${WAITED} -lt 120 ]; do
        local POD_STATUS
        POD_STATUS=$(kubectl get pods -l app=certsrv-proxy -n ${CERTSRV_NS} -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Pending")
        if [ "${POD_STATUS}" = "Running" ]; then
            echo "  [OK] certsrv-proxy pod running"
            break
        fi
        sleep 5
        WAITED=$((WAITED + 5))
        [ $((WAITED % 15)) -eq 0 ] && echo "    Waiting... (${WAITED}s, status: ${POD_STATUS})"
    done
    echo ""
}

###############################################################################
# Phase 7: Technitium DNS IngressRoute (direct access, no forward-auth)
###############################################################################
phase_dns_ingressroute() {
    echo "=== Phase 7: Technitium DNS IngressRoute ==="

    cat <<'IREOF' | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: technitium-web
  namespace: traefik
spec:
  ports:
    - port: 5380
      targetPort: 5380
      protocol: TCP
---
apiVersion: v1
kind: Endpoints
metadata:
  name: technitium-web
  namespace: traefik
subsets:
  - addresses:
      - ip: 192.168.0.2
    ports:
      - port: 5380
        protocol: TCP
---
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: technitium-dns
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`dns.vcf.lab`)
      kind: Rule
      services:
        - name: technitium-web
          port: 5380
  tls:
    secretName: dns-vcf-lab-tls
IREOF

    echo "  [OK] dns.vcf.lab IngressRoute created (direct access, no forward-auth)"
    echo ""
}

###############################################################################
# Phase 8: Vault IngressRoute (direct access, no forward-auth)
###############################################################################
phase_vault_ingressroute() {
    echo "=== Phase 8: Vault IngressRoute ==="

    cat <<'IREOF' | kubectl apply -f -
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: vault-ui
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`vault.vcf.lab`)
      kind: Rule
      services:
        - name: vault-ui
          namespace: vault-pki-lab
          port: 8200
  tls:
    secretName: vault-vcf-lab-tls
IREOF

    echo "  [OK] vault.vcf.lab IngressRoute created (direct access, no forward-auth)"
    echo ""
}

###############################################################################
# Phase 9: Authentik deployment
###############################################################################
phase_authentik_deploy() {
    echo "=== Phase 9: Authentik Deployment ==="

    local SECRET_KEY

    if [ -f "${AUTHENTIK_DIR}/bootstrap-token.txt" ]; then
        BOOTSTRAP_TOKEN=$(head -1 "${AUTHENTIK_DIR}/bootstrap-token.txt" | cut -d: -f2-)
        SECRET_KEY=$(kubectl get secret authentik-secret -n "${AUTHENTIK_NS}" -o jsonpath='{.data.AUTHENTIK_SECRET_KEY}' 2>/dev/null | base64 -d || openssl rand -base64 60 | tr -d '\n')
        echo "  Using existing bootstrap token"
    else
        SECRET_KEY=$(openssl rand -base64 60 | tr -d '\n')
        BOOTSTRAP_TOKEN=$(openssl rand -base64 32 | tr -d '\n' | tr -d '/' | head -c 40)
        echo "  Generated new secret key and bootstrap token"
    fi

    mkdir -p "${AUTHENTIK_DIR}" /opt/authentik-data/postgres /opt/authentik-data/files
    chown 1000:1000 /opt/authentik-data/files

    kubectl create namespace "${AUTHENTIK_NS}" --dry-run=client -o yaml | kubectl apply -f - > /dev/null 2>&1

    echo "  Applying storage..."
    cat <<'SCEOF' | kubectl apply -f - 2>/dev/null || true
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-storage
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
SCEOF

    cat <<'STEOF' | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: authentik-postgres-pv
spec:
  capacity:
    storage: 8Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-storage
  local:
    path: /opt/authentik-data/postgres
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - holorouter
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: authentik-postgres-pvc
  namespace: authentik
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-storage
  resources:
    requests:
      storage: 8Gi
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: authentik-data-pv
spec:
  capacity:
    storage: 1Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-storage
  local:
    path: /opt/authentik-data/files
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - holorouter
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: authentik-data-pvc
  namespace: authentik
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-storage
  resources:
    requests:
      storage: 1Gi
STEOF

    echo "  Applying secret..."
    cat <<SECEOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: authentik-secret
  namespace: ${AUTHENTIK_NS}
type: Opaque
stringData:
  AUTHENTIK_SECRET_KEY: "${SECRET_KEY}"
  AUTHENTIK_BOOTSTRAP_PASSWORD: "${PASSWORD}"
  AUTHENTIK_BOOTSTRAP_TOKEN: "${BOOTSTRAP_TOKEN}"
  AUTHENTIK_BOOTSTRAP_EMAIL: "${ADMIN_EMAIL}"
  POSTGRES_PASSWORD: "${PASSWORD}"
  AUTHENTIK_POSTGRESQL__PASSWORD: "${PASSWORD}"
SECEOF

    echo "TOKEN:${BOOTSTRAP_TOKEN}" > "${AUTHENTIK_DIR}/bootstrap-token.txt"

    echo "  Deploying PostgreSQL..."
    cat <<'PGEOF' | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: authentik-postgresql
  namespace: authentik
  labels:
    app: authentik-postgresql
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: authentik-postgresql
  template:
    metadata:
      labels:
        app: authentik-postgresql
    spec:
      containers:
        - name: postgresql
          image: docker.io/library/postgres:17-alpine
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 5432
          env:
            - name: POSTGRES_DB
              value: "authentik"
            - name: POSTGRES_USER
              value: "authentik"
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: POSTGRES_PASSWORD
          volumeMounts:
            - name: postgres-data
              mountPath: /var/lib/postgresql/data
              subPath: pgdata
          resources:
            requests:
              memory: "256Mi"
              cpu: "100m"
            limits:
              memory: "512Mi"
              cpu: "500m"
      volumes:
        - name: postgres-data
          persistentVolumeClaim:
            claimName: authentik-postgres-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: authentik-postgresql
  namespace: authentik
spec:
  selector:
    app: authentik-postgresql
  ports:
    - port: 5432
      targetPort: 5432
PGEOF

    echo "  Waiting for PostgreSQL..."
    kubectl wait --for=condition=Available deployment/authentik-postgresql \
        -n "${AUTHENTIK_NS}" --timeout=120s > /dev/null 2>&1

    echo "  Deploying Authentik Server..."
    cat <<'SRVEOF' | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: authentik-server
  namespace: authentik
  labels:
    app: authentik-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: authentik-server
  template:
    metadata:
      labels:
        app: authentik-server
    spec:
      securityContext:
        fsGroup: 1000
      containers:
        - name: server
          image: ghcr.io/goauthentik/server:2025.12.4
          imagePullPolicy: IfNotPresent
          args: ["server"]
          ports:
            - containerPort: 9000
              name: http
            - containerPort: 9443
              name: https
          env:
            - name: AUTHENTIK_POSTGRESQL__HOST
              value: "authentik-postgresql"
            - name: AUTHENTIK_POSTGRESQL__USER
              value: "authentik"
            - name: AUTHENTIK_POSTGRESQL__NAME
              value: "authentik"
            - name: AUTHENTIK_POSTGRESQL__PASSWORD
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_POSTGRESQL__PASSWORD
            - name: AUTHENTIK_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_SECRET_KEY
            - name: AUTHENTIK_BOOTSTRAP_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_BOOTSTRAP_PASSWORD
            - name: AUTHENTIK_BOOTSTRAP_TOKEN
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_BOOTSTRAP_TOKEN
            - name: AUTHENTIK_BOOTSTRAP_EMAIL
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_BOOTSTRAP_EMAIL
            - name: AUTHENTIK_ERROR_REPORTING__ENABLED
              value: "false"
            - name: AUTHENTIK_WEB__WORKERS
              value: "1"
          volumeMounts:
            - name: authentik-data
              mountPath: /data
          readinessProbe:
            httpGet:
              path: /-/health/ready/
              port: 9000
            initialDelaySeconds: 30
            periodSeconds: 30
            failureThreshold: 10
          livenessProbe:
            httpGet:
              path: /-/health/live/
              port: 9000
            initialDelaySeconds: 60
            periodSeconds: 30
            failureThreshold: 5
          resources:
            requests:
              memory: "256Mi"
              cpu: "100m"
            limits:
              memory: "768Mi"
              cpu: "500m"
      volumes:
        - name: authentik-data
          persistentVolumeClaim:
            claimName: authentik-data-pvc
SRVEOF

    echo "  Deploying Authentik Worker..."
    cat <<'WRKEOF' | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: authentik-worker
  namespace: authentik
  labels:
    app: authentik-worker
spec:
  replicas: 1
  selector:
    matchLabels:
      app: authentik-worker
  template:
    metadata:
      labels:
        app: authentik-worker
    spec:
      securityContext:
        fsGroup: 1000
      containers:
        - name: worker
          image: ghcr.io/goauthentik/server:2025.12.4
          imagePullPolicy: IfNotPresent
          args: ["worker"]
          env:
            - name: AUTHENTIK_POSTGRESQL__HOST
              value: "authentik-postgresql"
            - name: AUTHENTIK_POSTGRESQL__USER
              value: "authentik"
            - name: AUTHENTIK_POSTGRESQL__NAME
              value: "authentik"
            - name: AUTHENTIK_POSTGRESQL__PASSWORD
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_POSTGRESQL__PASSWORD
            - name: AUTHENTIK_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_SECRET_KEY
            - name: AUTHENTIK_BOOTSTRAP_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_BOOTSTRAP_PASSWORD
            - name: AUTHENTIK_BOOTSTRAP_TOKEN
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_BOOTSTRAP_TOKEN
            - name: AUTHENTIK_BOOTSTRAP_EMAIL
              valueFrom:
                secretKeyRef:
                  name: authentik-secret
                  key: AUTHENTIK_BOOTSTRAP_EMAIL
            - name: AUTHENTIK_ERROR_REPORTING__ENABLED
              value: "false"
            - name: AUTHENTIK_WORKER__CONCURRENCY
              value: "1"
          volumeMounts:
            - name: authentik-data
              mountPath: /data
          resources:
            requests:
              memory: "256Mi"
              cpu: "50m"
            limits:
              memory: "512Mi"
              cpu: "200m"
      volumes:
        - name: authentik-data
          persistentVolumeClaim:
            claimName: authentik-data-pvc
WRKEOF

    echo "  Creating Authentik Service..."
    cat <<SVCEOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: authentik-server
  namespace: ${AUTHENTIK_NS}
spec:
  type: ClusterIP
  selector:
    app: authentik-server
  externalIPs:
    - ${EXTERNAL_IP}
  ports:
    - name: http
      port: 9000
      targetPort: 9000
    - name: https
      port: 9443
      targetPort: 9443
SVCEOF

    echo "  Creating Authentik IngressRoute..."
    cat <<'IREOF' | kubectl apply -f -
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: authentik
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`auth.vcf.lab`)
      kind: Rule
      services:
        - name: authentik-server
          namespace: authentik
          port: 9000
  tls:
    secretName: auth-vcf-lab-tls
IREOF

    echo "  Waiting for Authentik Server..."
    kubectl wait --for=condition=Available deployment/authentik-server \
        -n "${AUTHENTIK_NS}" --timeout=300s > /dev/null 2>&1
    echo "  [OK] Authentik Server ready"

    echo "  Waiting for Authentik Worker..."
    kubectl wait --for=condition=Available deployment/authentik-worker \
        -n "${AUTHENTIK_NS}" --timeout=300s > /dev/null 2>&1
    echo "  [OK] Authentik Worker ready"

    echo ""
}

###############################################################################
# Phase 10: Authentik configuration (users, groups, apps, forward-auth)
###############################################################################
phase_authentik_configure() {
    echo "=== Phase 10: Authentik Configuration ==="

    local API_BASE="http://${EXTERNAL_IP}:9000/api/v3"
    local AUTH_HEADER="Authorization: Bearer ${BOOTSTRAP_TOKEN}"
    local CT="Content-Type: application/json"
    local MAX_RETRIES=30

    echo "  Waiting for Authentik API..."
    for i in $(seq 1 ${MAX_RETRIES}); do
        if curl -m 15 -sf -H "${AUTH_HEADER}" "${API_BASE}/core/users/" > /dev/null 2>&1; then
            echo "  API ready (attempt ${i})"
            break
        fi
        if [ "$i" -eq "${MAX_RETRIES}" ]; then
            echo "  ERROR: API not ready after ${MAX_RETRIES} attempts"
            exit 1
        fi
        sleep 10
    done

    # --- Helper functions ---
    # All helpers use `|| true` to prevent pipefail from killing the script
    # when API calls fail (e.g., Authentik still starting after restart).
    api_get() { curl -m 15 -sf -H "${AUTH_HEADER}" "$1" 2>/dev/null || true; }
    api_post() { curl -m 15 -sf -X POST "$1" -H "${AUTH_HEADER}" -H "${CT}" -d "$2" 2>/dev/null || true; }
    api_patch() { curl -m 15 -sf -X PATCH "$1" -H "${AUTH_HEADER}" -H "${CT}" -d "$2" 2>/dev/null || true; }

    get_pk() { echo "$1" | python3 -c "import json,sys; print(json.load(sys.stdin).get('pk',''))" 2>/dev/null || true; }
    get_results_pk() { echo "$1" | python3 -c "import json,sys; r=json.load(sys.stdin).get('results',[]); print(r[0]['pk'] if r else '')" 2>/dev/null || true; }

    create_user() {
        local username="$1" email="$2" name="$3" is_superuser="${4:-false}"
        echo "  Creating user: ${username}..." >&2
        local resp
        resp=$(api_post "${API_BASE}/core/users/" \
            "{\"username\":\"${username}\",\"email\":\"${email}\",\"name\":\"${name}\",\"is_active\":true,\"attributes\":{\"goauthentik.io/user/password-change-date\":\"2099-12-31T00:00:00Z\"}}")
        local pk
        pk=$(get_pk "${resp}")
        if [ -z "${pk}" ]; then
            pk=$(get_results_pk "$(api_get "${API_BASE}/core/users/?username=${username}")")
        fi
        if [ -n "${pk}" ]; then
            api_post "${API_BASE}/core/users/${pk}/set_password/" "{\"password\":\"${PASSWORD}\"}" > /dev/null 2>&1
            if [ "${is_superuser}" = "true" ]; then
                api_patch "${API_BASE}/core/users/${pk}/" "{\"is_superuser\":true}" > /dev/null 2>&1
            fi
            echo "    ${username} (pk=${pk}, superuser=${is_superuser})" >&2
        fi
        echo "${pk}"
    }

    create_group() {
        local name="$1" is_superuser="${2:-false}"
        echo "  Creating group: ${name}..." >&2
        local resp
        resp=$(api_post "${API_BASE}/core/groups/" "{\"name\":\"${name}\",\"is_superuser\":${is_superuser}}")
        local pk
        pk=$(get_pk "${resp}")
        if [ -z "${pk}" ]; then
            pk=$(get_results_pk "$(api_get "${API_BASE}/core/groups/?name=${name}")")
        fi
        echo "    ${name} (pk=${pk})" >&2
        echo "${pk}"
    }

    add_to_group() {
        local group_pk="$1" user_pk="$2"
        api_post "${API_BASE}/core/groups/${group_pk}/add_user/" "{\"pk\":${user_pk}}" > /dev/null 2>&1
    }

    # --- Update akadmin ---
    echo ""
    echo "  --- Updating akadmin ---"
    local akadmin_pk
    akadmin_pk=$(get_results_pk "$(api_get "${API_BASE}/core/users/?username=akadmin")")
    if [ -n "${akadmin_pk}" ]; then
        api_patch "${API_BASE}/core/users/${akadmin_pk}/" \
            "{\"email\":\"${ADMIN_EMAIL}\",\"name\":\"Authentik Admin\",\"attributes\":{\"goauthentik.io/user/password-change-date\":\"2099-12-31T00:00:00Z\"}}" > /dev/null 2>&1
        echo "    akadmin updated (pk=${akadmin_pk})"
    fi

    # --- Create users ---
    echo ""
    echo "  --- Creating Users ---"
    local holadmin_pk holuser_pk provider_admin_pk tenant_admin_pk tenant_user_pk

    holadmin_pk=$(create_user "holadmin" "holadmin@vcf.lab" "HOL Administrator" "true")
    holuser_pk=$(create_user "holuser" "holuser@vcf.lab" "HOL User" "false")
    provider_admin_pk=$(create_user "provider-admin" "provider-admin@vcf.lab" "Provider Admin" "false")
    tenant_admin_pk=$(create_user "tenant-admin" "tenant-admin@vcf.lab" "Tenant Admin" "false")
    tenant_user_pk=$(create_user "tenant-user" "tenant-user@vcf.lab" "Tenant User" "false")

    # --- Create groups ---
    echo ""
    echo "  --- Creating Groups ---"
    local provider_admins_pk tenant_admins_pk tenant_users_pk app_users_pk

    provider_admins_pk=$(create_group "provider-admins")
    tenant_admins_pk=$(create_group "tenant-admins")
    tenant_users_pk=$(create_group "tenant-users")
    app_users_pk=$(create_group "app-users")

    # --- Add users to groups ---
    echo ""
    echo "  --- Group Memberships ---"
    [ -n "${provider_admin_pk}" ] && [ -n "${provider_admins_pk}" ] && add_to_group "${provider_admins_pk}" "${provider_admin_pk}"
    [ -n "${tenant_admin_pk}" ] && [ -n "${tenant_admins_pk}" ] && add_to_group "${tenant_admins_pk}" "${tenant_admin_pk}"
    [ -n "${tenant_user_pk}" ] && [ -n "${tenant_users_pk}" ] && add_to_group "${tenant_users_pk}" "${tenant_user_pk}"
    [ -n "${holuser_pk}" ] && [ -n "${tenant_users_pk}" ] && add_to_group "${tenant_users_pk}" "${holuser_pk}"
    [ -n "${tenant_user_pk}" ] && [ -n "${app_users_pk}" ] && add_to_group "${app_users_pk}" "${tenant_user_pk}"
    [ -n "${holuser_pk}" ] && [ -n "${app_users_pk}" ] && add_to_group "${app_users_pk}" "${holuser_pk}"
    echo "  Group memberships configured"

    # --- Remove password expiry ---
    echo ""
    echo "  --- Removing password expiry policies ---"
    local expiry_policies
    expiry_policies=$(api_get "${API_BASE}/policies/password_expiry/")
    if echo "${expiry_policies}" | grep -q '"pk"'; then
        echo "${expiry_policies}" | grep -o '"pk":"[^"]*"' | cut -d'"' -f4 | while read -r ppk; do
            curl -m 15 -sf -X DELETE "${API_BASE}/policies/password_expiry/${ppk}/" -H "${AUTH_HEADER}" > /dev/null 2>&1 || true
        done
        echo "  Password expiry policies removed"
    else
        echo "  No password expiry policies to remove"
    fi

    # --- Look up flows ---
    echo ""
    echo "  --- Looking up flows ---"
    local implicit_flow explicit_flow auth_flow invalidation_flow default_invalidation_flow

    implicit_flow=$(get_results_pk "$(api_get "${API_BASE}/flows/instances/?slug=default-provider-authorization-implicit-consent")")
    explicit_flow=$(get_results_pk "$(api_get "${API_BASE}/flows/instances/?slug=default-provider-authorization-explicit-consent")")
    auth_flow=$(get_results_pk "$(api_get "${API_BASE}/flows/instances/?slug=default-authentication-flow")")
    invalidation_flow=$(get_results_pk "$(api_get "${API_BASE}/flows/instances/?slug=default-provider-invalidation-flow")")
    default_invalidation_flow=$(get_results_pk "$(api_get "${API_BASE}/flows/instances/?slug=default-invalidation-flow")")

    echo "    implicit-consent: ${implicit_flow}"
    echo "    explicit-consent: ${explicit_flow}"
    echo "    auth-flow:        ${auth_flow}"
    echo "    invalidation:     ${invalidation_flow:-${default_invalidation_flow}}"

    # --- Scope mappings ---
    local scope_pks
    scope_pks=$(api_get "${API_BASE}/propertymappings/provider/scope/" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
pks = [r['pk'] for r in data.get('results', []) if r.get('managed','') in [
    'goauthentik.io/providers/proxy/scope-proxy',
    'goauthentik.io/providers/oauth2/scope-email',
    'goauthentik.io/providers/oauth2/scope-openid',
    'goauthentik.io/providers/oauth2/scope-profile',
    'goauthentik.io/providers/oauth2/scope-entitlements'
]]
print(json.dumps(pks))
" 2>/dev/null || echo "[]")

    # --- Upload icon files and set application icons (local storage) ---
    echo ""
    echo "  --- Uploading icon files to Authentik (Customization -> Files) ---"
    local ICON_DIR="/root/update-holorouter/images"
    local CDN_BASE="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons"

    local upload_failed=0 upload_failed_names=""

    upload_icon_file() {
        local filename="$1"
        local filepath="${ICON_DIR}/${filename}"
        [ -f "${filepath}" ] || return 0
        local mime="image/svg+xml"
        case "${filename}" in *.png) mime="image/png" ;; esac
        if ! curl -m 15 -sf -o /dev/null -X POST "${API_BASE}/admin/file/" \
            -H "${AUTH_HEADER}" \
            -F "file=@${filepath};type=${mime}" \
            -F "name=${filename}" 2>/dev/null; then
            upload_failed=$((upload_failed + 1))
            upload_failed_names="${upload_failed_names} ${filename}"
        fi
    }

    if [ -d "${ICON_DIR}" ] && [ "$(ls -A ${ICON_DIR})" ]; then
        local icon_count=0
        for f in "${ICON_DIR}"/*; do
            [ -f "${f}" ] || continue
            upload_icon_file "$(basename "${f}")"
            icon_count=$((icon_count + 1))
        done
        echo "  Uploaded ${icon_count} icon files to Authentik"
        if [ "${upload_failed}" -gt 0 ]; then
            echo "  WARN: ${upload_failed} icon uploads failed:${upload_failed_names}"
        fi
    else
        echo "  WARN: ${ICON_DIR} not found or empty, downloading core icons from CDN..."
        mkdir -p "${ICON_DIR}"
        local CDN_SVG="${CDN_BASE}/svg"
        local CDN_PNG="${CDN_BASE}/png"
        for icon in traefik-proxy vault-light technitium; do
            local ext="svg" url="${CDN_SVG}/${icon}.svg"
            if [ "${icon}" = "technitium" ]; then
                ext="png"
                url="${CDN_PNG}/${icon}.png"
            fi
            if [ ! -f "${ICON_DIR}/${icon}.${ext}" ]; then
                curl -m 10 -sfL -o "${ICON_DIR}/${icon}.${ext}" "${url}" 2>/dev/null || true
            fi
            upload_icon_file "${icon}.${ext}" 2>/dev/null || true
        done
        echo "  Downloaded and uploaded core icons from CDN"
    fi

    set_app_icon() {
        local slug="$1" filename="$2"
        api_patch "${API_BASE}/core/applications/${slug}/" \
            "{\"meta_icon\":\"${filename}\"}" > /dev/null 2>&1
    }

    # --- Create Traefik Dashboard proxy provider + app (WITH forward-auth) ---
    echo ""
    echo "  --- Traefik Dashboard (forward-auth protected) ---"
    local inval="${invalidation_flow:-${default_invalidation_flow}}"
    local traefik_prov_resp
    traefik_prov_resp=$(api_post "${API_BASE}/providers/proxy/" \
        "{\"name\":\"traefik-forward-auth\",\"authentication_flow\":\"${auth_flow}\",\"authorization_flow\":\"${explicit_flow}\",\"invalidation_flow\":\"${inval}\",\"mode\":\"forward_single\",\"external_host\":\"https://traefik.vcf.lab\",\"property_mappings\":${scope_pks}}")
    local traefik_prov_pk
    traefik_prov_pk=$(get_pk "${traefik_prov_resp}")
    if [ -z "${traefik_prov_pk}" ]; then
        traefik_prov_pk=$(get_results_pk "$(api_get "${API_BASE}/providers/proxy/?search=traefik-forward-auth")")
    fi
    echo "    Provider pk=${traefik_prov_pk}"

    api_post "${API_BASE}/core/applications/" \
        "{\"name\":\"Traefik Dashboard\",\"slug\":\"traefik-dashboard\",\"provider\":${traefik_prov_pk},\"meta_launch_url\":\"https://traefik.vcf.lab/dashboard/\",\"open_in_new_tab\":true,\"policy_engine_mode\":\"any\"}" > /dev/null 2>&1
    set_app_icon "traefik-dashboard" "traefik-proxy.svg"
    echo "    Application 'Traefik Dashboard' created"

    # --- Create Technitium DNS app tile (link-only, NO proxy provider) ---
    echo ""
    echo "  --- Technitium DNS (link-only app tile) ---"
    api_post "${API_BASE}/core/applications/" \
        "{\"name\":\"Technitium DNS\",\"slug\":\"technitium-dns\",\"meta_launch_url\":\"https://dns.vcf.lab\",\"open_in_new_tab\":true,\"policy_engine_mode\":\"any\"}" > /dev/null 2>&1
    set_app_icon "technitium-dns" "technitium.png"
    echo "    Application 'Technitium DNS' created (link-only tile)"

    # --- Create Vault app tile (link-only, NO proxy provider) ---
    echo ""
    echo "  --- Vault (link-only app tile) ---"
    api_post "${API_BASE}/core/applications/" \
        "{\"name\":\"HashiCorp Vault\",\"slug\":\"vault\",\"meta_launch_url\":\"https://vault.vcf.lab\",\"open_in_new_tab\":true,\"policy_engine_mode\":\"any\"}" > /dev/null 2>&1
    set_app_icon "vault" "vault-light.svg"
    echo "    Application 'HashiCorp Vault' created (link-only tile)"

    # --- Configure embedded outpost (only Traefik provider) ---
    echo ""
    echo "  --- Configuring embedded outpost ---"
    local outpost_pk
    outpost_pk=$(api_get "${API_BASE}/outposts/instances/" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
for r in data.get('results', []):
    if r.get('managed') == 'goauthentik.io/outposts/embedded':
        print(r['pk'])
        break
" 2>/dev/null || true)

    if [ -n "${outpost_pk}" ]; then
        api_patch "${API_BASE}/outposts/instances/${outpost_pk}/" \
            "{\"providers\":[${traefik_prov_pk}],\"config\":{\"log_level\":\"info\",\"authentik_host\":\"https://auth.vcf.lab\",\"authentik_host_insecure\":true,\"authentik_host_browser\":\"https://auth.vcf.lab\"}}" > /dev/null 2>&1
        echo "    Outpost configured with Traefik provider only"
    fi

    # --- Create forward-auth middleware (for Traefik dashboard only) ---
    echo ""
    echo "  --- Creating forward-auth middleware ---"
    cat <<'MWEOF' | kubectl apply -f -
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: authentik-forward-auth
  namespace: traefik
spec:
  forwardAuth:
    address: "http://authentik-server.authentik.svc.cluster.local:9000/outpost.goauthentik.io/auth/traefik"
    trustForwardHeader: true
    authResponseHeaders:
      - X-authentik-username
      - X-authentik-groups
      - X-authentik-entitlements
      - X-authentik-email
      - X-authentik-name
      - X-authentik-uid
      - X-authentik-jwt
      - X-authentik-meta-jwks
      - X-authentik-meta-outpost
      - X-authentik-meta-provider
      - X-authentik-meta-app
      - X-authentik-meta-version
MWEOF
    echo "  Forward-auth middleware created"

    # --- Traefik Dashboard IngressRoute (WITH forward-auth) ---
    echo ""
    echo "  --- Creating Traefik Dashboard IngressRoute (with forward-auth) ---"
    cat <<'IREOF' | kubectl apply -f -
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: traefik-dashboard
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: "Host(`traefik.vcf.lab`) && PathPrefix(`/outpost.goauthentik.io/`)"
      kind: Rule
      priority: 15
      services:
        - name: authentik-server
          namespace: authentik
          port: 9000
    - match: "Host(`traefik.vcf.lab`)"
      kind: Rule
      priority: 10
      middlewares:
        - name: authentik-forward-auth
          namespace: traefik
      services:
        - kind: TraefikService
          name: api@internal
  tls:
    secretName: traefik-vcf-lab-tls
IREOF
    echo "  Traefik Dashboard IngressRoute applied (forward-auth protected)"

    # --- Restart Authentik to reload outpost ---
    echo ""
    echo "  Restarting Authentik Server to reload outpost providers..."
    kubectl rollout restart deployment/authentik-server -n "${AUTHENTIK_NS}"
    sleep 5
    kubectl wait --for=condition=Available deployment/authentik-server \
        -n "${AUTHENTIK_NS}" --timeout=300s > /dev/null 2>&1
    echo "  Authentik Server restarted and ready"

    echo ""
}

###############################################################################
# Phase 11: Download dashboard icons
###############################################################################
phase_download_icons() {
    echo "=== Phase 11: Downloading Dashboard Icons ==="
    
    local DEST="/root/update-holorouter/images"
    local BASE="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons"
    mkdir -p "${DEST}"

    local downloaded=0 skipped=0 failed=0
    local failed_names=""

    dl() {
        local name="$1" url="$2"
        local outfile="${DEST}/${name}"
        if [ -f "${outfile}" ]; then
            skipped=$((skipped + 1))
            return 0
        fi
        if curl -m 10 -sfL -o "${outfile}" "${url}" 2>/dev/null; then
            local size
            size=$(stat -c%s "${outfile}" 2>/dev/null || echo 0)
            if [ "${size}" -gt 100 ]; then
                downloaded=$((downloaded + 1))
            else
                rm -f "${outfile}"
                failed=$((failed + 1))
                failed_names="${failed_names} ${name}"
            fi
        else
            rm -f "${outfile}"
            failed=$((failed + 1))
            failed_names="${failed_names} ${name}"
        fi
    }

    # SVG icons (base format SVG, single variant)
    for icon in traefik-proxy gitlab appflowy artifactory argo-cd baserow jenkins git gitea \
                grafana forgejo xcp-ng windmill vscode uptime-kuma truenas-scale \
                teleport pi-hole phpmyadmin pgadmin passbolt paperless-ng pangolin \
                owncloud openstack ntfy node-red nocodb nginx-proxy-manager \
                nextcloud-white n8n mail-in-a-box mailcow kestra keycloak kasm \
                joplin jfrog itop influxdb immich homebox homarr hedgedoc \
                hashicorp-boundary grist gitbook draw-io dockge ddns-updater \
                ddclient couchdb coolify code cloudflare budibase bookstack \
                bitwarden rustdesk roundcube redis rabbitmq poste seafile \
                zulip bentopdf; do
        dl "${icon}.svg" "${BASE}/svg/${icon}.svg"
    done

    # SVG icons with light AND dark variants
    dl "vault.svg" "${BASE}/svg/vault.svg"
    dl "vault-light.svg" "${BASE}/svg/vault-light.svg"
    dl "1password.svg" "${BASE}/svg/1password.svg"
    dl "1password-dark.svg" "${BASE}/svg/1password-dark.svg"
    dl "vaultwarden.svg" "${BASE}/svg/vaultwarden.svg"
    dl "vaultwarden-light.svg" "${BASE}/svg/vaultwarden-light.svg"
    dl "selfhosted.png" "${BASE}/png/selfhosted.png"
    dl "selfhosted-light.png" "${BASE}/png/selfhosted-light.png"
    dl "semaphore.svg" "${BASE}/svg/semaphore.svg"
    dl "semaphore-dark.svg" "${BASE}/svg/semaphore-dark.svg"
    dl "proxmox.svg" "${BASE}/svg/proxmox.svg"
    dl "proxmox-light.svg" "${BASE}/svg/proxmox-light.svg"
    dl "portainer.svg" "${BASE}/svg/portainer.svg"
    dl "portainer-dark.svg" "${BASE}/svg/portainer-dark.svg"
    dl "pocket-id.svg" "${BASE}/svg/pocket-id.svg"
    dl "pocket-id-light.svg" "${BASE}/svg/pocket-id-light.svg"
    dl "pocketbase.svg" "${BASE}/svg/pocketbase.svg"
    dl "pocketbase-dark.svg" "${BASE}/svg/pocketbase-dark.svg"
    dl "pfsense.svg" "${BASE}/svg/pfsense.svg"
    dl "pfsense-light.svg" "${BASE}/svg/pfsense-light.svg"
    dl "standard-notes.svg" "${BASE}/svg/standard-notes.svg"
    dl "standard-notes-light.svg" "${BASE}/svg/standard-notes-light.svg"
    dl "notesnook.svg" "${BASE}/svg/notesnook.svg"
    dl "notesnook-light.svg" "${BASE}/svg/notesnook-light.svg"
    dl "netbox.svg" "${BASE}/svg/netbox.svg"
    dl "netbox-dark.svg" "${BASE}/svg/netbox-dark.svg"
    dl "netbird.svg" "${BASE}/svg/netbird.svg"
    dl "guacamole.svg" "${BASE}/svg/guacamole.svg"
    dl "guacamole-light.svg" "${BASE}/svg/guacamole-light.svg"
    dl "heimdall.svg" "${BASE}/svg/heimdall.svg"
    dl "heimdall-light.svg" "${BASE}/svg/heimdall-light.svg"
    dl "dokploy.svg" "${BASE}/svg/dokploy.svg"
    dl "dokploy-dark.svg" "${BASE}/svg/dokploy-dark.svg"
    dl "karakeep.svg" "${BASE}/svg/karakeep.svg"
    dl "karakeep-dark.svg" "${BASE}/svg/karakeep-dark.svg"
    dl "open-webui.svg" "${BASE}/svg/open-webui.svg"
    dl "open-webui-light.svg" "${BASE}/svg/open-webui-light.svg"

    # PNG-only icons
    dl "technitium.png" "${BASE}/png/technitium.png"
    dl "sftpgo.png" "${BASE}/png/sftpgo.png"
    dl "unifi-controller.png" "${BASE}/png/unifi-controller.png"
    dl "unbound.svg" "${BASE}/svg/unbound.svg"
    dl "lubelogger.png" "${BASE}/png/lubelogger.png"
    dl "linkwarden.png" "${BASE}/png/linkwarden.png"
    dl "comfy-ui.png" "${BASE}/png/comfy-ui.png"
    dl "docmost.png" "${BASE}/png/docmost.png"
    dl "dockhand.png" "${BASE}/png/dockhand.png"

    # freeipa - try SVG first, fall back to PNG
    dl "freeipa.svg" "${BASE}/svg/freeipa.svg"
    if [ ! -f "${DEST}/freeipa.svg" ]; then
        dl "freeipa.png" "${BASE}/png/freeipa.png"
    fi

    echo "  Downloaded: ${downloaded}, Skipped: ${skipped}, Failed: ${failed}"
    if [ "${failed}" -gt 0 ]; then
        echo "  Failed images:${failed_names}"
    fi
    echo ""
}

###############################################################################
# Phase 12: Dashboard icon application tiles
###############################################################################
phase_dashboard_icons() {
    echo "=== Phase 11: Creating Dashboard Icon Application Tiles ==="

    local API_BASE="http://${EXTERNAL_IP}:9000/api/v3"
    local AUTH_HEADER="Authorization: Bearer ${BOOTSTRAP_TOKEN}"
    local CT="Content-Type: application/json"
    local created=0 skipped=0

    create_icon_app() {
        local slug="$1" name="$2" icon_file="$3"
        local existing
        existing=$(curl -m 15 -sf "${API_BASE}/core/applications/${slug}/" -H "${AUTH_HEADER}" 2>/dev/null || true)
        if echo "${existing}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('slug',''))" 2>/dev/null | grep -q "${slug}" 2>/dev/null; then
            curl -m 15 -sf -X PATCH "${API_BASE}/core/applications/${slug}/" \
                -H "${AUTH_HEADER}" -H "${CT}" \
                -d "{\"meta_icon\":\"${icon_file}\"}" > /dev/null 2>&1 || true
            skipped=$((skipped + 1))
            return 0
        fi
        curl -m 15 -sf -X POST "${API_BASE}/core/applications/" \
            -H "${AUTH_HEADER}" -H "${CT}" \
            -d "{\"name\":\"${name}\",\"slug\":\"${slug}\",\"meta_launch_url\":\"\",\"open_in_new_tab\":true,\"policy_engine_mode\":\"any\",\"meta_icon\":\"${icon_file}\"}" > /dev/null 2>&1 || true
        created=$((created + 1))
    }

    create_icon_app "gitlab" "GitLab" "gitlab.svg"
    create_icon_app "appflowy" "AppFlowy" "appflowy.svg"
    create_icon_app "artifactory" "JFrog Artifactory" "artifactory.svg"
    create_icon_app "argo-cd" "Argo CD" "argo-cd.svg"
    create_icon_app "baserow" "Baserow" "baserow.svg"
    create_icon_app "jenkins" "Jenkins" "jenkins.svg"
    create_icon_app "git" "Git" "git.svg"
    create_icon_app "gitea" "Gitea" "gitea.svg"
    create_icon_app "grafana" "Grafana" "grafana.svg"
    create_icon_app "forgejo" "Forgejo" "forgejo.svg"
    create_icon_app "1password" "1Password" "1password.svg"
    create_icon_app "bentopdf" "BentoPDF" "bentopdf.svg"
    create_icon_app "zulip" "Zulip" "zulip.svg"
    create_icon_app "xcp-ng" "XCP-ng" "xcp-ng.svg"
    create_icon_app "windmill" "Windmill" "windmill.svg"
    create_icon_app "vscode" "VS Code" "vscode.svg"
    create_icon_app "vaultwarden" "Vaultwarden" "vaultwarden-light.svg"
    create_icon_app "uptime-kuma" "Uptime Kuma" "uptime-kuma.svg"
    create_icon_app "unifi-controller" "UniFi Controller" "unifi-controller.png"
    create_icon_app "unbound" "Unbound" "unbound.svg"
    create_icon_app "truenas-scale" "TrueNAS Scale" "truenas-scale.svg"
    create_icon_app "teleport" "Teleport" "teleport.svg"
    create_icon_app "standard-notes" "Standard Notes" "standard-notes.svg"
    create_icon_app "sftpgo" "SFTPGo" "sftpgo.png"
    create_icon_app "selfhosted" "Self-Hosted" "selfhosted-light.png"
    create_icon_app "semaphore" "Semaphore" "semaphore.svg"
    create_icon_app "seafile" "Seafile" "seafile.svg"
    create_icon_app "rustdesk" "RustDesk" "rustdesk.svg"
    create_icon_app "roundcube" "Roundcube" "roundcube.svg"
    create_icon_app "redis" "Redis" "redis.svg"
    create_icon_app "rabbitmq" "RabbitMQ" "rabbitmq.svg"
    create_icon_app "proxmox" "Proxmox" "proxmox-light.svg"
    create_icon_app "poste" "Poste" "poste.svg"
    create_icon_app "portainer" "Portainer" "portainer.svg"
    create_icon_app "pocket-id" "Pocket ID" "pocket-id-light.svg"
    create_icon_app "pocketbase" "PocketBase" "pocketbase.svg"
    create_icon_app "pi-hole" "Pi-hole" "pi-hole.svg"
    create_icon_app "phpmyadmin" "phpMyAdmin" "phpmyadmin.svg"
    create_icon_app "pfsense" "pfSense" "pfsense-light.svg"
    create_icon_app "pgadmin" "pgAdmin" "pgadmin.svg"
    create_icon_app "passbolt" "Passbolt" "passbolt.svg"
    create_icon_app "paperless-ng" "Paperless-ngx" "paperless-ng.svg"
    create_icon_app "pangolin" "Pangolin" "pangolin.svg"
    create_icon_app "owncloud" "ownCloud" "owncloud.svg"
    create_icon_app "openstack" "OpenStack" "openstack.svg"
    create_icon_app "open-webui" "Open WebUI" "open-webui-light.svg"
    create_icon_app "ntfy" "ntfy" "ntfy.svg"
    create_icon_app "notesnook" "Notesnook" "notesnook-light.svg"
    create_icon_app "node-red" "Node-RED" "node-red.svg"
    create_icon_app "nocodb" "NocoDB" "nocodb.svg"
    create_icon_app "nginx-proxy-manager" "Nginx Proxy Manager" "nginx-proxy-manager.svg"
    create_icon_app "nextcloud-white" "Nextcloud" "nextcloud-white.svg"
    create_icon_app "netbox" "NetBox" "netbox.svg"
    create_icon_app "netbird" "NetBird" "netbird.svg"
    create_icon_app "n8n" "n8n" "n8n.svg"
    create_icon_app "mail-in-a-box" "Mail-in-a-Box" "mail-in-a-box.svg"
    create_icon_app "mailcow" "Mailcow" "mailcow.svg"
    create_icon_app "lubelogger" "LubeLogger" "lubelogger.png"
    create_icon_app "linkwarden" "Linkwarden" "linkwarden.png"
    create_icon_app "kestra" "Kestra" "kestra.svg"
    create_icon_app "keycloak" "Keycloak" "keycloak.svg"
    create_icon_app "karakeep" "Karakeep" "karakeep.svg"
    create_icon_app "kasm" "Kasm" "kasm.svg"
    create_icon_app "joplin" "Joplin" "joplin.svg"
    create_icon_app "jfrog" "JFrog" "jfrog.svg"
    create_icon_app "itop" "iTop" "itop.svg"
    create_icon_app "influxdb" "InfluxDB" "influxdb.svg"
    create_icon_app "immich" "Immich" "immich.svg"
    create_icon_app "homebox" "Homebox" "homebox.svg"
    create_icon_app "homarr" "Homarr" "homarr.svg"
    create_icon_app "hedgedoc" "HedgeDoc" "hedgedoc.svg"
    create_icon_app "heimdall" "Heimdall" "heimdall-light.svg"
    create_icon_app "hashicorp-boundary" "HashiCorp Boundary" "hashicorp-boundary.svg"
    create_icon_app "guacamole" "Guacamole" "guacamole-light.svg"
    create_icon_app "grist" "Grist" "grist.svg"
    create_icon_app "gitbook" "GitBook" "gitbook.svg"
    create_icon_app "freeipa" "FreeIPA" "freeipa.svg"
    create_icon_app "draw-io" "draw.io" "draw-io.svg"
    create_icon_app "dokploy" "Dokploy" "dokploy.svg"
    create_icon_app "docmost" "Docmost" "docmost.png"
    create_icon_app "dockhand" "Dockhand" "dockhand.png"
    create_icon_app "dockge" "Dockge" "dockge.svg"
    create_icon_app "ddns-updater" "DDNS Updater" "ddns-updater.svg"
    create_icon_app "ddclient" "DDClient" "ddclient.svg"
    create_icon_app "couchdb" "CouchDB" "couchdb.svg"
    create_icon_app "coolify" "Coolify" "coolify.svg"
    create_icon_app "comfy-ui" "ComfyUI" "comfy-ui.png"
    create_icon_app "code" "Code" "code.svg"
    create_icon_app "cloudflare" "Cloudflare" "cloudflare.svg"
    create_icon_app "budibase" "Budibase" "budibase.svg"
    create_icon_app "bookstack" "BookStack" "bookstack.svg"
    create_icon_app "bitwarden" "Bitwarden" "bitwarden.svg"

    echo "  Dashboard icon apps: ${created} created, ${skipped} already existed"
    echo ""
}

###############################################################################
# Phase 12: Save container image tarballs
###############################################################################
phase_save_images() {
    echo "=== Phase 12: Saving Container Image Tarballs ==="

    mkdir -p "${IMAGE_DIR}"

    local RUNNING_TRAEFIK
    RUNNING_TRAEFIK=$(kubectl get deployment traefik -n "${TRAEFIK_NS}" \
        -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "${TRAEFIK_IMAGE}")
    # containerd stores Docker Hub images with docker.io/library/ prefix
    if echo "${RUNNING_TRAEFIK}" | grep -q '^docker.io/traefik:'; then
        RUNNING_TRAEFIK="${RUNNING_TRAEFIK/docker.io\/traefik:/docker.io\/library\/traefik:}"
    fi

    declare -A SAVE_IMAGES=(
        ["authentik-server"]="${AUTHENTIK_IMAGE}"
        ["postgres17"]="${POSTGRES_IMAGE}"
        ["traefik"]="${RUNNING_TRAEFIK}"
    )

    for name in "${!SAVE_IMAGES[@]}"; do
        local image="${SAVE_IMAGES[$name]}"
        local tarball="${IMAGE_DIR}/${name}.tar"
        if [ ! -f "${tarball}" ]; then
            echo "  [SAVE] ${image} -> ${tarball}"
            ctr -n k8s.io images export "${tarball}" "${image}" 2>/dev/null || true
        else
            echo "  [SKIP] ${tarball} already exists"
        fi
    done
    echo ""
}

###############################################################################
# Phase 13: Summary
###############################################################################
phase_summary() {
    echo "============================================================"
    echo "  Holorouter Customization Complete!"
    echo "============================================================"
    echo ""
    echo "  SSL-Enabled Endpoints:"
    echo "    https://auth.vcf.lab       Authentik IdP (native login)"
    echo "    https://ca.vcf.lab         MSADCS Proxy (Basic Auth)"
    echo "    https://vault.vcf.lab      HashiCorp Vault (token auth)"
    echo "    https://traefik.vcf.lab    Traefik Dashboard (Authentik forward-auth)"
    echo "    https://dns.vcf.lab        Technitium DNS (native login)"
    echo ""
    echo "  Authentik Users:"
    echo "    akadmin    - Authentik Super Admin (default)"
    echo "    holadmin   - HOL Administrator (superuser)"
    echo "    holuser    - HOL User"
    echo "    provider-admin, tenant-admin, tenant-user"
    echo ""
    echo "  All passwords: (contents of /root/creds.txt)"
    echo ""
    echo "  API Token:  ${BOOTSTRAP_TOKEN}"
    echo "  Token file: ${AUTHENTIK_DIR}/bootstrap-token.txt"
    echo ""
    echo "  Dashboard Icons: 92+ application tiles with locally-stored icons"
    echo ""
    echo "  Data Persistence:"
    echo "    Authentik DB:    /opt/authentik-data/postgres/"
    echo "    Authentik Files: /opt/authentik-data/files/"
    echo "    Certsrv Proxy:   /root/certsrv-proxy/"
    echo "    TLS Certs:       /root/traefik-certs/"
    echo "    Image Tarballs:  /root/containerd-images/"
    echo ""
    echo "  Kubernetes:"
    kubectl get pods -A --no-headers 2>/dev/null | awk '{printf "    %-16s %-45s %s\n", $1, $2, $4}'
    echo ""
    echo "============================================================"
}

###############################################################################
# Main
###############################################################################
phase_preflight
phase_dns
phase_vault_pki
phase_certs
phase_images
phase_traefik
phase_certsrv
phase_dns_ingressroute
phase_vault_ingressroute
phase_authentik_deploy
phase_download_icons
phase_authentik_configure
phase_dashboard_icons
phase_save_images
phase_summary
