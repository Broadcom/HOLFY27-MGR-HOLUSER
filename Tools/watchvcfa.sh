#!/bin/bash
# Author: Burke Azbill
# Version: 1.3
# Date: 2026-03-03
# Script to watch VCF Automation appliance for issues and remediate them
# This script:
# 1. Checks if the VCFA host is reachable via SSH
# 2. Checks if the K8s API endpoint is reachable
# 3. Checks if the containerd is running
# 4. Checks if the kube-scheduler is running
# 5. Checks if the seaweedfs-master-0 pod is running
# 6. Checks if the volume attachments are stuck in deletion state
# 7. Checks if the vCenter vAPI endpoint is running
# 8. Checks if the vsphere-csi-controller is running
# 9. Checks if the CSI controller is running
# 10. Fixes RabbitMQ .erlang.cookie permissions (fsGroup breaks Erlang requirement)
# 11. Fixes provisioning-service Spring Boot deadlock (PrometheusExemplarsAutoConfiguration)

#REBOOTS=0
SEAWEEDPOD="."
CONATAINERDREADY="."
KUBESCHEDULER="."
STUCKVOLATTACH="."
#POSTGRES="."
#CCSK3SAPP="."
# For some reason, outputing to LOGFILE only shows up in the manager log, so attempting to log to both files...
LOGFILE="/home/holuser/hol/labstartup.log"
CONSOLELOG="/lmchol/hol/labstartup.log"
CREDS_FILE="/home/holuser/creds.txt"
VCFA_USER="vmware-system-user"

# VCFA hostname - DNS resolves to the correct IP in both VCF 9.0.x and 9.1.x
VCFA_FQDN="auto-a.site-a.vcf.lab"
VCFA_HOST=""
K8S_API=""

# Detect whether sudo requires a password (VCF 9.1.x) or not (VCF 9.0.x)
SUDO_NEEDS_PASSWORD=false

detect_sudo_mode() {
  # Try passwordless sudo first (VCF 9.0.x)
  if sshpass -f "${CREDS_FILE}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
      "${VCFA_USER}@${VCFA_HOST}" "sudo -n true" >/dev/null 2>&1; then
    SUDO_NEEDS_PASSWORD=false
  else
    SUDO_NEEDS_PASSWORD=true
  fi
}

# Helper function for SSH commands to VCFA
# Handles both passwordless sudo (VCF 9.0.x) and password-required sudo (VCF 9.1.x)
vcfa_ssh() {
  if [ "${SUDO_NEEDS_PASSWORD}" = true ]; then
    local password
    password=$(cat "${CREDS_FILE}")
    sshpass -f "${CREDS_FILE}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
      "${VCFA_USER}@${VCFA_HOST}" "echo '${password}' | sudo -S -i bash -c '$1'" 2>&1 \
      | grep -v "Welcome to Photon" | grep -v "\[sudo\] password for"
  else
    sshpass -f "${CREDS_FILE}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
      "${VCFA_USER}@${VCFA_HOST}" "sudo -i bash -c '$1'" 2>&1 \
      | grep -v "Welcome to Photon"
  fi
}

# Resolve the VCFA FQDN to an IP and verify SSH is reachable
detect_vcfa_host() {
  # Resolve FQDN to IP address
  local resolved_ip
  resolved_ip=$(getent hosts "${VCFA_FQDN}" 2>/dev/null | awk '{print $1}' | head -1)

  if [ -z "${resolved_ip}" ]; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> WARNING: Cannot resolve ${VCFA_FQDN} via DNS" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
    return 1
  fi

  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> ${VCFA_FQDN} resolves to ${resolved_ip}" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"

  # Verify SSH is reachable on the resolved IP
  if nc -z -w 5 "${resolved_ip}" 22 >/dev/null 2>&1; then
    VCFA_HOST="${resolved_ip}"
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Detected VCFA host: ${VCFA_HOST} (SSH reachable)" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
    return 0
  fi

  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> WARNING: ${VCFA_FQDN} (${resolved_ip}) SSH not reachable" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  return 1
}

# Auto-detect the K8s API endpoint from the kubeconfig on the VCFA host
# VCF 9.0.x: typically https://10.1.1.71:6443
# VCF 9.1.x: typically https://10.1.1.72:6443
detect_k8s_api() {
  local server
  server=$(vcfa_ssh 'cat /etc/kubernetes/super-admin.conf 2>/dev/null || cat /etc/kubernetes/admin.conf 2>/dev/null' | grep "server:" | head -1 | sed 's/.*server: *//')
  if [ -n "${server}" ]; then
    K8S_API="${server}"
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Detected K8s API endpoint: ${K8S_API}" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  else
    # Fallback: use the VCFA_HOST IP
    K8S_API="https://${VCFA_HOST}:6443"
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Could not detect K8s API from kubeconfig, using fallback: ${K8S_API}" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  fi
}

# Now try to remediate Automation
echo "[$(date +"%Y-%m-%d %H:%M:%S")] -------------WATCHVCFA RUN START-------------" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> VCFA Watcher started"  | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"

# Auto-detect VCFA host, sudo mode, and K8s API endpoint
detect_vcfa_host
if [ -z "${VCFA_HOST}" ]; then
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> ERROR: Cannot find VCFA host, exiting" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  exit 1
fi

# while [[ $REBOOTS -lt 3 || "$POSTGRES" != "2/2" ]]; do
  CNT=0
  while ! $(sshpass -f "${CREDS_FILE}" ssh -q -o ConnectTimeout=5 "${VCFA_USER}@${VCFA_HOST}" exit); do
    sleep 30
	  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Waiting for VCFA to come Online" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    ((CNT++))
    if [ $CNT -eq 10 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> VCFA Online check check tried 10 times (5m), continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  # Detect sudo mode and K8s API after VCFA is online
  detect_sudo_mode
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Sudo requires password: ${SUDO_NEEDS_PASSWORD}" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  detect_k8s_api

  ###### Containerd check/fix ######
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking containerd on VCFA for Ready,SchedulingDisabled..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$CONATAINERDREADY" != "" ]]; do
    ((CNT++))
    CONATAINERDREADY=$(vcfa_ssh "kubectl -s ${K8S_API} get nodes" | grep "Ready,SchedulingDisabled" | awk '{print $2}')
   
    if [ "$CONATAINERDREADY" == "Ready,SchedulingDisabled" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stale containerd found, restarting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "systemctl restart containerd" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      vcfa_ssh "kubectl -s ${K8S_API} get nodes" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 2 ]; then
      NODENAME=$(vcfa_ssh "kubectl -s ${K8S_API} get nodes" | grep "Ready,SchedulingDisabled" | awk '{print $1}')
      vcfa_ssh "kubectl -s ${K8S_API} uncordon ${NODENAME}"
    fi
    if [ $CNT -eq 3 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> containerd check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

###### Unknown Pods check/fix ######
  # Pods can get stuck in Unknown state if the node becomes unreachable or CNI fails.
  # When this happens, replicasets think the pods still exist and won't recreate them.
  # Force deleting them allows the controllers to recreate them.
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking for pods in Unknown state..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  UNKNOWN_PODS=$(vcfa_ssh 'kubectl get pods -A --no-headers | grep Unknown | wc -l')
  if [ "$UNKNOWN_PODS" -gt 0 ]; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Found ${UNKNOWN_PODS} pods in Unknown state, force deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    vcfa_ssh 'cat <<EOF > /tmp/del_unknown.py
import subprocess
out = subprocess.check_output(["kubectl", "get", "pods", "-A", "--no-headers"]).decode("utf-8")
for line in out.splitlines():
    if "Unknown" in line:
        parts = line.split()
        ns = parts[0]
        pod = parts[1]
        print(f"Deleting {ns}/{pod}")
        subprocess.call(["kubectl", "delete", "pod", pod, "-n", ns, "--force", "--grace-period=0"])
EOF
python3 /tmp/del_unknown.py' | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  fi

  ###### CAPI/CAPV Webhook/CNI check/fix ######
  # If antrea CNI fails to set up pod sandboxes, capv-webhook-service and capi-webhook-service
  # pods will fail readiness probes. This causes kube-apiserver to timeout when communicating
  # with them, which in turn causes kube-controller-manager to lose leader election.
  # The result is that NO new pods can be scheduled or created.
  # Restarting containerd and kubelet fixes the CNI socket issue.
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking CAPI/CAPV controller health..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CAPV_READY=$(vcfa_ssh 'kubectl get deployment capv-controller-manager -n vmsp-platform -o jsonpath="{.status.readyReplicas}"')
  CAPI_READY=$(vcfa_ssh 'kubectl get deployment capi-controller-manager -n vmsp-platform -o jsonpath="{.status.readyReplicas}"')
  
  if [ -z "$CAPV_READY" ] || [ "$CAPV_READY" == "0" ] || [ -z "$CAPI_READY" ] || [ "$CAPI_READY" == "0" ]; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CAPI/CAPV controllers are not ready. This indicates CNI/webhook failure. Restarting containerd and kubelet..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    vcfa_ssh "systemctl restart containerd kubelet" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    sleep 30
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Waiting for CAPI/CAPV controllers to recover..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    vcfa_ssh 'kubectl rollout status deployment capv-controller-manager -n vmsp-platform --timeout=60s' | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  fi

###### kube-scheduler check/fix ######
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking kube-scheduler on VCFA for 0/1 Running..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$KUBESCHEDULER" != "" ]]; do
    ((CNT++))
    sleep 30
    KUBESCHEDULER=$(vcfa_ssh "kubectl -n kube-system -s ${K8S_API} get pods" | grep "kube-scheduler" | grep "0/1" | awk '{print $2}')
   
    if [ "$KUBESCHEDULER" == "0/1" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stale kube-scheduler found, restarting containerd..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "systemctl restart containerd" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 120
      vcfa_ssh "kubectl -n kube-system -s ${K8S_API} get pods" | grep "kube-scheduler" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    if [ $CNT -eq 3 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> kube-scheduler check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  # seaweedfs-master-0 is stale in the captured vAppTemplate. When Automation starts, sometimes this pod is NOT
  #   cleaned up properly, resulting in the prevention of many other pods failint go start.
  #  Check this pod and delete it if it is old:
  # Delete the seaweedfs-master-0 pod if age over 1 hour
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking seaweedfs-master-0 pod from VCFA if older than 1 hour..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$SEAWEEDPOD" != "" ]]; do
    ((CNT++))
    SEAWEEDPOD=$(vcfa_ssh "kubectl -n vmsp-platform -s ${K8S_API} get pods seaweedfs-master-0 -o json" | \
    jq -r '. | select(.metadata.creationTimestamp | fromdateiso8601 < (now - 3600)) | .metadata.name ')
    
    if [ "$SEAWEEDPOD" == "seaweedfs-master-0" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stale seaweedfs-master-0 pod found, deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "kubectl -n vmsp-platform -s ${K8S_API} delete pod seaweedfs-master-0" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      vcfa_ssh "kubectl -n vmsp-platform -s ${K8S_API} get pods | grep seaweedfs" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> seaweedfs-master-0 check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  ###### Stuck Volume Attachments check/fix ######
  # Volume attachments can get stuck in deletion state with finalizers preventing cleanup.
  # This happens when pods are terminated but the CSI controller can't clean up the attachments.
  # The result is new pods getting stuck in ContainerCreating with "volume attachment is being deleted" errors.
  #
  # Root cause chain observed in production:
  #   1. CSI controller crashes (CrashLoopBackOff, e.g. CRD init error or vCenter unavailable)
  #   2. CSI controller sidecar containers (csi-attacher, csi-provisioner, etc.) can't connect to
  #      the CSI socket and also crash
  #   3. Volume attachments from the previous boot have deletionTimestamp set but the
  #      external-attacher finalizer can't be removed because the CSI controller is down
  #   4. New pods (vcfapostgres-0, rabbitmq-ha-0) get stuck in ContainerCreating/Init with
  #      "volume attachment is being deleted" errors
  #   5. Without postgres and rabbitmq, VCF Automation UI shows "no healthy upstream"
  #
  # Fix order:
  #   a. Remove finalizers from stuck volume attachments (unblocks volume mounts)
  #   b. Fix CSI controller if unhealthy (force-delete and wait for restart)
  #   c. Delete prelude pods stuck in ContainerCreating/Init to force fresh mount attempts
  STUCK_VA_FIXED=false
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking for stuck volume attachments with deletionTimestamp..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$STUCKVOLATTACH" != "" ]]; do
    ((CNT++))
    # Find volume attachments that have deletionTimestamp set (stuck in deletion)
    STUCKVOLATTACH=$(vcfa_ssh 'kubectl get volumeattachments -o json' | \
      jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name' | head -1)
    
    if [ -n "$STUCKVOLATTACH" ] && [ "$STUCKVOLATTACH" != "" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stuck volume attachment found: ${STUCKVOLATTACH}, removing finalizer..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      # Get all stuck volume attachments and remove their finalizers
      STUCK_VAS=$(vcfa_ssh 'kubectl get volumeattachments -o json' | \
        jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name')
      for VA in $STUCK_VAS; do
        echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Removing finalizer from volume attachment: ${VA}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        vcfa_ssh "kubectl patch volumeattachment ${VA} -p '{\"metadata\":{\"finalizers\":null}}' --type=merge" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      done
      STUCK_VA_FIXED=true
      sleep 5
      # Verify the stuck attachments are gone
      vcfa_ssh 'kubectl get volumeattachments' | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stuck volume attachment check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  ###### vCenter vAPI Endpoint check/fix ######
  # The vsphere-csi-controller requires vCenter's REST API (vmware-vapi-endpoint) to be running.
  # If this service is stopped, the CSI controller will crash with 503 errors when trying to
  # communicate with vCenter. Check and start the service if needed.
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking vCenter vAPI endpoint service..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  VCENTER_HOST="vc-mgmt-a.site-a.vcf.lab"
  # Check if vAPI endpoint is responding (404 is OK - means service is running, 503 means down)
  VAPI_STATUS=$(curl -s -k -o /dev/null -w "%{http_code}" "https://${VCENTER_HOST}/rest/com/vmware/cis/session" 2>&1)
  if [ "$VAPI_STATUS" == "503" ]; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> vCenter vAPI endpoint returning 503, attempting to start service..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    # Start the vmware-vapi-endpoint service on vCenter
    sshpass -f /home/holuser/creds.txt ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${VCENTER_HOST}" "service-control --start vmware-vapi-endpoint" 2>&1 | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    sleep 10
    # Verify the service is now responding
    VAPI_STATUS=$(curl -s -k -o /dev/null -w "%{http_code}" "https://${VCENTER_HOST}/rest/com/vmware/cis/session" 2>&1)
    if [ "$VAPI_STATUS" != "503" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> vCenter vAPI endpoint started successfully (HTTP ${VAPI_STATUS})" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    else
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> WARNING: vCenter vAPI endpoint still returning 503 after start attempt" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
  else
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> vCenter vAPI endpoint is responding (HTTP ${VAPI_STATUS})" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  fi

  ###### CSI Controller health check/fix ######
  # The vsphere-csi-controller can get stuck in CrashLoopBackOff if:
  # 1. The vCenter REST API (vmware-vapi-endpoint) is not running (checked above)
  # 2. Stale leader leases prevent the new pod from becoming leader
  # 3. CRD initialization error ("resource name may not be empty") causes main container to crash,
  #    which kills the CSI socket, causing all sidecar containers to fail with connection timeouts
  CSI_FIXED=false
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking vsphere-csi-controller health..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  CSISTATUS="."
  while [[ "$CSISTATUS" != "" ]]; do
    ((CNT++))
    # Check if CSI controller is in CrashLoopBackOff or not all containers ready
    CSISTATUS=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller -o jsonpath="{.items[0].status.containerStatuses[*].ready}"' | grep -o "false")
    
    if [ -n "$CSISTATUS" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CSI controller not fully ready (attempt ${CNT}/3)..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      
      # Get current CSI controller pod name
      CURRENT_CSI_POD=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller -o jsonpath="{.items[0].metadata.name}"')
      
      # Check for stale leases (leases held by pods that don't exist)
      STALE_LEASES=$(vcfa_ssh 'kubectl get leases -n kube-system -o json' | \
        jq -r --arg pod "$CURRENT_CSI_POD" '.items[] | select(.spec.holderIdentity != null and (.spec.holderIdentity | contains("vsphere-csi-controller")) and (.spec.holderIdentity != $pod)) | .metadata.name')
      
      if [ -n "$STALE_LEASES" ]; then
        echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Found stale CSI leases, deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        for LEASE in $STALE_LEASES; do
          echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Deleting stale lease: ${LEASE}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
          vcfa_ssh "kubectl delete lease ${LEASE} -n kube-system" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        done
        sleep 5
      fi

      # Check if CSI controller is failing due to password
      CSI_LOGS=$(vcfa_ssh "kubectl logs -n kube-system ${CURRENT_CSI_POD} -c vsphere-csi-controller --tail=20" 2>/dev/null)
      if echo "$CSI_LOGS" | grep -q "Cannot complete login due to an incorrect user name or password"; then
        echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CSI controller failing due to incorrect vCenter password. Fixing via dir-cli..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        PASSWORD=$(cat /home/holuser/creds.txt)
        CSI_USER=$(vcfa_ssh 'kubectl get secret vsphere-config-secret -n kube-system -o jsonpath="{.data.csi-vsphere\.conf}" 2>/dev/null | base64 -d | grep user | awk -F"\"" "{print \$2}"')
        CSI_ACCOUNT=$(echo "$CSI_USER" | cut -d@ -f1)
        CSI_PASS=$(vcfa_ssh 'kubectl get secret vsphere-cloud-secret -n kube-system -o jsonpath="{.data.vc-mgmt-a\.site-a\.vcf\.lab\.password}" 2>/dev/null | base64 -d')
        if [ -n "$CSI_ACCOUNT" ] && [ -n "$CSI_PASS" ]; then
          sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no root@vc-mgmt-a.site-a.vcf.lab "/usr/lib/vmware-vmafd/bin/dir-cli password reset --account ${CSI_ACCOUNT} --new '${CSI_PASS}' --login administrator@vsphere.local --password '${PASSWORD}'" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        fi
      fi

      # Force-delete the unhealthy CSI controller pod to trigger a fresh restart.
      # A normal delete can hang for minutes if containers are in CrashLoopBackOff,
      # so we use --grace-period=0 --force to immediately remove it.
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Force-deleting unhealthy CSI controller pod: ${CURRENT_CSI_POD}..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "kubectl delete pod ${CURRENT_CSI_POD} -n kube-system --grace-period=0 --force" 2>&1 | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      CSI_FIXED=true
      sleep 30

      # Wait up to 90s for the new CSI controller pod to become fully ready (7/7)
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Waiting for new CSI controller pod to become ready..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      CSI_WAIT=0
      while [ $CSI_WAIT -lt 180 ]; do
        NEW_CSI_READY=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller' | grep -v "NAME" | grep -v "Terminating" | awk '{print $2}')
        if [ "$NEW_CSI_READY" == "7/7" ]; then
          echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CSI controller is now fully ready (${NEW_CSI_READY})" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
          CSISTATUS=""
          break
        fi
        echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CSI controller status: ${NEW_CSI_READY:-pending} (${CSI_WAIT}/90s)" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        sleep 15
        CSI_WAIT=$((CSI_WAIT + 15))
      done
    fi
    if [ $CNT -eq 3 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CSI controller check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  ###### RabbitMQ .erlang.cookie permissions fix ######
  # The fsGroup: 200 pod security context causes Kubernetes to set group-read/write (0660)
  # on all PVC files, but Erlang requires .erlang.cookie to be owner-only (0400).
  # If RabbitMQ is in CrashLoopBackOff with "Cookie file must be accessible by owner only",
  # fix the permissions via a temporary root pod.
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking RabbitMQ health..." | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  RABBIT_STATUS=$(vcfa_ssh 'kubectl get pods -n prelude rabbitmq-ha-0 --no-headers 2>/dev/null' | awk '{print $3}')
  if [ "$RABBIT_STATUS" == "CrashLoopBackOff" ] || [ "$RABBIT_STATUS" == "Error" ]; then
    RABBIT_LOGS=$(vcfa_ssh 'kubectl logs -n prelude rabbitmq-ha-0 --tail=20 2>/dev/null')
    if echo "$RABBIT_LOGS" | grep -q "Cookie file.*must be accessible by owner only"; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> RabbitMQ failing due to .erlang.cookie permissions. Fixing..." | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh 'kubectl run rabbitmq-cookie-fix --rm -i --restart=Never -n prelude \
        --image=registry.vmsp-platform.svc.cluster.local:5000/images/prelude/rabbitmq:9.0.0.0.24701403 \
        --overrides='"'"'{"spec":{"securityContext":{"runAsUser":0},"containers":[{"name":"rabbitmq-cookie-fix","image":"registry.vmsp-platform.svc.cluster.local:5000/images/prelude/rabbitmq:9.0.0.0.24701403","command":["sh","-c","chmod 400 /var/lib/rabbitmq/.erlang.cookie && echo FIXED"],"volumeMounts":[{"mountPath":"/var/lib/rabbitmq","name":"rabbit-pvc"}]}],"volumes":[{"name":"rabbit-pvc","persistentVolumeClaim":{"claimName":"rabbit-pvc-rabbitmq-ha-0"}}]}}'"'"' 2>/dev/null' | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Restarting rabbitmq-ha-0..." | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh 'kubectl delete pod rabbitmq-ha-0 -n prelude 2>/dev/null' | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 30
      vcfa_ssh 'kubectl get pods -n prelude rabbitmq-ha-0 --no-headers 2>/dev/null' | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
    fi
  else
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> RabbitMQ status: ${RABBIT_STATUS:-not found}" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  fi

  ###### provisioning-service deadlock fix ######
  # The provisioning-service-app can hit a deterministic deadlock during Spring Boot
  # initialization between the main thread and ebs-1 thread, caused by
  # PrometheusExemplarsAutoConfiguration. Fix by adding JVM flag to disable exemplars.
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking provisioning-service for deadlock..." | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
  PROV_READY=$(vcfa_ssh 'kubectl get deployment provisioning-service-app -n prelude -o jsonpath="{.status.readyReplicas}" 2>/dev/null')
  if [ -z "$PROV_READY" ] || [ "$PROV_READY" == "0" ]; then
    PROV_JAVA_OPTS=$(vcfa_ssh 'kubectl get deployment provisioning-service-app -n prelude -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name==\"JAVA_OPTS\")].value}" 2>/dev/null')
    if ! echo "$PROV_JAVA_OPTS" | grep -q "exemplars.enabled=false"; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Patching provisioning-service to disable Prometheus exemplars (deadlock fix)..." | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh 'kubectl get deployment provisioning-service-app -n prelude -o json 2>/dev/null' > /tmp/prov-deploy.json
      python3 -c "
import json
with open('/tmp/prov-deploy.json') as f:
    data = json.load(f)
fix = '-Dmanagement.prometheus.metrics.export.exemplars.enabled=false'
for c in data['spec']['template']['spec']['containers']:
    if c['name'] == 'provisioning-service-app':
        for env in c.get('env', []):
            if env['name'] == 'JAVA_OPTS':
                if fix not in env.get('value', ''):
                    env['value'] = env['value'].rstrip() + '\n' + fix
                break
        break
with open('/tmp/prov-deploy-patched.json', 'w') as f:
    json.dump(data, f)
print('Patched')
"
      sshpass -f "${CREDS_FILE}" scp -o StrictHostKeyChecking=no /tmp/prov-deploy-patched.json "${VCFA_USER}@${VCFA_HOST}:/tmp/prov-deploy-patched.json" 2>/dev/null
      vcfa_ssh 'kubectl apply -f /tmp/prov-deploy-patched.json 2>/dev/null' | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> provisioning-service patched, new pod will roll out" | tee -a "${LOGFILE}" >> "${CONSOLELOG}"
    fi
  fi

  ###### Prelude Pods Stuck Volume Mount Recovery ######
  # After fixing stuck volume attachments and/or the CSI controller, pods that were stuck
  # in ContainerCreating or Init due to "volume attachment is being deleted" errors will
  # NOT automatically recover - they must be deleted so the StatefulSet controller recreates
  # them with fresh volume mount attempts.
  if [ "$STUCK_VA_FIXED" = true ] || [ "$CSI_FIXED" = true ]; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Volume attachments or CSI controller were fixed, checking prelude pods..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    sleep 10

    # Check for pods stuck in ContainerCreating or Init states in the prelude namespace
    STUCK_PODS=$(vcfa_ssh "kubectl get pods -n prelude -s ${K8S_API}" | \
      grep -E "ContainerCreating|Init:" | awk '{print $1}')

    if [ -n "$STUCK_PODS" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Found stuck prelude pods after volume/CSI fix, deleting to force fresh mount..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      for POD in $STUCK_PODS; do
        echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Deleting stuck pod: ${POD}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        vcfa_ssh "kubectl delete pod ${POD} -n prelude -s ${K8S_API}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      done
      # Wait for pods to be recreated and volumes to attach
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Waiting 60s for pods to be recreated with fresh volume mounts..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 60
      vcfa_ssh "kubectl get pods -n prelude -s ${K8S_API}" | grep -v "Completed" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    else
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> No stuck prelude pods found, services should recover normally" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
  fi
  

  # CNT=0
  # while [[ "$POSTGRES" != "2/2" ]]; do 
  #   sleep 60;
	#   POSTGRES=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s ${K8S_API}'" | grep vcfapostgres-0 | awk '{ print $2 }')
	#   ((CNT++))
  #   echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> PG Running pods Result: $POSTGRES - Attempt: $CNT" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   if [ $CNT -eq 5 ]; then
  #     echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Rebooting after 5 minutes with Postgres only $POSTGRES" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  #     #sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c reboot"
  #     # Instead of a full reboot try deleting the vcfapostgres-0 pods, this forces the ReplicaSet to re-create them:
  #     sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -n prelude delete pods vcfapostgres-0'"
  #     sleep 30
  #     break
  #   fi
  # done

  # CNT=0
  # while [[ "$CCSK3SAPP" != "2/2" ]]; do 
  #   sleep 300;
	#   CCSK3SAPP=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s ${K8S_API}'" | grep ccs-k3s-app | awk '{ print $2 }')
	#   ((CNT++))
  #   echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> CCS Running pods Result: $CCSK3SAPP - Attempt: $CNT" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   if [ $CNT -eq 12 ]; then
  #     echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Rebooting after 60 minutes with CCS-K3SAPP only $POSTGRES" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  #     sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c reboot"
  #     sleep 30
  #     break
  #   fi
  # done
  
  # if [ "$CCSK3SAPP" == "2/2" ]; then
  #   CCSK3SAPPNAME=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s ${K8S_API}'" | grep ccs-k3s-app | awk '{ print $1 }')
  #   sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl delete pod '\"$CCSK3SAPPNAME\"' -n prelude -s ${K8S_API}'"
  #   echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Deleted CCS-K3S-APP for CPU usage bug" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   break
  # fi
  # if [ "$POSTGRES" == "2/2" ]; then
  #   echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Postgres is up, waiting for CCS-K3S-APP" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   break
  # fi
#   ((REBOOTS++))
# done
