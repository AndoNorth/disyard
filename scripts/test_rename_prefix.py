"""rename-prefix.py tests, run by `just ci`: rewrite, path moves, history payloads and the gate."""

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "rename-prefix.py"
spec = importlib.util.spec_from_file_location("rename_prefix", SCRIPT)
rename_prefix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rename_prefix)


def test_rewrite_maps_each_case_at_a_word_boundary():
    text = "wft-infra wft_keys WFT_PRESET WftTenant dev.wft.job /wft/x"
    assert (
        rename_prefix.rewrite(text) == "dsy-infra dsy_keys DSY_PRESET DsyTenant dev.dsy.job /dsy/x"
    )


def test_rewrite_maps_the_project_name():
    text = "Weft runs on https://github.com/AndoNorth/weft ~/.cache/weft/build WEFT"
    assert rename_prefix.rewrite(text) == (
        "Disyard runs on https://github.com/AndoNorth/disyard ~/.cache/disyard/build DISYARD"
    )


def test_rewrite_maps_a_markdown_anchor_and_the_lock_key():
    text = '[a](alerts.md#wftpodrestart) LOCK_KEY = 0x7766746D  # "wftm"'
    assert rename_prefix.rewrite(text) == (
        '[a](alerts.md#dsypodrestart) LOCK_KEY = 0x6473796D  # "dsym"'
    )


def test_rewrite_leaves_words_and_base64_alone():
    text = 'wftx awft <img src="data:font/woff2;base64,AAwft+wft/=="> wft'
    assert (
        rename_prefix.rewrite(text)
        == 'wftx awft <img src="data:font/woff2;base64,AAwft+wft/=="> dsy'
    )


def test_history_payloads_are_rewritten_and_nothing_else():
    payload = base64.b64encode(b'{"type": "dev.wft.job.accepted.v1"}').decode()
    text = json.dumps({"events": [{"data": payload, "note": "wft"}]}, indent=2) + "\n"
    doc = json.loads(rename_prefix.rewrite_history(text))
    assert base64.b64decode(doc["events"][0]["data"]) == b'{"type": "dev.dsy.job.accepted.v1"}'
    assert doc["events"][0]["note"] == "wft"


def git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


def run(*args, cwd):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd, capture_output=True, text=True)


def test_apply_moves_paths_keeps_allowlisted_lines_and_is_idempotent(tmp_path):
    (tmp_path / "packages/wft_keys").mkdir(parents=True)
    (tmp_path / "packages/wft_keys/__init__.py").write_text("import wft_events\n")
    (tmp_path / "contracts/events").mkdir(parents=True)
    readme = "the version 5 UUID of `wdf.dev` in the DNS namespace\nsource /wft/dispatch\n"
    (tmp_path / "contracts/events/README.md").write_text(readme)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")

    first = run("apply", tmp_path, cwd=tmp_path)
    assert first.stdout == "rewrote 2 files, moved 1 paths\n"
    assert (tmp_path / "packages/dsy_keys/__init__.py").read_text() == "import dsy_events\n"
    assert (tmp_path / "contracts/events/README.md").read_text() == readme.replace("/wft/", "/dsy/")
    assert run("apply", tmp_path, cwd=tmp_path).stdout == "rewrote 0 files, moved 0 paths\n"


def test_check_refuses_any_case_outside_the_allowlist(tmp_path):
    (tmp_path / "ok.md").write_text("dsy only\n")
    (tmp_path / "bad.md").write_text("fine\nlock key 0x7766746D spells wftm\n")
    assert run("check", "ok.md", cwd=tmp_path).returncode == 0
    refused = run("check", "ok.md", "bad.md", cwd=tmp_path)
    assert refused.returncode == 1
    assert "bad.md:2: lock key 0x7766746D spells wftm" in refused.stdout


def test_check_refuses_the_old_project_name(tmp_path):
    (tmp_path / "bad.md").write_text("see github.com/AndoNorth/Weft\n")
    refused = run("check", "bad.md", cwd=tmp_path)
    assert refused.returncode == 1
    assert "bad.md:1: see github.com/AndoNorth/Weft" in refused.stdout
