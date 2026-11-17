#!/usr/bin/env bash
# Builds the image of one pool worker from this tree: wheels of the workspace packages that wdf-worker with the pool's
# extra needs, built on the host, over the third-party packages that uv.lock pins by hash, on the pinned Python base,
# with the pool's release binaries from the url of nix/wasm-tools.nix (wasm-tools on compile, wasmtime on simulate),
# each refused when its SHA-256 differs from the pin's hash. uv reaches the compile image as a wheel of its extra.
# The release assets and the third-party packages are read from the build cache first, $WDF_BUILD_CACHE, by default
# $XDG_CACHE_HOME/wasm-dag-forge/build, so a repeated build downloads nothing.
# The default tag carries the short commit, for tracing only.
# Usage: image.sh POOL [REF]. REF defaults to EXPECTED_REF, which Tilt sets, then to <POOL>-worker:<short commit>.
# Exit 0 built, 1 a failed fetch, hash check or build, 2 usage.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

usage() { echo "usage: image.sh compile|simulate|aggregate [REF]" >&2; exit 2; }
pool=${1:-}
case "$pool" in
    compile) tools=(wasm-tools) ;;
    simulate) tools=(wasmtime) ;;
    aggregate) tools=() ;;
    *) usage ;;
esac
[ $# -le 2 ] || usage
ref=${2:-${EXPECTED_REF:-$pool-worker:$(git rev-parse --short HEAD 2>/dev/null || echo local)}}

context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
mkdir "$context/bin"

cache=${WDF_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/wasm-dag-forge/build}

# The binary of one release asset of nix/wasm-tools.nix, with a fixed modification time, which names the wasmtime
# module cache. The asset is cached under its SHA-256, and a cached copy is checked again before it is used.
release() {
    local name=$1 block url sri want cached
    block=$(grep -A3 "pname = \"$name\"" nix/wasm-tools.nix)
    url=$(sed -n 's/.*url = "\(.*\)";/\1/p' <<<"$block")
    sri=$(sed -n 's/.*hash = "sha256-\(.*\)";/\1/p' <<<"$block")
    want=$(base64 -d <<<"$sri" | od -An -v -tx1 | tr -d ' \n')
    cached=$cache/assets/$want
    if ! echo "$want  $cached" | sha256sum -c --quiet --status 2>/dev/null; then
        curl -sSfL -o "$context/$name.tar" "$url"
        echo "$want  $context/$name.tar" | sha256sum -c --quiet --status ||
            { echo "error: $name: the SHA-256 of $url differs from its hash in nix/wasm-tools.nix" >&2; exit 1; }
        mkdir -p "$cache/assets"
        mv "$context/$name.tar" "$cached.$$" && mv "$cached.$$" "$cached"
    fi
    tar -xf "$cached" -C "$context/bin" --wildcards --no-anchored --strip-components=1 "*/$name"
    touch -d @1 "$context/bin/$name"
}
for tool in "${tools[@]}"; do release "$tool"; done

export=(uv export --frozen --quiet --no-dev --package wdf-worker --extra "$pool")
"${export[@]}" --no-emit-workspace -o "$context/requirements.txt"
"${export[@]}" --only-emit-workspace --no-editable --no-hashes --no-annotate --no-header | while read -r package; do
    uv build --quiet --wheel --out-dir "$context/wheels" "$package"
done

base="python:$(yq '.images.python.version' deploy/versions.yaml)@$(yq '.images.python.digest' deploy/versions.yaml)"
deploy/wheel-cache.sh "$base" "$context/requirements.txt" "$context/dists"
docker build --quiet -f deploy/workers/Dockerfile --target "$pool" -t "$ref" --build-arg "BASE=$base" \
    "$context" >/dev/null
echo "built $ref"
