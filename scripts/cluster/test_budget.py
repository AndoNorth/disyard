"""Cluster budget tests, run by `just ci`: each preset holds its modules in the worst case."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRESETS = ["ci", "dev", "soak"]


def budget(preset, modules=None):
    env = {**os.environ}
    if modules is not None:
        env["DSY_MODULES_DIR"] = str(modules)
    script = ROOT / "scripts/cluster/budget.sh"
    return subprocess.run([script, preset], cwd=ROOT, env=env, capture_output=True, text=True)


def module(root, name, namespace, memory, tiltfile=""):
    group = namespace.removeprefix("dsy-")
    path = root / name
    path.mkdir()
    size = f"{{cpu: 100m, memory: {memory}}}"
    (path / "module.yaml").write_text(
        f"name: {name}\ngroup: {group}\nnamespace: {namespace}\nprofiles: [ci]\nresources:\n"
        f"  {name}:\n    requests: {size}\n    limits: {size}\n"
    )
    (path / "Tiltfile.module").write_text(tiltfile)


@pytest.mark.parametrize("preset", PRESETS)
def test_every_preset_holds_its_modules(preset):
    run = budget(preset)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OVER" not in run.stdout


def test_ci_infra_node_figures():
    run = budget("ci")
    assert "node infra requests cpu: 2730m of 2900m, free 170m, fits" in run.stdout
    assert "node infra requests memory: 5130Mi of 5504Mi, free 374Mi, fits" in run.stdout
    assert "workflow-worker (workflow-worker, second build)" in run.stdout
    assert "temporal-schema (temporal)" in run.stdout
    assert "appdb-migrate" not in run.stdout
    assert "item dsy-infra rabbitmq (rabbitmq): cpu 100m/500m, memory 320Mi/320Mi" in run.stdout
    assert "rabbitmq (reserve)" not in run.stdout
    assert "item dsy-control admission (admission): cpu 50m/250m, memory 128Mi/128Mi" in run.stdout
    assert "admission (reserve)" not in run.stdout
    assert "item dsy-control dispatch (dispatch): cpu 100m/500m, memory 256Mi/256Mi" in run.stdout
    assert "dispatch (reserve)" not in run.stdout


def test_infra_node_of_5120_mib_is_over():
    target = ROOT / "deploy/presets/ci.yaml"
    original = target.read_text()
    try:
        main = original.replace("memory_mib: 11904", "memory_mib: 10880")
        target.write_text(main.replace("memory_mib: 6144", "memory_mib: 5120"))
        run = budget("ci")
    finally:
        target.write_text(original)
    assert run.returncode == 1
    assert "node infra requests memory: 5130Mi of 4544Mi, OVER by 586Mi" in run.stdout


def test_module_set_over_the_node_is_refused(tmp_path):
    module(tmp_path, "big", "dsy-infra", "5000Mi")
    module(tmp_path, "side", "dsy-control", "512Mi", tiltfile='build_id_env = "DSY_BUILD_ID"\n')
    run = budget("ci", tmp_path)
    assert run.returncode == 1
    assert "side (side, second build)" in run.stdout
    assert "OVER by" in run.stdout


def test_module_replaces_the_reserve_of_its_name(tmp_path):
    module(tmp_path, "rabbitmq", "dsy-infra", "300Mi")
    run = budget("ci", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "item dsy-infra rabbitmq (rabbitmq): cpu 100m/100m, memory 300Mi/300Mi" in run.stdout
    assert "rabbitmq (reserve)" not in run.stdout
    assert "dispatch (reserve)" in run.stdout
