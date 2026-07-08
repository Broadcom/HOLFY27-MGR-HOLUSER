#!/usr/bin/bash
#
# This script, if present in your vpodrepo root, is run at the end of labsartup.
# It is called during the "final.py"
# Here's the overall flow:
# prelim.py -> ESXi.py -> VCF.py -> VVF.py -> vSphere.py -> pings.py -> services.py -> Kubernetes.py -> urls.py -> VCFfinal.py -> final.py -> odyssey.py 
#
# If you prefer scripting in Python:
# You may optionally place a "lab-update.py" in this folder and it would be called immediately folling the call of this lab-update.sh script
# 
# Source the .bashrc file for settings/paths/etc...
. /home/holuser/.bashrc
# Insert your custom code here:
echo "VXP lab-update.sh ran"

# Read vPod_SKU from the vPod.txt file
# shellcheck source=/dev/null
. /lmchol/hol/vPod.txt

# Update with additional case values, script calls as needed
case "$vPod_SKU" in
	"VXP-K8s-91")
		echo "Processing Lab Updates for vPod_SKU: $vPod_SKU"
    # Add your custom script(s)/command calls for VXP-K8s-91 here...
		;;
	"VXP-91-PAIS")
		echo "Processing AI Lab Updates for vPod_SKU: $vPod_SKU"
    # Add your custom script(s)/command calls for VXP-91-PAIS here...
		;;
	*)
		echo "No matching lab update logic for vPod_SKU: $vPod_SKU"
		;;
esac

# Example to echo text into file on Console VM. 
# NOTE: when this script runs, /lmchol is mounted to the "/" of the Console VM
# echo "Functional Testing!" > /lmchol/home/holuser/Documents/FT.txt