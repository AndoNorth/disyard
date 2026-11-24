#!/usr/bin/env bash
# Usage: itest.sh [domain | -- pytest-args...]
# Integration tests, optionally limited to packages/<domain> or by pytest arguments.
# pytest exit 5 (nothing collected) is success, and exit 4 is a refusal: the T1 lock wait ran out.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "error: uv not on PATH, run inside nix develop" >&2; exit 1; }
# --frozen runs the pytest pinned in uv.lock, and uv fails loudly when it cannot provide one.
pytest=(uv run --frozen pytest)

case "${1:-}" in
    "") target=() ;;
    --) target=("${@:2}") ;;
    *) target=("packages/$1") ;;
esac

rc=0
"${pytest[@]}" -m integration "${target[@]}" || rc=$?
if [ "$rc" -eq 5 ]; then echo "notice: no integration tests collected"; exit 0; fi
exit "$rc"
