#!/usr/bin/env bash
# Stops the node containers of the kind cluster dsy and keeps the cluster, the registry and the host data directory.
# `just up` starts the same containers again. A host with no cluster is already down.
# Exit 0 stopped or nothing to stop, 1 a container did not stop, 4 refused: the cluster lock was not taken in time.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck source=/dev/null
source scripts/cluster/lock.sh
cluster_lock 4

running=$(docker ps -q --filter label=io.x-k8s.kind.cluster=dsy)
if [ -z "$running" ]; then
    echo "cluster dsy is already down"
    exit 0
fi
# shellcheck disable=SC2086
docker stop $running >/dev/null || { echo "could not stop the node containers of dsy" >&2; exit 1; }
echo "cluster dsy stopped, data kept"
