"""release_notes.py and check_pr_title.py tests, run by `just ci`, with git replaced by a fake."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release_notes", SCRIPTS / "release_notes.py")
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

URL = "https://github.com/owner/repo"
SHA = "a" * 40


def fake_git(monkeypatch, answers):
    """Answers each git call from a dict keyed by its arguments joined with spaces."""
    calls = []

    def run_git(args):
        key = " ".join(args)
        calls.append(key)
        if key not in answers:
            raise RuntimeError(f"unexpected git {key}")
        return answers[key]

    monkeypatch.setattr(release_notes, "run_git", run_git)
    return calls


def entry(subject, sha=SHA, timestamp=0):
    return release_notes.Entry(sha, timestamp, release_notes.parse(subject))


# Parser


@pytest.mark.parametrize("kind", release_notes.TYPES)
def test_each_type_lands_in_its_category(kind):
    assert release_notes.parse(f"{kind}: do a thing (#1)").category == kind


def test_a_full_subject_is_split_into_its_parts():
    parsed = release_notes.parse("feat(api)!: [#12] [#13] drop the v0 route (#99)")
    assert parsed == release_notes.Parsed(
        "feat", "api", True, ["12", "13"], "99", "drop the v0 route"
    )


def test_a_subject_without_a_type_lands_in_other():
    parsed = release_notes.parse("[#464] reaper namespace fix (#523)")
    assert parsed == release_notes.Parsed(
        "other", "", False, ["464"], "523", "reaper namespace fix"
    )


@pytest.mark.parametrize(
    "subject", ["Feat: add x", "feature: add x", "feat:add x", "merge branch x"]
)
def test_an_unknown_or_malformed_type_lands_in_other(subject):
    parsed = release_notes.parse(subject)
    assert (parsed.category, parsed.message) == ("other", subject)


def test_a_subject_without_a_pr_keeps_its_category():
    parsed = release_notes.parse("fix(events): [#7] keep the order")
    assert (parsed.category, parsed.pr, parsed.issues) == ("fix", None, ["7"])


def test_a_word_with_a_colon_inside_the_summary_is_not_a_second_type():
    parsed = release_notes.parse("docs: note: the gate runs once")
    assert (parsed.category, parsed.message) == ("docs", "note: the gate runs once")


@pytest.mark.parametrize(
    "title",
    [
        "feat(acceptance): [#543] add the smoke driver",
        "fix: keep the order",
        "refactor(api)!: [#1] [#2] drop the v0 route",
        "docs(readme): re-render the GIF (#607)",
        "ci: CI_ENABLED skips the title check",
    ],
)
def test_a_conforming_title_passes(title):
    assert release_notes.check_title(title) == []


@pytest.mark.parametrize(
    ("title", "problem"),
    [
        ("Add the smoke driver", "start with"),
        ("feature: add the smoke driver", "start with"),
        ("feat:add the smoke driver", "start with"),
        ("feat(): add the smoke driver", "empty `()`"),
        ("feat: [#543]", "add a summary"),
        ("feat: add the smoke driver.", "full stop"),
        ("feat: Add the smoke driver", "lower case"),
    ],
)
def test_a_title_off_the_format_fails(title, problem):
    problems = release_notes.check_title(title)
    assert len(problems) == 1 and problem in problems[0]


def test_the_title_script_exit_code_follows_the_check():
    script = [sys.executable, SCRIPTS / "check_pr_title.py"]
    good = subprocess.run([*script, "feat: add x"], capture_output=True, text=True)
    bad = subprocess.run([*script, "Add x"], capture_output=True, text=True)
    assert (good.returncode, bad.returncode) == (0, 1)
    assert "does not follow the commit format" in bad.stderr


# Formatter


def test_each_entry_links_its_issues_pr_and_commit():
    line = release_notes.render_entry(entry("feat(api): [#12] add x (#99)"), URL)
    assert line == (
        f"- **api:** add x ([#12]({URL}/issues/12), [PR #99]({URL}/pull/99), "
        f"[aaaaaaa]({URL}/commit/{SHA}))"
    )


def test_sections_follow_the_category_map_with_counts():
    entries = [entry(s) for s in ["chore: c", "fix: b", "feat: a", "ci: d", "build: e", "odd one"]]
    notes = release_notes.render(SHA, entries, [], URL)
    headers = [line for line in notes.splitlines() if line.startswith("## ")]
    assert headers == [
        "## Summary",
        "## Contract versions",
        "## Features (1 change)",
        "## Fixes (1 change)",
        "## CI and build (2 changes)",
        "## Chores (1 change)",
        "## Other (1 change)",
        "## Gate",
    ]


def test_breaking_changes_and_contracts_come_first_and_the_gate_last():
    entries = [entry("fix: a"), entry("feat(api)!: drop b")]
    notes = release_notes.render(SHA, entries, [("events", "4", "5")], URL)
    lines = notes.splitlines()
    assert lines[0] == SHA
    headers = [line for line in lines if line.startswith("## ")]
    assert headers[1:3] == ["## Breaking changes (1 change)", "## Contract versions"]
    assert headers[-1] == "## Gate"
    assert "| `events` | 4 | 5 |" in lines
    assert lines[-1].startswith("- [ ] ")
    # A breaking entry is listed under its type as well.
    assert sum("drop b" in line for line in lines) == 2


def test_no_contract_change_says_so():
    assert "No contract version changed." in release_notes.render(SHA, [], [], URL)


# Git layer


@pytest.mark.parametrize(
    ("remote", "url"),
    [
        ("git@github.com:owner/repo.git", URL),
        ("https://github.com/owner/repo.git", URL),
        ("https://github.com/owner/repo", URL),
    ],
)
def test_repo_url_from_either_remote_form(monkeypatch, remote, url):
    fake_git(monkeypatch, {"remote get-url origin": remote})
    assert release_notes.repo_url() == url


def test_the_highest_tag_is_chosen_by_version_order(monkeypatch):
    fake_git(monkeypatch, {"tag --list v*": "v0.10.0\nv0.9.1\nv0.2.0\nvnext\nv1.0.0-rc.1"})
    assert release_notes.latest_tag() == "v0.10.0"


def test_no_tag_means_no_previous_release(monkeypatch):
    fake_git(monkeypatch, {"tag --list v*": ""})
    assert release_notes.latest_tag() is None


def test_commits_are_listed_oldest_first(monkeypatch):
    calls = fake_git(monkeypatch, {"log v0.1.0..HEAD --format=%H|%ct|%s": "h2|200|b|c\nh1|100|a"})
    assert release_notes.commits("v0.1.0", "HEAD") == [("h1", 100, "a"), ("h2", 200, "b|c")]
    assert calls == ["log v0.1.0..HEAD --format=%H|%ct|%s"]


def test_no_tag_lists_the_whole_history(monkeypatch):
    fake_git(monkeypatch, {"log HEAD --format=%H|%ct|%s": "h1|100|a"})
    assert release_notes.commits(None, "HEAD") == [("h1", 100, "a")]


def contracts_at(ref, versions):
    paths = [f"contracts/{name}/VERSION" for name in versions] + ["contracts/README.md"]
    answers = {f"ls-tree -r --name-only {ref} -- contracts": "\n".join(paths)}
    answers |= {f"show {ref}:contracts/{name}/VERSION": v for name, v in versions.items()}
    return answers


def test_a_changed_contract_version_is_listed(monkeypatch):
    answers = contracts_at("v0.1.0", {"events": "4", "api": "1"})
    answers |= contracts_at("HEAD", {"events": "5", "api": "1", "store": "1"})
    fake_git(monkeypatch, answers)
    assert release_notes.contract_changes("v0.1.0", "HEAD") == [
        ("events", "4", "5"),
        ("store", "", "1"),
    ]


def test_with_no_previous_tag_every_contract_is_listed(monkeypatch):
    fake_git(monkeypatch, contracts_at("HEAD", {"events": "5", "api": "1"}))
    assert release_notes.contract_changes(None, "HEAD") == [("api", "", "1"), ("events", "", "5")]


# End to end


def test_the_whole_notes_file(monkeypatch):
    answers = {
        "tag --list v*": "v0.1.0",
        "rev-parse HEAD^{commit}": SHA,
        f"log v0.1.0..{SHA} --format=%H|%ct|%s": "\n".join(
            [
                f"{'c' * 40}|300|docs: [#5] explain the gate (#11)",
                f"{'b' * 40}|200|feat(api)!: [#4] drop the v0 route (#10)",
            ]
        ),
        "remote get-url origin": "git@github.com:owner/repo.git",
    }
    answers |= contracts_at("v0.1.0", {"api": "1"}) | contracts_at(SHA, {"api": "2"})
    fake_git(monkeypatch, answers)
    feat = (
        f"- **api:** drop the v0 route ([#4]({URL}/issues/4), [PR #10]({URL}/pull/10), "
        f"[bbbbbbb]({URL}/commit/{'b' * 40}))"
    )
    docs = (
        f"- explain the gate ([#5]({URL}/issues/5), [PR #11]({URL}/pull/11), "
        f"[ccccccc]({URL}/commit/{'c' * 40}))"
    )
    gate = [f"- [ ] {check}" for check in release_notes.GATE]
    assert release_notes.generate().splitlines() == [
        SHA,
        "",
        "## Summary",
        "",
        "<!-- Write the release summary here. -->",
        "",
        "## Breaking changes (1 change)",
        "",
        feat,
        "",
        "## Contract versions",
        "",
        "| Contract | Old | New |",
        "| --- | --- | --- |",
        "| `api` | 1 | 2 |",
        "",
        "## Features (1 change)",
        "",
        feat,
        "",
        "## Documentation (1 change)",
        "",
        docs,
        "",
        "## Gate",
        "",
        *gate,
    ]
