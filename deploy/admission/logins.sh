#!/usr/bin/env bash
# Applies or checks the admission service login: logins.sh [apply|check]. See scripts/storage/db/logins.sh.
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/../../scripts/storage/db/logins.sh" admission "$@"
