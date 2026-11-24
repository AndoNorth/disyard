#!/usr/bin/env bash
# Series and keys spike: brings up the ci preset with its ci profile modules, drives the Temporal probe through
# port-forwards on 127.0.0.1, runs the newest kube-state-metrics, an OOM pod, a limit-bound pod and two local-path
# claims on the obs node, and prints every series line docs/delivery/spikes/S14/series.md names.
# Run in the dev shell from the repository root, holding the cluster lock:
#   flock /tmp/dsy-cluster.lock env DSY_CLUSTER_LOCK_HELD=1 nix develop -c docs/delivery/spikes/S14/code/run.sh [out-dir]
# Destroys the cluster at the end, unless DSY_S14_KEEP=1.
# Exit 0 the run completed, 1 a step failed, 2 usage, 4 refused: too little memory or the lock is not held.
set -uo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)
cd "$root" || exit 2
[ $# -le 1 ] || { echo "usage: run.sh [out-dir]" >&2; exit 2; }
[ -n "${DSY_CLUSTER_LOCK_HELD:-}" ] || { echo "refused: run under flock /tmp/dsy-cluster.lock" >&2; exit 4; }
out=${1:-$(mktemp -d "${TMPDIR:-/tmp}/dsy-s14.XXXXXX")}
mkdir -p "$out"
code=docs/delivery/spikes/S14/code
ksm=registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0@sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b
canary="busybox:$(yq '.images.canary.version' deploy/versions.yaml)@$(yq '.images.canary.digest' deploy/versions.yaml)"
ctx=kind-dsy
obs_node_label=dsy.dev/pool=obs
slot=$(basename "$(dirname "$root")")
[[ $slot =~ ^[0-9]$ ]] || slot=0
base=$((19000 + slot * 10))
k() { kubectl --context "$ctx" "$@"; }
say() { echo "[$(date +%T)] $*" >&2; }
pids=()

# The 12,032 MiB of the ci preset plus 512 MiB for the host worker and the port-forwards.
need_kb=$(((12032 + 512) * 1024))
have_kb=$(awk '/^MemAvailable:/ { print $2 }' /proc/meminfo)
[ "$have_kb" -ge "$need_kb" ] || { echo "refused: MemAvailable $have_kb kB, need $need_kb kB" >&2; exit 4; }

# shellcheck disable=SC2329
cleanup() {
    for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
    if [ "${DSY_S14_KEEP:-}" != 1 ]; then
        say "destroying the cluster"
        scripts/nuke.sh >"$out/nuke.log" 2>&1 || say "nuke failed, see $out/nuke.log"
    fi
}
trap cleanup EXIT

forward() {
    k -n "$1" port-forward --address 127.0.0.1 "$2" "$3" >>"$out/port-forward.log" 2>&1 &
    pids+=($!)
}

say "cluster up"
scripts/up.sh >"$out/up.log" 2>&1 || { say "up failed, see $out/up.log"; exit 1; }
say "ci profile modules"
scripts/tilt/run.sh ci --timeout 40m >"$out/tilt.log" 2>&1 || { say "tilt failed, see $out/tilt.log"; exit 1; }
node=$(k get node -l "$obs_node_label" -o jsonpath='{.items[0].metadata.name}')
echo "== versions"
k get node "$node" -o jsonpath='kubelet {.status.nodeInfo.kubeletVersion}, {.status.nodeInfo.containerRuntimeVersion}{"\n"}'
docker exec "$node" stat -fc 'node cgroup filesystem %T' /sys/fs/cgroup
k -n dsy-infra get pod -l app.kubernetes.io/component=matching -o jsonpath='temporal server {.items[0].status.containerStatuses[0].image}{"\n"}'
uv run --frozen python -c 'import importlib.metadata as m; print("temporalio", m.version("temporalio"))'
echo "kube-state-metrics $ksm"

echo "== temporal"
forward dsy-infra svc/temporal-frontend "$((base + 3)):7233"
forward dsy-infra deploy/temporal-matching "$((base + 4)):9090"
forward dsy-infra deploy/temporal-history "$((base + 5)):9090"
sleep 3
uv run --frozen python "$code/temporal_probe.py" --frontend "127.0.0.1:$((base + 3))" \
    --matching "http://127.0.0.1:$((base + 4))/metrics" --history "http://127.0.0.1:$((base + 5))/metrics" \
    --sdk-port "$((base + 6))" 2>"$out/temporal-probe.err" || { say "probe failed, see $out/temporal-probe.err"; exit 1; }

say "obs node pods"
docker pull -q "$ksm" >/dev/null && docker tag "$ksm" localhost:5001/s14-ksm:pinned && docker push -q localhost:5001/s14-ksm:pinned >/dev/null || exit 1
docker pull -q "$canary" >/dev/null && docker tag "$canary" localhost:5001/canary:pinned && docker push -q localhost:5001/canary:pinned >/dev/null || exit 1
for f in obs-node volumes; do
    sed -e 's#KSM_IMAGE#localhost:5001/s14-ksm:pinned#' -e 's#CANARY_IMAGE#localhost:5001/canary:pinned#' "$code/$f.yaml" >"$out/$f.yaml"
    k apply -f "$out/$f.yaml" >/dev/null || exit 1
done
k -n dsy-obs rollout status deploy/s14-ksm --timeout=180s >/dev/null || exit 1
k -n dsy-obs wait --for=condition=Ready pod/s14-volumes pod/s14-limit --timeout=180s >/dev/null || exit 1
# Two OOM kills and restarts, the limit pod's writes, and a kubelet volume stats period.
sleep 120
k -n dsy-obs get pods -o wide

echo "== kube-state-metrics"
forward dsy-obs deploy/s14-ksm "$((base + 7)):8080"
sleep 3
curl -s "127.0.0.1:$((base + 7))/metrics" | grep 'pod="s14-oom"' | grep -E 'last_terminated|restarts_total'

echo "== cadvisor on $node"
k get --raw "/api/v1/nodes/$node/proxy/metrics/cadvisor" >"$out/cadvisor.txt"
grep -E '^container_(memory_failcnt|oom_events_total|pressure_memory_(stalled|waiting)_seconds_total)\{' "$out/cadvisor.txt" |
    grep 'pod="s14-limit"' | sed -E 's/,(id|image|name)="[^"]*"//g'
echo "== memory.events of s14-limit, pod cgroup and container cgroups, read on the node"
uid=$(k -n dsy-obs get pod s14-limit -o jsonpath='{.metadata.uid}' | tr - _)
cg=/sys/fs/cgroup/kubelet.slice/kubelet-kubepods.slice/kubelet-kubepods-pod$uid.slice
docker exec "$node" sh -c "echo pod \$(grep -E '^(max|oom_kill) ' $cg/memory.events | tr '\n' ' ');
    for c in $cg/cri-containerd-*.scope; do echo \${c##*/} \$(grep -E '^(max|oom_kill) ' \$c/memory.events | tr '\n' ' '); done"
echo "== memory pressure on every node: containers above 0.01 s stalled, other than s14-limit"
for n in $(k get nodes -o jsonpath='{.items[*].metadata.name}'); do
    k get --raw "/api/v1/nodes/$n/proxy/metrics/cadvisor" |
        grep -E '^container_pressure_memory_stalled_seconds_total\{container="[a-z]' | grep -v 'pod="s14-limit"' |
        sed -E 's/,(id|image|name)="[^"]*"//g' | awk -v n="$n" '$2 > 0.01 { print n, $1, $2 }'
done
echo "end of list"

echo "== kubelet volume stats on $node"
k get pv -o custom-columns='CLAIM:.spec.claimRef.name,HOSTPATH:.spec.hostPath.path,LOCAL:.spec.local.path' | grep -E '^CLAIM|s14-'
k get --raw "/api/v1/nodes/$node/proxy/metrics" | grep -E '^kubelet_volume_stats_(used|capacity)_bytes' || echo "no kubelet_volume_stats series"
k -n dsy-obs exec s14-volumes -- du -sk /hostpath /local

k delete -f "$out/volumes.yaml" -f "$out/obs-node.yaml" --wait=false >/dev/null
say "done"
