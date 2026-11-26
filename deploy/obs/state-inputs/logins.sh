#!/usr/bin/env bash
# Applies or checks the state inputs exporter login: logins.sh [apply|check]. See scripts/storage/db/logins.sh.
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/../../../scripts/storage/db/logins.sh" obs/state-inputs "$@"
