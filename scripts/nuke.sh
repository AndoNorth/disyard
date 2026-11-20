#!/usr/bin/env bash
# Destroys the kind cluster wft, its registry and the host data directory of the preset that started it.
# Safe to repeat: with nothing left it prints so and exits 0.
# WFT_DATA_DIR names the directory to remove instead of the preset's, and is refused unless it is one a preset names.
# Exit 0 nothing left, 1 something could not be removed, 2 unknown preset, 4 refused: the data directory is not one the repo configures.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/verify/cluster/lib/tools.sh

state=${WFT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wasm-dag-forge}
preset=${WFT_PRESET:-$(cat "$state/cluster.owner" 2>/dev/null || echo ci)}
[ -f "deploy/presets/$preset.yaml" ] || { echo "usage: no preset $preset" >&2; exit 2; }

if [ -z "${WFT_CLUSTER_LOCK_HELD:-}" ]; then
    exec 9>/tmp/wft-cluster.lock
    flock -w 900 9 || { echo "could not take /tmp/wft-cluster.lock" >&2; exit 1; }
fi

expand() { local d=$1; echo "${d/#\~/$HOME}"; }
data_dir=${WFT_DATA_DIR:-$(expand "$(yq '.host.data_dir' "deploy/presets/$preset.yaml")")}
data_dir=$(realpath -m "$data_dir")

# Nothing is removed before this check, so a refused run leaves everything as it was.
allowed=false
for f in deploy/presets/*.yaml; do
    [ "$data_dir" = "$(realpath -m "$(expand "$(yq '.host.data_dir' "$f")")")" ] && allowed=true
done
$allowed || { echo "refused: $data_dir is not the data directory of any preset in deploy/presets" >&2; exit 4; }

kind delete cluster --name wft
# -v takes the registry's anonymous image volume with it, which would otherwise outlive the container.
docker rm -f -v wft-registry >/dev/null 2>&1 || true

# The node containers write as root, so the contents go through a container and the directory itself is the user's.
if [ -d "$data_dir" ]; then
    image="busybox:$(yq '.images.canary.version' deploy/versions.yaml)@$(yq '.images.canary.digest' deploy/versions.yaml)"
    docker run --rm -v "$data_dir:/data" "$image" sh -c 'rm -rf /data/* /data/.[!.]* /data/..?*'
    rmdir "$data_dir"
fi
rm -f "$state/cluster.owner"
echo "cluster wft, registry wft-registry and $data_dir are gone"
