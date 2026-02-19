#!/bin/bash
# Author: Burke Azbill
# Version: 1.0
# Date: 2026-02-13
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

# Source shared logging library
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/log_functions.sh"

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
    log_warn "Cannot resolve ${VCFA_FQDN} via DNS" "$LOGFILE" "$CONSOLELOG"
    return 1
  fi

  log_msg "${VCFA_FQDN} resolves to ${resolved_ip}" "$LOGFILE" "$CONSOLELOG"

  # Verify SSH is reachable on the resolved IP
  if nc -z -w 5 "${resolved_ip}" 22 >/dev/null 2>&1; then
    VCFA_HOST="${resolved_ip}"
    log_msg "Detected VCFA host: ${VCFA_HOST} (SSH reachable)" "$LOGFILE" "$CONSOLELOG"
    return 0
  fi

  log_warn "${VCFA_FQDN} (${resolved_ip}) SSH not reachable" "$LOGFILE" "$CONSOLELOG"
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
    log_msg "Detected K8s API endpoint: ${K8S_API}" "$LOGFILE" "$CONSOLELOG"
  else
    # Fallback: use the VCFA_HOST IP
    K8S_API="https://${VCFA_HOST}:6443"
    log_msg "Could not detect K8s API from kubeconfig, using fallback: ${K8S_API}" "$LOGFILE" "$CONSOLELOG"
  fi
}

log_msg "-------------WATCHVCFA RUN START-------------" "$LOGFILE" "$CONSOLELOG"
log_msg "VCFA Watcher started" "$LOGFILE" "$CONSOLELOG"

# Auto-detect VCFA host, sudo mode, and K8s API endpoint
detect_vcfa_host
if [ -z "${VCFA_HOST}" ]; then
  log_error "Cannot find VCFA host, exiting" "$LOGFILE" "$CONSOLELOG"
  exit 1
fi

# while [[ $REBOOTS -lt 3 || "$POSTGRES" != "2/2" ]]; do
  CNT=0
  while ! $(sshpass -f "${CREDS_FILE}" ssh -q -o ConnectTimeout=5 "${VCFA_USER}@${VCFA_HOST}" exit); do
    sleep 30
	  log_msg "Waiting for VCFA to come Online" "$LOGFILE" "$CONSOLELOG"
    ((CNT++))
    if [ $CNT -eq 10 ]; then
      log_msg "VCFA Online check check tried 10 times (5m), continuing..." "$LOGFILE" "$CONSOLELOG"
      break
    fi
  done

  # Detect sudo mode and K8s API after VCFA is online
  detect_sudo_mode
  log_msg "Sudo requires password: ${SUDO_NEEDS_PASSWORD}" "$LOGFILE" "$CONSOLELOG"
  detect_k8s_api

  ###### Containerd check/fix ######
  log_msg "Checking containerd on VCFA for Ready,SchedulingDisabled..." "$LOGFILE" "$CONSOLELOG"
  CNT=0
  while [[ "$CONATAINERDREADY" != "" ]]; do
    ((CNT++))
    CONATAINERDREADY=$(vcfa_ssh "kubectl -s ${K8S_API} get nodes" | grep "Ready,SchedulingDisabled" | awk '{print $2}')
   
    if [ "$CONATAINERDREADY" == "Ready,SchedulingDisabled" ]; then
      log_msg "Stale containerd found, restarting..." "$LOGFILE" "$CONSOLELOG"
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
      log_msg "containerd check tried 3 times, continuing..." "$LOGFILE" "$CONSOLELOG"
      break
    fi
  done

###### kube-scheduler check/fix ######
  log_msg "Checking kube-scheduler on VCFA for 0/1 Running..." "$LOGFILE" "$CONSOLELOG"
  CNT=0
  while [[ "$KUBESCHEDULER" != "" ]]; do
    ((CNT++))
    sleep 30
    KUBESCHEDULER=$(vcfa_ssh "kubectl -n kube-system -s ${K8S_API} get pods" | grep "kube-scheduler" | grep "0/1" | awk '{print $2}')
   
    if [ "$KUBESCHEDULER" == "0/1" ]; then
      log_msg "Stale kube-scheduler found, restarting containerd..." "$LOGFILE" "$CONSOLELOG"
      vcfa_ssh "systemctl restart containerd" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 120
      vcfa_ssh "kubectl -n kube-system -s ${K8S_API} get pods" | grep "kube-scheduler" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    if [ $CNT -eq 3 ]; then
      log_msg "kube-scheduler check tried 3 times, continuing..." "$LOGFILE" "$CONSOLELOG"
      break
    fi
  done

  # seaweedfs-master-0 is stale in the captured vAppTemplate. When Automation starts, sometimes this pod is NOT
  #   cleaned up properly, resulting in the prevention of many other pods failint go start.
  #  Check this pod and delete it if it is old:
  # Delete the seaweedfs-master-0 pod if age over 1 hour
  log_msg "Checking seaweedfs-master-0 pod from VCFA if older than 1 hour..." "$LOGFILE" "$CONSOLELOG"
  CNT=0
  while [[ "$SEAWEEDPOD" != "" ]]; do
    ((CNT++))
    SEAWEEDPOD=$(vcfa_ssh "kubectl -n vmsp-platform -s ${K8S_API} get pods seaweedfs-master-0 -o json" | \
    jq -r '. | select(.metadata.creationTimestamp | fromdateiso8601 < (now - 3600)) | .metadata.name ')
    
    if [ "$SEAWEEDPOD" == "seaweedfs-master-0" ]; then
      log_msg "Stale seaweedfs-master-0 pod found, deleting..." "$LOGFILE" "$CONSOLELOG"
      vcfa_ssh "kubectl -n vmsp-platform -s ${K8S_API} delete pod seaweedfs-master-0" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      vcfa_ssh "kubectl -n vmsp-platform -s ${K8S_API} get pods | grep seaweedfs" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      log_msg "seaweedfs-master-0 check tried 3 times, continuing..." "$LOGFILE" "$CONSOLELOG"
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
  log_msg "Checking for stuck volume attachments with deletionTimestamp..." "$LOGFILE" "$CONSOLELOG"
  CNT=0
  while [[ "$STUCKVOLATTACH" != "" ]]; do
    ((CNT++))
    # Find volume attachments that have deletionTimestamp set (stuck in deletion)
    STUCKVOLATTACH=$(vcfa_ssh 'kubectl get volumeattachments -o json' | \
      jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name' | head -1)
    
    if [ -n "$STUCKVOLATTACH" ] && [ "$STUCKVOLATTACH" != "" ]; then
      log_msg "Stuck volume attachment found: ${STUCKVOLATTACH}, removing finalizer..." "$LOGFILE" "$CONSOLELOG"
      # Get all stuck volume attachments and remove their finalizers
      STUCK_VAS=$(vcfa_ssh 'kubectl get volumeattachments -o json' | \
        jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name')
      for VA in $STUCK_VAS; do
        log_msg "Removing finalizer from volume attachment: ${VA}" "$LOGFILE" "$CONSOLELOG"
        vcfa_ssh "kubectl patch volumeattachment ${VA} -p '{\"metadata\":{\"finalizers\":null}}' --type=merge" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      done
      STUCK_VA_FIXED=true
      sleep 5
      # Verify the stuck attachments are gone
      vcfa_ssh 'kubectl get volumeattachments' | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      log_msg "Stuck volume attachment check tried 3 times, continuing..." "$LOGFILE" "$CONSOLELOG"
      break
    fi
  done

  ###### vCenter vAPI Endpoint check/fix ######
  # The vsphere-csi-controller requires vCenter's REST API (vmware-vapi-endpoint) to be running.
  # If this service is stopped, the CSI controller will crash with 503 errors when trying to
  # communicate with vCenter. Check and start the service if needed.
  log_msg "Checking vCenter vAPI endpoint service..." "$LOGFILE" "$CONSOLELOG"
  VCENTER_HOST="vc-mgmt-a.site-a.vcf.lab"
  # Check if vAPI endpoint is responding (404 is OK - means service is running, 503 means down)
  VAPI_STATUS=$(curl -s -k -o /dev/null -w "%{http_code}" "https://${VCENTER_HOST}/rest/com/vmware/cis/session" 2>&1)
  if [ "$VAPI_STATUS" == "503" ]; then
    log_msg "vCenter vAPI endpoint returning 503, attempting to start service..." "$LOGFILE" "$CONSOLELOG"
    # Start the vmware-vapi-endpoint service on vCenter
    sshpass -f /home/holuser/creds.txt ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${VCENTER_HOST}" "service-control --start vmware-vapi-endpoint" 2>&1 | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    sleep 10
    # Verify the service is now responding
    VAPI_STATUS=$(curl -s -k -o /dev/null -w "%{http_code}" "https://${VCENTER_HOST}/rest/com/vmware/cis/session" 2>&1)
    if [ "$VAPI_STATUS" != "503" ]; then
      log_msg "vCenter vAPI endpoint started successfully (HTTP ${VAPI_STATUS})" "$LOGFILE" "$CONSOLELOG"
    else
      log_warn "vCenter vAPI endpoint still returning 503 after start attempt" "$LOGFILE" "$CONSOLELOG"
    fi
  else
    log_msg "vCenter vAPI endpoint is responding (HTTP ${VAPI_STATUS})" "$LOGFILE" "$CONSOLELOG"
  fi

  ###### CSI Controller health check/fix ######
  # The vsphere-csi-controller can get stuck in CrashLoopBackOff if:
  # 1. The vCenter REST API (vmware-vapi-endpoint) is not running (checked above)
  # 2. Stale leader leases prevent the new pod from becoming leader
  # 3. CRD initialization error ("resource name may not be empty") causes main container to crash,
  #    which kills the CSI socket, causing all sidecar containers to fail with connection timeouts
  CSI_FIXED=false
  log_msg "Checking vsphere-csi-controller health..." "$LOGFILE" "$CONSOLELOG"
  CNT=0
  CSISTATUS="."
  while [[ "$CSISTATUS" != "" ]]; do
    ((CNT++))
    # Check if CSI controller is in CrashLoopBackOff or not all containers ready
    CSISTATUS=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller -o jsonpath="{.items[0].status.containerStatuses[*].ready}"' | grep -o "false")
    
    if [ -n "$CSISTATUS" ]; then
      log_msg "CSI controller not fully ready (attempt ${CNT}/3)..." "$LOGFILE" "$CONSOLELOG"
      
      # Get current CSI controller pod name
      CURRENT_CSI_POD=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller -o jsonpath="{.items[0].metadata.name}"')
      
      # Check for stale leases (leases held by pods that don't exist)
      STALE_LEASES=$(vcfa_ssh 'kubectl get leases -n kube-system -o json' | \
        jq -r --arg pod "$CURRENT_CSI_POD" '.items[] | select(.spec.holderIdentity != null and (.spec.holderIdentity | contains("vsphere-csi-controller")) and (.spec.holderIdentity != $pod)) | .metadata.name')
      
      if [ -n "$STALE_LEASES" ]; then
        log_msg "Found stale CSI leases, deleting..." "$LOGFILE" "$CONSOLELOG"
        for LEASE in $STALE_LEASES; do
          log_msg "Deleting stale lease: ${LEASE}" "$LOGFILE" "$CONSOLELOG"
          vcfa_ssh "kubectl delete lease ${LEASE} -n kube-system" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        done
        sleep 5
      fi

      # Force-delete the unhealthy CSI controller pod to trigger a fresh restart.
      # A normal delete can hang for minutes if containers are in CrashLoopBackOff,
      # so we use --grace-period=0 --force to immediately remove it.
      log_msg "Force-deleting unhealthy CSI controller pod: ${CURRENT_CSI_POD}..." "$LOGFILE" "$CONSOLELOG"
      vcfa_ssh "kubectl delete pod ${CURRENT_CSI_POD} -n kube-system --grace-period=0 --force" 2>&1 | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      CSI_FIXED=true
      sleep 30

      # Wait up to 90s for the new CSI controller pod to become fully ready (7/7)
      log_msg "Waiting for new CSI controller pod to become ready..." "$LOGFILE" "$CONSOLELOG"
      CSI_WAIT=0
      while [ $CSI_WAIT -lt 90 ]; do
        NEW_CSI_READY=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller' | grep -v "NAME" | grep -v "Terminating" | awk '{print $2}')
        if [ "$NEW_CSI_READY" == "7/7" ]; then
          log_msg "CSI controller is now fully ready (${NEW_CSI_READY})" "$LOGFILE" "$CONSOLELOG"
          CSISTATUS=""
          break
        fi
        log_msg "CSI controller status: ${NEW_CSI_READY:-pending} (${CSI_WAIT}/90s)" "$LOGFILE" "$CONSOLELOG"
        sleep 15
        CSI_WAIT=$((CSI_WAIT + 15))
      done
    fi
    if [ $CNT -eq 3 ]; then
      log_msg "CSI controller check tried 3 times, continuing..." "$LOGFILE" "$CONSOLELOG"
      break
    fi
  done

  ###### Prelude Pods Stuck Volume Mount Recovery ######
  # After fixing stuck volume attachments and/or the CSI controller, pods that were stuck
  # in ContainerCreating or Init due to "volume attachment is being deleted" errors will
  # NOT automatically recover - they must be deleted so the StatefulSet controller recreates
  # them with fresh volume mount attempts.
  if [ "$STUCK_VA_FIXED" = true ] || [ "$CSI_FIXED" = true ]; then
    log_msg "Volume attachments or CSI controller were fixed, checking prelude pods..." "$LOGFILE" "$CONSOLELOG"
    sleep 10

    # Check for pods stuck in ContainerCreating or Init states in the prelude namespace
    STUCK_PODS=$(vcfa_ssh "kubectl get pods -n prelude -s ${K8S_API}" | \
      grep -E "ContainerCreating|Init:" | awk '{print $1}')

    if [ -n "$STUCK_PODS" ]; then
      log_msg "Found stuck prelude pods after volume/CSI fix, deleting to force fresh mount..." "$LOGFILE" "$CONSOLELOG"
      for POD in $STUCK_PODS; do
        log_msg "Deleting stuck pod: ${POD}" "$LOGFILE" "$CONSOLELOG"
        vcfa_ssh "kubectl delete pod ${POD} -n prelude -s ${K8S_API}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      done
      # Wait for pods to be recreated and volumes to attach
      log_msg "Waiting 60s for pods to be recreated with fresh volume mounts..." "$LOGFILE" "$CONSOLELOG"
      sleep 60
      vcfa_ssh "kubectl get pods -n prelude -s ${K8S_API}" | grep -v "Completed" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    else
      log_msg "No stuck prelude pods found, services should recover normally" "$LOGFILE" "$CONSOLELOG"
    fi
  fi
  

  # CNT=0
  # while [[ "$POSTGRES" != "2/2" ]]; do 
  #   sleep 60;
	#   POSTGRES=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s ${K8S_API}'" | grep vcfapostgres-0 | awk '{ print $2 }')
	#   ((CNT++))
  #   log_msg "PG Running pods Result: $POSTGRES - Attempt: $CNT" "$LOGFILE" "$CONSOLELOG"
  #   if [ $CNT -eq 5 ]; then
  #     log_msg "Rebooting after 5 minutes with Postgres only $POSTGRES" "$LOGFILE" "$CONSOLELOG"
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
  #   log_msg "CCS Running pods Result: $CCSK3SAPP - Attempt: $CNT" "$LOGFILE" "$CONSOLELOG"
  #   if [ $CNT -eq 12 ]; then
  #     log_msg "Rebooting after 60 minutes with CCS-K3SAPP only $POSTGRES" "$LOGFILE" "$CONSOLELOG"
  #     sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c reboot"
  #     sleep 30
  #     break
  #   fi
  # done
  
  # if [ "$CCSK3SAPP" == "2/2" ]; then
  #   CCSK3SAPPNAME=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s ${K8S_API}'" | grep ccs-k3s-app | awk '{ print $1 }')
  #   sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl delete pod '\"$CCSK3SAPPNAME\"' -n prelude -s ${K8S_API}'"
  #   log_msg "Deleted CCS-K3S-APP for CPU usage bug" "$LOGFILE" "$CONSOLELOG"
  #   break
  # fi
  # if [ "$POSTGRES" == "2/2" ]; then
  #   log_msg "Postgres is up, waiting for CCS-K3S-APP" "$LOGFILE" "$CONSOLELOG"
  #   break
  # fi
#   ((REBOOTS++))
# done
