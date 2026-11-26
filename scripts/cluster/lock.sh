#!/usr/bin/env bash
# The one cluster lock: source this file, then call `cluster_lock <exit_code>`.
# Takes fd 9 on /tmp/dsy-cluster.lock unless a caller already holds it, waiting DSY_CLUSTER_LOCK_TIMEOUT seconds
# (default 900). A wait that runs out prints a refusal and exits with <exit_code>.
# Always exports DSY_CLUSTER_LOCK_HELD=1, so a script this one calls does not wait on its caller.

cluster_lock() {
    local exit_code=${1:?usage: cluster_lock <exit_code>}
    local timeout_s=${DSY_CLUSTER_LOCK_TIMEOUT:-900}
    if [ -z "${DSY_CLUSTER_LOCK_HELD:-}" ]; then
        exec 9>/tmp/dsy-cluster.lock
        flock -w "$timeout_s" 9 || { echo "refused: could not take /tmp/dsy-cluster.lock in $timeout_s s" >&2; exit "$exit_code"; }
    fi
    export DSY_CLUSTER_LOCK_HELD=1
}
