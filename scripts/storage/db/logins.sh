#!/usr/bin/env bash
# Creates or checks the service logins of one service in the app database, as its superuser, through a port-forward
# bound to 127.0.0.1. The service is the deploy/ directory holding its logins.py. Waits for the migrated database
# first, so it can run as a Tilt local_resource or by hand, and running it again changes nothing.
#   apply  create or converge the logins and print what changed (default)
#   check  exit 1 when a login differs or cannot log in
# Usage: logins.sh <service> [apply|check]. Exit 0 ok, 1 the database or the forward did not come up or a login
# differs, 2 usage.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
# shellcheck source=/dev/null
source scripts/cluster/forward.sh

service=${1:-}
mode=${2:-apply}
[ -f "deploy/$service/logins.py" ] || { echo "usage: logins.sh <service> [apply|check]" >&2; exit 2; }
case $mode in apply | check) ;; *) echo "usage: logins.sh <service> [apply|check]" >&2; exit 2 ;; esac

context=kind-dsy
namespace=dsy-infra
k() { kubectl --context "$context" -n "$namespace" "$@"; }

k wait --for=condition=Ready pod/appdb-0 --timeout=300s >/dev/null

log=$(mktemp)
trap 'forward_stop "$log"' EXIT
forward_start "$log" pod/appdb-0 :5432

# The forward picks a free local port and prints it once it listens.
port=
for _ in $(seq 60); do
    port=$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$log" | head -n 1)
    [ -n "$port" ] && break
    forward_alive || { cat "$log" >&2; exit 1; }
    sleep 0.5
done
[ -n "$port" ] || { echo "port-forward to appdb-0 did not come up" >&2; exit 1; }

PGPASSWORD=$(scripts/storage/db/secrets.sh password) \
    uv run --frozen --quiet python "deploy/$service/logins.py" "$mode" "postgresql://postgres@127.0.0.1:$port/postgres"
