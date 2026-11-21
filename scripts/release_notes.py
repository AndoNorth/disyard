"""Prints the release notes for the commits after the highest v* tag, up to a ref.

The notes are a skeleton for the GitHub Release body: the target commit's full sha on the first
line, a summary placeholder, breaking changes, the contract versions that changed, one section per
commit type in a fixed order, and a gate checklist for the release evidence.
Each commit subject is read as `type(scope)!: [#issue] summary (#PR)`, as
docs/delivery/release-process.md describes.
Usage: release_notes.py [ref], where ref defaults to HEAD.
"""

import re
import subprocess
import sys
from typing import NamedTuple

# Commit type to section header; the order of first appearance is the order of the sections.
CATEGORIES = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
    "refactor": "Refactors",
    "docs": "Documentation",
    "test": "Tests",
    "ci": "CI and build",
    "build": "CI and build",
    "chore": "Chores",
    "other": "Other",
}
TYPES = [t for t in CATEGORIES if t != "other"]

SUBJECT_RE = re.compile(r"(?P<type>\w+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<rest>.*)")
ISSUE_RE = re.compile(r"\[#(\d+)\]")
PR_RE = re.compile(r"\s*\(#(\d+)\)$")
TAG_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
CONTRACT_VERSION_RE = re.compile(r"contracts/([^/]+)/VERSION")

GATE = [
    "The `ci` workflow's `check` job concluded `success` on the push of this commit to `main`",
    "The `kind-smoke` workflow concluded `success` on the push of this commit to `main`",
    "`just smoke` passed on a fresh cluster deployed from a clean export of this commit",
    "`just e2e` passed on that same deploy",
    "`just versions --check` passed",
]


class Parsed(NamedTuple):
    category: str
    scope: str
    breaking: bool
    issues: list[str]
    pr: str | None
    message: str


class Entry(NamedTuple):
    sha: str
    timestamp: int
    parsed: Parsed


def run_git(args):
    run = subprocess.run(["git", *args], capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip())
    return run.stdout.strip()


def repo_url():
    """The https URL of the origin remote, from either its ssh or its https form."""
    url = run_git(["remote", "get-url", "origin"]).removesuffix(".git")
    if url.startswith("git@"):
        host, path = url.removeprefix("git@").split(":", 1)
        return f"https://{host}/{path}"
    return url


def latest_tag():
    """The highest vX.Y.Z tag by version order, or None when no release exists yet."""
    tags = [t for t in run_git(["tag", "--list", "v*"]).splitlines() if TAG_RE.fullmatch(t)]
    if not tags:
        return None
    return max(tags, key=lambda t: tuple(int(n) for n in TAG_RE.fullmatch(t).groups()))


def commits(start, end):
    """(sha, commit time, subject) for each commit after start up to end, oldest first."""
    span = f"{start}..{end}" if start else end
    out = run_git(["log", span, "--format=%H|%ct|%s"])
    rows = []
    for line in out.splitlines():
        sha, timestamp, subject = line.split("|", 2)
        rows.append((sha, int(timestamp), subject))
    return sorted(rows, key=lambda row: row[1])


def contract_versions(ref):
    """Each contract's VERSION at ref, by contract name."""
    paths = run_git(["ls-tree", "-r", "--name-only", ref, "--", "contracts"]).splitlines()
    return {
        m.group(1): run_git(["show", f"{ref}:{path}"])
        for path in paths
        if (m := CONTRACT_VERSION_RE.fullmatch(path))
    }


def contract_changes(start, end):
    """(name, old, new) for each contract whose version differs; old is empty with no start."""
    old = contract_versions(start) if start else {}
    new = contract_versions(end)
    return [
        (name, old.get(name, ""), new.get(name, ""))
        for name in sorted(old.keys() | new.keys())
        if old.get(name) != new.get(name)
    ]


def parse(subject):
    """Splits a subject into its parts; a subject without a known type lands in other."""
    text = subject.strip()
    pr = None
    if m := PR_RE.search(text):
        pr = m.group(1)
        text = text[: m.start()]
    category, scope, breaking = "other", "", False
    m = SUBJECT_RE.fullmatch(text)
    if m and m.group("type") in TYPES:
        category, scope, breaking = (
            m.group("type"),
            m.group("scope") or "",
            bool(m.group("breaking")),
        )
        text = m.group("rest")
    issues = ISSUE_RE.findall(text)
    message = " ".join(ISSUE_RE.sub("", text).split())
    return Parsed(category, scope, breaking, issues, pr, message)


def check_title(title):
    """The ways a PR title breaks the commit format, empty when it conforms."""
    m = SUBJECT_RE.fullmatch(title.strip())
    if not m or m.group("type") not in TYPES:
        return [f"start with `type(scope)!: `, where type is one of {', '.join(TYPES)}"]
    problems = []
    if m.group("scope") == "":
        problems.append("drop the empty `()` or name a scope")
    message = parse(title).message
    if not message:
        problems.append("add a summary after the type")
    elif message.endswith("."):
        problems.append("drop the trailing full stop")
    elif re.match(r"[A-Z][a-z]", message):
        problems.append("start the summary in lower case")
    return problems


def link(url, path, text):
    return f"[{text}]({url}/{path})" if url else text


def render_entry(entry, url):
    p = entry.parsed
    refs = [link(url, f"issues/{n}", f"#{n}") for n in p.issues]
    if p.pr:
        refs.append(link(url, f"pull/{p.pr}", f"PR #{p.pr}"))
    refs.append(link(url, f"commit/{entry.sha}", entry.sha[:7]))
    scope = f"**{p.scope}:** " if p.scope else ""
    return f"- {scope}{p.message} ({', '.join(refs)})"


def section(title, entries, url):
    noun = "change" if len(entries) == 1 else "changes"
    return [f"## {title} ({len(entries)} {noun})", "", *(render_entry(e, url) for e in entries), ""]


def render(sha, entries, contracts, url):
    lines = [sha, "", "## Summary", "", "<!-- Write the release summary here. -->", ""]
    breaking = [e for e in entries if e.parsed.breaking]
    if breaking:
        lines += section("Breaking changes", breaking, url)
    lines += ["## Contract versions", ""]
    if contracts:
        lines += ["| Contract | Old | New |", "| --- | --- | --- |"]
        lines += [f"| `{name}` | {old} | {new} |" for name, old, new in contracts]
    else:
        lines.append("No contract version changed.")
    lines.append("")
    for header in dict.fromkeys(CATEGORIES.values()):
        group = [e for e in entries if CATEGORIES[e.parsed.category] == header]
        if group:
            lines += section(header, group, url)
    lines += ["## Gate", "", *(f"- [ ] {check}" for check in GATE)]
    return "\n".join(lines) + "\n"


def generate(end="HEAD"):
    start = latest_tag()
    sha = run_git(["rev-parse", f"{end}^{{commit}}"])
    entries = [Entry(h, t, parse(s)) for h, t, s in commits(start, sha)]
    return render(sha, entries, contract_changes(start, sha), repo_url())


def main():
    if len(sys.argv) > 2:
        print("usage: release_notes.py [ref]", file=sys.stderr)
        return 2
    sys.stdout.write(generate(*sys.argv[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
