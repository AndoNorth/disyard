#!/usr/bin/env bash
# Spike S12: the start order of one single-slot worker on a fresh pool-shaped queue, against one
# Temporal dev server started through dsy_itest. Run in the dev shell:
#   nix develop -c docs/delivery/spikes/S12/code/run.sh [measure.py flags]
# Takes one T1 lock file through dsy_itest, removes the container it started, and prints one
# `figure <name> <value>` line per number of docs/delivery/spikes/S12/queue-order.md.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)
exec uv run --frozen --project "$root" python "$root/docs/delivery/spikes/S12/code/measure.py" "$@"
