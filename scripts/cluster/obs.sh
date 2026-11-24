#!/usr/bin/env bash
# The observability switch: whether the stack and its larger obs node are part of the cluster dsy.
#   obs.sh                  print the value up.sh recorded for the cluster, on or off; a missing record is off
#   obs.sh preset <preset>  print the preset as the switch in DSY_OBS sizes it: with on, every obs node at obs_stack
#                           and memory_mib at obs_memory_mib; with off or unset, the preset as it is
#   obs.sh groups <profile> print the Tilt groups the profile enables, with its opt_in groups when DSY_OBS is on
# Exit 0 printed, 2 usage: an unknown preset or profile, a DSY_OBS other than on or off, or on for a preset without
# obs_stack.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"

state=${DSY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/disyard}

if [ $# -eq 0 ]; then
    recorded=$(cat "$state/cluster.obs" 2>/dev/null || true)
    [ "$recorded" = on ] && echo on || echo off
    exit 0
fi

# shellcheck source=/dev/null
source scripts/verify/cluster/lib/tools.sh

usage() { echo "usage: obs.sh [preset <preset> | groups <profile>]: $*" >&2; exit 2; }
[ $# -eq 2 ] || usage "wrong arguments"
obs=${DSY_OBS:-off}
[[ $obs =~ ^(on|off)$ ]] || usage "DSY_OBS is $obs, expected on or off"

case $1 in
    preset)
        file=deploy/presets/$2.yaml
        [ -f "$file" ] || usage "no $file"
        if [ "$obs" = off ]; then
            cat "$file"
        else
            [ "$(yq 'has("obs_stack")' "$file")" = true ] || usage "DSY_OBS=on needs obs_stack, which preset $2 does not have"
            # shellcheck disable=SC2016 # $s is a yq variable
            yq '.obs_stack as $s | (.nodes[] | select(.role == "obs")) |= (.cpus = $s.cpus | .memory_mib = $s.memory_mib) |
                .memory_mib = .obs_memory_mib' "$file"
        fi
        ;;
    groups)
        profiles=deploy/tilt/profiles.yaml
        [ "$(yq ".profiles | has(\"$2\")" "$profiles")" = true ] || usage "no profile $2 in $profiles"
        if [ "$obs" = on ]; then
            yq ".profiles.$2 | (.groups + (.opt_in // [])) | join(\" \")" "$profiles"
        else
            yq ".profiles.$2.groups | join(\" \")" "$profiles"
        fi
        ;;
    *) usage "unknown mode $1" ;;
esac
