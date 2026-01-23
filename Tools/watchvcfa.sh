#!/bin/bash
#REBOOTS=0
SEAWEEDPOD="."
CONATAINERDREADY="."
KUBESCHEDULER="."
#POSTGRES="."
#CCSK3SAPP="."
# For some reason, outputing to LOGFILE only shows up in the manager log, so attempting to log to both files...
LOGFILE="/home/holuser/hol/labstartup.log"
CONSOLELOG="/lmchol/hol/labstartup.log"


# Now try to remediate Automation
echo "$(date +"%m/%d/%Y %T") -------------WATCHVCFA RUN START-------------" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
echo "$(date +"%m/%d/%Y %T")-> VCFA Watcher started"  | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"

# while [[ $REBOOTS -lt 3 || "$POSTGRES" != "2/2" ]]; do
  CNT=0
  while ! $(sshpass -f /home/holuser/creds.txt ssh -q -o ConnectTimeout=5 "vmware-system-user@10.1.1.71" exit); do
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
    CONATAINERDREADY=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -s https://10.1.1.71:6443 get nodes '" | grep "Ready,SchedulingDisabled" | awk '{print $2}')
   
    if [ "$CONATAINERDREADY" == "Ready,SchedulingDisabled" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stale containerd found, restarting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'systemctl restart containerd'" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -s https://10.1.1.71:6443 get nodes'" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 2 ]; then
      NODENAME=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -s https://10.1.1.71:6443 get nodes '" | grep "Ready,SchedulingDisabled" | awk '{print $1}')
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -s https://10.1.1.71:6443 uncordon ${NODENAME}'"
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
    KUBESCHEDULER=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -n kube-system -s https://10.1.1.71:6443 get pods '" | grep "kube-scheduler" | grep "0/1" | awk '{print $2}')
   
    if [ "$KUBESCHEDULER" == "0/1" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stale kube-scheduler found, restarting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'systemctl restart containerd'" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 120
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -n kube-system -s https://10.1.1.71:6443 get pods'" | grep "kube-scheduler" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
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
    SEAWEEDPOD=$(sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -n vmsp-platform -s https://10.1.1.71:6443 get pods seaweedfs-master-0 -o json'" | \
    jq -r '. | select(.metadata.creationTimestamp | fromdateiso8601 < (now - 3600)) | .metadata.name ')
    
    if [ "$SEAWEEDPOD" == "seaweedfs-master-0" ]; then
      echo "$(date +"%m/%d/%Y %T")-> Stale seaweedfs-master-0 pod found, deleting..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -n vmsp-platform -s https://10.1.1.71:6443 delete pod seaweedfs-master-0'" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
      sleep 5
      sshpass -f /home/holuser/creds.txt ssh vmware-system-user@10.1.1.71 "sudo -i bash -c 'kubectl -n vmsp-platform -s https://10.1.1.71:6443 get pods | grep seaweedfs'" | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
    fi
    sleep 5
    if [ $CNT -eq 3 ]; then
      echo "$(date +"%m/%d/%Y %T")-> seaweedfs-master-0 check tried 3 times, continuing..." | tee -a  "${LOGFILE}" >> "${CONSOLELOG}"
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
