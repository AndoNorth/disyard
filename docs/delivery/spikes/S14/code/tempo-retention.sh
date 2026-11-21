#!/usr/bin/env bash
# Tempo retention check: runs the newest Tempo monolithic in docker on local filesystem storage with one of its two
# block_retention keys set to 2m, pushes one trace by OTLP HTTP, and prints when its block reaches the backend and when
# retention removes it, read from Tempo's own blocklist and backend bytes series, so the result names the key that
# enforces time retention.
#   tempo-retention.sh <worker|scheduler> [port]
# Exit 0 the block left the blocklist within the wait, 1 it did not or a step failed, 2 usage.
set -uo pipefail
key=${1:-}
port=${2:-19021}
image=${WFT_S14_TEMPO:-grafana/tempo:3.1.1}
case $key in
    worker) worker_retention=2m scheduler_retention=336h ;;
    scheduler) worker_retention=336h scheduler_retention=2m ;;
    *) echo "usage: tempo-retention.sh <worker|scheduler> [port]" >&2; exit 2 ;;
esac
name=wft-s14-tempo-$key
dir=$(mktemp -d)
trap 'docker rm -f "$name" >/dev/null 2>&1; rm -rf "$dir"' EXIT
say() { echo "[$(date +%T)] $*"; }

cat >"$dir/tempo.yaml" <<EOF
server:
  http_listen_port: 3200
distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:4318
storage:
  trace:
    backend: local
    local:
      path: /var/tempo/traces
    wal:
      path: /var/tempo/wal
    blocklist_poll: 30s
backend_scheduler:
  provider:
    retention:
      interval: 20s
    compaction:
      compaction:
        block_retention: $scheduler_retention
        compacted_block_retention: 1m
backend_worker:
  compaction:
    block_retention: $worker_retention
    compacted_block_retention: 1m
EOF
docker rm -f "$name" >/dev/null 2>&1
docker run -d --name "$name" -p "127.0.0.1:$port:3200" -p "127.0.0.1:$((port + 1)):4318" \
    -v "$dir/tempo.yaml:/etc/tempo.yaml:ro" "$image" -config.file=/etc/tempo.yaml >/dev/null || exit 1
say "image $(docker inspect --format '{{.Image}}' "$name") key $key"
for _ in $(seq 60); do curl -sf "127.0.0.1:$port/ready" >/dev/null && break; sleep 1; done
curl -s "127.0.0.1:$port/status/config" | grep -n 'block_retention\|interval: 20s'

trace=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
now=$(date +%s%N)
curl -sf -X POST -H 'Content-Type: application/json' "127.0.0.1:$((port + 1))/v1/traces" -d @- <<EOF || exit 1
{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"s14"}}]},
"scopeSpans":[{"spans":[{"traceId":"$trace","spanId":"0102030405060708","name":"probe","kind":1,
"startTimeUnixNano":"$now","endTimeUnixNano":"$((now + 1000000))"}]}]}]}
EOF
echo
say "pushed trace $trace"

metric() { curl -s "127.0.0.1:$port/metrics" | awk -v m="$1" 'index($0, m) == 1 { print $NF; exit }'; }
seen=0 n=0 bytes=0 code=0
for i in $(seq 240); do
    n=$(metric 'tempodb_blocklist_length{tenant="single-tenant"}')
    bytes=$(metric 'tempodb_backend_bytes_total{status="live",tenant="single-tenant"}')
    code=$(curl -s -o /dev/null -w '%{http_code}' "127.0.0.1:$port/api/v2/traces/$trace")
    if [ "$seen" = 0 ] && [ "${n:-0}" -gt 0 ]; then
        seen=1
        say "block in backend after $((i * 5)) s: blocklist $n, live bytes $bytes, trace lookup $code"
    fi
    if [ "$seen" = 1 ] && [ "${n:-0}" -eq 0 ]; then
        say "block gone after $((i * 5)) s: blocklist $n, live bytes $bytes, trace lookup $code"
        docker logs "$name" 2>&1 | grep -E 'marking block for deletion|deleting block' | cut -c1-160
        exit 0
    fi
    sleep 5
done
say "block not gone within 1200 s: blocklist $n, live bytes $bytes, trace lookup $code"
docker logs "$name" 2>&1 | grep -E 'marking block for deletion|deleting block' | cut -c1-160
exit 1
