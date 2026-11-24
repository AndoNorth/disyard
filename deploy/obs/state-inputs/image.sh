#!/usr/bin/env bash
# Builds the image of the state inputs exporter from this tree: wheels of the workspace packages that dsy-obs and its
# stateops extra need, built on the host, over the third-party packages that uv.lock pins by hash, on the pinned Python
# base. The third-party packages are read from the build cache first, as deploy/wheel-cache.sh says.
# Usage: image.sh [REF]. REF defaults to EXPECTED_REF, which Tilt sets. Exit 0 built, 1 a failed build, 2 usage.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

ref=${1:-${EXPECTED_REF:-}}
[ -n "$ref" ] && [ $# -le 1 ] || { echo "usage: image.sh [REF]" >&2; exit 2; }

base="python:$(yq '.images.python.version' deploy/versions.yaml)@$(yq '.images.python.digest' deploy/versions.yaml)"
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT

export=(uv export --frozen --quiet --no-dev --package dsy-obs --extra stateops)
"${export[@]}" --no-emit-workspace -o "$context/requirements.txt"
"${export[@]}" --only-emit-workspace --no-editable --no-hashes --no-annotate --no-header | while read -r package; do
    uv build --quiet --wheel --out-dir "$context/wheels" "$package"
done

deploy/wheel-cache.sh "$base" "$context/requirements.txt" "$context/dists"
docker build --quiet -f deploy/obs/state-inputs/Dockerfile -t "$ref" --build-arg "BASE=$base" "$context" >/dev/null
echo "built $ref"
