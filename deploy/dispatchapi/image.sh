#!/usr/bin/env bash
# Builds the image of the dispatch and control API from this tree: wheels of the workspace packages that
# wdf-dispatchapi needs, built on the host, over the third-party packages that uv.lock pins by hash, on the pinned
# Python base.
# python-qpid-proton is pinned by the hash of its sdist, which the Dockerfile builds into a wheel in a stage of its own.
# The third-party packages are read from the build cache first, as deploy/wheel-cache.sh says.
# Usage: image.sh [REF]. REF defaults to EXPECTED_REF, which Tilt sets. Exit 0 built, 1 a failed build, 2 usage.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

ref=${1:-${EXPECTED_REF:-}}
[ -n "$ref" ] && [ $# -le 1 ] || { echo "usage: image.sh [REF]" >&2; exit 2; }

base="python:$(yq '.images.python.version' deploy/versions.yaml)@$(yq '.images.python.digest' deploy/versions.yaml)"
context=$(mktemp -d)
pinned=$(mktemp)
trap 'rm -rf "$context" "$pinned"' EXIT

export=(uv export --frozen --quiet --no-dev --package wdf-dispatchapi)
"${export[@]}" --no-emit-workspace -o "$pinned"
"${export[@]}" --no-emit-workspace --no-emit-package python-qpid-proton -o "$context/requirements.txt"
# The requirement of python-qpid-proton with its hashes, the lines up to the first that does not continue.
awk '/^python-qpid-proton==/ { found = 1 } found { print; if (!/\\$/) exit }' "$pinned" >"$context/proton.txt"
[ -s "$context/proton.txt" ] || { echo "uv.lock pins no python-qpid-proton for wdf-dispatchapi" >&2; exit 1; }
"${export[@]}" --only-emit-workspace --no-editable --no-hashes --no-annotate --no-header | while read -r package; do
    uv build --quiet --wheel --out-dir "$context/wheels" "$package"
done

deploy/wheel-cache.sh "$base" "$pinned" "$context/dists"
docker build --quiet -f deploy/dispatchapi/Dockerfile -t "$ref" --build-arg "BASE=$base" "$context" >/dev/null
echo "built $ref"
