# Sourced by the scripts that reach a cluster service through a background kubectl port-forward.
# forward_start <log> <kubectl port-forward args...> starts the forward as kubectl itself, so $forward is the process
# that holds the port, with the cluster lock descriptor closed so the forward can never keep the lock after its caller
# exits. forward_stop ends it and removes the log. Install it as the EXIT trap so every exit path, a failure included,
# cleans up: trap 'forward_stop "$log"' EXIT
forward=
forward_start() {
    local log=$1
    shift
    kubectl --context "${context:-kind-dsy}" -n "${namespace:-dsy-infra}" port-forward --address 127.0.0.1 "$@" \
        >"$log" 2>&1 9>&- &
    forward=$!
}
forward_stop() {
    [ -z "$forward" ] || kill "$forward" 2>/dev/null || true
    [ -z "${1:-}" ] || rm -f "$1"
}
forward_alive() { kill -0 "$forward" 2>/dev/null; }
