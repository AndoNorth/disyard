#!/usr/bin/env bash
# Stops the node containers of the kind cluster wft and keeps the cluster, the registry and the host data directory.
# `just up` starts the same containers again. A host with no cluster is already down.
# Exit 0 stopped or nothing to stop, 1 a container did not stop.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -z "${WFT_CLUSTER_LOCK_HELD:-}" ]; then
    exec 9>/tmp/wft-cluster.lock
    flock -w 900 9 || { echo "could not take /tmp/wft-cluster.lock" >&2; exit 1; }
fi

running=$(docker ps -q --filter label=io.x-k8s.kind.cluster=wft)
if [ -z "$running" ]; then
    echo "cluster wft is already down"
    exit 0
fi
# shellcheck disable=SC2086
docker stop $running >/dev/null || { echo "could not stop the node containers of wft" >&2; exit 1; }
echo "cluster wft stopped, data kept"
