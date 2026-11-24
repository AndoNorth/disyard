#!/usr/bin/env bash
# Storage series and retention keys of the backends, in docker on the host: Pyroscope's effective retention keys, the
# storage bytes series that Prometheus, Loki and Pyroscope report of themselves (Tempo's is in tempo-retention.sh), and
# SeaweedFS's disk series from `weed server` with and without -metricsPort.
#   nix develop -c docs/delivery/spikes/S14/code/backends.sh [port]
# Exit 0 the checks ran, 1 a container did not start.
set -uo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)
port=${1:-19025}
weed="chrislusf/seaweedfs:4.51@$(yq '.images["chrislusf/seaweedfs"].digest' "$root/deploy/versions.yaml")"
name=dsy-s14-backend
trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT

start() {
    docker rm -f "$name" >/dev/null 2>&1
    docker run -d --name "$name" -p "127.0.0.1:$port:$1" "${@:2}" >/dev/null || exit 1
    echo "== $(docker inspect --format '{{.Config.Image}}' "$name") $(docker inspect --format '{{.Args}}' "$name")"
}
ready() { for _ in $(seq 90); do curl -sf "127.0.0.1:$port$1" >/dev/null && return; sleep 1; done; }
help_of() { curl -s "127.0.0.1:$port/metrics" | grep -E "^# (HELP|TYPE) ($1) "; }

start 4040 grafana/pyroscope:2.4.4 -retention-period=2m -ingester.min-ready-duration=0
ready /ready
curl -s "127.0.0.1:$port/api/v1/status/config" |
    grep -nE '^architecture_storage|write_path:|retention_period|min_free_disk_gb|min_disk_available|cleanup_(grace_period|interval)'
curl -s "127.0.0.1:$port/metrics" | grep -E '^# TYPE pyroscope\S*bytes\S* gauge'
help_of 'objstore_bucket_operation_transferred_bytes|pyroscope_segment_writer_client_sent_bytes'

start 9090 prom/prometheus:v3.16.0
ready /-/ready
help_of 'prometheus_tsdb_storage_blocks_bytes|prometheus_tsdb_wal_storage_size_bytes|prometheus_tsdb_head_chunks_storage_size_bytes'

start 3100 grafana/loki:3.7.10 -config.file=/etc/loki/local-config.yaml
ready /ready
curl -s "127.0.0.1:$port/metrics" | grep -E '^# TYPE loki_\S*bytes\S* gauge'
help_of 'loki_ingester_chunk_size_bytes'

# The store's own server flags plus -metricsPort, which the deployed store does not set; the ports a server without the
# flag might serve /metrics on are probed first. The metrics port listens on the container address, not loopback.
start 9327 "$weed" server -dir=/data -volume.max=0 -master.volumeSizeLimitMB=292 -metricsPort=9328
docker exec "$name" sh -c 'until curl -sf 127.0.0.1:9333/cluster/healthz >/dev/null; do sleep 1; done
    for p in 9333 8080; do echo "port $p /metrics $(curl -s -o /dev/null -w %{http_code} 127.0.0.1:$p/metrics)"; done
    until a=$(curl -s 127.0.0.1:9333/dir/assign) && [ "${a#*\"fid\"}" != "$a" ]; do sleep 1; done
    fid=${a#*\"fid\":\"}; fid=${fid%%\"*}; url=${a#*\"url\":\"}; url=${url%%\"*}
    head -c 4194304 /dev/urandom >/tmp/f && curl -s -F file=@/tmp/f "http://$url/$fid"; echo
    for _ in $(seq 60); do
        curl -s "$(hostname -i):9328/metrics" | grep -E "^SeaweedFS_volumeServer_total_disk_size.*normal" | grep -qv " 0$" && break
        sleep 2
    done
    curl -s "$(hostname -i):9328/metrics" | grep -E "^SeaweedFS_volumeServer_(total_disk_size|resource)"'
