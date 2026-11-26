#!/usr/bin/env bash
# Checks that the cluster dsy is the one its preset describes and that every module the preset's profile enables is
# ready, without starting a pod or changing anything, so smoke can call it before it drives a workload.
# Static checks need no cluster: the preset and topology loaders, the pod slots and the module budget.
# Live checks: nodes, pool labels and taints; node container limits and allocatable; the namespace quotas and
# LimitRanges; each module's own readiness; live requests per node within the budget; no Pending pod, quota refusal,
# OOM kill or memory pressure in the infra and control namespaces; and every host port bound to loopback only.
# The worker namespace is left out of the pressure check, because pool OOM kills are workload behaviour.
# The observability switch is the value recorded for the cluster, exported as DSY_OBS over the caller's own, so the
# budget, render and quota checks see the obs node that exists. DSY_MODULES_DIR names the directory searched for
# module.yaml (default deploy).
# DSY_PRESET selects the preset (default ci). Every check prints "ok <check> <detail>" or "FAIL <check> <detail>",
# then a count line follows.
# Exit 0 every check ok, 1 a check failed, 2 usage, 4 refused: no cluster dsy, or one started for another preset.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/verify/cluster/lib/tools.sh

[ $# -eq 0 ] || { echo "usage: cluster-verify.sh, with the preset in DSY_PRESET" >&2; exit 2; }
preset=${DSY_PRESET:-ci}
file=deploy/presets/$preset.yaml
[ -f "$file" ] || { echo "usage: no preset $file" >&2; exit 2; }
state=${DSY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/disyard}
context=kind-dsy
k() { kubectl --context "$context" "$@"; }
refuse() { echo "refused: $*" >&2; exit 4; }

# shellcheck source=/dev/null
source scripts/cluster/lock.sh
cluster_lock 1

kind get clusters 2>/dev/null | grep -qx dsy || refuse "no kind cluster named dsy, start one with: just up"
started=$(cat "$state/cluster.owner" 2>/dev/null || true)
[ "$started" = "$preset" ] || refuse "cluster dsy was started for preset ${started:-unknown}, not $preset"
DSY_OBS=$(scripts/cluster/obs.sh)
export DSY_OBS

topology=contracts/topology/topology.yaml
pool_label=$(yq '.pool_label' "$topology")
taint_effect=$(yq '.taint_effect' "$topology")
profiles=deploy/tilt/profiles.yaml
groups=" $(scripts/cluster/obs.sh groups "$preset") "
modules_dir=${DSY_MODULES_DIR:-deploy}
module_profile=$(yq ".profiles.$preset.modules // \"$preset\"" "$profiles")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

passed=0 failed=0
ok() { echo "ok $1 $2"; passed=$((passed + 1)); }
fail() { echo "FAIL $1 $2"; failed=$((failed + 1)); }
milli() { case $1 in *m) echo "${1%m}" ;; *) awk -v c="$1" 'BEGIN { printf "%.0f", c * 1000 }' ;; esac; }
mib() {
    case $1 in
        *Gi) echo $((${1%Gi} * 1024)) ;; *Mi) echo "${1%Mi}" ;; *Ki) echo $((${1%Ki} / 1024)) ;;
        *G) echo $((${1%G} * 1000000000 / 1048576)) ;; *M) echo $((${1%M} * 1000000 / 1048576)) ;; *) echo $(($1 / 1048576)) ;;
    esac
}

check_preset() {
    local out
    if out=$(contracts/preset/check.sh "$file" 2>&1 && contracts/preset/check.sh "$(yq '.host.ports' "$file")" 2>&1 &&
        contracts/topology/check.sh "$topology" 2>&1); then
        ok preset "$file and $topology valid"
    else
        fail preset "$(tail -n 1 <<<"$out")"
    fi
}

check_slots() {
    local out
    if out=$(scripts/cluster/slots.sh "$preset" 2>&1); then
        ok slots "$(grep -c fits <<<"$out") slots fit their pool node"
    else
        fail slots "$(grep -v fits <<<"$out" | head -n 1)"
    fi
}

check_budget() {
    if scripts/cluster/budget.sh "$preset" >"$work/budget" 2>&1; then
        ok budget "$(grep '^node infra' "$work/budget" | sed 's/^node infra requests //; s/, free/ free/; s/, fits//' | paste -sd ';' | sed 's/;/; /g')"
    else
        fail budget "$(grep -E 'OVER|usage|bad' "$work/budget" | paste -sd ';' | sed 's/;/; /g')"
    fi
}

# One line per node: name, pool, Ready status, the pool taint as value:effect, with "-" for none.
k_nodes() {
    k get nodes -o json | POOL="$pool_label" yq -p json '.items[] | .metadata.name + " " +
        (.metadata.labels[strenv(POOL)] // "-") + " " +
        (.status.conditions[] | select(.type == "Ready") | .status) + " " +
        (([(.spec.taints // [])[] | select(.key == strenv(POOL)) | .value + ":" + .effect] | join(",") | select(. != "")) // "-")'
}

check_nodes() {
    local role count pool n
    k_nodes >"$work/nodes"
    while read -r role count; do
        if [ "$role" = control-plane ]; then
            pool=- want="- True -"
        else
            pool=$(yq ".pools.$role" "$topology") want="$pool True $pool:$taint_effect"
        fi
        for ((n = 0; n < count; n++)); do echo "$want"; done
    done < <(yq '.nodes[] | .role + " " + .count' "$file") | sort >"$work/nodes.want"
    cut -d' ' -f2- "$work/nodes" | sort >"$work/nodes.got"
    if cmp -s "$work/nodes.want" "$work/nodes.got"; then
        ok nodes "$(wc -l <"$work/nodes.got") nodes Ready with the pools and taints of $file"
    else
        fail nodes "want [$(paste -sd ',' "$work/nodes.want")] got [$(paste -sd ',' "$work/nodes.got")]"
    fi
}

check_limits() {
    local container cpus mem swap got want bad=""
    if ! DSY_RENDER_DIR="$work/render" scripts/cluster/render.sh "$preset" >/dev/null 2>"$work/render.err"; then
        fail limits "render: $(tail -n 1 "$work/render.err")"
        return
    fi
    local limits=$work/render/$preset/limits.yaml
    while read -r container cpus mem swap; do
        got=$(docker inspect --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' "$container" 2>/dev/null || echo missing)
        want="$(awk -v c="$cpus" 'BEGIN { printf "%.0f", c * 1e9 }') $((mem * 1048576)) $((swap * 1048576))"
        [ "$got" = "$want" ] || bad+=" $container ($got, want $want)"
    done < <(yq '.nodes[] | .container + " " + .cpus + " " + .memory_mib + " " + .memory_swap_mib' "$limits")
    scripts/cluster/allocatable.sh "$limits" >"$work/allocatable" 2>&1 || bad+=" allocatable: $(grep -c MISMATCH "$work/allocatable") nodes differ"
    if [ -z "$bad" ]; then
        ok limits "$(yq '.nodes | length' "$limits") node containers at their preset size and allocatable"
    else
        fail limits "${bad# }"
    fi
}

check_quotas() {
    local ns bad=""
    scripts/cluster/namespaces.sh render "$preset" >"$work/quotas"
    for ns in $(yq '.namespaces | keys | .[]' "$topology"); do
        [ "$(k -n "$ns" get resourcequota -o name 2>/dev/null | paste -sd ' ')" = resourcequota/pool-capacity ] || { bad+=" $ns: not one quota pool-capacity"; continue; }
        [ "$(k -n "$ns" get limitrange -o name 2>/dev/null | paste -sd ' ')" = limitrange/defaults ] || bad+=" $ns: not one LimitRange defaults"
        diff -q <(k -n "$ns" get resourcequota pool-capacity -o yaml | yq '.spec.hard | to_entries | .[] | .key + "=" + .value' | sort) \
            <(NS="$ns" yq 'select(.metadata.namespace == strenv(NS)) | .spec.hard | to_entries | .[] | .key + "=" + .value' "$work/quotas" | sort) >/dev/null ||
            bad+=" $ns: quota differs from namespaces.sh render $preset"
    done
    if [ -z "$bad" ]; then ok quotas "every namespace has its rendered quota and LimitRange"; else fail quotas "${bad# }"; fi
}

# shellcheck disable=SC2016 # $k and $v are template variables
# A resource is ready when its own condition is True, or, for a kind whose controller writes no such condition (a
# StatefulSet), when every pod it selects has the condition True.
resource_ready() { # <namespace> <kind/name> <condition>
    local status selector pods
    status=$(k -n "$1" get "$2" -o jsonpath="{.status.conditions[?(@.type==\"$3\")].status}" 2>/dev/null) || return 1
    [ -z "$status" ] || { [ "$status" = True ]; return; }
    selector=$(k -n "$1" get "$2" -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}')
    pods=$(k -n "$1" get pods -l "${selector%,}" -o jsonpath="{range .items[*]}{.status.conditions[?(@.type==\"$3\")].status}{\"\\n\"}{end}")
    [ -n "$pods" ] && ! grep -qvx True <<<"$pods"
}

check_ready() {
    local m name ns n=0 bad="" command
    for m in $(find "$modules_dir" -mindepth 2 -maxdepth 3 -name module.yaml | sort); do
        [[ $groups == *" $(yq '.group' "$m") "* ]] && [ "$(yq ".profiles | contains([\"$module_profile\"])" "$m")" = true ] || continue
        name=$(yq '.name' "$m") ns=$(yq '.namespace' "$m") n=$((n + 1))
        if [ "$(yq '.readiness | has("command")' "$m")" = true ]; then
            mapfile -t command < <(yq '.readiness.command[]' "$m")
            "${command[@]}" >/dev/null 2>&1 || bad+=" $name"
        else
            resource_ready "$ns" "$(yq '.readiness.resource' "$m")" "$(yq '.readiness.condition' "$m")" || bad+=" $name"
        fi
    done
    if [ -z "$bad" ]; then ok ready "$n modules of profile $preset with DSY_OBS=$DSY_OBS ready"; else fail ready "not ready:$bad"; fi
}

# shellcheck disable=SC2016 # $n is a yq variable
check_drift() {
    local node pool cpu mem bad="" detail=""
    declare -A pool_of live_cpu live_mem
    while read -r node pool _; do pool_of[$node]=$pool; done <"$work/nodes"
    while read -r node cpu mem; do
        pool=${pool_of[$node]:-}
        [ -n "$pool" ] || continue
        live_cpu[$pool]=$((${live_cpu[$pool]:-0} + $(milli "$cpu")))
        live_mem[$pool]=$((${live_mem[$pool]:-0} + $(mib "$mem")))
    done < <(k get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json |
        yq -p json '.items[] | select(.spec.nodeName) | .spec.nodeName as $n | .spec.containers[] | $n + " " + (.resources.requests.cpu // "0") + " " + (.resources.requests.memory // "0")')
    while read -r pool cpu mem; do
        detail+="; $pool ${live_cpu[$pool]:-0}m/${cpu}m ${live_mem[$pool]:-0}Mi/${mem}Mi"
        [ "${live_cpu[$pool]:-0}" -le "$cpu" ] && [ "${live_mem[$pool]:-0}" -le "$mem" ] || bad+=" $pool"
    done < <(sed -n 's/^node \([a-z]*\) requests cpu: \([0-9]*\)m .*/\1 \2/p' "$work/budget" | while read -r pool cpu; do
        echo "$pool $cpu $(sed -n "s/^node $pool requests memory: \([0-9]*\)Mi .*/\1/p" "$work/budget")"
    done)
    if [ -z "$bad" ]; then ok drift "live requests within the budget${detail:+:${detail#;}}"; else fail drift "over the budget on:$bad${detail}"; fi
}

check_pressure() {
    local ns found=""
    for ns in dsy-infra dsy-control; do
        found+=$(k -n "$ns" get pods --field-selector=status.phase=Pending -o jsonpath='{range .items[*]}'"$ns"'/{.metadata.name} Pending: {.status.conditions[?(@.type=="PodScheduled")].message}{"\n"}{end}')
        found+=$(k -n "$ns" get pods -o jsonpath='{range .items[*]}'"$ns"'/{.metadata.name} {.status.containerStatuses[*].lastState.terminated.reason} {.status.containerStatuses[*].state.terminated.reason}{"\n"}{end}' |
            awk '/OOMKilled/ { print $1 " OOMKilled" }')
        found+=$(k -n "$ns" get replicasets -o jsonpath='{range .items[*]}'"$ns"'/{.metadata.name} {.status.conditions[?(@.type=="ReplicaFailure")].message}{"\n"}{end}' |
            awk 'NF > 1')
    done
    found+=$(k get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.status.conditions[?(@.type=="MemoryPressure")].status}{"\n"}{end}' |
        awk '$2 == "True" { print $1 " MemoryPressure" }')
    if [ -z "$found" ]; then
        ok pressure "no Pending pod, quota refusal or OOM kill in dsy-infra and dsy-control, no node under memory pressure"
    else
        fail pressure "$(sed '/^$/d' <<<"$found" | paste -sd ';' | sed 's/;/; /g')"
    fi
}

check_ports() {
    local port addrs bad=""
    for port in $(yq '.ports[].host_port' "$(yq '.host.ports' "$file")"); do
        addrs=$(ss -ltnH "sport = :$port" | awk '{ print $4 }' | sort -u | paste -sd ' ')
        [ -n "$addrs" ] || { bad+=" $port not listening"; continue; }
        [ "$addrs" = "127.0.0.1:$port" ] || bad+=" $port on $addrs"
    done
    if [ -z "$bad" ]; then ok ports "every host port listens on 127.0.0.1 only"; else fail ports "${bad# }"; fi
}

check_preset
check_slots
check_budget
check_nodes
check_limits
check_quotas
check_ready
check_drift
check_pressure
check_ports
echo "cluster-verify $preset: $passed ok, $failed failed"
[ "$failed" -eq 0 ]
