#!/usr/bin/env bash
# Fills OUT with the distributions that REQUIREMENTS pins by hash, for the interpreter and platform of the image BASE,
# because pip runs in BASE. They come from the build cache with no network when every one is there, and from the
# package index otherwise, which then adds them to the cache. The cache is $WDF_BUILD_CACHE/wheels, and
# WDF_BUILD_CACHE defaults to $XDG_CACHE_HOME/wasm-dag-forge/build.
# Usage: wheel-cache.sh BASE REQUIREMENTS OUT. Exit 0 filled, 1 a failed download, 2 usage.
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: wheel-cache.sh BASE REQUIREMENTS OUT" >&2; exit 2; }
base=$1
cache=${WDF_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/wasm-dag-forge/build}/wheels
mkdir -p "$cache" "$3"
requirements=$(realpath "$2") out=$(realpath "$3")

run=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1
    -v "$cache:/cache:ro" -v "$requirements:/requirements.txt:ro" -v "$out:/out")
download=(pip download --quiet --no-cache-dir --no-deps --require-hashes -r /requirements.txt --dest /out
    --find-links /cache)
"${run[@]}" --network none "$base" "${download[@]}" --no-index 2>/dev/null && exit 0
"${run[@]}" "$base" "${download[@]}"
# Each file lands under its name in one rename, so a concurrent build never reads a partial one.
for file in "$out"/*; do
    name=$(basename "$file")
    [ -e "$cache/$name" ] || { cp "$file" "$cache/.$name.$$" && mv "$cache/.$name.$$" "$cache/$name"; }
done
