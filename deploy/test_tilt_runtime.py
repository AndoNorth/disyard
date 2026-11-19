"""The render tests' Tilt writes its kubeconfig away from the one a running Tilt hands out."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


def test_kubeconfig_lands_in_the_private_runtime_dir(tmp_path, tmp_path_factory):
    (tmp_path / "Tiltfile").write_text("local('true')\n")
    env = os.environ | {"KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wdf.yaml")}
    run = subprocess.run(
        ["tilt", "alpha", "tiltfile-result", "-f", str(tmp_path / "Tiltfile")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    runtime = Path(env["XDG_RUNTIME_DIR"])
    assert runtime.is_relative_to(tmp_path_factory.getbasetemp())
    assert (runtime / "tilt-dev/tilt-default/cluster/default.yml").is_file()
