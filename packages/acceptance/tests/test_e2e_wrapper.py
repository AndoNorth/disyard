"""scripts/e2e.sh with only fake host tools on PATH, so no cluster is touched."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
E2E = ROOT / "scripts/e2e.sh"
BASH = shutil.which("bash")
SUITE = "uv run --frozen pytest packages/acceptance/tests/e2e -q -p no:cacheprovider"

# Each fake appends its arguments and the lock variable it saw to calls.log.
FAKES = {
    "kind": 'echo "${FAKE_KIND_OUT-dsy}"',
    "kubectl": "exit 0",
    "flock": 'exit "${FAKE_FLOCK_EXIT:-0}"',
    # Runs the command after `-s INT -k 10 <seconds>`, unless it stands in for the suite's timer.
    "timeout": """case "$*" in
    *pytest*) [ -z "${FAKE_TIMEOUT_EXIT:-}" ] || exit "$FAKE_TIMEOUT_EXIT" ;;
esac
shift 5
exec "$@"
""",
    # The smoke driver and the suite both run through uv.
    "uv": 'case "$*" in *dsy_acceptance.smoke*) exit "${FAKE_SMOKE_EXIT:-0}" ;; esac\n'
    'exit "${FAKE_UV_EXIT:-0}"',
}


@pytest.fixture
def bin_dir(tmp_path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        fake = bin_dir / name
        log = 'echo "${0##*/} lock=${DSY_CLUSTER_LOCK_HELD:-} $*" >>"$FAKE_LOG"'
        fake.write_text(f"#!{BASH}\n{log}\n{body}\n")
        fake.chmod(0o755)
    # What the real obs.sh and smoke.sh run on.
    for name in ("dirname", "cat", "env", "bash"):
        (bin_dir / name).symlink_to(shutil.which(name))
    state = tmp_path / "state"
    state.mkdir()
    (state / "cluster.obs").write_text("on\n")
    return bin_dir


def e2e(bin_dir: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    log = bin_dir.parent / "calls.log"
    log.touch()
    base = {k: v for k, v in os.environ.items() if not k.startswith(("DSY_", "FAKE_"))}
    result = subprocess.run(
        [BASH, E2E],
        capture_output=True,
        text=True,
        env={
            **base,
            "PATH": str(bin_dir),
            "DSY_STATE_DIR": str(bin_dir.parent / "state"),
            "FAKE_LOG": str(log),
            **env,
        },
    )
    return result, log.read_text().splitlines()


def test_another_preset_is_usage(bin_dir):
    result, calls = e2e(bin_dir, DSY_PRESET="dev")
    assert result.returncode == 2
    assert "preset ci, the only preset that deploys the pool workers, not dev" in result.stderr
    assert calls == []


def test_a_missing_tool_is_usage(bin_dir):
    (bin_dir / "kind").unlink()
    result, calls = e2e(bin_dir)
    assert result.returncode == 2
    assert "usage: kind not on PATH" in result.stderr
    assert calls == []


def test_a_lock_not_taken_is_refused(bin_dir):
    result, calls = e2e(bin_dir, FAKE_FLOCK_EXIT="1")
    assert result.returncode == 4
    assert result.stderr.strip() == "refused: could not take /tmp/dsy-cluster.lock in 900 s"
    assert calls == ["flock lock= -w 900 9"]


def test_the_stack_off_is_refused(bin_dir):
    (bin_dir.parent / "state/cluster.obs").write_text("off\n")
    result, calls = e2e(bin_dir)
    assert result.returncode == 4
    assert "observability stack off" in result.stderr
    assert calls == ["flock lock= -w 900 9"]


def test_a_missing_cluster_is_refused(bin_dir):
    result, calls = e2e(bin_dir, FAKE_KIND_OUT="other")
    assert result.returncode == 4
    assert "no kind cluster dsy" in result.stderr
    assert [c.split()[0] for c in calls] == ["flock", "kind"]


def test_the_lock_held_by_a_caller_is_not_taken_again(bin_dir):
    result, calls = e2e(bin_dir, DSY_CLUSTER_LOCK_HELD="1")
    assert result.returncode == 0
    assert "flock" not in [c.split()[0] for c in calls]


def test_smoke_runs_first_under_the_held_lock_then_the_suite(bin_dir):
    result, calls = e2e(bin_dir)
    assert result.returncode == 0
    smoke = "uv run --frozen python -m dsy_acceptance.smoke"
    assert calls == [
        "flock lock= -w 900 9",
        "kind lock=1 get clusters",
        f"timeout lock=1 -s INT -k 10 300 {smoke}",
        f"uv lock=1 {smoke.removeprefix('uv ')}",
        f"timeout lock=1 -s INT -k 10 600 {SUITE}",
        f"uv lock=1 {SUITE.removeprefix('uv ')}",
    ]


@pytest.mark.parametrize("smoke_exit", [1, 2, 4])
def test_a_failed_smoke_ends_the_run_with_its_exit(bin_dir, smoke_exit):
    result, calls = e2e(bin_dir, FAKE_SMOKE_EXIT=str(smoke_exit))
    assert result.returncode == smoke_exit
    assert not any("pytest" in c for c in calls)


@pytest.mark.parametrize(("suite_exit", "want"), [(0, 0), (1, 1), (2, 1), (3, 1)])
def test_the_suite_exit_maps_to_the_recipe_contract(bin_dir, suite_exit, want):
    result, _ = e2e(bin_dir, FAKE_UV_EXIT=str(suite_exit))
    assert result.returncode == want


def test_no_scenario_collected_fails(bin_dir):
    result, _ = e2e(bin_dir, FAKE_UV_EXIT="5")
    assert result.returncode == 1
    assert result.stdout.strip() == "FAIL scenarios none collected"


def test_the_budget_running_out_fails(bin_dir):
    result, _ = e2e(bin_dir, FAKE_TIMEOUT_EXIT="124")
    assert result.returncode == 1
    assert result.stdout == ""


def test_a_suite_killed_on_the_budget_fails_with_the_wrapper_summary(bin_dir):
    result, _ = e2e(bin_dir, FAKE_TIMEOUT_EXIT="137")
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["FAIL budget", "e2e ci: budget ran out after 600 s"]
