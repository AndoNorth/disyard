#!/usr/bin/env bash
# Usage: scan.sh [deps | images | secrets | all]
# The vulnerability and secret gates: the same command runs locally and in .github/workflows/ci.yml.
# deps scans the packages locked in uv.lock, images builds every service image of deploy/*/image.sh and scans it, secrets scans the git history.
# Findings of high and critical severity fail the scan. Accepted findings live in security/trivy-ignore.yaml, and every
# entry there carries a statement and an expiry, so an old acceptance comes back as a failure.
set -euo pipefail
cd "$(dirname "$0")/.."

SEVERITY=HIGH,CRITICAL
IGNORE=security/trivy-ignore.yaml
trivy_args=(--quiet --severity "$SEVERITY" --exit-code 1 --ignorefile "$IGNORE")

# Trivy reads a uv.lock through the root pyproject.toml, which lists no dependency of a workspace, so it sees none of
# them. It reads the pinned requirements of every package, group and extra in the lock instead.
scan_deps() {
    echo "== scan deps"
    local dir
    dir=$(mktemp -d)
    trap 'rm -rf "$dir"' RETURN
    uv export --frozen --quiet --all-packages --all-groups --all-extras --no-emit-workspace --no-hashes \
        --no-annotate --no-header -o "$dir/requirements.txt"
    trivy fs "${trivy_args[@]}" --scanners vuln "$dir/requirements.txt"
}

# Each image script takes a reference, and the worker script a pool before it, one image per pool.
scan_images() {
    local script dir pool ref
    for script in deploy/*/image.sh; do
        dir=$(basename "$(dirname "$script")")
        if [ "$dir" = workers ]; then
            for pool in compile simulate aggregate; do scan_image "$dir-$pool" "$script" "$pool"; done
        else
            scan_image "$dir" "$script"
        fi
    done
}

# scan_image NAME SCRIPT [POOL]: builds the image, scans it and removes it.
scan_image() {
    local name=$1 script=$2 ref="weft-scan/$1:scan"
    echo "== scan image $name"
    "$script" ${3:+"$3"} "$ref"
    local rc=0
    trivy image "${trivy_args[@]}" --scanners vuln "$ref" || rc=$?
    docker image rm "$ref" >/dev/null
    return "$rc"
}

scan_secrets() {
    echo "== scan secrets"
    gitleaks git --no-banner --redact --config security/gitleaks.toml .
}

case "${1:-all}" in
    deps) scan_deps ;;
    images) scan_images ;;
    secrets) scan_secrets ;;
    all) scan_deps; scan_secrets; scan_images ;;
    *) echo "usage: scan.sh [deps | images | secrets | all]" >&2; exit 2 ;;
esac
