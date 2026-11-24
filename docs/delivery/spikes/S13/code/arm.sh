#!/usr/bin/env bash
# One arm of the wiring comparison: restarts every service and pool worker so each container's peak counts only this
# arm, snapshots, runs the smoke job three times and the staging-cap job once, and snapshots again.
# The cluster lock is held by the caller. Usage: arm.sh <label> <output directory>
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../../../.." && pwd)
label=$1
out=$2
mkdir -p "$out"
k="kubectl --context kind-dsy"
cd "$root"
export DSY_CLUSTER_LOCK_HELD=1

for d in admission dispatch placement; do $k -n dsy-control rollout restart deployment/$d; done
$k -n dsy-control rollout restart deployment/"$($k -n dsy-control get deploy -o name | grep workflow-worker | cut -d/ -f2)"
for d in compile-worker simulate-worker aggregate-worker; do $k -n dsy-workers rollout restart deployment/$d; done
for d in admission dispatch placement; do $k -n dsy-control rollout status deployment/$d --timeout=300s; done
$k -n dsy-control rollout status deployment/"$($k -n dsy-control get deploy -o name | grep workflow-worker | cut -d/ -f2)" --timeout=300s
for d in compile-worker simulate-worker aggregate-worker; do $k -n dsy-workers rollout status deployment/$d --timeout=600s; done
sleep 60
python3 "$here/snapshot.py" "$label-start" >>"$out/snapshots.jsonl"

for n in 1 2 3; do
    nix develop -c timeout -s INT -k 10 300 uv run --frozen python "$here/smoke_s13.py" >"$out/$label-smoke-$n.log" 2>&1 || true
    tail -3 "$out/$label-smoke-$n.log"
    python3 "$here/snapshot.py" "$label-smoke-$n" >>"$out/snapshots.jsonl"
done
nix develop -c uv run --frozen python "$here/capjob.py" >"$out/$label-cap.log" 2>&1 || true
cat "$out/$label-cap.log"
python3 "$here/snapshot.py" "$label-cap" >>"$out/snapshots.jsonl"
