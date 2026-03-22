---
name: vcf-troubleshooting
description: Diagnose and resolve common issues in VMware Cloud Foundation (VCF) 9.0 and 9.1 Holodeck nested virtualization lab environments. Covers Supervisor configuration failures, WCP certificate issues, K8s node NotReady flapping, VCF Automation volume attachment stalls, content library sync failures, VCF component shutdown/startup, vCenter service autostart failures, console black screen, proxy/DNS issues, CSI password rotation after upgrade, SSH host key mismatches, VCF Automation microservice scaling, Fleet LCM failures, VCF Automation API shutdown issues, SDDC Manager credential remediation failures, and VSP cluster image pull failures. Use when troubleshooting VCF, Supervisor stuck, WCP errors, Kubernetes NotReady, VCF Automation down, content library sync, lab startup failures, black console screen, proxy issues, CSI controller crash, SSH host key changed, VCFA 503 errors, SDDC Manager passwords, credential UNKNOWN status, resource locks, password remediation failures, VSP ImagePullBackOff, or containerd NO_PROXY.
---

# VCF 9.x Troubleshooting Guide

This environment is a **Holodeck nested virtualization lab**. All passwords are in `/home/holuser/creds.txt`.

## Table of Known Issues

| Issue | Symptom | Root Cause | Section |
| --- | --- | --- | --- |
| Supervisor stuck CONFIGURING | PackageInstall errors, nginx CrashLoopBackOff | Unresolvable `fleet-01a` hostname in nginx | 1 |
| WCP notification error | "Problem with notifications mechanism" | SCP hypercrypt waiting for encryption keys | 2 |
| K8s node flapping NotReady | Node cycles Ready/NotReady every ~17 min | etcd gRPC stalls + Kyverno `failurePolicy: Fail` | 3 |
| VCF Automation "no healthy upstream" | Pods stuck in ContainerCreating | Stuck volume attachments + CSI controller crash | 4 |
| Content library sync failure | "Read timed out" on VMDK download | Nginx proxy_read_timeout too low + no buffering | 5 |
| VCF components stopped | Salt, Telemetry, Depot, LCM, etc. Stopped in UI | Deployments scaled to 0 after cold boot | 6 |
| vCenter services not autostarting | `vapi-endpoint`, `trustmanagement` STOPPED | vmon startup data missing for these services | 7 |
| Console black screen | VMware console shows black, desktop works via SSH | NoMachine EGL capture blanks framebuffer | 8 |
| apt update hangs | Stuck at noble-backports InRelease | Broken upstream Ubuntu mirror IPs | 9 |
| Proxy wait timeout | Lab startup stuck "Waiting for proxy" | Circular dependency between manager and router boot | 10 |
| VCF Management not functional | Fleet LCM UI shows "not functional", HTTP 500 | Postgres instances suspended, fleet-lcm pods CrashLoopBackOff | 11 |
| VCF Automation API shutdown fails | suite-api proxy returns HTTP 500 on shutdown action | suite-api does not proxy lifecycle actions; use fleet-lcm direct API | 12 |
| CSI password rotated after upgrade | CSI CrashLoopBackOff, "incorrect user name or password" | VCF upgrade rotates `svc-vcfsp-vc-*` SSO service account password | 13 |
| SSH host key mismatch after upgrade | SSH rejected with "REMOTE HOST IDENTIFICATION HAS CHANGED" | VCF upgrade regenerates vCenter SSH host keys | 14 |
| VCFA microservices at 0 replicas | VCF Automation returns "no healthy upstream" (503) | Shutdown scaled all ~50 prelude deployments to 0, not auto-restored | 15 |
| SDDC Manager credentials all UNKNOWN | All 43 credentials show UNKNOWN status in UI | Service account passwords out of sync with vCenter SSO + stale resource locks | 16 |
| SDDC Manager REMEDIATE blocked by locks | "Unable to acquire resource level lock(s)" | Stale locks in platform.lock table from failed/cancelled tasks | 17 |
| SDDC Manager REMEDIATE resources not ready | "Resources [...] are not available/ready" | Host/NSX/vCenter status stuck in ERROR/ACTIVATING in platform DB | 18 |
| SDDC Manager REMEDIATE SSO login failure | "Cannot complete login due to incorrect credentials" | SDDC Manager service account passwords wrong in vCenter SSO | 19 |
| Kube-apiserver timeouts, pods stuck Unknown | Pods in Unknown state, kube-apiserver timeouts | CAPI/CAPV controllers not ready after cold boot | 20 |
| vCenter browser warning banner (UNSOLVABLE) | "Unsupported browser" yellow banner on login | JSP compilation/Tomcat caching defeats websso.js patching | 21 |
| CCI 503 "no healthy upstream" at startup | CCI URL returns 503 instead of 401 JSON | CCI-related prelude deployments at 0 replicas after cold boot | 22 |
| SCP VMs powered off after shutdown (9.0.x) | Supervisor stuck CONFIGURING/ERROR, SCP VMs poweredOff | EAM-managed VMs not auto-started; vCenter PowerOn denied with NoPermission | 23 |
| VCF Automation VIP drops after boot | auto-a pings then stops, "no route to host" | kube-vip releases VIP when istio-ingressgateway has no endpoints (ImagePullBackOff) | 24 |
| Harbor unreachable, Supervisor DNS broken | Harbor pods CrashLoopBackOff with "i/o timeout" on DNS lookups | kube-dns endpoint points to LB IP causing asymmetric DLB routing | 25 |
| VSP image pulls fail after boot | All VSP pods in ImagePullBackOff, Fleet LCM/Ops Logs HTTP 500 | VSP service CIDR `198.18.128.0/17` not in NO_PROXY; containerd routes internal pulls through Squid proxy | 26 |
| VCF Ops Fleet Mgmt rejects Microsoft CA | "Failed to update certificate authorities" in UI | certsrv proxy returns 301 for `/certsrv`; VCF Ops Java client does not follow redirects | 27 |
| certsrv-proxy Address already in use | Pod CrashLoopBackOff with `OSError: [Errno 98]` | Orphaned Python process on host still bound to port 443 after force pod delete | 28 |
| SDDC Manager "Public key mismatch" on cert install | "Public key in CSR and server certificate are not matching" | PKCS#7 DER encoding sorts SET OF elements, putting CA cert before leaf cert | 29 |
| NSX ReTrust fails after vCenter cert replacement | "Failed to import the trusted root certificate for compute manager" | NSX tries to re-trust vCenter while services are still restarting; transient timing issue | 30 |
| NSX Compute Manager DOWN after vCenter cert replacement | `connection_status: DOWN`, `REGISTERED_WITH_ERRORS`, error 7059/MP2179 | `dir-cli trustedcert publish` double-cert in TRUSTED_ROOTS; NSX rejects multi-cert PEM | 31 |

---

## 1. Supervisor Stuck in CONFIGURING (VCF 9.1)

**Symptom**: `kubectl get pkgi -A` shows `Reconcile failed` errors with `http: server gave HTTP response to HTTPS client`.

**Diagnosis**:

```bash
# SSH to SCP node
sshpass -p "$(cat /home/holuser/creds.txt)" ssh root@10.1.1.188

# Check nginx pod
kubectl get pod -n kube-system -l component=kubectl-plugin-vsphere
# If CrashLoopBackOff, check logs:
kubectl logs -n kube-system kubectl-plugin-vsphere-<id> -c kubectl-plugin-vsphere
# Look for: "host not found in upstream fleet-01a.site-a.vcf.lab"
```

**Root Cause**: The Jinja template at `/etc/vmware/wcp/nginx/forwarding_rules_vcf_cli.conf.jinja` uses `proxy_pass` with a direct hostname. Nginx resolves all upstream hostnames at startup — if `fleet-01a` DNS is missing, nginx crashes, taking down the Docker registry TLS proxy on port 5000.

**Fix**:

*(Note to Agent: When running complex python/json parsing over SSH, consider writing a local `.py` script and executing it remotely to avoid bash quoting issues).*

```bash
# Fix the Jinja template for persistence
sed -i '/proxy_pass.*fds_file_depot_host_port/i\            resolver 127.0.0.53;' \
  /etc/vmware/wcp/nginx/forwarding_rules_vcf_cli.conf.jinja

# Fix the rendered config for immediate effect
# Replace: proxy_pass https://fleet-01a.site-a.vcf.lab:443/...;
# With:    resolver 127.0.0.53;
#          set $fds_upstream https://fleet-01a.site-a.vcf.lab:443;
#          proxy_pass $fds_upstream/...;

# Restart static pod
MANIFEST=/etc/kubernetes/manifests/kubectl-plugin-vsphere.yaml
sed -i "s/restart-trigger: .*/restart-trigger: \"$(date +%s)\"/" $MANIFEST

# Kick failed PackageInstalls
for app in $(kubectl get apps -A -o json | python3 -c "
import json,sys
for i in json.load(sys.stdin).get('items',[]):
    s=i.get('status',{}).get('friendlyDescription','')
    if 'fail' in s.lower() or 'error' in s.lower():
        print(f\"{i['metadata']['namespace']}/{i['metadata']['name']}\")
"); do
    ns=$(echo $app | cut -d/ -f1); name=$(echo $app | cut -d/ -f2)
    kubectl patch app -n $ns $name --type=merge -p '{"spec":{"paused":true}}'
    sleep 1
    kubectl patch app -n $ns $name --type=merge -p '{"spec":{"paused":false}}'
done
```

---

## 2. WCP "Problem with Notifications" / Supervisor Control Plane Down

**Symptom**: vCenter UI shows "There was a problem with the notifications mechanism."

**Diagnosis**:

```bash
# Check WCP service on vCenter
ssh root@vc-wld01-a.site-a.vcf.lab "vmon-cli --status wcp"

# Check SCP VM
sshpass -p "$(cat /home/holuser/creds.txt)" ssh root@10.1.1.86
systemctl status hypercrypt
systemctl status kubelet
ls /dev/shm/wcp_decrypted_data/  # should NOT be empty
```

**Root Cause**: After cold boot, `hypercrypt.service` waits for encryption keys at `/dev/shm/secret`. These are delivered by the ESXi spherelet. If the delivery fails, the SCP cannot decrypt its kubeconfig, kubelet cannot start, and the Supervisor VIP is unreachable.

**Resolution**:

1. Verify `trustmanagement` service on vCenter: `vmon-cli --status trustmanagement` — start if stopped.
2. Reboot the SCP VM from inside: `ssh root@10.1.1.86 reboot`.
3. If hypercrypt remains stuck, the key delivery mechanism is broken — requires Supervisor cluster re-enablement in vCenter UI.

---

## 3. Kubernetes Node Flapping NotReady (VSP Cluster)

**Symptom**: `vsp-01a-zpnzk` (or similar) cycles NotReady/Ready every ~17 minutes.

**Diagnosis**:

```bash
# From VSP control plane
kubectl get events --field-selector involvedObject.name=vsp-01a-zpnzk | tail -20

# Check etcd gRPC errors
kubectl logs -n kube-system kube-apiserver-vsp-01a-gtg5d --tail=50 | grep "127.0.0.1:2379"

# Check kyverno webhook policy
kubectl get validatingwebhookconfigurations kyverno-resource-validating-webhook-cfg \
  -o jsonpath='{.webhooks[0].failurePolicy}'
```

**Root Cause Chain**: etcd gRPC connection drops (~every 30s) -> API server stalls -> Kyverno webhooks (`failurePolicy: Fail`, 10s timeout) block all API writes -> kubelet cannot renew node lease -> NodeNotReady after 40s grace period.

**Remediation**:

```bash
# 1. Increase node-monitor-grace-period (persists across pod restarts)
grep -q node-monitor-grace-period /etc/kubernetes/manifests/kube-controller-manager.yaml || \
  sed -i '/- --use-service-account-credentials=true/a\    - --node-monitor-grace-period=90s' \
  /etc/kubernetes/manifests/kube-controller-manager.yaml

# 2. Restart etcd and API server to clear gRPC state
ETCD=$(crictl pods --name etcd-vsp-01a-gtg5d -s Ready -q | head -1)
crictl stopp $ETCD; sleep 15
API=$(crictl pods --name kube-apiserver-vsp-01a-gtg5d -s Ready -q | head -1)
crictl stopp $API; sleep 20

# 3. Increase control plane VM resources (the root fix)
# Increase from 4 vCPU/10GB to 8 vCPU/16GB via vCenter
```

**Note**: Kyverno `failurePolicy: Fail` is managed by `vmsp-operator` via FluxCD HelmRelease and **cannot be persistently changed** from within the cluster. The permanent fix is increasing control plane VM resources.

---

## 4. VCF Automation "No Healthy Upstream"

**Symptom**: `https://auto-a.site-a.vcf.lab` returns 503. Pods stuck in ContainerCreating.

**Diagnosis**:

```bash
# Check pods
kubectl get pods -A | grep -v Running | grep -v Completed

# Check volume attachments
kubectl get volumeattachments -o json | python3 -c "
import json,sys
for va in json.load(sys.stdin)['items']:
    dt = va['metadata'].get('deletionTimestamp','')
    if dt: print(f\"STUCK: {va['metadata']['name']} deleting since {dt}\")
"

# Check CSI controller
kubectl get pods -n vmware-system-csi
```

**Root Cause**: Volume attachments stuck in deletion state with `external-attacher/csi-vsphere-vmware-com` finalizer. CSI controller in CrashLoopBackOff because vCenter `vmware-vapi-endpoint` service was stopped.

**Fix**:

```bash
# 1. Start vAPI endpoint on vCenter
ssh root@vc-wld01-a.site-a.vcf.lab "vmon-cli --start vmware-vapi-endpoint"

# 2. Remove finalizers from stuck volume attachments
for va in $(kubectl get volumeattachments -o json | python3 -c "
import json,sys
for v in json.load(sys.stdin)['items']:
    if v['metadata'].get('deletionTimestamp'):
        print(v['metadata']['name'])
"); do
    kubectl patch volumeattachment $va --type=merge -p '{"metadata":{"finalizers":null}}'
done

# 3. Delete stale CSI leader leases
for lease in external-attacher-leader-csi-vsphere-vmware-com \
             external-resizer-csi-vsphere-vmware-com; do
    kubectl delete lease -n vmware-system-csi $lease 2>/dev/null
done

# 4. Restart CSI controller
kubectl delete pod -n vmware-system-csi $(kubectl get pods -n vmware-system-csi -l app=vsphere-csi-controller -o name | head -1)
```

---

## 5. Content Library Sync Failure (VMDK timeout)

**Symptom**: Library sync fails with `source error: IO error during transfer ... Read timed out`.

**Root Cause**: The fleet depot stack (`fleet-01a.site-a.vcf.lab`) has nginx `proxy_read_timeout 300s` (5 min) and `proxy_max_temp_file_size 0` (no disk buffering). A 4GB+ VMDK takes >300s to proxy.

**Fix (on VSP control plane)**:

```bash
# Increase nginx proxy timeout via HelmRelease
kubectl patch helmrelease depot-service -n vcf-fleet-depot --type=merge \
  -p '{"spec":{"values":{"upstream":{"readTimeout":1800},"performance":{"proxyMaxTempFileSize":"1024m"}}}}'

# Create envoy BackendTrafficPolicy for longer gateway timeout
cat <<'EOF' | kubectl apply -f -
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: depot-file-server-backendtrafficpolicy
  namespace: vcf-fleet-depot
spec:
  targetRefs:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: depot-file-server
  timeout:
    http:
      requestTimeout: 3600s
EOF
```

---

## 6. VCF Components Stopped After Cold Boot

**Symptom**: Salt, Telemetry, Software Depot, Identity Broker, etc. show "Stopped" in VCF Services Runtime UI.

**Automated Fix**: `python3 /home/holuser/hol/Startup/VCFfinal.py` (Task 2e handles component startup).

**Manual Fix (on VSP control plane at 10.1.1.142)**:

```bash
# Unsuspend postgres instances (two-step: remove label + restore numberOfInstances)
for ns in salt-raas vcf-fleet-lcm vcf-sddc-lcm vidb-external; do
    kubectl label postgresinstances.database.vmsp.vmware.com --all -n $ns \
      database.vmsp.vmware.com/suspended-
    for pg in $(kubectl get postgresqls.acid.zalan.do -n $ns -o jsonpath='{.items[*].metadata.name}'); do
        kubectl patch postgresqls.acid.zalan.do $pg -n $ns --type=merge \
          -p '{"spec":{"numberOfInstances":1}}'
    done
done

# Scale up all stopped deployments/statefulsets
for ns_res in salt:deployment/salt-master salt:deployment/salt-minion \
              salt-raas:deployment/redis salt-raas:deployment/raas \
              telemetry:deployment/telemetry-acceptor \
              vcf-fleet-depot:deployment/depot-service \
              vcf-fleet-depot:deployment/distribution-service \
              vcf-fleet-lcm:deployment/vcf-fleet-build-service-fleetbuild \
              vcf-fleet-lcm:deployment/vcf-fleet-upgrade-service-fleetupgrade \
              vcf-sddc-lcm:deployment/vcf-sddc-build-service-sddcbuild \
              vcf-sddc-lcm:deployment/vcf-sddc-upgrade-service-sddcupgrade \
              vidb-external:deployment/vidb-service \
              ops-logs:statefulset/log-processor ops-logs:statefulset/log-store \
              vodap:deployment/vcf-obs-collector-controller-service \
              vodap:deployment/vcf-obs-data-query-service \
              vodap:deployment/vcf-obs-esx-collector-service \
              vodap:deployment/vcf-obs-netops-collector-service \
              vodap:deployment/vcf-obs-vc-collector-service \
              vmsp-metrics-store:deployment/clickhouse-operator-altinity-clickhouse-operator \
              vmsp-metrics-store:deployment/vsp-metrics-store-operator; do
    ns=${ns_res%%:*}; res=${ns_res#*:}
    kubectl scale -n $ns $res --replicas=1
done

# CRITICAL: components.api.vmsp.vmware.com is cluster-scoped (NOT namespaced)
# Do NOT use -A or -n flags — they silently fail
for comp in $(kubectl get components.api.vmsp.vmware.com -o json | python3 -c "
import json,sys
for c in json.load(sys.stdin)['items']:
    ann=c['metadata'].get('annotations',{})
    if ann.get('component.vmsp.vmware.com/operational-status')=='NotRunning':
        print(c['metadata']['name'])
"); do
    kubectl annotate components.api.vmsp.vmware.com $comp \
      component.vmsp.vmware.com/operational-status=Running --overwrite
done
```

### Graceful Shutdown Procedure (Reverse of Startup)

Managed by `python3 /home/holuser/hol/Shutdown/Shutdown.py --phase 2b`:

```bash
# Scale down in reverse order (last started = first stopped)
# Config: /tmp/config.ini [VCFFINAL] vcfcomponents (format: namespace:resource_type/name)

# Suspend postgres (two-step: add label + set numberOfInstances to 0)
kubectl label postgresinstances.database.vmsp.vmware.com --all -n salt-raas \
  database.vmsp.vmware.com/suspended=true --overwrite
kubectl patch postgresqls.acid.zalan.do pgdatabase -n salt-raas --type=merge \
  -p '{"spec":{"numberOfInstances":0}}'

# Annotate components as NotRunning (cluster-scoped — NO -n flag)
kubectl annotate components.api.vmsp.vmware.com salt \
  component.vmsp.vmware.com/operational-status=NotRunning --overwrite
```

**Note**: vodap ClickHouse statefulsets (`chi-vcf-obs-*`, `chk-vcf-obs-keeper-*`) are managed
by the clickhouse-operator but must be scaled down separately — stopping the operator alone
does not stop the pods.

### Supervisor Workload Shutdown (Phase 3b)

Managed by `python3 /home/holuser/hol/Shutdown/Shutdown.py --phase 3b`:

Phase 3b dynamically discovers and gracefully shuts down all Supervisor-managed workloads
**before** WCP is stopped (Phase 3). This ensures VKS/TKG cluster VMs and Supervisor Service
pods (Harbor, etc.) drain gracefully instead of being hard-killed when ESXi hosts shut down.

Steps performed:
1. SSH to WLD vCenter, run `decryptK8Pwd.py` to obtain SCP password and VIP
2. SSH to SCP VIP, discover TKG/VKS clusters via `kubectl get clusters -A`
3. Delete discovered clusters (triggers graceful node drain and VM power-off)
4. Discover Supervisor Service namespaces (e.g., `svc-harbor-domain-c10`)
5. Scale down all deployments and statefulsets in those namespaces to 0 replicas
6. Wait 30s for workload VMs to power off

Phase 4 also includes dynamic VM discovery from the WLD vCenter — any Supervisor-managed
workload VMs not matched by configured regex patterns are automatically found and shut down.

---

## 7. vCenter Services Not Autostarting

**Symptom**: `vapi-endpoint` and/or `trustmanagement` STOPPED despite `Starttype: AUTOMATIC`.

**Root Cause**: vmon's startup data file is missing/corrupt. vmon logs show `[ReadSvcSubStartupData] No startup information from <service>`.

**Fix**: Check and start during lab startup scripts. Already handled by `/home/holuser/hol/Tools/check_wcp_vcenter.sh`, `/home/holuser/hol/Startup/VCFfinal.py`, and `/home/holuser/hol/Startup/vSphere.py` (TASK 7) in the startup codebase.

**Important**: SSH and bash shell must be enabled on vCenter before these checks can run. The `/home/holuser/hol/Startup/vSphere.py` module (TASK 6b) now automatically enables SSH and shell via the vCenter REST API (`PUT /api/appliance/access/ssh` and `PUT /api/appliance/access/shell`) before checking autostart services, so this works even on fresh labs where `confighol` has not been run.

```bash
# Enable SSH and shell via REST API (no SSH required)
VC="vc-mgmt-a.site-a.vcf.lab"
PASSWORD=$(cat /home/holuser/creds.txt)
SESSION=$(curl -sk -X POST "https://${VC}/api/session" \
  -u "administrator@vsphere.local:${PASSWORD}" | tr -d '"')
curl -sk -X PUT "https://${VC}/api/appliance/access/ssh" \
  -H "vmware-api-session-id: ${SESSION}" -H "Content-Type: application/json" -d 'true'
curl -sk -X PUT "https://${VC}/api/appliance/access/shell" \
  -H "vmware-api-session-id: ${SESSION}" -H "Content-Type: application/json" \
  -d '{"enabled":true,"timeout":0}'

# Then check and start services on each vCenter
for svc in vapi-endpoint trustmanagement wcp; do
    STATUS=$(ssh root@vc-mgmt-a.site-a.vcf.lab "vmon-cli --status $svc" | \
             grep RunState | sed 's/.*RunState: //' | head -1)
    if [ "$STATUS" != "STARTED" ]; then
        ssh root@vc-mgmt-a.site-a.vcf.lab "vmon-cli --start $svc"
    fi
done
```

---

## 8. Console Black Screen (Ubuntu Desktop VM)

**Root Cause**: NoMachine EGL capture (`EnableEGLCapture 1`) intercepts gnome-shell DRM calls and blanks the CRTC.

**Fix**:

```bash
# Disable EGL capture
sed -i 's/^EnableEGLCapture 1/EnableEGLCapture 0/' /usr/NX/etc/node.cfg
sed -i 's/^EnableScreenBlankingEffect 1/EnableScreenBlankingEffect 0/' /usr/NX/etc/node.cfg
/etc/NX/nxserver --restart

# Unblank framebuffer
echo 0 > /sys/class/graphics/fb0/blank
xset -dpms; xset s off; xset s noblank

# Fix monitor name in lmcstart.sh
sed -i 's/Virtual-1/Virtual1/g' /home/holuser/desktop-hol/lmcstart.sh
```

---

## 9. apt update Hangs

**Root Cause**: `91.189.92.{22,23,24}` Ubuntu mirror IPs accept TCP but never respond. Squid picks one and hangs.

**Fix**:

```bash
# Block broken IPs on router
for ip in 91.189.92.22 91.189.92.23 91.189.92.24; do
    iptables -I OUTPUT 1 -d $ip -p tcp --dport 80 -j REJECT --reject-with tcp-reset
done

# Add squid timeouts
cat >> /etc/squid/squid.conf <<'EOF'
connect_timeout 5 seconds
read_timeout 30 seconds
forward_timeout 20 seconds
EOF
squid -k reconfigure
```

---

## 10. Lab Startup "Waiting for Proxy"

**Root Cause**: `gitpull.sh` tests proxy by curling GitHub through it, but squid can't reach GitHub until iptables/squid config is applied by `getrules.sh` on the router — which waits for `gitdone` from `gitpull.sh`. Circular dependency.

**Fix**: The proxy check was changed from `curl -x proxy:3128 https://github.com` (wrong) to `nc -z -w3 proxy 3128` (TCP port check). Remediation at attempt 31 restarts squid on router via SSH.

---

## Holodeck Environment Architecture

```
External Network
    |
    v
[holorouter (10.1.1.1 / 10.1.10.129)]
  - NAT, DNS (Technitium), Squid Proxy
  - K3s cluster: Vault, Keycloak, Authentik
    |
    v
[manager (holuser@manager)]
  - Lab startup scripts (/home/holuser/hol/)
  - creds.txt, config.ini
    |
    +-> [console] Ubuntu 24.04 Gnome Desktop
    |
    +-> [esx-01a..07a] ESXi 8.x hosts
         |
         +-> Management Domain (esx-01a..04a)
         |   - vc-mgmt-a (vCenter)
         |   - nsx-mgmt-01a (NSX Manager)
         |   - sddcmanager-a (SDDC Manager)
         |   - ops-a (VCF Operations)
         |
         +-> Workload Domain (esx-05a..07a)
             - vc-wld01-a (vCenter, SSO: wld.sso)
             - nsx-wld01-01a (NSX Manager)
             - auto-a (VCF Automation)
             - SupervisorControlPlaneVM
             - VSP Cluster (vsp-01a-*, 5 VMs):
               - Control Plane: 10.1.1.143 (VIP: 10.1.1.142)
               - Workers: 10.1.1.141, 10.1.1.144, 10.1.1.145, 10.1.1.147
               - vmsp-gateway:   10.1.1.131 = vsp-01a.site-a.vcf.lab
               - vmsp-gateway-0: 10.1.1.132 = instance-01a.site-a.vcf.lab
               - vmsp-gateway-1: 10.1.1.36  = fleet-01a.site-a.vcf.lab
```

---

## 11. VCF Management "Not Functional" (Fleet LCM Down)

**Symptom**: In VCF Operations UI (`ops-a` > Build > Lifecycle), VCF Management shows "not currently functional." Direct curl to `https://fleet-01a.site-a.vcf.lab/fleet-lcm/v1/components` returns HTTP 500.

**Diagnosis**:

*(Note to Agent: It is safer to write this script to a temporary `.py` file locally and execute it via SSH rather than dealing with nested quotes).*

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
VU='vmware-system-user'
VSP_CP='10.1.1.142'

# Check fleet-lcm pods
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new ${VU}@${VSP_CP} \
  "echo '${PASSWORD}' | sudo -S -i kubectl get pods -n vcf-fleet-lcm --no-headers 2>/dev/null"

# Check Postgres suspension state
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new ${VU}@${VSP_CP} \
  "echo '${PASSWORD}' | sudo -S -i kubectl get postgresinstances.database.vmsp.vmware.com \
   -A -o json 2>/dev/null" | python3 -c "
import json,sys
data = json.load(sys.stdin)
for item in data.get('items', []):
    ns = item['metadata']['namespace']
    name = item['metadata']['name']
    labels = item['metadata'].get('labels', {})
    suspended = labels.get('database.vmsp.vmware.com/suspended', 'false')
    print(f'{ns}/{name}: suspended={suspended}')
"
```

**Root Cause**: After a cold boot, the `VCFfinal.py` startup script may fail to unsuspend Postgres instances due to an SSH escaping bug with `kubectl custom-columns`. The `database.vmsp.vmware.com/suspended=true` label persists, and the Zalando operator keeps `numberOfInstances=0`.

**Fix**: See Section 6 (VCF Components Stopped After Cold Boot) for the unsuspend procedure.

---

## 12. VCF Automation API Shutdown Fails (HTTP 500 via suite-api)

**Symptom**: `POST /suite-api/internal/components/{id}?action=shutdown` on `ops-a` returns HTTP 500.

**Root Cause**: The VCF Operations `suite-api` internal proxy does **not** support lifecycle action endpoints (shutdown, startup). These actions return HTTP 500.

**Fix**: Use the fleet-lcm direct API on `fleet-01a.site-a.vcf.lab`. Requires JWT from VSP Identity Service. See the vcf-9-api skill for Fleet LCM JWT acquisition.

---

## 13. CSI Service Account Password Rotated After VCF Upgrade

**Symptom**: CSI controller on VCF Automation (`auto-a`) in CrashLoopBackOff. Logs show:
`failed to login to vc. err: ServerFaultCode: Cannot complete login due to an incorrect user name or password.`

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Check CSI controller logs
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl logs -n kube-system \
   -l app=vsphere-csi-controller -c vsphere-csi-controller --tail=10 2>/dev/null"
# Look for: "Cannot complete login due to an incorrect user name or password"

# Check what credentials CSI is using
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get secret vsphere-config-secret \
   -n kube-system -o jsonpath='{.data.csi-vsphere\.conf}' 2>/dev/null" | base64 -d

# Test the credentials against vCenter
curl -sk 'https://vc-mgmt-a.site-a.vcf.lab/rest/com/vmware/cis/session' \
  -u 'SERVICE_ACCOUNT:PASSWORD_FROM_SECRET' -X POST -o /dev/null -w '%{http_code}'
```

**Root Cause**: VCF upgrades (e.g., 9.0.0 to 9.0.1) can rotate the vSphere CSI service account password (`svc-vcfsp-vc-*@vsphere.local`) in vCenter SSO without updating the K8s secrets on the VCF Automation VM. The CSI controller then crashes because it can't authenticate to vCenter.

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# 1. Find the service account name from the CSI secret
CSI_USER=$(sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get secret vsphere-config-secret \
   -n kube-system -o jsonpath='{.data.csi-vsphere\.conf}' 2>/dev/null" | base64 -d | grep user | awk -F'"' '{print $2}')
CSI_ACCOUNT=$(echo "$CSI_USER" | cut -d@ -f1)

# 2. Reset password + set never-expire (see vcf-9-api skill Section 8 for dir-cli syntax)
# 3. Update both K8s secrets: vsphere-config-secret and vsphere-cloud-secret
# 4. Restart CSI pods:
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i bash -c '\
   kubectl delete pod -n kube-system -l app=vsphere-csi-controller --force; \
   kubectl delete pod -n kube-system -l app=vsphere-csi-node --force'"
```

---

## 14. SSH Host Key Mismatch After VCF Upgrade

**Symptom**: SSH to vCenters fails with `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!` and `Password authentication is disabled to avoid man-in-the-middle attacks.`

**Root Cause**: VCF upgrades (e.g., 9.0.0 to 9.0.1) regenerate vCenter SSH host keys. The old fingerprints in `/home/holuser/.ssh/known_hosts` no longer match, and SSH refuses to connect.

**Impact**: Lab startup scripts that SSH to vCenters to start `vapi-endpoint` and `trustmanagement` services will fail silently, causing cascading failures (CSI controller can't reach vCenter, volumes can't attach, pods can't start).

**Fix**:

```bash
# Remove stale host keys for all vCenters
ssh-keygen -f '/home/holuser/.ssh/known_hosts' -R 'vc-mgmt-a.site-a.vcf.lab'
ssh-keygen -f '/home/holuser/.ssh/known_hosts' -R 'vc-wld01-a.site-a.vcf.lab'

# Reconnect with auto-accept (future SSH commands should use this flag)
sshpass -p "$(cat /home/holuser/creds.txt)" ssh -o StrictHostKeyChecking=accept-new \
  root@vc-mgmt-a.site-a.vcf.lab "echo connected"
```

**Prevention**: All SSH commands in startup scripts should use `-o StrictHostKeyChecking=accept-new` instead of `-o StrictHostKeyChecking=no` to auto-accept new keys without prompting, or explicitly clear known_hosts entries before connecting.

---

## 15. VCF Automation Microservices All at 0 Replicas

**Symptom**: VCF Automation (`https://auto-a.site-a.vcf.lab`) returns "no healthy upstream" (HTTP 503) even though `tenant-manager-0` is Running and the VCD cell has completed startup. The cell.log shows `Cell startup completed`.

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Check deployments in prelude namespace
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get deployments -n prelude --no-headers 2>/dev/null" \
  | head -20
# Look for 0/0 in the READY column — all microservices at zero replicas

# Check vcfa-service-manager
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get deployment vcfa-service-manager \
   -n prelude --no-headers 2>/dev/null"
```

**Root Cause**: During shutdown, all VCF Automation deployments in the `prelude` namespace (api-gateway, cloud-automation-ui, authentication, catalog-service, etc. — approximately 50 deployments) are scaled to 0 replicas. The `vcfa-service-manager` is also scaled to 0. On restart, the infrastructure pods (rabbitmq, postgres, kafka, tenant-manager) come up, but the service-manager reconciles addons/service-accounts without scaling up the application deployments.

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# 1. Scale up vcfa-service-manager first
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl scale deployment vcfa-service-manager \
   -n prelude --replicas=1 2>/dev/null"

# 2. Scale up ALL zero-replica deployments in prelude namespace
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get deployments -n prelude -o json 2>/dev/null" \
  | python3 -c "
import json,sys
data = json.load(sys.stdin)
for d in data.get('items', []):
    name = d['metadata']['name']
    if d['spec'].get('replicas', 1) == 0:
        print(name)
" | while read dep; do
    sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
      "echo '${PASSWORD}' | sudo -S -i kubectl scale deployment ${dep} \
       -n prelude --replicas=1 2>/dev/null"
done

# 3. Wait ~5 minutes for all pods to start (50+ microservices)
# Monitor with:
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get deployments -n prelude --no-headers 2>/dev/null" \
  | grep -c '1/1'
# Should reach ~50 when fully started
```

**Key Details**:
- The VCF Automation K8s API is on `10.1.1.71` (VCF 9.0) or `10.1.1.72` (VCF 9.1), NOT `auto-a` (10.1.1.70) which is the istio ingress IP.
- The `tenant-manager-0` pod runs the VCD cell (Java process). It takes ~2 minutes to initialize. Check: `kubectl exec tenant-manager-0 -n prelude -- tail -5 /opt/vmware/vcloud-director/logs/cell.log`
- The `vco-app-0` StatefulSet (Orchestrator) also needs to be running. Check with `kubectl get statefulset -n prelude`.
- After scaling up, it takes ~5 minutes for all 50 microservices to reach 1/1 Running state.

---

## SDDC Manager Credential Troubleshooting (Sections 16-19)

Sections 16-19 all use the same SDDC Manager Bearer token and PostgreSQL access. See `vcf-9-api` skill Sections 9 and 11 for connection details.

```bash
# Shared setup for sections 16-19
PASSWORD=$(cat /home/holuser/creds.txt)
SDDC="sddcmanager-a.site-a.vcf.lab"
TOKEN=$(curl -sk -X POST "https://${SDDC}/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('accessToken',''))")
# PostgreSQL: SSH as vcf, PGPASSWORD from /root/.pgpass, psql -h 127.0.0.1 -U postgres -d platform
```

---

## 16. SDDC Manager Credentials All Showing UNKNOWN Status

**Symptom**: All 43 credentials show `UNKNOWN` in SDDC Manager UI.

**Diagnosis**: Use token above to query `/v1/credentials` and check `accountStatus` fields.

**Root Cause**: Compound failure: (1) SSO service accounts have wrong passwords, (2) stale resource locks, (3) resource statuses stuck in ERROR/ACTIVATING.

**Fix** (in order):
1. Reset SSO service account passwords — Section 19
2. Clear stale resource locks — Section 17
3. Fix resource statuses — Section 18
4. Run REMEDIATE one resource at a time (10-task concurrency limit)

---

## 17. SDDC Manager REMEDIATE Blocked by Stale Resource Locks

**Symptom**: REMEDIATE fails with "Unable to acquire resource level lock(s)."

**Root Cause**: Failed/timed-out credential operations leave entries in `platform.lock`. The `/v1/resource-locks` API is read-only — cannot DELETE via API.

**Fix** (using PostgreSQL access from shared setup above):

*(Note to Agent: Write these psql commands to a local `.sh` script and execute it via `ssh < script.sh` to avoid nested quote parsing errors).*

```bash
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vcf@sddcmanager-a.site-a.vcf.lab \
  "export PGPASSWORD='<from /root/.pgpass>'; \
   /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform \
   -c 'SELECT * FROM lock' \
   -c 'DELETE FROM lock'"
```

---

## 18. SDDC Manager REMEDIATE Fails — Resources Not Ready

**Symptom**: REMEDIATE fails with "Resources [...] are not available/ready."

**Diagnosis**: Check `/v1/hosts`, `/v1/nsxt-clusters`, `/v1/vcenters` for non-ACTIVE statuses.

**Root Cause**: Resource statuses stuck in `ERROR`/`ACTIVATING` in platform DB after failed ops or lab restarts.

**Fix** (using PostgreSQL access from shared setup):

*(Note to Agent: Write these psql commands to a local `.sh` script and execute it via `ssh < script.sh` to avoid nested quote parsing errors).*

```bash
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vcf@sddcmanager-a.site-a.vcf.lab \
  "export PGPASSWORD='<from /root/.pgpass>'; \
   /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform \
   -c \"UPDATE host SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE nsxt SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE vcenter SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE nsxt_edge_cluster SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE domain SET status = 'ACTIVE' WHERE status != 'ACTIVE'\""

# Then restart SDDC Manager services
# (requires root via expect/su): systemctl restart operationsmanager commonsvcs domainmanager
```

---

## 19. SDDC Manager REMEDIATE Fails — SSO Service Account Login Error

**Symptom**: REMEDIATE fails with "Cannot complete login due to incorrect credentials: ... svc-sddcmanager-a-vc-*"

**Root Cause**: SSO service account passwords in SDDC Manager DB don't match vCenter SSO. Happens after lab resets or DB restores.

**Important**: Fix service accounts FIRST — SDDC Manager uses them for ALL credential validation, even ESXi host passwords.

**Fix**: Query `/v1/credentials` to find `svc-*` accounts (IDs are unique per deployment), then reset via `dir-cli`:

```bash
# Management vCenter (SSO domain: vsphere.local)
for acct in svc-sddcmanager-a-vc-mgmt-a-XXXX svc-nsx-mgmt-a-vc-mgmt-a-XXXX; do
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
    "/usr/lib/vmware-vmafd/bin/dir-cli password reset \
     --account ${acct} --new '${PASSWORD}' \
     --login administrator@vsphere.local --password '${PASSWORD}'"
done

# Workload vCenter (SSO domain: wld.sso)
for acct in svc-sddcmanager-a-vc-wld01-a-XXXX svc-nsx-wld01-a-vc-wld01-a-XXXX; do
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-wld01-a.site-a.vcf.lab \
    "/usr/lib/vmware-vmafd/bin/dir-cli password reset \
     --account ${acct} --new '${PASSWORD}' \
     --login administrator@wld.sso --password '${PASSWORD}'"
done
# Then: systemctl restart operationsmanager commonsvcs domainmanager
```

---

## 20. Kube-apiserver Timeouts and Pods Stuck in Unknown State (VCF Automation)

**Symptom**: VCF Automation pods (in `prelude` namespace) are stuck in `Unknown` state. `kube-controller-manager` logs show `leaderelection lost` and `context deadline exceeded`. `kube-apiserver` logs show `Failed calling webhook ... connect: connection refused` for `capv-webhook-service` and `capi-webhook-service`.

**Diagnosis**:
```bash
# Check for Unknown pods
kubectl get pods -A | grep Unknown

# Check for webhook service endpoints (will be empty if failing)
kubectl get endpoints -n vmsp-platform capv-webhook-service

# Check kubelet or pod events for CNI errors
kubectl get events -n kube-system | grep "dial unix /var/run/antrea/cni.sock: connect: no such file or directory"
```

**Root Cause**: The Antrea CNI socket (`/var/run/antrea/cni.sock`) becomes unavailable or containerd loses track of it. This prevents new pod sandboxes from being created, causing `capv-webhook-service` and `capi-webhook-service` to fail their readiness probes. Because these webhooks are unavailable, `kube-apiserver` times out on API requests, which causes `kube-controller-manager` to lose its leader election lease. Existing pods may be marked as `Unknown` and replicasets won't recreate them because they still exist in the API.

**Fix**:
```bash
# 1. Restart containerd and kubelet to restore the CNI socket
systemctl restart containerd kubelet

# 2. Wait for CAPI/CAPV controllers to become ready
kubectl rollout status deployment capv-controller-manager -n vmsp-platform --timeout=60s

# 3. Force delete all pods in Unknown state so replicasets can recreate them
kubectl get pods -A --no-headers | grep Unknown | awk '{print $1, $2}' | while read ns pod; do
  kubectl delete pod $pod -n $ns --force --grace-period=0
done
```

## 21. vCenter Browser Warning Banner Cannot Be Reliably Removed (VCF 9.1)

**Symptom**: The vCenter Client login page (`https://vc-*/ui/login`) shows a yellow "unsupported browser" banner when using Firefox on Linux.

**Attempted Fixes (All Failed)**:

1. **Patching `websso.js` inside `libvmidentity-sts-server.jar`** — Replacing `return false` with `return true` in `isBrowserSupportedVC()` and rebuilding the JAR. Even with `jar uf`, clearing Tomcat `work`/`temp` directories, restarting STS (`vmware-stsd`), and full vCenter reboots, the banner persists.

2. **Patching deployed/exploded `websso.js` files** — Using `find -exec sed` under `/usr/lib/vmware-sso/` to patch any extracted copies. Files were either not found as standalone or patches did not survive service restarts.

3. **Patching compiled JSP source** — Editing `unpentry_jsp.java` in `/var/lib/sso/workDir/` to change `visibility: hidden` to `display: none` and neutralizing the `!isBrowserSupportedVC()` condition, then deleting the `.class` file to force recompilation. Did not take effect.

4. **CSS injection into JSP templates** — Adding `<style>.browser-validation-banner{display:none!important}</style>` into the compiled JSP source. Did not take effect.

**Root Cause**: In VCF 9.1, the STS (Tomcat) login page rendering pipeline involves multiple layers of caching and JSP compilation that defeat simple file patching. The JAR contents may be regenerated by a pre-start script, and Tomcat's JSP compilation cache has additional mechanisms beyond the `work`/`temp` directories. The Envoy reverse proxy in front of the UI may also cache responses.

**Status**: UNSOLVABLE via file patching in VCF 9.1. The browser warning is cosmetic only and does not affect functionality. Accept the banner or investigate VMware-supported methods for customizing the login page.

---

## 22. CCI 503 "No Healthy Upstream" at Startup

**Symptom**: CCI URL (`https://auto-a.site-a.vcf.lab/cci/kubernetes/apis/project.cci.vmware.com/v1alpha2/projects`) returns HTTP 503 "no healthy upstream" instead of the expected 401 JSON response (`{"kind":"Status","message":"Unauthorized"}`). Lab startup retries this check for up to 30 minutes.

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Test CCI URL
curl -sk 'https://auto-a.site-a.vcf.lab/cci/kubernetes/apis/project.cci.vmware.com/v1alpha2/projects'
# Returns: "no healthy upstream" (503) instead of 401 JSON with "kind":"Status"

# Check CCI dependency chain (route: /cci/kubernetes -> ccs-k3s service)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@auto-a.site-a.vcf.lab \
  "echo '${PASSWORD}' | sudo -S -i kubectl get pods -n prelude --no-headers 2>/dev/null" \
  2>/dev/null | grep -E 'ccs-k3s|ccs-infra|ccs-gateway|provisioning-service|project-service|ebs|rabbitmq'

# Check RabbitMQ (root of dependency chain)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@auto-a.site-a.vcf.lab \
  "echo '${PASSWORD}' | sudo -S -i kubectl logs -n prelude rabbitmq-ha-0 --tail=5 2>/dev/null" 2>/dev/null
# Look for: "Cookie file /var/lib/rabbitmq/.erlang.cookie must be accessible by owner only"

# If RabbitMQ is ok, check provisioning-service for deadlock
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@auto-a.site-a.vcf.lab \
  "echo '${PASSWORD}' | sudo -S -i kubectl exec -n prelude -l app=provisioning-service-app -- \
   jcmd 1 Thread.print 2>/dev/null" 2>/dev/null | grep -A 5 '"main"'
# Look for: WAITING (parking) on PrometheusExemplarsAutoConfiguration
```

**Root Cause**: Two compound failures block the CCI dependency chain:

1. **RabbitMQ `.erlang.cookie` permissions** (0660 instead of 0400): The `fsGroup: 200` pod security context causes Kubernetes to set group-read/write on all PVC files, including `.erlang.cookie`. Erlang requires this file to be owner-only readable (0400/0600). RabbitMQ crashes in CrashLoopBackOff, which blocks `ebs-app` → `project-service` → `provisioning-service` → `ccs-infra-eas` → `ccs-k3s`.

2. **provisioning-service Spring Boot deadlock**: Even after RabbitMQ is fixed, `provisioning-service-app` hits a deterministic deadlock during initialization. The `main` thread holds a `ConcurrentHashMap` lock and waits for a `ReentrantLock` in `PrometheusExemplarsAutoConfiguration`, while the `ebs-1` thread holds that `ReentrantLock` and waits for the `ConcurrentHashMap`. The service never starts.

**Dependency chain**: `ccs-k3s-app` → `ccs-infra-eas` (waits for `provisioning-service`) → `provisioning-service` (waits for `project-service`) → `project-service` (waits for `ebs-service`) → `ebs-app` (waits for `rabbitmq-ha`)

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Fix 1: RabbitMQ .erlang.cookie permissions
# Run a temporary pod as root to chmod the cookie file on the PVC
cat > /tmp/rabbitmq-fix-pod.yaml << 'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: rabbitmq-fix
  namespace: prelude
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 0
  containers:
  - name: fix
    image: registry.vmsp-platform.svc.cluster.local:5000/images/prelude/rabbitmq:9.0.0.0.24701403
    command: ["sh", "-c", "chmod 400 /var/lib/rabbitmq/.erlang.cookie && ls -la /var/lib/rabbitmq/.erlang.cookie && echo FIXED"]
    volumeMounts:
    - mountPath: /var/lib/rabbitmq
      name: rabbit-pvc
  volumes:
  - name: rabbit-pvc
    persistentVolumeClaim:
      claimName: rabbit-pvc-rabbitmq-ha-0
EOF

sshpass -p "${PASSWORD}" scp -o StrictHostKeyChecking=accept-new \
  /tmp/rabbitmq-fix-pod.yaml vmware-system-user@auto-a.site-a.vcf.lab:/tmp/rabbitmq-fix-pod.yaml

sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@auto-a.site-a.vcf.lab \
  "echo '${PASSWORD}' | sudo -S -i kubectl apply -f /tmp/rabbitmq-fix-pod.yaml 2>/dev/null"
sleep 15
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@auto-a.site-a.vcf.lab \
  "echo '${PASSWORD}' | sudo -S -i kubectl logs -n prelude rabbitmq-fix 2>/dev/null && \
   echo '${PASSWORD}' | sudo -S -i kubectl delete pod rabbitmq-fix -n prelude 2>/dev/null && \
   echo '${PASSWORD}' | sudo -S -i kubectl delete pod rabbitmq-ha-0 -n prelude 2>/dev/null"

# Fix 2: Disable Prometheus exemplar tracing to prevent provisioning-service deadlock
# Append -Dmanagement.prometheus.metrics.export.exemplars.enabled=false to JAVA_OPTS
# (See /tmp/fix-provisioning-deadlock.py for automated approach)
```

**Key Details**:
- The RabbitMQ fix survives pod restarts because it modifies the PVC, but a full PVC recreation would re-introduce the issue.
- The provisioning-service deadlock fix requires patching the deployment's JAVA_OPTS to append `-Dmanagement.prometheus.metrics.export.exemplars.enabled=false`. This disables the `PrometheusExemplarsAutoConfiguration` that causes the circular lock.
- After both fixes, the full CCI chain takes ~5-8 minutes to cascade through all init container dependency checks.

---

## 23. Supervisor Control Plane VMs Powered Off After Shutdown (VCF 9.0.x)

**Symptom**: Supervisor reports `config_status: CONFIGURING, kubernetes_status: ERROR` after a clean lab shutdown/restart. The CCI URL returns 503 "no healthy upstream". The `/home/holuser/hol/Tools/check_fix_wcp.sh` script cannot reach the SCP VMs.

**Diagnosis**:

```bash
# Check SCP VM power state via vCenter API
PASSWORD=$(cat /home/holuser/creds.txt)
SESSION=$(curl -sk -X POST "https://vc-wld01-a.site-a.vcf.lab/api/session" \
  -u "administrator@wld.sso:${PASSWORD}" | tr -d '"')
curl -sk -H "vmware-api-session-id: ${SESSION}" \
  "https://vc-wld01-a.site-a.vcf.lab/api/vcenter/namespace-management/clusters" | python3 -m json.tool

# Check via pyVmomi
python3 -c "
from pyVim.connect import SmartConnect
from pyVmomi import vim
import ssl
ctx = ssl._create_unverified_context()
password = open('/home/holuser/creds.txt').read().strip()
si = SmartConnect(host='vc-wld01-a.site-a.vcf.lab', user='administrator@wld.sso', pwd=password, sslContext=ctx)
for vm in si.RetrieveContent().viewManager.CreateContainerView(si.RetrieveContent().rootFolder, [vim.VirtualMachine], True).view:
    if 'SupervisorControlPlane' in vm.name:
        print(f'{vm.name}: power={vm.runtime.powerState}, host={vm.runtime.host.name}')
"
```

**Root Cause**: On VCF 9.0.x, Supervisor Control Plane VMs are EAM-managed and reside on the WLD cluster ESXi hosts (esx-05a through esx-07a). During clean shutdown, these VMs are powered off. However:
1. The startup scripts (`VCFfinal.py`) only *verified* Supervisor status; they did not contain logic to power on SCP VMs.
2. Attempting to power on SCP VMs through `vc-wld01-a` with `administrator@wld.sso` fails with `vim.fault.NoPermission` because vCenter's EAM agent restricts direct VM management of its managed VMs.
3. Restarting the WCP service or spherelet on ESXi hosts does not trigger EAM to power on the VMs.

**Fix**: Connect directly to the individual ESXi hosts that host the SCP VMs and issue `PowerOnVM_Task()` as `root`, bypassing vCenter's EAM restrictions:

```bash
python3 -c "
from pyVim.connect import SmartConnect
from pyVmomi import vim
import ssl

ctx = ssl._create_unverified_context()
password = open('/home/holuser/creds.txt').read().strip()

for host in ['esx-05a.site-a.vcf.lab', 'esx-06a.site-a.vcf.lab', 'esx-07a.site-a.vcf.lab']:
    si = SmartConnect(host=host, user='root', pwd=password, sslContext=ctx)
    content = si.RetrieveContent()
    for vm in content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True).view:
        if 'SupervisorControlPlane' in vm.name and vm.runtime.powerState != 'poweredOn':
            vm.PowerOnVM_Task()
            print(f'{vm.name} on {host}: PowerOn submitted')
"
```

After power-on, the Supervisor takes approximately 3-5 minutes to transition from CONFIGURING/ERROR through CONFIGURING/WARNING to RUNNING/READY.

**Permanent Fix**: `/home/holuser/hol/Startup/VCFfinal.py` Task 2a2 now automatically detects powered-off SCP VMs, tries vCenter first, and falls back to direct ESXi host connections when `NoPermission` is encountered. This is safe for VCF 9.1 (where SCP VMs may not exist on WLD ESXi hosts).

**Key Details**:
- This issue only affects VCF 9.0.x. On VCF 9.1, the Supervisor runs on the VSP cluster which is handled separately.
- The SCP VMs are on esx-05a, esx-06a, esx-07a in the default Holodeck layout.
- The K8s VIP for the Supervisor is 10.1.1.85; individual SCP VMs are at 10.1.1.86-88.
- After SCP VMs boot, `hypercrypt` and `kubelet` services must both reach `active` state before the K8s API becomes available.
- The `decryptK8Pwd.py` script on the WLD vCenter provides the SCP root password needed for SSH.

---

## 24. VCF Automation VIP Drops After Boot (kube-vip + istio-ingressgateway)

**Symptom**: `auto-a.site-a.vcf.lab` (10.1.1.70) responds to pings for ~30 seconds after boot, then becomes unreachable ("Destination Host Unreachable" or "No route to host"). SSH and HTTPS fail. However, 10.1.1.71 (the actual VM eth0 address) remains reachable.

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Verify 10.1.1.71 responds but 10.1.1.70 does not
ping -c 2 -W 2 10.1.1.71  # OK
ping -c 2 -W 2 10.1.1.70  # Unreachable

# Check if VIP is present on eth0
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i ip addr show eth0 | grep 10.1.1.70"
# If empty: VIP is missing

# Check kube-vip logs
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i crictl logs --tail 20 \$(crictl ps --name kube-vip -q | head -1) 2>&1"
# Look for: "[VIP] Releasing the Virtual IP [10.1.1.70]"
# Look for: "lost leadership, restarting kube-vip"
```

**Root Cause Chain**:

1. After cold boot, the Antrea CNI agent takes time to initialize. During this window, ClusterIP service routing is broken (`antrea-agent` readiness probe returns HTTP 500).
2. The `registry.vmsp-platform.svc.cluster.local:5000` ClusterIP is unreachable even though the registry pod is running on its pod IP.
3. The `istio-ingressgateway` DaemonSet pod in `istio-ingress` namespace needs to pull the `proxyv2:1.24.0` image from the registry. With the ClusterIP broken, this fails with `ImagePullBackOff`.
4. kube-vip manages the 10.1.1.70 VIP for both the control plane (kube-apiserver) and the LoadBalancer service (istio-ingressgateway). When it detects the istio-ingressgateway endpoint has been removed (pod not running), it **releases the VIP** and logs: `"[endpoints] existing [198.18.0.24] has been removed, no remaining endpoints for leaderElection"`.
5. With the VIP gone, kube-vip can no longer reach the kube-apiserver (configured at `10.1.1.70:6443` in `super-admin.conf`), loses its leader lease, and crashes with `"lost leadership, restarting kube-vip"`.
6. The kube-scheduler may also lose its leader lease during this instability, leaving it with stale RBAC caches. New pods get stuck in `Pending` with no scheduling events.
7. This creates a self-reinforcing crash loop: kube-vip restarts → briefly adds VIP → sees no istio endpoints → releases VIP → loses lease → crashes.

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# 1. Manually add the VIP to break the crash loop
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i ip addr add 10.1.1.70/32 dev eth0 2>/dev/null; \
   ip addr show eth0 | grep 10.1.1.70"

# 2. Wait for kube-vip to stabilize and kubectl to work
sleep 15

# 3. Restart containerd+kubelet to clear stuck schedulers and stale containers
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i bash -c 'systemctl restart containerd && sleep 3 && systemctl restart kubelet'"

# 4. Re-add VIP (restart may have cleared it)
sleep 15
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i bash -c 'ip addr show eth0 | grep 10.1.1.70 || ip addr add 10.1.1.70/32 dev eth0'"

# 5. Wait for antrea-agent readiness (ClusterIP routing)
sleep 30

# 6. Delete any ImagePullBackOff pods so they retry with working ClusterIP
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i bash -c 'export KUBECONFIG=/etc/kubernetes/super-admin.conf; \
   kubectl get pods -A --no-headers 2>/dev/null | grep ImagePullBackOff | while read ns pod rest; do \
     kubectl delete pod \$pod -n \$ns --force --grace-period=0 2>/dev/null; done'"
```

**Key Details**:
- kube-vip uses the `/etc/kube-vip.hosts` file which maps `kubernetes` to `127.0.0.1` for its own API server access. However, the `super-admin.conf` kubeconfig points to `10.1.1.70:6443`. When kube-vip uses this kubeconfig, losing the VIP means it can't reach the API server.
- The container restart count on kube-vip (typically >100) is diagnostic — it indicates this crash loop has been happening since boot.
- The Antrea agent readiness probe returns HTTP 500 while it's connecting to the antrea-controller. Once both antrea-agent containers are Ready, ClusterIP routing works.
- After the istio-ingressgateway pod starts, kube-vip will acquire the VIP lease properly and the manually-added VIP will be managed by kube-vip going forward.
- `/home/holuser/hol/Startup/VCFfinal.py` Task 4b now automatically detects and remediates this condition.

---

## 25. Harbor Unreachable / Supervisor DNS Broken After Cold Boot

**Symptom**: Harbor URL `https://harbor-01a.site-a.vcf.lab` times out. Harbor pods (`harbor-core`, `harbor-nginx`, `harbor-jobservice`) crash with `dial tcp: lookup harbor-core: i/o timeout`. DNS resolution fails for all vSphere Pods on the Supervisor.

**Diagnosis**:

```bash
# 1. Check kube-dns endpoint - should point to CoreDNS pod IPs (172.16.200.x)
sshpass -p "$K8S_PWD" ssh root@$SCP_VIP \
  "kubectl get endpoints -n kube-system kube-dns -o jsonpath='{.subsets[0].addresses[*].ip}'"

# If it shows 10.1.0.4 (or any 10.1.0.x LB IP), that's the problem

# 2. Check NSX DLB pool on ESXi - confirms asymmetric routing
sshpass -p "$PASSWORD" ssh root@esx-05a.site-a.vcf.lab \
  "nsxcli -c 'get load-balancer pool <pool-uuid>'"
# Pool member should show CoreDNS pod IPs, not the LB IP

# 3. Verify CoreDNS pods are running
sshpass -p "$K8S_PWD" ssh root@$SCP_VIP \
  "kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide"
```

**Root Cause**:

The `kube-dns` K8s Service (ClusterIP `10.96.0.10`) has its Endpoint pointing to `10.1.0.4`, which is the `kube-dns-lb` LoadBalancer external IP. The NSX Distributed Load Balancer (DLB) on ESXi intercepts ClusterIP traffic and forwards it to the endpoint IP. When that endpoint is a routed IP (`10.1.0.4`) rather than an overlay IP (`172.16.200.x`), the DNS query goes through the T1 Service Router but the response returns directly to the pod via the overlay, bypassing the DLB's connection tracking. The DLB drops the orphaned session and the pod gets no DNS response.

This configuration is the default in VCF 9.0.x Supervisor deployments but normally works because the NSX T1 SR handles the NAT correctly. After an ungraceful shutdown, the SR on the STANDBY edge node may fail to properly initialize its interfaces, or the routing path becomes inconsistent, causing the asymmetric path to fail.

**Fix**:

```bash
# Patch kube-dns endpoint to point to CoreDNS pod IPs
K8S_PWD=$(sshpass -p "$PASSWORD" ssh root@vc-wld01-a.site-a.vcf.lab \
  "python3 /usr/lib/vmware-wcp/decryptK8Pwd.py" | grep PWD | awk '{print $2}')

COREDNS_IPS=$(sshpass -p "$K8S_PWD" ssh root@$SCP_VIP \
  "kubectl get pods -n kube-system -l k8s-app=kube-dns \
   -o jsonpath='{.items[*].status.podIP}'")

# Build and apply the corrected endpoint
python3 -c "
import json
ips = '${COREDNS_IPS}'.split()
ep = json.dumps({
    'apiVersion': 'v1', 'kind': 'Endpoints',
    'metadata': {'name': 'kube-dns', 'namespace': 'kube-system'},
    'subsets': [{'addresses': [{'ip': ip} for ip in ips],
                 'ports': [{'name': 'dns', 'port': 53, 'protocol': 'UDP'},
                           {'name': 'dns-tcp', 'port': 53, 'protocol': 'TCP'}]}]
})
print(ep)
" | sshpass -p "$K8S_PWD" ssh root@$SCP_VIP "kubectl apply -f -"
```

**Key Details**:
- The DLB pool on ESXi (`nsxcli -c 'get load-balancer pool <uuid>'`) mirrors the K8s endpoint. Changing the endpoint triggers NCP to update the NSX DLB pool within ~10 seconds.
- `kube-dns-lb` (LoadBalancer service) has its own separate NSX LB pool that correctly points to CoreDNS pod IPs — only the ClusterIP `kube-dns` endpoint is misconfigured.
- The STANDBY SR on an NSX Edge having `Op_state: down` interfaces is **normal** for `ACTIVE_STANDBY` mode. The ACTIVE SR on the other edge handles all traffic.
- A clean shutdown/restart of the NSX edges (via the shutdown script phases 5-7 followed by VCF.py startup) resolves this by ensuring the SR HA state is properly initialized.
- `/home/holuser/hol/Startup/VCFfinal.py` Task 2c2 now automatically detects and fixes this condition during startup.

---

## 26. VSP Cluster Image Pull Failures After Cold Boot (Service CIDR Not in NO_PROXY)

**Symptom**: All VCF component pods on the VSP cluster (salt, raas, fleet-lcm, sddc-lcm, vidb, vodap, ops-logs, telemetry, etc.) stuck in `ImagePullBackOff` or `ErrImagePull` after lab startup. Fleet LCM API returns HTTP 500. VCF Ops for Logs returns HTTP 500. Lab startup fails at URL checks with 30-minute timeout.

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Check for image pull failures
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.142 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get pods -A --no-headers 2>/dev/null" 2>/dev/null \
  | grep -E 'ImagePullBackOff|ErrImagePull'

# Verify: works without proxy
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.142 \
  "echo '${PASSWORD}' | sudo -S -i curl -sk --noproxy '*' https://198.18.128.16:5000/v2/_catalog 2>&1" 2>/dev/null | head -1

# Check service CIDR
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.142 \
  "echo '${PASSWORD}' | sudo -S -i kubectl cluster-info dump 2>/dev/null" 2>/dev/null | grep service-cluster-ip-range
# Shows: --service-cluster-ip-range=198.18.128.0/17
```

**Root Cause**: The VSP cluster uses a non-standard Kubernetes service CIDR (`198.18.128.0/17`) instead of the typical `10.96.0.0/12`. Containerd resolves `registry.vmsp-platform.svc.cluster.local` to ClusterIP `198.18.128.16` and then makes the HTTPS request. When containerd has `HTTPS_PROXY=http://10.1.1.1:3128` configured but `NO_PROXY` does not include `198.18.0.0/16`, the request is proxied through the holorouter Squid proxy. Squid rejects CONNECT to private IPs with `ERR_ACCESS_DENIED` (HTTP 403 "Forbidden").

The `confighol-9.1.py` proxy configuration step included hostname `registry.vmsp-platform.svc.cluster.local` in NO_PROXY, but containerd bypasses hostname-based NO_PROXY matching after resolving the hostname to an IP. The IP `198.18.128.16` must also be covered by CIDR in NO_PROXY.

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
NEW_NO_PROXY="localhost,127.0.0.1,10.1.1.0/24,10.96.0.0/12,172.16.0.0/12,198.18.0.0/16,.site-a.vcf.lab,.svc,.cluster.local,.svc.cluster.local,10.1.0.0/24,registry.vmsp-platform.svc.cluster.local"

# Get all VSP node IPs
VSP_NODES=$(sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.142 \
  "echo '${PASSWORD}' | sudo -S -i kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type==\"InternalIP\")].address}{\" \"}{end}'" 2>/dev/null)

# Create proxy config locally then SCP to each node
cat > /tmp/containerd-proxy.conf << EOF
[Service]
Environment="HTTP_PROXY=http://10.1.1.1:3128"
Environment="HTTPS_PROXY=http://10.1.1.1:3128"
Environment="NO_PROXY=${NEW_NO_PROXY}"
EOF

for NODE_IP in $VSP_NODES; do
  sshpass -p "${PASSWORD}" scp -o StrictHostKeyChecking=accept-new \
    /tmp/containerd-proxy.conf vmware-system-user@${NODE_IP}:/tmp/containerd-proxy.conf
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@${NODE_IP} \
    "echo '${PASSWORD}' | sudo -S cp /tmp/containerd-proxy.conf /etc/systemd/system/containerd.service.d/http-proxy.conf && \
     echo '${PASSWORD}' | sudo -S systemctl daemon-reload && \
     echo '${PASSWORD}' | sudo -S systemctl restart containerd && \
     sleep 3 && echo '${PASSWORD}' | sudo -S systemctl restart kubelet"
done

# Force-delete all stuck pods (SCP script to avoid SSH escaping issues)
cat > /tmp/fix-pods.sh << 'SCRIPT'
#!/bin/bash
export KUBECONFIG=/etc/kubernetes/super-admin.conf
kubectl get pods -A --no-headers 2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull|CrashLoopBackOff' | while IFS= read -r line; do
    ns=$(echo "$line" | awk '{print $1}')
    pod=$(echo "$line" | awk '{print $2}')
    kubectl delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null &
done
wait
SCRIPT
sshpass -p "${PASSWORD}" scp -o StrictHostKeyChecking=accept-new \
  /tmp/fix-pods.sh vmware-system-user@10.1.1.142:/tmp/fix-pods.sh
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.142 \
  "echo '${PASSWORD}' | sudo -S -i bash /tmp/fix-pods.sh"
```

**Permanent Fix**: `/home/holuser/hol/Tools/confighol-9.1.py` Step 9 updated to include `198.18.0.0/16` in the NO_PROXY list for all VSP node proxy configurations.

**Key Details**:
- The VSP cluster service CIDR `198.18.128.0/17` is unique to VCF 9.1 and not covered by the standard `10.96.0.0/12` that Kubernetes typically uses.
- The `containerd` hosts.toml at `/etc/containerd/certs.d/registry.vmsp-platform.svc.cluster.local:5000/hosts.toml` specifies `host."https://198.18.128.16:5000"` with a CA cert, confirming the ClusterIP is used for image pulls.
- Writing files to VSP nodes via `bash -c` heredoc through SSH+sudo layers often fails silently (produces 0-byte files). Use `scp` to transfer config files to `/tmp` and then `cp` to the final location.
- After fixing containerd proxy, pods in `ImagePullBackOff` have very long backoff timers (5+ minutes after hours of failures). Force-deleting them ensures immediate retry.

---

> **Certificate-related issues 27-30**: For the complete certificate management guide (Vault PKI, MSADCS proxy, PKCS#7 ordering, SDDC Manager workflows), see the consolidated `vcf-certs` skill.

## 27. VCF Operations Fleet Management Rejects MSADCS Proxy as Microsoft CA

**Symptom**: VCF Operations Fleet Management UI shows "Failed to update certificate authorities" when configuring the MSADCS Proxy (`https://ca.vcf.lab/certsrv`) as a Microsoft CA. SDDC Manager API (`PUT /v1/certificate-authorities`) succeeds, but the UI reports failure.

**Diagnosis**:

```bash
# Check certsrv-proxy logs on the holorouter
ssh root@router kubectl logs -l app=certsrv-proxy --tail=50

# Look for 301 redirects from VCF Operations IP (10.1.1.30)
# Example log line showing the problem:
# [2026-03-11 15:00:01] INFO 10.1.1.30 "GET /certsrv HTTP/1.1" 301 -
```

VCF Operations (`ops-a.site-a.vcf.lab`, IP `10.1.1.30`) sends `GET /certsrv` (without trailing slash) to validate the CA. If the proxy returns `301 Moved Permanently` redirecting to `/certsrv/`, the VCF Operations Java HTTP client does not follow the redirect and treats it as a validation failure. SDDC Manager (`10.1.1.5`) validates via `GET /certsrv/certrqxt.asp` which returns 200 directly — that's why SDDC Manager succeeds while VCF Operations fails.

**Root Cause**: The proxy's `do_GET` handler was redirecting `/certsrv` to `/certsrv/` with a 301. VCF Operations' Java HTTP client does not follow 301 redirects during CA validation.

**Fix**: Modify the proxy to serve the home page content directly for `/certsrv` and `/certsrv/default.asp` instead of redirecting:

```python
# In do_GET handler, serve content directly for these paths:
if path in ('/certsrv', '/certsrv/default.asp', ''):
    self._send_html(200, CERTSRV_HOME_HTML)
    return
```

After fixing, redeploy the proxy script and restart the pod:

```bash
sshpass -p "${PASSWORD}" scp -o StrictHostKeyChecking=accept-new \
  certsrv_proxy-beta.py root@router:/root/certsrv-proxy/certsrv_proxy-beta.py
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@router \
  "kubectl delete pod -l app=certsrv-proxy --force --grace-period=0"
```

Verify fix:

```bash
# Must return 200, not 301
curl -sk -u 'admin:password' -o /dev/null -w '%{http_code}' https://ca.vcf.lab/certsrv
```

## 28. certsrv-proxy "Address Already in Use" After Pod Restart

**Symptom**: After force-deleting the certsrv-proxy pod (`kubectl delete pod --force --grace-period=0`), the new pod enters CrashLoopBackOff with `OSError: [Errno 98] Address already in use`.

**Diagnosis**:

```bash
# Check pod logs
ssh root@router kubectl logs -l app=certsrv-proxy

# Check what process is holding port 443
ssh root@router ss -tlnp | grep :443
```

**Root Cause**: When using `hostNetwork: true` and `--force --grace-period=0`, the old Python process on the host may not be terminated before the new pod tries to bind to the same port. The DaemonSet's hostNetwork mode means the container process binds directly to the host's network namespace.

**Fix**: Manually kill the orphaned process, then delete the pod again:

```bash
# Find the PID holding port 443
ssh root@router ss -tlnp | grep :443
# Kill it
ssh root@router kill -9 <pid>
# Delete the pod to trigger a clean restart
ssh root@router kubectl delete pod -l app=certsrv-proxy --force --grace-period=0
```

**Prevention**: Use a graceful pod deletion (without `--force --grace-period=0`) when possible, or add a `preStop` hook to the container spec that kills the Python process cleanly.

## 29. SDDC Manager "Public Key Mismatch" During Certificate Installation

> **Note**: Fleet-managed cert replacement stuck at NOT_STARTED is covered in `vcf-certs` skill Section 9 and `vcf-9-api` skill Section 15 (orchestrator types).

**Symptom**: SDDC Manager certificate installation fails with `"Public key in CSR and server certificate are not matching."` The log shows `SslCertValidator` comparing the CSR against `CN=vcf.lab Root Authority` (the CA cert) instead of the issued leaf cert.

**Diagnosis**:

```bash
# Check operationsmanager log on SDDC Manager
ssh vcf@sddcmanager-a.site-a.vcf.lab
grep "Public key mismatch" /var/log/vmware/vcf/operationsmanager/operationsmanager.log | tail -5
# Look for: "Public key mismatch in CSR (CN=...) and provided certificate (CN=vcf.lab Root Authority)"
```

**Root Cause**: Python's `cryptography` library's `pkcs7.serialize_certificates()` uses strict DER encoding, which requires SET OF elements to be sorted by their encoded byte values. This reorders certificates inside the PKCS#7 structure regardless of the order passed to the function. SDDC Manager's `CertificateOperationOrchestratorImpl.generateCertificate()` method calls `getCertificateChain()` which returns `X509Certificate[]` from parsing the PKCS#7, then takes `certs[0]` as the signed certificate and `certs[1..]` as the CA chain. When DER sorting puts the CA cert first, SDDC Manager treats it as the signed cert and the public key comparison fails.

**Fix**: Replace `serialize_certificates()` with a custom `build_ordered_pkcs7()` function that manually constructs the PKCS#7 ASN.1 structure, preserving certificate insertion order. The certificates field uses tag `0xA0` (context [0] constructed) and the leaf cert DER bytes must appear before the CA cert DER bytes.

```python
def build_ordered_pkcs7(cert_der_list: list[bytes]) -> bytes:
    """Build PKCS#7 SignedData preserving certificate order."""
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

**Verification**: Test the PKCS#7 output with Java's `CertificateFactory.generateCertificates()` on the SDDC Manager to confirm `certs[0]` is the leaf cert.

## 30. NSX ReTrust Failure After vCenter Certificate Replacement

**Symptom**: SDDC Manager reports `CERTIFICATE_RETRUST_OPERATION_FAILED` after successfully replacing the vCenter certificate. NSX Manager returns HTTP 400 with error `"Failed to import the trusted root certificate for compute manager"`.

**Diagnosis**:

```bash
ssh vcf@sddcmanager-a.site-a.vcf.lab
grep "Retrust for NSX failed" /var/log/vmware/vcf/operationsmanager/operationsmanager.log | tail -3
```

**Root Cause**: After vCenter certificate replacement, SDDC Manager performs ReTrust operations against NSX Managers to update their trust stores. This can fail transiently when vCenter services are still restarting or when the new certificate hasn't propagated to all endpoints. The actual certificate replacement and installation on vCenter are successful — only the post-install NSX trust update fails.

**Fix**: Usually resolves on a retry. From the SDDC Manager UI, re-select the vCenter and click "Install Certificates" again. The ReTrust operation will retry. If the certificate dates already show the new values, the cert is installed correctly and only the trust relationship needs refreshing.

## 31. NSX Compute Manager DOWN After vCenter Certificate Replacement (Double-Cert Bug)

**Symptom**: NSX Managers show compute managers as `connection_status: DOWN` and `registration_status: REGISTERED_WITH_ERRORS` with error code `7059`: "Unable to connect to the compute manager as its trusted root certificate cannot be found." PUT to re-register returns HTTP 400 error `90348`: "Failed to import the trusted root certificate for compute manager."

**Diagnosis**:

```bash
# Check compute manager status
curl -sk -u "admin:$PASSWORD" \
  "https://nsx-mgmt-01a.site-a.vcf.lab/api/v1/fabric/compute-managers/$CM_ID/status" | python3 -m json.tool

# Check NSX error logs for the real cause
ssh root@nsx-mgmt-01a.site-a.vcf.lab \
  'grep "MP2179\|90348\|multiple certificates" /var/log/proton/nsxapi.log | tail -5'

# Check vCenter TRUSTED_ROOTS for double-cert entries
ssh root@vc-mgmt-a.site-a.vcf.lab \
  'for alias in $(/usr/lib/vmware-vmafd/bin/vecs-cli entry list --store TRUSTED_ROOTS | grep Alias | awk -F":\t" "{print \$2}" | xargs); do
     count=$(/usr/lib/vmware-vmafd/bin/vecs-cli entry getcert --store TRUSTED_ROOTS --alias "$alias" | grep -c "BEGIN CERTIFICATE")
     subject=$(/usr/lib/vmware-vmafd/bin/vecs-cli entry getcert --store TRUSTED_ROOTS --alias "$alias" | openssl x509 -noout -subject 2>/dev/null)
     echo "$alias: $count cert(s) - $subject"
   done'
```

**Root Cause**: `dir-cli trustedcert publish` silently appends a duplicate PEM into the same vmdir entry when called twice with the same certificate, creating a multi-cert PEM under a single alias. When NSX tries to import trusted roots from vCenter during compute manager registration, `TrustStoreServiceImpl` rejects the PEM with error `MP2179`: "This certificate PEM contains multiple certificates." This cascades to error `90348` and the compute manager goes DOWN.

**Fix**:

```bash
# 1. Download the clean single-cert PEM
VAULT_CA=$(curl -sk http://10.1.1.1:32000/v1/pki/ca/pem)

# 2. Unpublish the corrupted double-cert entry from vmdir
ssh root@vc-mgmt-a.site-a.vcf.lab "
  echo '$VAULT_CA' > /tmp/vault-ca-single.pem
  /usr/lib/vmware-vmafd/bin/dir-cli trustedcert unpublish \
    --cert /tmp/vault-ca-single.pem \
    --login administrator@vsphere.local --password '$PASSWORD'
  /usr/lib/vmware-vmafd/bin/vecs-cli force-refresh
"

# 3. Republish as a clean single-cert entry
ssh root@vc-mgmt-a.site-a.vcf.lab "
  /usr/lib/vmware-vmafd/bin/dir-cli trustedcert publish \
    --cert /tmp/vault-ca-single.pem \
    --login administrator@vsphere.local --password '$PASSWORD'
  /usr/lib/vmware-vmafd/bin/vecs-cli force-refresh
  rm -f /tmp/vault-ca-single.pem
"

# 4. Import Vault CA into NSX trust store (if not already present)
curl -sk -u "admin:$PASSWORD" -X POST \
  'https://nsx-mgmt-01a.site-a.vcf.lab/api/v1/trust-management/certificates?action=import' \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"vcf.lab Root Authority","pem_encoded":"'"$VAULT_CA"'"}'

# 5. Re-register compute manager with new thumbprint
THUMB=$(echo | openssl s_client -connect vc-mgmt-a.site-a.vcf.lab:443 2>/dev/null \
  | openssl x509 -fingerprint -sha256 -noout | sed 's/sha256 Fingerprint=//')
# GET compute manager, modify credential.thumbprint, PUT back
```

**Prevention**: Always check `dir-cli trustedcert list | grep -c 'vcf.lab Root Authority'` before calling `dir-cli trustedcert publish`. The `/home/holuser/hol/Tools/confighol-9.1.py` v2.11+ includes this guard.

**Automated Fix**: `/home/holuser/hol/Tools/cert-replacement.py` (in `Tools/`) includes `NSXComputeManagerFixer` which automatically runs after vCenter/NSX certificate replacements. It fixes double-cert entries, ensures Vault CA is in NSX trust stores, and re-registers compute managers with the new thumbprint. Must also fix WLD vCenter (SSO admin: `administrator@wld.sso`) — both vCenters can have the double-cert issue.

**Key detail for re-registration PUT**: Strip read-only fields (`_create_time`, `_create_user`, `_last_modified_time`, `_last_modified_user`, `_protection`, `_system_owned`, `certificate`, `origin_properties`) from the GET response before PUTting back. Include full credential block: `credential_type`, `username`, `password`, and the new `thumbprint`.

## 32. VCF Operations Fleet Management Cannot Replace SDDC Manager Certificate (API Compatibility)

**Symptom**: Using VCF Operations Fleet Management UI → selecting SDDC Manager → "Generate CSRs" (works) → "Replace With Configured CA Certificate" fails immediately with:
```
CertificateGenericException: Certificate task REPLACE_CERTIFICATE for sddcmanager-a.site-a.vcf.lab has failed.
Error message: Unable to generate Certificate using an existing CSR for sddcmanager-a.site-a.vcf.lab
Caught exception: java.lang.reflect.UndeclaredThrowableException
```

**Diagnosis**:
```bash
# Check SDDC Manager logs for the real error
ssh vcf@sddcmanager-a.site-a.vcf.lab \
  "grep 'Cannot deserialize.*ResourceCertificateSpec\|REST_INVALID_API_INPUT' \
   /var/log/vmware/vcf/operationsmanager/operationsmanager.log | tail -5"
# Output: Cannot deserialize value of type ArrayList<ResourceCertificateSpec> from Object value
```

**Root Cause**: VCF Operations' `ReplaceCertificateTask` calls `PUT /v1/domains/{id}/resource-certificates` with `resources` as a JSON object `{...}` but SDDC Manager 9.x expects an array `[...]`. SDDC Manager returns HTTP 400, which the LCM plugin wraps as `UndeclaredThrowableException`. This is a version compatibility bug between VCF Operations Fleet LCM plugin and SDDC Manager 9.x API.

**Fix**: Bypass VCF Operations and replace the certificate directly via the SDDC Manager API. The correct body for the install endpoint is an **array** of `ResourceCertificateSpec`:

```bash
# Full replacement workflow (assumes CSR already generated via SDDC Manager UI or API)
PASSWORD=$(cat /home/holuser/creds.txt)
DOMAIN_ID="6e8d8359-f0c2-4dd4-a23d-6613945351b2"

TOKEN=$(curl -sk -X POST "https://sddcmanager-a.site-a.vcf.lab/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"$PASSWORD\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['accessToken'])")

# 1. Get existing CSR
CSR=$(curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://sddcmanager-a.site-a.vcf.lab/v1/domains/$DOMAIN_ID/csrs" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['elements'][0]['csrEncodedContent'])")

# 2. Normalize CSR (fix spacing) and sign with Vault sign-verbatim
# (see vcf-certs SKILL.md Section 6 for normalize_csr_pem() and sign-verbatim usage)

# 3. Build ordered PKCS#7 (leaf first, CA second) - see vcf-certs SKILL.md Section 7

# 4. Install via SDDC Manager API — body MUST be a JSON array
# *(Note to Agent: If modifying the curl payload above, construct the JSON safely using Python `requests` or `jq` instead of struggling with bash strings).*
curl -sk -X PUT "https://sddcmanager-a.site-a.vcf.lab/v1/domains/$DOMAIN_ID/resource-certificates" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "[{
    \"caType\": \"MICROSOFT\",
    \"certificateChain\": \"<PKCS7_PEM_HERE>\",
    \"resourceFqdn\": \"sddcmanager-a.site-a.vcf.lab\",
    \"resourceType\": \"SDDC_MANAGER\"
  }]"
# Returns HTTP 202 with {"id":"...","status":"IN_PROGRESS"}
# Poll GET /v1/tasks/{id} until status=Successful (~3-5 minutes)
```

**Note**: SDDC Manager restarts services during install — the HTTPS connection may drop temporarily. The `status: Successful` confirmation is visible once SDDC Manager comes back up. The new cert issuer changes from `VMCA` to `CN=vcf.lab Root Authority`.
