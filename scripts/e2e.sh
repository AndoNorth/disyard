#!/usr/bin/env bash
# The end to end suite (level 4): runs the smoke gate first, then the scenarios, in a 600 s budget.
# Attaches to the running cluster dsy with the observability stack on and never starts, deploys or tears one down.
# The suite prints one "ok|FAIL|skip <scenario> <detail>" line per scenario, then a count line.
# Exit 0 every scenario ok, 1 a scenario or the smoke gate failed or the budget ran out, 2 usage, 4 refused: no cluster
# dsy, the observability stack off, or the cluster lock was not taken in time.
# A caller that holds the cluster lock for several runs sets DSY_CLUSTER_LOCK_HELD, which this script then does not take.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

preset=${DSY_PRESET:-ci}
budget_s=600

# Only ci deploys the pool workers the jobs run on, and with the stack on, the query step.
[ "$preset" = ci ] || { echo "usage: e2e needs preset ci, the only preset that deploys the pool workers, not $preset" >&2; exit 2; }
for tool in kind kubectl flock timeout uv; do
    command -v "$tool" >/dev/null || { echo "usage: $tool not on PATH, run inside nix develop" >&2; exit 2; }
done

# A lock wait that runs out is a refusal, so a queued run is not mistaken for a failed one.
if [ -z "${DSY_CLUSTER_LOCK_HELD:-}" ]; then
    exec 9>/tmp/dsy-cluster.lock
    flock -w 900 9 || { echo "refused: could not take /tmp/dsy-cluster.lock in 900 s" >&2; exit 4; }
fi
export DSY_CLUSTER_LOCK_HELD=1

[ "$(scripts/cluster/obs.sh)" = on ] || { echo "refused: the cluster has the observability stack off, start it with DSY_OBS=on just up" >&2; exit 4; }
case $'\n'$(kind get clusters 2>/dev/null)$'\n' in
    *$'\n'dsy$'\n'*) ;;
    *) echo "refused: no kind cluster dsy, start one with just up" >&2; exit 4 ;;
esac

# Every level runs only when the level below it is green, and smoke reports its own rows and exit.
rc=0
scripts/smoke.sh || rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

# On the budget the suite gets SIGINT to restore shared state and print its last lines, and is killed 10 s later.
rc=0
timeout -s INT -k 10 "$budget_s" uv run --frozen pytest packages/acceptance/tests/e2e -q -p no:cacheprovider || rc=$?
case $rc in
    0 | 1) exit "$rc" ;;
    124) exit 1 ;;
    137)
        echo "FAIL budget"
        echo "e2e $preset: budget ran out after $budget_s s"
        exit 1
        ;;
    5)
        echo "FAIL scenarios none collected"
        exit 1
        ;;
    *)
        echo "FAIL suite exited $rc"
        exit 1
        ;;
esac
