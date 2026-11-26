"""Each locked script exits 4 when the lock wait runs out, with a fake flock that times out."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BASH = shutil.which("bash")
REFUSAL = "refused: could not take /tmp/dsy-cluster.lock in 900 s"

LOCKED = [
    ["scripts/up.sh"],
    ["scripts/down.sh"],
    ["scripts/nuke.sh"],
    ["scripts/cluster-verify.sh"],
    ["scripts/tilt/run.sh", "ci"],
    ["scripts/tilt/run.sh", "soak"],
    ["scripts/cluster/canary.sh", "cpu"],
]


@pytest.fixture
def fakes(tmp_path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"flock": "exit 1", "tilt": "exit 0"}.items():
        fake = bin_dir / name
        fake.write_text(f'#!{BASH}\necho "${{0##*/}} $*" >>"$FAKE_LOG"\n{body}\n')
        fake.chmod(0o755)
    return bin_dir


def run(fakes: Path, args: list[str]) -> tuple[subprocess.CompletedProcess, list[str]]:
    log = fakes.parent / "calls.log"
    log.touch()
    base = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    result = subprocess.run(
        [BASH, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **base,
            "PATH": f"{fakes}:{base['PATH']}",
            "FAKE_LOG": str(log),
            "DSY_STATE_DIR": str(fakes.parent / "state"),
        },
        timeout=60,
    )
    return result, log.read_text().splitlines()


@pytest.mark.parametrize("args", LOCKED, ids=" ".join)
def test_a_lock_timeout_is_a_refusal(fakes, args):
    result, calls = run(fakes, args)
    assert (result.returncode, result.stderr.strip().splitlines()[-1]) == (4, REFUSAL)
    assert calls == ["flock -w 900 9"]


def test_the_dev_preset_takes_no_lock(fakes):
    result, calls = run(fakes, ["scripts/tilt/run.sh", "dev"])
    assert result.returncode == 0
    assert calls == ["tilt up -- --profile dev"]
