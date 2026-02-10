#!/bin/bash
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
VCFA_HOST="10.1.1.71"
VCFA_USER="vmware-system-user"

# Helper function for SSH commands to VCFA
vcfa_ssh() {
  sshpass -f /home/holuser/creds.txt ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${VCFA_USER}@${VCFA_HOST}" "sudo -i bash -c '$1'" 2>&1 | grep -v "Welcome to Photon"
}

# Now try to remediate Automation
echo "[$(date +"%Y-%m-%d %H:%M:%S")] -------------WATCHVCFA RUN START-------------" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> VCFA Watcher started"  | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"

# while [[ $REBOOTS -lt 3 || "$POSTGRES" != "2/2" ]]; do
  CNT=0
  while ! $(sshpass -f /home/holuser/creds.txt ssh -q -o ConnectTimeout=5 "${VCFA_USER}@${VCFA_HOST}" exit); do
    sleep 30
	  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Waiting for VCFA to come Online" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    ((CNT++))
    if [ $CNT -eq 10 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> VCFA Online check check tried 10 times (5m), continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done
  # echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> VCFA online, reboot# $REBOOTS" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 

  ###### Containerd check/fix ######
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking containerd on VCFA for Ready,SchedulingDisabled..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$CONATAINERDREADY" != "" ]]; do
    ((CNT++))
    CONATAINERDREADY=$(vcfa_ssh "kubectl -s https://${VCFA_HOST}:6443 get nodes" | grep "Ready,SchedulingDisabled" | awk '{print $2}')
   
    if [ "$CONATAINERDREADY" == "Ready,SchedulingDisabled" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stale containerd found, restarting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "systemctl restart containerd" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      vcfa_ssh "kubectl -s https://${VCFA_HOST}:6443 get nodes" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 2 ]; then
      NODENAME=$(vcfa_ssh "kubectl -s https://${VCFA_HOST}:6443 get nodes" | grep "Ready,SchedulingDisabled" | awk '{print $1}')
      vcfa_ssh "kubectl -s https://${VCFA_HOST}:6443 uncordon ${NODENAME}"
    fi
    if [ $CNT -eq 3 ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> containerd check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

###### kube-scheduler check/fix ######
  echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Checking kube-scheduler on VCFA for 0/1 Running..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$KUBESCHEDULER" != "" ]]; do
    ((CNT++))
    sleep 30
    KUBESCHEDULER=$(vcfa_ssh "kubectl -n kube-system -s https://${VCFA_HOST}:6443 get pods" | grep "kube-scheduler" | grep "0/1" | awk '{print $2}')
   
    if [ "$KUBESCHEDULER" == "0/1" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stale kube-scheduler found, restarting containerd..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "systemctl restart containerd" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 120
      vcfa_ssh "kubectl -n kube-system -s https://${VCFA_HOST}:6443 get pods" | grep "kube-scheduler" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
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
    SEAWEEDPOD=$(vcfa_ssh "kubectl -n vmsp-platform -s https://${VCFA_HOST}:6443 get pods seaweedfs-master-0 -o json" | \
    jq -r '. | select(.metadata.creationTimestamp | fromdateiso8601 < (now - 3600)) | .metadata.name ')
    
    if [ "$SEAWEEDPOD" == "seaweedfs-master-0" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Stale seaweedfs-master-0 pod found, deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "kubectl -n vmsp-platform -s https://${VCFA_HOST}:6443 delete pod seaweedfs-master-0" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      vcfa_ssh "kubectl -n vmsp-platform -s https://${VCFA_HOST}:6443 get pods | grep seaweedfs" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
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
      while [ $CSI_WAIT -lt 90 ]; do
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

  ###### Prelude Pods Stuck Volume Mount Recovery ######
  # After fixing stuck volume attachments and/or the CSI controller, pods that were stuck
  # in ContainerCreating or Init due to "volume attachment is being deleted" errors will
  # NOT automatically recover - they must be deleted so the StatefulSet controller recreates
  # them with fresh volume mount attempts.
  if [ "$STUCK_VA_FIXED" = true ] || [ "$CSI_FIXED" = true ]; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Volume attachments or CSI controller were fixed, checking prelude pods..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    sleep 10

    # Check for pods stuck in ContainerCreating or Init states in the prelude namespace
    STUCK_PODS=$(vcfa_ssh 'kubectl get pods -n prelude -s https://10.1.1.71:6443' | \
      grep -E "ContainerCreating|Init:" | awk '{print $1}')

    if [ -n "$STUCK_PODS" ]; then
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Found stuck prelude pods after volume/CSI fix, deleting to force fresh mount..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      for POD in $STUCK_PODS; do
        echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Deleting stuck pod: ${POD}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        vcfa_ssh "kubectl delete pod ${POD} -n prelude -s https://10.1.1.71:6443" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      done
      # Wait for pods to be recreated and volumes to attach
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Waiting 60s for pods to be recreated with fresh volume mounts..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 60
      vcfa_ssh 'kubectl get pods -n prelude -s https://10.1.1.71:6443' | grep -v "Completed" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    else
      echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> No stuck prelude pods found, services should recover normally" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
  fi
  

  # CNT=0
  # while [[ "$POSTGRES" != "2/2" ]]; do 
  #   sleep 60;
	#   POSTGRES=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s https://10.1.1.71:6443'" | grep vcfapostgres-0 | awk '{ print $2 }')
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
	#   CCSK3SAPP=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s https://10.1.1.71:6443'" | grep ccs-k3s-app | awk '{ print $2 }')
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
  #   CCSK3SAPPNAME=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s https://10.1.1.71:6443'" | grep ccs-k3s-app | awk '{ print $1 }')
  #   sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl delete pod '\"$CCSK3SAPPNAME\"' -n prelude -s https://10.1.1.71:6443'"
  #   echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Deleted CCS-K3S-APP for CPU usage bug" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   break
  # fi
  # if [ "$POSTGRES" == "2/2" ]; then
  #   echo "[$(date +"%Y-%m-%d %H:%M:%S")]-> Postgres is up, waiting for CCS-K3S-APP" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   break
  # fi
#   ((REBOOTS++))
# done
