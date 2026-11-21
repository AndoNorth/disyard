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
    text = "wdf-infra wdf_keys WDF_PRESET WdfTenant dev.wdf.job /wdf/x"
    assert (
        rename_prefix.rewrite(text) == "wft-infra wft_keys WFT_PRESET WftTenant dev.wft.job /wft/x"
    )


def test_rewrite_leaves_words_and_base64_alone():
    text = 'wdfm awdf <img src="data:font/woff2;base64,AAwdf+wdf/=="> wdf'
    assert (
        rename_prefix.rewrite(text)
        == 'wdfm awdf <img src="data:font/woff2;base64,AAwdf+wdf/=="> wft'
    )


def test_history_payloads_are_rewritten_and_nothing_else():
    payload = base64.b64encode(b'{"type": "dev.wdf.job.accepted.v1"}').decode()
    text = json.dumps({"events": [{"data": payload, "note": "wdf"}]}, indent=2) + "\n"
    doc = json.loads(rename_prefix.rewrite_history(text))
    assert base64.b64decode(doc["events"][0]["data"]) == b'{"type": "dev.wft.job.accepted.v1"}'
    assert doc["events"][0]["note"] == "wdf"


def git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


def run(*args, cwd):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd, capture_output=True, text=True)


def test_apply_moves_paths_keeps_allowlisted_lines_and_is_idempotent(tmp_path):
    (tmp_path / "packages/wdf_keys").mkdir(parents=True)
    (tmp_path / "packages/wdf_keys/__init__.py").write_text("import wdf_events\n")
    (tmp_path / "contracts/events").mkdir(parents=True)
    readme = "the version 5 UUID of `wdf.dev` in the DNS namespace\nsource /wdf/dispatch\n"
    (tmp_path / "contracts/events/README.md").write_text(readme)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")

    first = run("apply", tmp_path, cwd=tmp_path)
    assert first.stdout == "rewrote 2 files, moved 1 paths\n"
    assert (tmp_path / "packages/wft_keys/__init__.py").read_text() == "import wft_events\n"
    assert (tmp_path / "contracts/events/README.md").read_text() == readme.replace("/wdf/", "/wft/")
    assert run("apply", tmp_path, cwd=tmp_path).stdout == "rewrote 0 files, moved 0 paths\n"


def test_check_refuses_any_case_outside_the_allowlist(tmp_path):
    (tmp_path / "ok.md").write_text("wft only\n")
    (tmp_path / "bad.md").write_text("fine\nlock key 0x7764666D spells wdfm\n")
    assert run("check", "ok.md", cwd=tmp_path).returncode == 0
    refused = run("check", "ok.md", "bad.md", cwd=tmp_path)
    assert refused.returncode == 1
    assert "bad.md:2: lock key 0x7764666D spells wdfm" in refused.stdout
