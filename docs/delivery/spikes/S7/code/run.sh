#!/usr/bin/env bash
# Spike S7: one submit call of 100,000 jobs through the D8 library sequence, against one T1 Postgres
# started through dsy_itest, in chunks of 500, 1,000 and 2,000. Run in the dev shell:
#   nix develop -c docs/delivery/spikes/S7/code/run.sh [measure.py flags]
# Takes one T1 lock file through dsy_itest, removes the container it started, and prints one
# `figure <name> <value>` line per number of docs/delivery/spikes/S7/batch-submit.md.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)
exec uv run --frozen --project "$root" python "$root/docs/delivery/spikes/S7/code/measure.py" "$@"
