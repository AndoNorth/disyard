#!/usr/bin/env bash
# Runs the workflow harness with 2,000 jobs on fake pools back to back for at least DURATION seconds, with a snapshot
# between batches and at least every 10 minutes. The cluster lock is held by the caller.
# Usage: harness-loop.sh <duration seconds> <output directory>
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../../../.." && pwd)
duration=$1
out=$2
mkdir -p "$out"
cd "$root" || exit
export WFT_CLUSTER_LOCK_HELD=1
end=$(($(date +%s) + duration))
n=0
last=0
while [ "$(date +%s)" -lt "$end" ]; do
    n=$((n + 1))
    started=$(date +%s)
    nix develop -c uv run --frozen python docs/delivery/spikes/S13/code/harness_idem.py --kube-context kind-wft smoke --jobs 2000 \
        >"$out/harness-$n.log" 2>&1
    echo "batch $n exit $? in $(($(date +%s) - started)) s: $(grep -E '^(finished|failed|infra_retries):' "$out/harness-$n.log" | tr '\n' ' ')"
    if [ $(($(date +%s) - last)) -ge 600 ]; then
        python3 "$here/snapshot.py" "harness-$n" >>"$out/snapshots.jsonl"
        last=$(date +%s)
    fi
done
python3 "$here/snapshot.py" "harness-end" >>"$out/snapshots.jsonl"
