#!/usr/bin/env bash
# Sums the requests and limits of every module the preset's profile enables, per node and per quota line, for the worst
# case the cluster must hold at once: every module workload, one more copy of each workload of a build-versioned module
# (its Tiltfile.module reads the build id, so two builds run side by side during a rollout), a reserve row for each
# planned component that has no module.yaml yet, and the daemon set pods of each node, whose requests count on the node.
# A workload whose requests and limits are all 1m and 1Mi is the nominal entry of a host-side helper and runs no pod.
# A node line counts requests against the node allocatable, because the scheduler places pods on requests, and a quota
# line counts what each line of namespaces.sh render limits. The worker pools are left to slots.sh, whose pod slots
# own their room.
# WFT_MODULES_DIR names the directory searched for module.yaml, one or two levels down as the Tiltfile searches.
# Usage: budget.sh <preset>. Exit 0 everything fits, 1 a node or a quota line is over, 2 usage.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
# shellcheck source=/dev/null
source scripts/verify/cluster/lib/tools.sh

[ $# -eq 1 ] && [ -f "deploy/presets/$1.yaml" ] || { echo "usage: budget.sh <preset>" >&2; exit 2; }
preset=deploy/presets/$1.yaml
modules_dir=${WFT_MODULES_DIR:-deploy}
profiles=deploy/tilt/profiles.yaml
[ "$(yq ".profiles | has(\"$1\")" "$profiles")" = true ] || { echo "usage: budget.sh <preset>: no profile $1 in $profiles" >&2; exit 2; }
groups=" $(yq ".profiles.$1.groups | join(\" \")" "$profiles") "
module_profile=$(yq ".profiles.$1.modules // \"$1\"" "$profiles")

# The infra node also runs the control pods, as namespaces.sh maps them.
declare -A node_of=([wft-infra]=infra [wft-control]=infra [wft-obs]=obs)
cpu_reserved_milli=100
# Requests of the daemon set pods on a worker node, as measured for slots.sh.
DAEMON_CPU_MILLI=100
DAEMON_MEM_MIB=50
# Planned components that have no module.yaml yet, at the figures the infra node is sized for:
# name group namespace cpu-request cpu-limit (millicores) memory-request memory-limit (MiB).
# A row is dropped as soon as a module.yaml of that name exists, so the owner's real figures replace it.
RESERVES=(
    "rabbitmq infra wft-infra 100 500 320 320"
    "dispatch control wft-control 100 500 256 256"
    "admission control wft-control 50 250 128 128"
)

milli() { case $1 in *m) echo "${1%m}" ;; *) awk -v c="$1" 'BEGIN { print c * 1000 }' ;; esac; }
mib() { case $1 in *Gi) echo $((${1%Gi} * 1024)) ;; *Mi) echo "${1%Mi}" ;; *) echo "bad quantity: $1" >&2; return 1 ;; esac; }

declare -A used node_cpu node_mem
add() { # <namespace> <workload> <source> <cpu request> <cpu limit> <memory request> <memory limit>, in m and MiB
    echo "item $1 $2 ($3): cpu ${4}m/${5}m, memory ${6}Mi/${7}Mi"
    used[$1 requests.cpu]=$((${used[$1 requests.cpu]:-0} + $4))
    used[$1 limits.cpu]=$((${used[$1 limits.cpu]:-0} + $5))
    used[$1 requests.memory]=$((${used[$1 requests.memory]:-0} + $6))
    used[$1 limits.memory]=$((${used[$1 limits.memory]:-0} + $7))
    local node=${node_of[$1]:-}
    [ -z "$node" ] && return
    node_cpu[$node]=$((${node_cpu[$node]:-0} + $4))
    node_mem[$node]=$((${node_mem[$node]:-0} + $6))
}

echo "budget $1: profile $module_profile, groups ${groups:1:-1}"
declare -A present
manifests=$(find "$modules_dir" -mindepth 2 -maxdepth 3 -name module.yaml | sort)
for m in $manifests; do
    name=$(yq '.name' "$m")
    present[$name]=1
    group=$(yq '.group' "$m")
    [[ $groups == *" $group "* ]] && [ "$(yq ".profiles | contains([\"$module_profile\"])" "$m")" = true ] || continue
    ns=$(yq '.namespace' "$m")
    copies=1
    grep -q build_id_env "$(dirname "$m")/Tiltfile.module" 2>/dev/null && copies=2
    while read -r workload rc lc rm lm; do
        [ "$rc $lc $rm $lm" = "1m 1m 1Mi 1Mi" ] && continue
        args=("$(milli "$rc")" "$(milli "$lc")" "$(mib "$rm")" "$(mib "$lm")")
        add "$ns" "$workload" "$name" "${args[@]}"
        [ "$copies" -eq 1 ] || add "$ns" "$workload" "$name, second build" "${args[@]}"
    done < <(yq '.resources | to_entries | .[] | .key + " " + .value.requests.cpu + " " + .value.limits.cpu + " " + .value.requests.memory + " " + .value.limits.memory' "$m")
done
for row in "${RESERVES[@]}"; do
    read -r name group ns rc lc rm lm <<<"$row"
    [ -z "${present[$name]:-}" ] && [[ $groups == *" $group "* ]] || continue
    add "$ns" "$name" "reserve" "$rc" "$lc" "$rm" "$lm"
done

status=0
report() { # <label> <used> <limit> <unit>
    local free=$(($3 - $2))
    if [ "$free" -ge 0 ]; then
        echo "$1 $2$4 of $3$4, free $free$4, fits"
    else
        echo "$1 $2$4 of $3$4, OVER by $((-free))$4"
        status=1
    fi
}
for node in $(printf '%s\n' "${node_of[@]}" | sort -u); do
    read -r count cpus mem < <(yq ".nodes[] | select(.role == \"$node\") | .count + \" \" + .cpus + \" \" + .memory_mib" "$preset")
    report "node $node requests cpu:" $((${node_cpu[$node]:-0} + count * DAEMON_CPU_MILLI)) \
        $((count * ($(milli "$cpus") - cpu_reserved_milli))) m
    report "node $node requests memory:" $((${node_mem[$node]:-0} + count * DAEMON_MEM_MIB)) \
        $((count * (mem - (256 + (mem + 15) / 16)))) Mi
done
# shellcheck disable=SC2016 # $ns is a yq variable
while read -r ns key hard; do
    case $key in *cpu) unit=m limit=$(milli "$hard") ;; *) unit=Mi limit=$(mib "$hard") ;; esac
    report "quota $ns $key:" "${used[$ns $key]:-0}" "$limit" "$unit"
done < <(scripts/cluster/namespaces.sh render "$1" | yq -N '.metadata.namespace as $ns | .spec.hard | to_entries | .[] | $ns + " " + .key + " " + .value')
exit $status
