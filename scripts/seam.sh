#!/usr/bin/env bash
# Usage: seam.sh
# Level 3a: runs `pytest -m seam` once per group and prints one summary line per group that has tests, then the total.
# pytest exit 5 (nothing collected) means the group has no tests yet and is skipped, and exit 4 is a refusal:
# the T1 lock wait ran out.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "error: uv not on PATH, run inside nix develop" >&2; exit 1; }

shopt -s nullglob
# A group whose path does not exist yet has no tests, and the glob drops it.
boot=(packages/*/tests/service packages/servicekit/tests/test_lifecycle.py)
contract=(packages/*/tests/service/test_contract.py packages/servicekit/tests/test_testing.py)
example=(packages/exampleflow/tests*)
layering=(packages/servicekit/tests/test_layering.py)

budget_s=300
start=$SECONDS
group() {
    local name=$1 ignore=$2 rc=0 begin=$SECONDS
    shift 2
    [ "$#" -gt 0 ] || return 0
    # The budget is shared by the groups, so a slow level fails loudly and is not skipped.
    timeout -k 10 "$((budget_s - (SECONDS - start)))" uv run --frozen pytest -m seam -q ${ignore:+--ignore-glob="$ignore"} "$@" || rc=$?
    if [ "$rc" -eq 5 ]; then return 0; fi
    echo "seam $name: $([ "$rc" -eq 0 ] && echo ok || echo "failed (exit $rc)") in $((SECONDS - begin)) s"
    return "$rc"
}

group boot '*/test_contract.py' "${boot[@]}"
group contract '' "${contract[@]}"
group example '' "${example[@]}"
group layering '' "${layering[@]}"
echo "seam total: $((SECONDS - start)) s of $budget_s s"
