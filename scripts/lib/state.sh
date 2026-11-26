#!/usr/bin/env bash
# The state directory of the cluster scripts and the credentials generated into it. Sourced, never run.

state_dir() {
    echo "${DSY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/disyard}"
}

# ensure_credential <file name> <bytes>: prints the path of the credential, generating it on first use.
# The write is atomic, so a reader never sees a partial value, and the file is private to the user.
ensure_credential() {
    local dir file tmp
    dir=$(state_dir)
    file=$dir/$1
    if [ ! -s "$file" ]; then
        mkdir -p "$dir"
        tmp=$(umask 077 && mktemp "$file.XXXXXX")
        head -c "$2" /dev/urandom | base64 | tr -d '=+/\n' >"$tmp"
        mv "$tmp" "$file"
    fi
    echo "$file"
}
