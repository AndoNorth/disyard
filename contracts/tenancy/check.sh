#!/usr/bin/env bash
# Tenancy contract loader: validates one tenancy file or vector.
# Usage: check.sh <file> [--as NAME]. Exit 0 valid, 1 invalid with "reason: <code>: <detail>" on stderr, 2 usage.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

[ $# -ge 1 ] && [ -f "$1" ] || { echo "usage: check.sh <file> [--as NAME]" >&2; exit 2; }
exec uv run --frozen --quiet --project "$here/../.." python -m dsy_broker.contract --check "$@"
