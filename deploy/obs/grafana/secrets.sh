#!/usr/bin/env bash
# The admin credential of Grafana: generated on first use into the state directory of the cluster scripts, shown as a
# Kubernetes Secret for the Tilt module and as a bare value for the host-side callers of the HTTP API. Never committed.
# The user is admin. Grafana keeps the password in its database from its first start, so a credential file that is
# deleted while the cluster lives no longer matches the pod, and `just nuke` is what resets both.
#   render    print the Secret manifest (default)
#   password  print the credential
# Usage: secrets.sh [render|password]. Exit 0 ok, 2 usage.
set -euo pipefail

state=${DSY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/disyard}
file=$state/grafana.credential
mode=${1:-render}

case $mode in render | password) ;; *) echo "usage: secrets.sh [render|password]" >&2; exit 2 ;; esac

if [ ! -s "$file" ]; then
    mkdir -p "$state"
    (umask 077 && head -c 32 /dev/urandom | base64 | tr -d '=+/\n' >"$file")
fi

value=$(cat "$file")
case $mode in
    password) echo "$value" ;;
    render)
        cat <<SECRET
apiVersion: v1
kind: Secret
metadata:
  name: grafana-admin
  namespace: dsy-obs
type: Opaque
stringData:
  admin-user: admin
  admin-password: $value
SECRET
        ;;
esac
