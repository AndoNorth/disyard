#!/usr/bin/env bash
# The smoke gate: after a deploy, says whether the platform is healthy within a 300 s budget, by checking that every
# layer answers and that one level 0 job goes through and produces the known answer.
# Attaches to the running cluster wft and never starts, deploys or tears one down.
# The driver prints one "ok|FAIL|skip <row> <detail>" line per row, then a count line.
# Exit 0 every row ok, 1 a row failed or the budget ran out, 2 usage, 4 refused: no cluster wft, one started for
# another preset, or the cluster lock was not taken in time.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

preset=${WFT_PRESET:-ci}
budget_s=300

# Only ci deploys the pool workers the job runs on.
[ "$preset" = ci ] || { echo "usage: smoke needs preset ci, the only preset that deploys the pool workers, not $preset" >&2; exit 2; }
for tool in kind kubectl flock timeout uv; do
    command -v "$tool" >/dev/null || { echo "usage: $tool not on PATH, run inside nix develop" >&2; exit 2; }
done

# Unlike up.sh, a lock wait that runs out is a refusal, so a queued gate is not mistaken for a failed one.
if [ -z "${WFT_CLUSTER_LOCK_HELD:-}" ]; then
    exec 9>/tmp/wft-cluster.lock
    flock -w 900 9 || { echo "refused: could not take /tmp/wft-cluster.lock in 900 s" >&2; exit 4; }
fi
export WFT_CLUSTER_LOCK_HELD=1

# On the budget the driver gets SIGINT to print its last rows and cancel its job, and is killed 10 s later.
rc=0
timeout -s INT -k 10 "$budget_s" uv run --frozen python -m wft_acceptance.smoke || rc=$?
case $rc in
    0 | 1 | 2 | 4) exit "$rc" ;;
    124) exit 1 ;;
    137)
        echo "FAIL budget"
        echo "smoke $preset: budget ran out after $budget_s s"
        exit 1
        ;;
    *)
        echo "FAIL driver exited $rc"
        exit 1
        ;;
esac
