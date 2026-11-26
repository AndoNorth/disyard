import ast
import os
import sys
import time
from pathlib import Path

import pytest
from dsy_servicekit import testing

TOY_DIR = Path(__file__).parent / "toy"
TOY = [sys.executable, "-m", "toy"]
SRC = Path(testing.__file__)


def _env(tmp_path, **extra):
    return {
        **os.environ,
        "PYTHONPATH": str(TOY_DIR.parent),
        "TOY_NAME": "x",
        "TOY_READY_FILE": str(tmp_path / "ready"),
        **extra,
    }


def _broken(code):
    return [sys.executable, "-c", code]


EXIT_1_WITHOUT_ENV = "import sys; sys.stderr.write('TOY_NAME'); sys.exit(1)"
LEAVES_READY_FILE = (
    "import os, signal, sys, pathlib\n"
    "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
    "pathlib.Path(os.environ['TOY_READY_FILE']).touch()\n"
    "signal.pause()\n"
)
IGNORES_SIGTERM = (
    "import os, signal, time, pathlib\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "pathlib.Path(os.environ['TOY_READY_FILE']).touch()\n"
    "time.sleep(60)\n"
)


@pytest.mark.seam
def test_missing_env_check_fails_on_exit_1_and_passes_on_the_toy(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOY_DIR.parent))
    with pytest.raises(AssertionError, match="exit 1"):
        testing.check_missing_env_exits_2(_broken(EXIT_1_WITHOUT_ENV), ["TOY_NAME"])
    testing.check_missing_env_exits_2(TOY, ["TOY_NAME"])


@pytest.mark.seam
def test_ready_file_check_fails_on_a_file_left_behind_and_passes_on_the_toy(tmp_path):
    ready = tmp_path / "ready"
    env = _env(tmp_path)
    with pytest.raises(AssertionError, match="left behind"):
        testing.check_ready_file_lifecycle(_broken(LEAVES_READY_FILE), env, ready)
    ready.unlink()
    testing.check_ready_file_lifecycle(TOY, env, ready)


@pytest.mark.seam
def test_sigterm_check_fails_on_an_ignored_signal_and_passes_on_the_toy(tmp_path):
    ready = tmp_path / "ready"
    env = _env(tmp_path)
    with pytest.raises(AssertionError, match="ignored SIGTERM"):
        testing.check_sigterm_exits_0_within(_broken(IGNORES_SIGTERM), env, ready, 1)
    ready.unlink()
    testing.check_sigterm_exits_0_within(TOY, env, ready, 5)


def test_free_port_can_be_bound():
    import socket

    port = testing.free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_until_returns_the_truthy_result_and_names_what_timed_out():
    calls = iter([0, 0, "done"])
    assert testing.until(lambda: next(calls), 5, "a result", interval_s=0.01) == "done"
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="waiting for the moon"):
        testing.until(lambda: False, 0.2, "the moon", interval_s=0.05)
    assert time.monotonic() - start < 2


def test_testing_imports_only_the_standard_library_at_module_level():
    tree = ast.parse(SRC.read_text())
    top = {
        (a.name if isinstance(node, ast.Import) else node.module).split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for a in (node.names if isinstance(node, ast.Import) else [node])
    }
    assert top <= set(sys.stdlib_module_names), top - set(sys.stdlib_module_names)
