---
name: vcf-troubleshooting
description: Diagnose and resolve common issues in VMware Cloud Foundation (VCF) 9.0 and 9.1 Holodeck nested virtualization lab environments. Covers Supervisor configuration failures, WCP certificate issues, K8s node NotReady flapping, VCF Automation volume attachment stalls, content library sync failures, VCF component shutdown/startup, vCenter service autostart failures, console black screen, and proxy/DNS issues. Use when troubleshooting VCF, Supervisor stuck, WCP errors, Kubernetes NotReady, VCF Automation down, content library sync, lab startup failures, black console screen, or proxy issues.
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

**Fix**: Check and start during lab startup scripts. Already handled by `check_wcp_vcenter.sh` and `VCFfinal.py` in the startup codebase.

```bash
# Check and start on each vCenter
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

## 11. VCF Management "Not Functional" (Fleet LCM Down)

**Symptom**: In VCF Operations UI (`ops-a` → Build → Lifecycle), VCF Management shows "not currently functional." Direct curl to `https://fleet-01a.site-a.vcf.lab/fleet-lcm/v1/components` returns HTTP 500.

**Diagnosis**:

```bash
PASSWORD=$(cat /home/holuser/creds.txt)
VU='vmware-system-user'
VSP_CP='10.1.1.142'

# Check fleet-lcm pods
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
  "echo '${PASSWORD}' | sudo -S -i kubectl get pods -n vcf-fleet-lcm --no-headers 2>/dev/null"
# Look for: CrashLoopBackOff

# Check fleet-lcm pod logs
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
  "echo '${PASSWORD}' | sudo -S -i kubectl logs -n vcf-fleet-lcm \
   \$(kubectl get pods -n vcf-fleet-lcm -o name | head -1) --tail=20 2>/dev/null"
# Look for: "Connection to vcf-fleet-lcm-db.vcf-fleet-lcm:5432 refused"

# Check Postgres suspension state
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
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

**Root Cause**: After a cold boot, the `VCFfinal.py` startup script may fail to unsuspend Postgres instances due to an SSH escaping bug with `kubectl custom-columns` and dotted label keys. The `database.vmsp.vmware.com/suspended=true` label persists, and the Zalando operator keeps `numberOfInstances=0`, so the Postgres pods never start. Fleet-lcm pods then crash because their database is unreachable.

**Fix**:

```bash
# Unsuspend Postgres + restore numberOfInstances
for ns in salt-raas vcf-fleet-lcm vcf-sddc-lcm vidb-external; do
    sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
      "echo '${PASSWORD}' | sudo -S -i kubectl label postgresinstances.database.vmsp.vmware.com \
       --all -n ${ns} database.vmsp.vmware.com/suspended- 2>/dev/null"

    sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
      "echo '${PASSWORD}' | sudo -S -i kubectl get postgresqls.acid.zalan.do -n ${ns} \
       -o name 2>/dev/null" | while read pg; do
        sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
          "echo '${PASSWORD}' | sudo -S -i kubectl patch ${pg} -n ${ns} --type=merge \
           -p '{\"spec\":{\"numberOfInstances\":1}}' 2>/dev/null"
    done
done

# Delete crashed pods to force restart (they'll reconnect to the now-running Postgres)
sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no ${VU}@${VSP_CP} \
  "echo '${PASSWORD}' | sudo -S -i kubectl delete pods -n vcf-fleet-lcm \
   --field-selector=status.phase!=Running 2>/dev/null"
```

**Prevention**: The `VCFfinal.py` startup script (v4.2+) now uses `kubectl get ... -o json` parsed locally in Python instead of `custom-columns` via SSH, avoiding the escaping issue. It also has a fallback that unconditionally unsuspends known namespaces if JSON parsing fails.

---

## 12. VCF Automation API Shutdown Fails (HTTP 500 via suite-api)

**Symptom**: `fleet.py:shutdown_products_v91()` returns HTTP 500 when calling `POST /suite-api/internal/components/{id}?action=shutdown` on `ops-a`.

**Root Cause**: The VCF Operations `suite-api` internal proxy passes through GET/list requests to the fleet-lcm backend but does **not** support lifecycle action endpoints (shutdown, startup). These actions return HTTP 500.

**Fix**: Use the fleet-lcm direct API on `fleet-01a.site-a.vcf.lab` instead. This requires a JWT from the VSP Identity Service — see the vcf-9-api skill, section 8 (Fleet LCM Direct API).

**Implementation**: `VCFshutdown.py` (v2.4+) Phase 1 now tries the fleet-lcm direct API first, falling back to the suite-api proxy (which works for component listing but not actions), then VCF 9.0 legacy, then Phase 1b VM power-off.

The key functions in `fleet.py`:
- `get_fleet_lcm_jwt()` — handles IAM credential discovery + JWT acquisition
- `shutdown_products_fleet_lcm()` — orchestrates component shutdown with task polling
- `shutdown_component_fleet_lcm()` — triggers shutdown for a single component
- `wait_for_fleet_lcm_task()` — polls task status until SUCCEEDED/FAILED/timeout

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
