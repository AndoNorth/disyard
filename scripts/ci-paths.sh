#!/usr/bin/env bash
# Reads changed paths, one per line, and prints docs when every one is documentation, full otherwise.
# Documentation is Markdown outside the code trees, plus anything under docs/ except Python, which pytest would collect.
# Markdown under contracts/ and packages/ stays full because the unit tests read those READMEs.
set -euo pipefail

# The verdict is printed only after all input is read: a writer still producing would otherwise die of SIGPIPE, and pipefail reports that as 141.
full() { cat >/dev/null; echo full; exit; }

verdict=full
while IFS= read -r path; do
    [ -n "$path" ] || continue
    case $path in
        contracts/* | deploy/* | packages/* | scripts/* | nix/* | just/* | .github/*) full ;;
        flake.nix | flake.lock | justfile) full ;;
        docs/*.py) full ;;
        docs/* | *.md) verdict=docs ;;
        *) full ;;
    esac
done
echo "$verdict"
