#!/usr/bin/env bash
# Brings up the kind cluster dsy and its local registry for DSY_PRESET (default ci) through ctlptl.
# DSY_OBS=on turns the observability switch on: the obs node takes the preset's obs_stack size, and the value is
# recorded beside the owner, because the node size is fixed when the cluster is created.
# Applies the namespaces and quotas of the preset once every node is Ready.
# A second run with nothing changed leaves every container as it was.
# Exit 0 every node Ready, 1 the cluster did not come up, 2 usage (DSY_OBS=on on a preset without obs_stack or under
# GitHub Actions), 4 refused: the preset needs more memory than the host has, or the cluster dsy is one this script did
# not start (or started for another preset, or with the other switch value).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/verify/cluster/lib/tools.sh

preset=${DSY_PRESET:-ci}
# shellcheck source=/dev/null
source scripts/lib/state.sh
state=$(state_dir)
context=kind-dsy

obs=${DSY_OBS:-off}

refuse() { echo "refused: $*" >&2; exit 4; }

# GitHub CI never deploys the observability stack.
[ "$obs" = on ] && [ "${GITHUB_ACTIONS:-}" = true ] && { echo "usage: DSY_OBS=on is refused under GitHub Actions" >&2; exit 2; }
scripts/cluster/obs.sh preset "$preset" >/dev/null || exit $?

# Every recipe that touches the cluster holds one lock, unless a caller already holds it for a longer sequence.
# shellcheck source=/dev/null
source scripts/cluster/lock.sh
cluster_lock 1

if [ -f "$state/cluster.owner" ] && kind get clusters 2>/dev/null | grep -qx dsy; then
    recorded=$(scripts/cluster/obs.sh)
    [ "$recorded" = "$obs" ] || refuse "cluster dsy was started with DSY_OBS=$recorded, not $obs, run with DSY_OBS=$recorded or delete it with: just nuke"
fi

# A new cluster binds the registry and the host ports, and a running one holds them itself.
kind get clusters 2>/dev/null | grep -qx dsy || scripts/check-dev-ports.sh || exit 1

out=$(scripts/cluster/render.sh "$preset") || exit $?

need=$(yq "$([ "$obs" = on ] && echo .obs_memory_mib || echo .memory_mib)" "deploy/presets/$preset.yaml")
have=$(($(sed -n 's/^MemTotal: *\([0-9]*\) kB.*/\1/p' /proc/meminfo) / 1024))
[ "$have" -ge "$need" ] || refuse "preset $preset with DSY_OBS=$obs needs $need MiB and the host has $have MiB"

# The owner file is written before the cluster exists, so a run that dies half way still owns what it created.
if kind get clusters 2>/dev/null | grep -qx dsy; then
    started=$(cat "$state/cluster.owner" 2>/dev/null || true)
    [ -n "$started" ] || refuse "a kind cluster named dsy exists that this script did not start, delete it with: kind delete cluster --name dsy"
    [ "$started" = "$preset" ] || refuse "cluster dsy was started for preset $started, not $preset, delete it with: kind delete cluster --name dsy"
else
    mkdir -p "$state"
    echo "$preset" >"$state/cluster.owner"
    echo "$obs" >"$state/cluster.obs"
fi

# A node container that exited is started again, because the API server keeps reporting its node Ready for a while.
stopped=$(docker ps -aq --filter label=io.x-k8s.kind.cluster=dsy --filter status=exited --filter status=created)
# shellcheck disable=SC2086
# ctlptl recreates a cluster whose API server does not answer, so a restarted cluster is waited for before it is applied.
if [ -n "$stopped" ]; then
    echo "starting stopped node containers: $stopped"
    docker start $stopped >/dev/null
    for _ in $(seq 120); do
        kubectl --context "$context" get --raw /readyz >/dev/null 2>&1 && break
        sleep 1
    done
fi

ctlptl apply -f "$out/registry.yaml" -f "$out/cluster.yaml"
scripts/cluster/limits.sh "$out/limits.yaml"

want=$(yq '.nodes[].count' "deploy/presets/$preset.yaml" | awk '{ n += $1 } END { print n }')
kubectl --context "$context" wait --for=condition=Ready node --all --timeout=300s
got=$(kubectl --context "$context" get nodes --no-headers | wc -l)
[ "$got" -eq "$want" ] || { echo "cluster dsy has $got nodes, preset $preset has $want" >&2; exit 1; }
# kind drops a kubeadm patch that does not match its generated apiVersion without a word, so the pool labels are checked.
pool_label=$(yq '.pool_label' contracts/topology/topology.yaml)
pooled=$(yq '.nodes[] | select(.role != "control-plane") | .count' "deploy/presets/$preset.yaml" | awk '{ n += $1 } END { print n }')
labelled=$(kubectl --context "$context" get nodes -l "$pool_label" --no-headers | wc -l)
[ "$labelled" -eq "$pooled" ] || {
    echo "$labelled of $pooled pool nodes carry the $pool_label label, delete the cluster with: kind delete cluster --name dsy" >&2
    exit 1
}
running=$(docker ps -q --filter label=io.x-k8s.kind.cluster=dsy --filter status=running | wc -l)
[ "$running" -eq "$want" ] || { echo "cluster dsy has $running running node containers, preset $preset has $want" >&2; exit 1; }
# The kubelet reservation is written when a node is created, so a preset resized after that ends in a mismatch here.
scripts/cluster/allocatable.sh "$out/limits.yaml" || {
    echo "node allocatable differs from preset $preset, a resized preset needs the cluster created again: just nuke, then just up" >&2
    exit 1
}
scripts/cluster/namespaces.sh apply "$preset"
echo "cluster dsy ready: preset $preset, DSY_OBS=$obs, $got nodes"
