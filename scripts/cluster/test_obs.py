"""Observability switch tests, run by `just ci`.

DSY_OBS at `just up`, its record, and every reader of the obs node.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OFF_MIB, ON_MIB = 640, 4096


def run(args, tmp_path, **env):
    base = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "DSY_STATE_DIR": str(tmp_path / "state"),
        "DSY_RENDER_DIR": str(tmp_path / "render"),
        "DSY_CLUSTER_LOCK_HELD": "1",
        "DSY_HOST_MEM_KIB": "16000000",
        "DSY_HOST_CPUS": "8",
    }
    return subprocess.run(
        [str(a) for a in args], cwd=ROOT, env={**base, **env}, capture_output=True, text=True
    )


def obs_limit(tmp_path, **env):
    out = run([ROOT / "scripts/cluster/render.sh", "ci"], tmp_path, **env)
    assert out.returncode == 0, out.stderr
    limits = (Path(out.stdout.strip()) / "limits.yaml").read_text()
    return re.search(r"role: obs\n    cpus: (\S+)\n    memory_mib: (\d+)\n", limits).groups()


def obs_quota(tmp_path, **env):
    out = run([ROOT / "scripts/cluster/namespaces.sh", "render", "ci"], tmp_path, **env)
    assert out.returncode == 0, out.stderr
    doc = next(d for d in out.stdout.split("---") if "namespace: dsy-obs" in d)
    return re.search(r"requests.memory: (\d+)Mi", doc).group(1)


def quota_of(node_mib):
    allocatable = node_mib - (256 + -(-node_mib // 16))
    return str(-(-allocatable * 105 // 100))


def test_ci_renders_the_obs_node_and_its_quota_both_ways(tmp_path):
    assert obs_limit(tmp_path) == ("0.5", str(OFF_MIB))
    assert obs_quota(tmp_path) == quota_of(OFF_MIB)
    assert obs_limit(tmp_path, DSY_OBS="on") == ("1.5", str(ON_MIB))
    assert obs_quota(tmp_path, DSY_OBS="on") == quota_of(ON_MIB)


@pytest.mark.parametrize("preset", ["ci", "soak"])
def test_budget_holds_both_ways_and_prints_the_obs_stack_node(preset, tmp_path):
    off = run([ROOT / "scripts/cluster/budget.sh", preset], tmp_path)
    on = run([ROOT / "scripts/cluster/budget.sh", preset], tmp_path, DSY_OBS="on")
    assert off.returncode == 0, off.stdout + off.stderr
    assert on.returncode == 0, on.stdout + on.stderr
    allocatable = ON_MIB - (256 + -(-ON_MIB // 16))
    assert f"node obs requests memory: 50Mi of {allocatable}Mi" in on.stdout
    assert "node obs requests cpu: 100m of 1400m" in on.stdout
    assert f"of {allocatable}Mi" not in off.stdout


def test_switch_on_is_a_usage_error_on_dev(tmp_path):
    cluster = ROOT / "scripts/cluster"
    for args in (["budget.sh", "dev"], ["render.sh", "dev"], ["namespaces.sh", "render", "dev"]):
        assert run([cluster / args[0], *args[1:]], tmp_path, DSY_OBS="on").returncode == 2
    assert run([cluster / "budget.sh", "ci"], tmp_path, DSY_OBS="yes").returncode == 2


@pytest.mark.parametrize("record, want", [("on", "on"), ("off", "off"), (None, "off")])
def test_obs_prints_the_recorded_value(record, want, tmp_path):
    if record is not None:
        (tmp_path / "state").mkdir()
        (tmp_path / "state/cluster.obs").write_text(record + "\n")
    out = run([ROOT / "scripts/cluster/obs.sh"], tmp_path)
    assert (out.returncode, out.stdout) == (0, want + "\n")


def test_up_refuses_the_switch_under_github_actions_before_it_renders(tmp_path):
    out = run([ROOT / "scripts/up.sh"], tmp_path, DSY_OBS="on", GITHUB_ACTIONS="true")
    assert out.returncode == 2, out.stderr
    assert "refused under GitHub Actions" in out.stderr
    assert not (tmp_path / "render").exists()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("record, switch", [("off", "on"), (None, "on"), ("on", None)])
def test_up_refuses_an_existing_cluster_recorded_with_the_other_value(record, switch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kind").write_text("#!/bin/sh\necho dsy\n")
    (bin_dir / "kind").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "cluster.owner").write_text("ci\n")
    if record is not None:
        (state / "cluster.obs").write_text(record + "\n")
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    if switch is not None:
        env["DSY_OBS"] = switch
    out = run([ROOT / "scripts/up.sh"], tmp_path, **env)
    assert out.returncode == 4, out.stderr
    assert f"started with DSY_OBS={record or 'off'}, not {switch or 'off'}" in out.stderr
    assert not (tmp_path / "render").exists()


def doctor_memory(tmp_path, mib, **env):
    host = tmp_path / f"host-{mib}"
    (host / "proc").mkdir(parents=True, exist_ok=True)
    (host / "proc/meminfo").write_text(f"MemTotal:        {mib * 1024} kB\n")
    out = run(
        [ROOT / "scripts/doctor.sh"],
        tmp_path,
        IN_NIX_SHELL="impure",
        DSY_DOCTOR_ROOT=str(host),
        DSY_DOCTOR_REPO=str(ROOT),
        **env,
    )
    return next(line for line in out.stdout.splitlines() if " memory-vs-preset: " in line)


@pytest.mark.parametrize("mib", [12032, 13000, 15487])
def test_doctor_passes_off_and_warns_on_between_the_two_sums(mib, tmp_path):
    assert doctor_memory(tmp_path, mib).startswith("ok memory-vs-preset:")
    assert doctor_memory(tmp_path, mib, DSY_OBS="on").startswith("warn memory-vs-preset:")


def test_doctor_reads_obs_memory_mib_with_the_switch_on(tmp_path):
    assert doctor_memory(tmp_path, 15488, DSY_OBS="on").startswith("ok memory-vs-preset:")
    assert doctor_memory(tmp_path, 12031, DSY_OBS="on").startswith("fail memory-vs-preset:")
    assert doctor_memory(tmp_path, 16000, DSY_OBS="on", DSY_PRESET="dev").startswith(
        "fail memory-vs-preset:"
    )


def test_no_workflow_names_the_switch():
    workflows = sorted((ROOT / ".github/workflows").rglob("*"))
    assert workflows
    assert [p for p in workflows if p.is_file() and "DSY_OBS" in p.read_text()] == []
