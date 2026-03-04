---
name: vcf-troubleshooting
description: Diagnose and resolve common issues in VMware Cloud Foundation (VCF) 9.0 and 9.1 Holodeck nested virtualization lab environments. Covers Supervisor configuration failures, WCP certificate issues, K8s node NotReady flapping, VCF Automation volume attachment stalls, content library sync failures, VCF component shutdown/startup, vCenter service autostart failures, console black screen, proxy/DNS issues, CSI password rotation after upgrade, SSH host key mismatches, VCF Automation microservice scaling, Fleet LCM failures, VCF Automation API shutdown issues, and SDDC Manager credential remediation failures. Use when troubleshooting VCF, Supervisor stuck, WCP errors, Kubernetes NotReady, VCF Automation down, content library sync, lab startup failures, black console screen, proxy issues, CSI controller crash, SSH host key changed, VCFA 503 errors, SDDC Manager passwords, credential UNKNOWN status, resource locks, or password remediation failures.
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

---

## 7. vCenter Services Not Autostarting

**Symptom**: `vapi-endpoint` and/or `trustmanagement` STOPPED despite `Starttype: AUTOMATIC`.

**Root Cause**: vmon's startup data file is missing/corrupt. vmon logs show `[ReadSvcSubStartupData] No startup information from <service>`.

**Fix**: Check and start during lab startup scripts. Already handled by `check_wcp_vcenter.sh`, `VCFfinal.py`, and `vSphere.py` (TASK 7) in the startup codebase.

**Important**: SSH and bash shell must be enabled on vCenter before these checks can run. The `vSphere.py` module (TASK 6b) now automatically enables SSH and shell via the vCenter REST API (`PUT /api/appliance/access/ssh` and `PUT /api/appliance/access/shell`) before checking autostart services, so this works even on fresh labs where `confighol` has not been run.

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

# 2. Reset the password in vCenter SSO (use dir-cli, NOT the REST API)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli password reset --account ${CSI_ACCOUNT} \
   --new 'NEW_PASSWORD_HERE' --login administrator@vsphere.local --password '${PASSWORD}'"

# 3. Also set password to never expire
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
  "/usr/lib/vmware-vmafd/bin/dir-cli user modify --account ${CSI_ACCOUNT} \
   --password-never-expires --login administrator@vsphere.local --password '${PASSWORD}'"

# 4. Update the K8s secrets on auto-a with the new password
# Generate new config content, base64 encode, and patch both secrets:
#   - vsphere-config-secret (csi-vsphere.conf key)
#   - vsphere-cloud-secret (vc-mgmt-a.site-a.vcf.lab.password key)

# 5. Restart CSI pods
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new vmware-system-user@10.1.1.71 \
  "echo '${PASSWORD}' | sudo -S -i bash -c '\
   kubectl delete pod -n kube-system -l app=vsphere-csi-controller --force; \
   kubectl delete pod -n kube-system -l app=vsphere-csi-node --force'"
```

**Key Detail**: `dir-cli` on vCenter is at `/usr/lib/vmware-vmafd/bin/dir-cli`. The `user modify` subcommand does NOT support changing passwords — you must use `password reset`. The `user modify --password-never-expires` only controls expiration policy.

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

## 16. SDDC Manager Credentials All Showing UNKNOWN Status

**Symptom**: SDDC Manager UI (Inventory > Passwords) shows all 43 credentials with `UNKNOWN` account status. The GUI displays password warning/error icons for multiple resources.

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
SDDC="sddcmanager-a.site-a.vcf.lab"

TOKEN=$(curl -sk -X POST "https://${SDDC}/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin@local\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('accessToken',''))")

curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/credentials" | python3 -c "
import json,sys
creds = json.load(sys.stdin).get('elements', [])
for c in creds:
    print(f\"{c['resource']['resourceName']:40s} {c['username']:50s} {c.get('accountStatus','?')}\")
print(f'Total: {len(creds)} credentials')
"
```

**Root Cause**: This is a compound failure with multiple interdependent causes:
1. SDDC Manager's SSO service accounts (`svc-sddcmanager-a-vc-*`) have incorrect passwords in vCenter SSO, so SDDC Manager cannot authenticate to vCenters to validate any credentials.
2. Previous failed credential operations left stale resource locks in the database, blocking new operations.
3. Resource statuses are stuck in `ERROR` or `ACTIVATING`, preventing credential validation.

**Fix** (must be done in this order):

1. **Reset vCenter SSO service account passwords** — see Section 19
2. **Clear stale resource locks** — see Section 17
3. **Fix resource statuses** — see Section 18
4. **Run REMEDIATE operations** one resource at a time (not all at once to avoid the 10-task concurrency limit)

---

## 17. SDDC Manager REMEDIATE Blocked by Stale Resource Locks

**Symptom**: `PATCH /v1/credentials` with `operationType: REMEDIATE` fails with "Unable to acquire resource level lock(s)."

**Diagnosis**:

```bash
# Check for existing locks (API endpoint is read-only)
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/resource-locks"
```

**Root Cause**: Failed, cancelled, or timed-out credential operations leave lock entries in the `platform.lock` database table. The `/v1/resource-locks` API does NOT support DELETE — locks can only be cleared via direct database access.

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vcf@sddcmanager-a.site-a.vcf.lab \
  "export PGPASSWORD='iHk0JKypNFrR9C5iOI2PmBmUCfSbdrjFxaGoxEEFz3w='; \
   /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform \
   -c 'SELECT * FROM lock' \
   -c 'DELETE FROM lock'"
```

**Note**: The PostgreSQL password is found in `/root/.pgpass` on SDDC Manager. It may differ per deployment. SSH as `vcf` user can run `psql` directly without needing root.

---

## 18. SDDC Manager REMEDIATE Fails — Resources Not Ready

**Symptom**: `PATCH /v1/credentials` with `operationType: REMEDIATE` fails with "Resources [esx-01a.site-a.vcf.lab] are not available/ready."

**Diagnosis**:

```bash
# Check resource statuses via API
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/hosts" | python3 -c "
import json,sys
for h in json.load(sys.stdin).get('elements', []):
    print(f\"{h['fqdn']:40s} status={h['status']}\")
"

# Similarly check NSX and vCenter
curl -sk -H "Authorization: Bearer ${TOKEN}" "https://${SDDC}/v1/nsxt-clusters"
curl -sk -H "Authorization: Bearer ${TOKEN}" "https://${SDDC}/v1/vcenters"
```

**Root Cause**: SDDC Manager's internal database tracks resource statuses. After failed credential operations or lab restarts, resources can get stuck in `ERROR`, `ACTIVATING`, or other non-`ACTIVE` states. The credential validation pre-check rejects operations on non-ACTIVE resources, even if the actual components are reachable.

**Fix**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new -T \
  vcf@sddcmanager-a.site-a.vcf.lab \
  "export PGPASSWORD='iHk0JKypNFrR9C5iOI2PmBmUCfSbdrjFxaGoxEEFz3w='; \
   /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform \
   -c \"UPDATE host SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE nsxt SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE vcenter SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE nsxt_edge_cluster SET status = 'ACTIVE' WHERE status != 'ACTIVE'\" \
   -c \"UPDATE domain SET status = 'ACTIVE' WHERE status != 'ACTIVE'\""
```

After updating, restart SDDC Manager services:

```bash
# Requires root (via expect or su) on SDDC Manager
systemctl restart operationsmanager commonsvcs domainmanager
```

---

## 19. SDDC Manager REMEDIATE Fails — SSO Service Account Login Error

**Symptom**: `PATCH /v1/credentials` with `operationType: REMEDIATE` fails with "Cannot complete login due to incorrect credentials: ... svc-sddcmanager-a-vc-mgmt-a-9382@vsphere.local"

**Diagnosis**: SDDC Manager uses vCenter SSO service accounts to authenticate to vCenters during credential validation. These service accounts are managed in vCenter's SSO directory, NOT in SDDC Manager's credential store.

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# List SDDC Manager's service accounts
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${SDDC}/v1/credentials" | python3 -c "
import json,sys
for el in json.load(sys.stdin).get('elements', []):
    if el['username'].startswith('svc-'):
        print(f\"{el['resource']['resourceName']:40s} {el['username']}\")
"
```

**Root Cause**: The service account passwords stored in SDDC Manager's database no longer match what vCenter SSO expects. This can happen after lab resets, SDDC Manager DB restores, or manual password changes.

**Fix**: Reset each service account's password in vCenter SSO using `dir-cli`, then update SDDC Manager's credential via REMEDIATE.

```bash
PASSWORD=$(cat /home/holuser/creds.txt)

# Management vCenter service accounts (SSO domain: vsphere.local)
for acct in svc-sddcmanager-a-vc-mgmt-a-9382 svc-nsx-mgmt-a-vc-mgmt-a-5529; do
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-mgmt-a.site-a.vcf.lab \
    "/usr/lib/vmware-vmafd/bin/dir-cli password reset \
     --account ${acct} --new '${PASSWORD}' \
     --login administrator@vsphere.local --password '${PASSWORD}'"
done

# Workload vCenter service accounts (SSO domain: wld.sso)
for acct in svc-sddcmanager-a-vc-wld01-a-7530 svc-nsx-wld01-a-vc-wld01-a-1894; do
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=accept-new root@vc-wld01-a.site-a.vcf.lab \
    "/usr/lib/vmware-vmafd/bin/dir-cli password reset \
     --account ${acct} --new '${PASSWORD}' \
     --login administrator@wld.sso --password '${PASSWORD}'"
done
```

**Note**: Service account IDs (e.g., `-9382`, `-7530`, `-5529`, `-1894`) are unique per deployment. Always query `/v1/credentials` to discover the exact names. After resetting in vCenter SSO, restart SDDC Manager services (`systemctl restart operationsmanager commonsvcs domainmanager`) before retrying credential operations.

**Important**: Fix service accounts BEFORE attempting REMEDIATE on any other credentials. SDDC Manager uses these accounts as part of its validation pipeline for ALL credential operations — even ESXi host password checks flow through the vCenter SSO service account.

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

**Symptom**: Supervisor reports `config_status: CONFIGURING, kubernetes_status: ERROR` after a clean lab shutdown/restart. The CCI URL returns 503 "no healthy upstream". The `check_fix_wcp.sh` script cannot reach the SCP VMs.

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

**Permanent Fix**: `VCFfinal.py` Task 2a2 now automatically detects powered-off SCP VMs, tries vCenter first, and falls back to direct ESXi host connections when `NoPermission` is encountered. This is safe for VCF 9.1 (where SCP VMs may not exist on WLD ESXi hosts).

**Key Details**:
- This issue only affects VCF 9.0.x. On VCF 9.1, the Supervisor runs on the VSP cluster which is handled separately.
- The SCP VMs are on esx-05a, esx-06a, esx-07a in the default Holodeck layout.
- The K8s VIP for the Supervisor is 10.1.1.85; individual SCP VMs are at 10.1.1.86-88.
- After SCP VMs boot, `hypercrypt` and `kubelet` services must both reach `active` state before the K8s API becomes available.
- The `decryptK8Pwd.py` script on the WLD vCenter provides the SCP root password needed for SSH.
