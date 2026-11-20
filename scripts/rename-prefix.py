#!/usr/bin/env python3
"""Rename the old project prefix wdf to wft, or check that none is left.

usage: rename-prefix.py apply [ROOT]
       rename-prefix.py check PATH...
apply rewrites every tracked text file under ROOT (default: the current directory),
moves every tracked path whose name carries the prefix,
and rewrites the prefix inside the payloads of the recorded replay histories.
A second apply changes nothing.
check prints every hit in the given paths, in any letter case, outside the allowlist,
and exits 1 if there is one.
The allowlist, rename-prefix.allow next to this script,
holds `path-regex line-regex` pairs that keep the old name.
"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

CASES = {"wdf": "wft", "WDF": "WFT", "Wdf": "Wft"}
# Left: not a letter or digit, so an underscore counts as a boundary.
# Right: lower case must not run on into a word.
TOKEN = re.compile(r"(?<![A-Za-z0-9])(wdf|WDF|Wdf)(?![a-z0-9])")
# The check is stricter than the rewrite, so a hit the rewrite leaves still fails the gate.
ANY_CASE = re.compile("wdf", re.IGNORECASE)
# Inline base64 payloads (fonts in exported SVGs) hold the prefix by chance and stay byte-identical.
BASE64 = re.compile(r"base64,[A-Za-z0-9+/=]+")
BINARY_SUFFIXES = {".gif", ".png", ".whl"}
# Recorded Temporal histories carry the prefix inside base64 JSON payloads, out of a text rewrite.
CORPUS = re.compile(r"(^|/)corpus/v[0-9]+/[^/]+\.json$")
ALLOW = Path(__file__).with_suffix(".allow")


def load_allow():
    rules = []
    for raw in ALLOW.read_text().splitlines():
        if raw.strip() and not raw.startswith("#"):
            path, _, line = raw.partition(" ")
            rules.append((re.compile(path), re.compile(line or ".")))
    return rules


def allowed(rules, path, line):
    return any(p.search(path) and pattern.search(line) for p, pattern in rules)


def rewrite(text):
    parts, last = [], 0
    for m in BASE64.finditer(text):
        parts.append(TOKEN.sub(lambda t: CASES[t.group(1)], text[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(TOKEN.sub(lambda t: CASES[t.group(1)], text[last:]))
    return "".join(parts)


def read_text(path):
    """The file's text, or None for a binary file."""
    if path.suffix in BINARY_SUFFIXES or path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8")


def rewrite_payloads(node):
    """Rewrite the prefix inside every base64 JSON `data` payload of a history, in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "data" and isinstance(value, str):
                try:
                    raw = base64.b64decode(value, validate=True).decode("utf-8")
                except ValueError:
                    continue
                node[key] = base64.b64encode(rewrite(raw).encode()).decode()
            else:
                rewrite_payloads(value)
    elif isinstance(node, list):
        for value in node:
            rewrite_payloads(value)


def rewrite_history(text):
    doc = json.loads(text)
    rewrite_payloads(doc)
    second = text.split("\n", 2)[1] if "\n" in text else ""
    indent = (len(second) - len(second.lstrip(" "))) or None
    return json.dumps(doc, indent=indent, ensure_ascii=False) + (
        "\n" if text.endswith("\n") else ""
    )


def tracked(root):
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def apply(root):
    rules = load_allow()
    changed = 0
    for rel in tracked(root):
        path = Path(root, rel)
        text = read_text(path)
        if text is None:
            continue
        # Allowlisted lines are history that must stay true, so they keep the old name.
        lines = text.splitlines(keepends=True)
        new = "".join(line if allowed(rules, rel, line) else rewrite(line) for line in lines)
        if CORPUS.search(rel):
            new = rewrite_history(new)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
    moved = 0
    # Deepest paths first, so a directory moves after the files inside it.
    for rel in sorted(tracked(root), key=lambda p: -p.count("/")):
        if allowed(rules, rel, rel):
            continue
        parts = rel.split("/")
        for i in range(len(parts) - 1, -1, -1):
            new = rewrite(parts[i])
            if new != parts[i]:
                src = "/".join(parts[: i + 1])
                dst = "/".join(parts[:i] + [new])
                if Path(root, src).exists():
                    subprocess.run(["git", "-C", root, "mv", src, dst], check=True)
                    moved += 1
                parts[i] = new
    print(f"rewrote {changed} files, moved {moved} paths")


def check(paths):
    rules = load_allow()
    bad = 0
    for rel in paths:
        if ANY_CASE.search(rel) and not allowed(rules, rel, rel):
            print(f"{rel}: path carries the old prefix")
            bad += 1
        text = read_text(Path(rel))
        if text is None:
            continue
        masked = BASE64.sub(lambda m: "x" * len(m.group(0)), text)
        for lineno, line in enumerate(masked.splitlines(), 1):
            if ANY_CASE.search(line) and not allowed(rules, rel, line):
                print(f"{rel}:{lineno}: {line.strip()[:160]}")
                bad += 1
    if bad:
        print(f"error: {bad} hits of the old prefix wdf, rename them to wft or allowlist them")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "apply" and len(sys.argv) <= 3:
        apply(sys.argv[2] if len(sys.argv) == 3 else ".")
    elif len(sys.argv) >= 2 and sys.argv[1] == "check":
        sys.exit(check(sys.argv[2:]))
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
