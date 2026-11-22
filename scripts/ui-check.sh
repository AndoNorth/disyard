#!/usr/bin/env bash
# Usage: ui-check.sh <url> <png-path>
# Opens a live page in a real browser through chrome-devtools-axi, reads its title and URL, and saves a screenshot.
# The browser comes from the environment, never from this repo: the weft browser-cdp instance exports the
# endpoint (CHROME_DEVTOOLS_AXI_BROWSER_URL) and the bridge session (CHROME_DEVTOOLS_AXI_SESSION).
# Prints "ok|FAIL <step> <detail>" lines. Exit 0 all steps ok, 1 a step failed, 2 usage or no browser.
set -euo pipefail

[ $# -eq 2 ] || { echo "usage: ui-check.sh <url> <png-path>" >&2; exit 2; }
url=$1
png=$2

# shellcheck disable=SC2016 # the command substitution is text for the user to run
hint='start the weft browser and load its env: BROWSER_CDP_INSTANCE=weft browser-cdp start && eval "$(BROWSER_CDP_INSTANCE=weft browser-cdp env)"'
command -v chrome-devtools-axi >/dev/null || { echo "usage: chrome-devtools-axi not on PATH, install the owner's dotfiles" >&2; exit 2; }
[ -n "${CHROME_DEVTOOLS_AXI_BROWSER_URL:-}" ] || { echo "usage: CHROME_DEVTOOLS_AXI_BROWSER_URL is not set, $hint" >&2; exit 2; }
# A bridge keeps the URL it started with, so a missing session would reuse another instance's bridge.
[ -n "${CHROME_DEVTOOLS_AXI_SESSION:-}" ] || { echo "usage: CHROME_DEVTOOLS_AXI_SESSION is not set, $hint" >&2; exit 2; }
curl -fsS -m 5 "${CHROME_DEVTOOLS_AXI_BROWSER_URL%/}/json/version" >/dev/null 2>&1 || {
    echo "usage: no browser answers at CHROME_DEVTOOLS_AXI_BROWSER_URL, $hint" >&2
    exit 2
}

rc=0
step() {
    local name=$1 out
    shift
    if out=$("$@" 2>&1); then
        echo "ok $name $(printf '%s' "$out" | head -n 1)"
    else
        echo "FAIL $name $(printf '%s' "$out" | head -n 1)"
        rc=1
    fi
}

mkdir -p "$(dirname "$png")"
if chrome-devtools-axi open "$url" >/dev/null 2>&1; then echo "ok open $url"; else echo "FAIL open $url"; rc=1; fi
step eval chrome-devtools-axi eval '({title: document.title, url: location.href})'
step screenshot chrome-devtools-axi screenshot "$png"
if [ -s "$png" ]; then
    echo "ok png $png $(stat -c %s "$png") bytes"
else
    echo "FAIL png $png is missing or empty"
    rc=1
fi
exit "$rc"
