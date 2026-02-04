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
echo "$(date +"%m/%d/%Y %T") -------------WATCHVCFA RUN START-------------" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
echo "$(date +"%m/%d/%Y %T")-> VCFA Watcher started"  | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"

# while [[ $REBOOTS -lt 3 || "$POSTGRES" != "2/2" ]]; do
  CNT=0
  while ! $(sshpass -f /home/holuser/creds.txt ssh -q -o ConnectTimeout=5 "${VCFA_USER}@${VCFA_HOST}" exit); do
    sleep 30
	  echo "$(date +"%m/%d/%Y %T")-> Waiting for VCFA to come Online" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    ((CNT++))
    if [ $CNT -eq 10 ]; then
      echo "$(date +"%m/%d/%Y %T")-> VCFA Online check check tried 10 times (5m), continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done
  # echo "$(date +"%m/%d/%Y %T")-> VCFA online, reboot# $REBOOTS" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 

  ###### Containerd check/fix ######
  echo "$(date +"%m/%d/%Y %T")-> Checking containerd on VCFA for Ready,SchedulingDisabled..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$CONATAINERDREADY" != "" ]]; do
    ((CNT++))
    CONATAINERDREADY=$(vcfa_ssh "kubectl -s https://${VCFA_HOST}:6443 get nodes" | grep "Ready,SchedulingDisabled" | awk '{print $2}')
   
    if [ "$CONATAINERDREADY" == "Ready,SchedulingDisabled" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stale containerd found, restarting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
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
      echo "$(date +"%m/%d/%Y %T")-> containerd check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

###### kube-scheduler check/fix ######
  echo "$(date +"%m/%d/%Y %T")-> Checking kube-scheduler on VCFA for 0/1 Running..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$KUBESCHEDULER" != "" ]]; do
    ((CNT++))
    sleep 30
    KUBESCHEDULER=$(vcfa_ssh "kubectl -n kube-system -s https://${VCFA_HOST}:6443 get pods" | grep "kube-scheduler" | grep "0/1" | awk '{print $2}')
   
    if [ "$KUBESCHEDULER" == "0/1" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stale kube-scheduler found, restarting containerd..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "systemctl restart containerd" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 120
      vcfa_ssh "kubectl -n kube-system -s https://${VCFA_HOST}:6443 get pods" | grep "kube-scheduler" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    if [ $CNT -eq 3 ]; then
      echo "$(date +"%m/%d/%Y %T")-> kube-scheduler check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  # seaweedfs-master-0 is stale in the captured vAppTemplate. When Automation starts, sometimes this pod is NOT
  #   cleaned up properly, resulting in the prevention of many other pods failint go start.
  #  Check this pod and delete it if it is old:
  # Delete the seaweedfs-master-0 pod if age over 1 hour
  echo "$(date +"%m/%d/%Y %T")-> Checking seaweedfs-master-0 pod from VCFA if older than 1 hour..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$SEAWEEDPOD" != "" ]]; do
    ((CNT++))
    SEAWEEDPOD=$(vcfa_ssh "kubectl -n vmsp-platform -s https://${VCFA_HOST}:6443 get pods seaweedfs-master-0 -o json" | \
    jq -r '. | select(.metadata.creationTimestamp | fromdateiso8601 < (now - 3600)) | .metadata.name ')
    
    if [ "$SEAWEEDPOD" == "seaweedfs-master-0" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stale seaweedfs-master-0 pod found, deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      vcfa_ssh "kubectl -n vmsp-platform -s https://${VCFA_HOST}:6443 delete pod seaweedfs-master-0" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      vcfa_ssh "kubectl -n vmsp-platform -s https://${VCFA_HOST}:6443 get pods | grep seaweedfs" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      echo "$(date +"%m/%d/%Y %T")-> seaweedfs-master-0 check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  ###### Stuck Volume Attachments check/fix ######
  # Volume attachments can get stuck in deletion state with finalizers preventing cleanup.
  # This happens when pods are terminated but the CSI controller can't clean up the attachments.
  # The result is new pods getting stuck in ContainerCreating with "volume attachment is being deleted" errors.
  echo "$(date +"%m/%d/%Y %T")-> Checking for stuck volume attachments with deletionTimestamp..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  while [[ "$STUCKVOLATTACH" != "" ]]; do
    ((CNT++))
    # Find volume attachments that have deletionTimestamp set (stuck in deletion)
    STUCKVOLATTACH=$(vcfa_ssh 'kubectl get volumeattachments -o json' | \
      jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name' | head -1)
    
    if [ -n "$STUCKVOLATTACH" ] && [ "$STUCKVOLATTACH" != "" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stuck volume attachment found: ${STUCKVOLATTACH}, removing finalizer..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      # Get all stuck volume attachments and remove their finalizers
      STUCK_VAS=$(vcfa_ssh 'kubectl get volumeattachments -o json' | \
        jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name')
      for VA in $STUCK_VAS; do
        echo "$(date +"%m/%d/%Y %T")-> Removing finalizer from volume attachment: ${VA}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        vcfa_ssh "kubectl patch volumeattachment ${VA} -p '{\"metadata\":{\"finalizers\":null}}' --type=merge" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      done
      sleep 5
      # Verify the stuck attachments are gone
      vcfa_ssh 'kubectl get volumeattachments' | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stuck volume attachment check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done

  ###### vCenter vAPI Endpoint check/fix ######
  # The vsphere-csi-controller requires vCenter's REST API (vmware-vapi-endpoint) to be running.
  # If this service is stopped, the CSI controller will crash with 503 errors when trying to
  # communicate with vCenter. Check and start the service if needed.
  echo "$(date +"%m/%d/%Y %T")-> Checking vCenter vAPI endpoint service..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  VCENTER_HOST="vc-mgmt-a.site-a.vcf.lab"
  # Check if vAPI endpoint is responding (404 is OK - means service is running, 503 means down)
  VAPI_STATUS=$(curl -s -k -o /dev/null -w "%{http_code}" "https://${VCENTER_HOST}/rest/com/vmware/cis/session" 2>&1)
  if [ "$VAPI_STATUS" == "503" ]; then
    echo "$(date +"%m/%d/%Y %T")-> vCenter vAPI endpoint returning 503, attempting to start service..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    # Start the vmware-vapi-endpoint service on vCenter
    sshpass -f /home/holuser/creds.txt ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${VCENTER_HOST}" "service-control --start vmware-vapi-endpoint" 2>&1 | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    sleep 10
    # Verify the service is now responding
    VAPI_STATUS=$(curl -s -k -o /dev/null -w "%{http_code}" "https://${VCENTER_HOST}/rest/com/vmware/cis/session" 2>&1)
    if [ "$VAPI_STATUS" != "503" ]; then
      echo "$(date +"%m/%d/%Y %T")-> vCenter vAPI endpoint started successfully (HTTP ${VAPI_STATUS})" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    else
      echo "$(date +"%m/%d/%Y %T")-> WARNING: vCenter vAPI endpoint still returning 503 after start attempt" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
  else
    echo "$(date +"%m/%d/%Y %T")-> vCenter vAPI endpoint is responding (HTTP ${VAPI_STATUS})" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  fi

  ###### CSI Controller health check/fix ######
  # The vsphere-csi-controller can get stuck in CrashLoopBackOff if:
  # 1. The vCenter REST API (vmware-vapi-endpoint) is not running (checked above)
  # 2. Stale leader leases prevent the new pod from becoming leader
  echo "$(date +"%m/%d/%Y %T")-> Checking vsphere-csi-controller health..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  CNT=0
  CSISTATUS="."
  while [[ "$CSISTATUS" != "" ]]; do
    ((CNT++))
    # Check if CSI controller is in CrashLoopBackOff or not all containers ready
    CSISTATUS=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller -o jsonpath="{.items[0].status.containerStatuses[*].ready}"' | grep -o "false")
    
    if [ -n "$CSISTATUS" ]; then
      echo "$(date +"%m/%d/%Y %T")-> CSI controller not fully ready, checking for stale leader leases..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      
      # Get current CSI controller pod name
      CURRENT_CSI_POD=$(vcfa_ssh 'kubectl get pods -n kube-system -l app=vsphere-csi-controller -o jsonpath="{.items[0].metadata.name}"')
      
      # Check for stale leases (leases held by pods that don't exist)
      STALE_LEASES=$(vcfa_ssh 'kubectl get leases -n kube-system -o json' | \
        jq -r --arg pod "$CURRENT_CSI_POD" '.items[] | select(.spec.holderIdentity != null and (.spec.holderIdentity | contains("vsphere-csi-controller")) and (.spec.holderIdentity != $pod)) | .metadata.name')
      
      if [ -n "$STALE_LEASES" ]; then
        echo "$(date +"%m/%d/%Y %T")-> Found stale CSI leases, deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        for LEASE in $STALE_LEASES; do
          echo "$(date +"%m/%d/%Y %T")-> Deleting stale lease: ${LEASE}" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
          vcfa_ssh "kubectl delete lease ${LEASE} -n kube-system" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        done
        sleep 5
        # Restart the CSI controller to pick up the new leases
        echo "$(date +"%m/%d/%Y %T")-> Restarting CSI controller pod..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        vcfa_ssh 'kubectl delete pod -n kube-system -l app=vsphere-csi-controller' | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
        sleep 30
      fi
    fi
    sleep 10
    if [ $CNT -eq 3 ]; then
      echo "$(date +"%m/%d/%Y %T")-> CSI controller check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      break
    fi
  done
  

  # CNT=0
  # while [[ "$POSTGRES" != "2/2" ]]; do 
  #   sleep 60;
	#   POSTGRES=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s https://10.1.1.71:6443'" | grep vcfapostgres-0 | awk '{ print $2 }')
	#   ((CNT++))
  #   echo "$(date +"%m/%d/%Y %T")-> PG Running pods Result: $POSTGRES - Attempt: $CNT" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   if [ $CNT -eq 5 ]; then
  #     echo "$(date +"%m/%d/%Y %T")-> Rebooting after 5 minutes with Postgres only $POSTGRES" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
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
  #   echo "$(date +"%m/%d/%Y %T")-> CCS Running pods Result: $CCSK3SAPP - Attempt: $CNT" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   if [ $CNT -eq 12 ]; then
  #     echo "$(date +"%m/%d/%Y %T")-> Rebooting after 60 minutes with CCS-K3SAPP only $POSTGRES" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
  #     sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c reboot"
  #     sleep 30
  #     break
  #   fi
  # done
  
  # if [ "$CCSK3SAPP" == "2/2" ]; then
  #   CCSK3SAPPNAME=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl get pods -n prelude -s https://10.1.1.71:6443'" | grep ccs-k3s-app | awk '{ print $1 }')
  #   sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl delete pod '\"$CCSK3SAPPNAME\"' -n prelude -s https://10.1.1.71:6443'"
  #   echo "$(date +"%m/%d/%Y %T")-> Deleted CCS-K3S-APP for CPU usage bug" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   break
  # fi
  # if [ "$POSTGRES" == "2/2" ]; then
  #   echo "$(date +"%m/%d/%Y %T")-> Postgres is up, waiting for CCS-K3S-APP" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}" 
  #   break
  # fi
#   ((REBOOTS++))
# done
