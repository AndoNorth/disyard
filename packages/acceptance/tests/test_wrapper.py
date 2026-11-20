"""scripts/smoke.sh with only fake host tools on PATH, so no cluster is touched."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / "scripts/smoke.sh"
BASH = shutil.which("bash")
DRIVER = ["uv", "run", "--frozen", "python", "-m", "wdf_acceptance.smoke"]

# Each fake appends its arguments and the lock variable it saw to calls.log.
FAKES = {
    "kind": "exit 0",
    "kubectl": "exit 0",
    "flock": 'exit "${FAKE_FLOCK_EXIT:-0}"',
    # Runs the command after `-s INT -k 10 300` unless it stands in for the timer running out.
    "timeout": '[ -z "${FAKE_TIMEOUT_EXIT:-}" ] || exit "$FAKE_TIMEOUT_EXIT"\nshift 5\nexec "$@"',
    "uv": 'exit "${FAKE_UV_EXIT:-0}"',
}


@pytest.fixture
def bin_dir(tmp_path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        fake = bin_dir / name
        log = 'echo "${0##*/} lock=${WDF_CLUSTER_LOCK_HELD:-} $*" >>"$FAKE_LOG"'
        fake.write_text(f"#!{BASH}\n{log}\n{body}\n")
        fake.chmod(0o755)
    (bin_dir / "dirname").symlink_to(shutil.which("dirname"))
    return bin_dir


def smoke(bin_dir: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    log = bin_dir.parent / "calls.log"
    log.touch()
    base = {k: v for k, v in os.environ.items() if not k.startswith(("WDF_", "FAKE_"))}
    result = subprocess.run(
        [BASH, SMOKE],
        capture_output=True,
        text=True,
        env={**base, "PATH": str(bin_dir), "FAKE_LOG": str(log), **env},
    )
    return result, log.read_text().splitlines()


def test_another_preset_is_usage(bin_dir):
    result, calls = smoke(bin_dir, WDF_PRESET="dev")
    assert result.returncode == 2
    assert "preset ci, the only preset that deploys the pool workers, not dev" in result.stderr
    assert calls == []


def test_a_missing_tool_is_usage(bin_dir):
    (bin_dir / "kubectl").unlink()
    result, calls = smoke(bin_dir)
    assert result.returncode == 2
    assert "usage: kubectl not on PATH" in result.stderr
    assert calls == []


def test_a_lock_not_taken_is_refused(bin_dir):
    result, calls = smoke(bin_dir, FAKE_FLOCK_EXIT="1")
    assert result.returncode == 4
    assert result.stderr.strip() == "refused: could not take /tmp/wdf-cluster.lock in 900 s"
    assert calls == ["flock lock= -w 900 9"]


def test_the_lock_held_by_a_caller_is_not_taken_again(bin_dir):
    result, calls = smoke(bin_dir, WDF_CLUSTER_LOCK_HELD="1")
    assert result.returncode == 0
    assert [c.split()[0] for c in calls] == ["timeout", "uv"]


@pytest.mark.parametrize("driver_exit", [0, 1, 2, 4])
def test_the_driver_exit_passes_through(bin_dir, driver_exit):
    result, calls = smoke(bin_dir, FAKE_UV_EXIT=str(driver_exit))
    assert result.returncode == driver_exit
    assert calls == [
        "flock lock= -w 900 9",
        f"timeout lock=1 -s INT -k 10 300 {' '.join(DRIVER)}",
        f"uv lock=1 {' '.join(DRIVER[1:])}",
    ]


def test_the_budget_running_out_fails(bin_dir):
    result, _ = smoke(bin_dir, FAKE_TIMEOUT_EXIT="124")
    assert result.returncode == 1
    assert result.stdout == ""


def test_a_driver_killed_on_the_budget_fails_with_the_wrapper_summary(bin_dir):
    result, _ = smoke(bin_dir, FAKE_TIMEOUT_EXIT="137")
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["FAIL budget", "smoke ci: budget ran out after 300 s"]
