#!/usr/bin/env bash
# Worker-in-the-slot spike: builds the stand-in pool worker image on the pinned Python base, then runs measure.py,
# which holds both T1 lock files (SeaweedFS and one capped worker container at a time) and prints one
# `figure <name> <value>` line per number of docs/delivery/spikes/S11/worker-in-slot.md. Run in the dev shell:
#   nix develop -c docs/delivery/spikes/S11/code/run.sh [out-dir] [measure.py flags]
# Exit 0 the run completed (the verdict is a figure), 1 a step failed, 2 usage.
set -uo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)
code=$root/docs/delivery/spikes/S11/code
out=$(realpath -m "${1:-$(mktemp -d "${TMPDIR:-/tmp}/wft-s11.XXXXXX")}")
shift $(($# > 0 ? 1 : 0))
mkdir -p "$out"
image=wft-s11-worker:local
say() { echo "[$(date +%T)] $*" >&2; }
figure() { echo "figure $1 $2" | tee -a "$out/figures.txt"; }
: >"$out/figures.txt"

docker info >/dev/null 2>&1 || { say "docker daemon unreachable"; exit 1; }
# A release asset of nix/wasm-tools.nix, refused when its SHA-256 differs from the pin's hash.
release() {
    local name=$1 block url sri want
    block=$(grep -A3 "pname = \"$name\"" "$root/nix/wasm-tools.nix")
    url=$(sed -n 's/.*url = "\(.*\)";/\1/p' <<<"$block")
    sri=$(sed -n 's/.*hash = "sha256-\(.*\)";/\1/p' <<<"$block")
    want=$(base64 -d <<<"$sri" | od -An -tx1 | tr -d ' \n')
    curl -sSfL -o "$context/$name.tar" "$url" || return 1
    echo "$want  $context/$name.tar" | sha256sum -c --quiet || return 1
    tar -xf "$context/$name.tar" -C "$context" --wildcards --no-anchored --strip-components=1 "*/$name" || return 1
    install -m 755 "$context/$name" "$context/bin/$name" && rm -f "$context/$name" "$context/$name.tar"
    touch -d @1 "$context/bin/$name"
}

say "build $image"
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
mkdir -p "$context/bin"
packages=(--package wft-workflow --package wft-aggregate)
(cd "$root" && uv export --frozen --quiet --no-dev "${packages[@]}" --no-emit-workspace -o "$context/requirements.txt") ||
    exit 1
(cd "$root" && uv export --frozen --quiet --no-dev "${packages[@]}" --only-emit-workspace --no-editable --no-hashes \
    --no-annotate --no-header) | while read -r package; do
    (cd "$root" && uv build --quiet --wheel --out-dir "$context/wheels" "$package") || exit 1
done || exit 1
for tool in wasmtime wasm-tools; do release "$tool" || { say "release asset $tool fetch failed"; exit 1; }; done
base="python:$(yq '.images.python.version' "$root/deploy/versions.yaml")@$(yq '.images.python.digest' "$root/deploy/versions.yaml")"
uv_version=$(uv --version | awk '{ print $2 }')
docker build --quiet -f "$code/Dockerfile" -t "$image" --build-arg "BASE=$base" --build-arg "UV_VERSION=$uv_version" \
    "$context" >/dev/null || exit 1

figure host.mem_total_mib "$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)"
figure host.mem_available_mib.before "$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)"
figure host.cpus "$(nproc)"
figure host.docker "$(docker version --format '{{.Server.Version}}')"
figure host.kernel "$(uname -r)"
figure host.other_containers "$(docker ps -q | wc -l)"
figure image.base "$base"
figure image.bytes "$(docker image inspect --format '{{.Size}}' "$image")"
(cd "$root" && uv run --frozen python "$code/measure.py" --image "$image" --out "$out" "$@") | tee -a "$out/figures.txt"
rc=${PIPESTATUS[0]}
[ "$rc" -eq 0 ] || say "a measurement step failed"
figure host.mem_available_mib.after "$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)"
figure containers.left.label_all "$(docker ps -aq --filter label=wft.dev/verify-run | wc -l)"
[ "$rc" -eq 0 ] || exit 1
