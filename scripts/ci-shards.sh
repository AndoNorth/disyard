#!/usr/bin/env bash
# Usage: ci-shards.sh [shard]
# The test shards CI runs side by side, one runner each. Without a shard, prints the shard names as a JSON array for
# the workflow matrix. With one, prints the pytest arguments that select its tests, one per line.
set -euo pipefail
cd "$(dirname "$0")/.."

# One shard per line: its name, then what it runs, where a word is a package under packages/ and a word ending in /
# is a top-level directory. The last shard names nothing and runs every path no other shard names, so a package
# missing from this list is still tested. Adding a line adds a runner.
shards=${CI_SHARDS:-"
workflow: workflow storecheck stateops jobstate jobdoc temporal jobcheck
admission: admission worker runtime events placement stages migrate kernel keys compat clusterkit jobpatch
dispatchapi: dispatchapi artifactclient artifactreaper compile aggregate jobtools plan itest acceptance
rest:
"}

names=()
declare -A paths seen
while IFS= read -r line; do
    [ -n "$line" ] || continue
    name=${line%%:*}
    names+=("$name")
    paths[$name]=
    for word in ${line#*:}; do
        case $word in */) path=${word%/} ;; *) path=packages/$word ;; esac
        [ -e "$path" ] || { echo "error: shard $name names $path, which does not exist" >&2; exit 1; }
        [ -z "${seen[$path]:-}" ] || { echo "error: $path is in shards ${seen[$path]} and $name" >&2; exit 1; }
        seen[$path]=$name
        paths[$name]+="$path "
    done
done <<<"$shards"
last=${names[-1]}
[ -z "${paths[$last]}" ] || { echo "error: the last shard, $last, must name nothing" >&2; exit 1; }

if [ $# -eq 0 ]; then
    json=$(printf ',"%s"' "${names[@]}")
    echo "[${json#,}]"
    exit
fi
[ -n "${paths[$1]+set}" ] || { echo "error: no shard $1, the shards are ${names[*]}" >&2; exit 2; }
if [ "$1" = "$last" ]; then
    for path in "${!seen[@]}"; do echo "--ignore=$path"; done | sort
else
    read -ra own <<<"${paths[$1]}"
    printf '%s\n' "${own[@]}"
fi
