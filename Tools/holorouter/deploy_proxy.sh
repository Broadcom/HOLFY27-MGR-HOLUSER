#!/bin/bash
# deploy_proxy.sh
# Deploys certsrv_proxy.py to the holorouter via the NFS mount
# and updates the DaemonSet automatically via the router's watcher script.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SRC="${SCRIPT_DIR}/certsrv_proxy.py"
NFS_MOUNT="/tmp/holorouter"
DEST="${NFS_MOUNT}/certsrv_proxy.py"

if [ ! -f "${PROXY_SRC}" ]; then
    echo "ERROR: ${PROXY_SRC} not found."
    exit 1
fi

if [ ! -d "${NFS_MOUNT}" ]; then
    echo "ERROR: NFS export directory ${NFS_MOUNT} not found."
    exit 1
fi

echo "Copying certsrv_proxy.py to ${DEST}..."
cp "${PROXY_SRC}" "${DEST}"

echo "Deployment complete. The holorouter's watcher.sh will detect the change within 5 seconds and restart the proxy pod."
