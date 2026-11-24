#!/usr/bin/env bash
# Applies the throwaway stack to the cluster dsy: the dashboards ConfigMap first, then both manifests.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../../../.." && pwd)
k="kubectl --context kind-dsy"
$k -n dsy-obs create configmap grafana-dashboards --from-file="$root/deploy/obs/grafana/dashboards" \
    --dry-run=client -o yaml | $k apply -f -
$k apply -f "$here/stack.yaml" -f "$here/grafana.yaml"
