#!/usr/bin/env bash
# Starts Tilt for a preset: `tilt ci` for the unattended presets (ci, soak), `tilt up` for dev.
# The observability switch is the value recorded for the cluster, exported as DSY_OBS over the caller's own.
# ci and soak take the cluster lock and hold it for the run; dev is interactive and stays unlocked.
# Usage: run.sh <ci|dev|soak> [tilt args...]. Exit codes: tilt's own, 2 usage, 4 refused: the lock was not taken.
set -euo pipefail
cd "$(dirname "$0")/../.."
DSY_OBS=$(scripts/cluster/obs.sh)
export DSY_OBS

preset=${1:-}
[ $# -gt 0 ] && shift
case $preset in
    ci | soak)
        # shellcheck source=/dev/null
        source scripts/cluster/lock.sh
        cluster_lock 4
        exec tilt ci "$@" -- --profile "$preset"
        ;;
    dev) exec tilt up "$@" -- --profile dev ;;
    *) echo "usage: run.sh <ci|dev|soak> [tilt args...]" >&2; exit 2 ;;
esac
