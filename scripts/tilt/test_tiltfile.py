"""Offline Tiltfile checks through `tilt alpha tiltfile-result`, run by `just test`.

No cluster is needed.
The context comes from a fixture kubeconfig and the modules from deploy/tilt/fixtures.
vector-lite is the `ci` module in group `obs` that the observability switch enables.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "scripts/tilt/fixtures"
MODULES = ROOT / "deploy/tilt/fixtures/modules"
PROFILES = yaml.safe_load((ROOT / "deploy/tilt/profiles.yaml").read_text())["profiles"]

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


def tiltfile_result(profile="ci", kubeconfig="kubeconfig-dsy.yaml", modules=MODULES, obs="off"):
    return subprocess.run(
        [
            "tilt", "alpha", "tiltfile-result", "--",
            "--profile", profile, "--modules_dir", str(modules),
        ],
        cwd=ROOT,
        env={**os.environ, "KUBECONFIG": str(FIXTURES / kubeconfig), "DSY_OBS": obs},
        capture_output=True,
        text=True,
    )  # fmt: skip


def selection(profile, obs="off"):
    run = tiltfile_result(profile, obs=obs)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    labels = {m["Name"]: next(iter(m["Labels"])) for m in result["Manifests"]}
    return result["EnabledManifests"], labels


@pytest.mark.parametrize("obs", ["off", "on"])
@pytest.mark.parametrize("profile", PROFILES)
def test_enabled_groups_match_the_profile_table(profile, obs):
    enabled, labels = selection(profile, obs)
    opt_in = PROFILES[profile].get("opt_in", []) if obs == "on" else []
    assert {labels[name] for name in enabled} == set(PROFILES[profile]["groups"] + opt_in)


def test_obs_tier_and_mirror_differ_per_profile():
    assert set(selection("minimal")[0]) == {"store"}
    assert set(selection("dev")[0]) == {"store", "control-plane", "worker", "pyservice"}
    assert "vector" in selection("full")[0] and "vector-lite" not in selection("full")[0]
    assert "vector-lite" in selection("ci", "on")[0] and "mirror" in selection("ci")[0]
    assert "vector-lite" not in selection("ci")[0]
    assert "mirror" not in selection("full")[0]


def test_an_unknown_switch_value_is_refused():
    run = tiltfile_result(obs="yes")
    assert run.returncode != 0
    assert "DSY_OBS is yes, expected on or off" in run.stderr


def fake(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def state_dir(tmp_path, record):
    state = tmp_path / "state"
    state.mkdir()
    (state / "cluster.owner").write_text("ci\n")
    if record is not None:
        (state / "cluster.obs").write_text(record + "\n")
    return state


@pytest.mark.parametrize("record, caller", [("on", "off"), ("off", "on"), (None, "on")])
def test_run_enables_the_obs_group_for_ci_only_with_the_recorded_switch_on(
    record, caller, tmp_path
):
    # A tilt on PATH that evaluates the Tiltfile with the profile run.sh passes after `--`.
    real = shutil.which("tilt")
    fake(
        tmp_path / "tilt",
        f'while [ "$1" != -- ]; do shift; done; shift\n'
        f'exec {real} alpha tiltfile-result -- "$@" --modules_dir {MODULES}\n',
    )
    run = subprocess.run(
        [ROOT / "scripts/tilt/run.sh", "ci"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "KUBECONFIG": str(FIXTURES / "kubeconfig-dsy.yaml"),
            "DSY_STATE_DIR": str(state_dir(tmp_path, record)),
            "DSY_OBS": caller,
        },
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert ("vector-lite" in json.loads(run.stdout)["EnabledManifests"]) == (record == "on")


def test_obs_env_is_empty_with_the_switch_off(tmp_path):
    modules = tmp_path / "modules"
    shutil.copytree(MODULES, modules, ignore=shutil.ignore_patterns("pyservice"))
    probe = modules / "probe"
    shutil.copytree(MODULES / "vector-lite", probe)
    (probe / "module.yaml").write_text(
        (probe / "module.yaml").read_text().replace("vector-lite", "probe").replace("obs", "infra")
    )
    (probe / "Tiltfile.module").write_text(
        f"load('{ROOT / 'deploy/tilt/obs.star'}', 'obs_env')\n"
        "local_resource('probe', cmd='echo %s' % sorted(obs_env().items()), labels=['infra'])\n"
    )
    cmds = {}
    for obs in ("off", "on"):
        run = tiltfile_result(modules=modules, obs=obs)
        assert run.returncode == 0, run.stderr
        cmds[obs] = json.dumps(
            next(m for m in json.loads(run.stdout)["Manifests"] if m["Name"] == "probe")
        )
    assert "4318" not in cmds["off"] and "4040" not in cmds["off"]
    assert "http://collector.dsy-obs:4318" in cmds["on"]
    assert "http://pyroscope.dsy-obs:4040" in cmds["on"]


def test_cluster_verify_follows_the_record_over_the_callers_switch(tmp_path):
    # Fakes that answer as a cluster with the quotas of the switch-off render; nothing else is live.
    quotas = tmp_path / "quotas.yaml"
    render = subprocess.run(
        [ROOT / "scripts/cluster/namespaces.sh", "render", "ci"],
        cwd=ROOT,
        env={**os.environ, "DSY_OBS": "off"},
        capture_output=True,
        text=True,
        check=True,
    )
    quotas.write_text(render.stdout)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake(bin_dir / "kind", "echo dsy\n")
    fake(bin_dir / "docker", "exit 0\n")
    fake(bin_dir / "ss", "exit 0\n")
    fake(
        bin_dir / "kubectl",
        'ns=; for a; do [ "$prev" = -n ] && ns=$a; prev=$a; done\n'
        'case "$*" in\n'
        '  *"get resourcequota -o name"*) echo resourcequota/pool-capacity ;;\n'
        '  *"get limitrange -o name"*) echo limitrange/defaults ;;\n'
        '  *"get node "*) echo 0Ki 0 ;;\n'
        '  *"get resourcequota pool-capacity -o yaml"*)\n'
        f"    NS=$ns yq 'select(.metadata.namespace == strenv(NS))' {quotas} ;;\n"
        "esac\n",
    )
    run = subprocess.run(
        [ROOT / "scripts/cluster-verify.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DSY_STATE_DIR": str(state_dir(tmp_path, "off")),
            "DSY_OBS": "on",
            "DSY_CLUSTER_LOCK_HELD": "1",
            "DSY_MODULES_DIR": str(MODULES),
            "DSY_RENDER_DIR": str(tmp_path / "render"),
            "DSY_HOST_MEM_KIB": "16000000",
            "DSY_HOST_CPUS": "8",
        },
        capture_output=True,
        text=True,
    )
    assert "ok ready 5 modules of profile ci with DSY_OBS=off ready" in run.stdout, (
        run.stdout + run.stderr
    )
    # The obs node is kind's worker2, wanted at 0.5 CPU and 640 MiB.
    assert "dsy-worker2 (, want 500000000 671088640 671088640)" in run.stdout
    assert "ok quotas every namespace has its rendered quota and LimitRange" in run.stdout


def test_guard_refuses_a_second_kind_context():
    run = tiltfile_result(kubeconfig="kubeconfig-other.yaml")
    assert run.returncode != 0
    assert "refusing context kind-other, only kind-dsy is allowed" in run.stderr


def test_unknown_profile_is_refused():
    run = tiltfile_result("nope")
    assert run.returncode != 0
    assert "unknown profile nope" in run.stderr


def test_module_failing_the_contract_fails_the_load_with_its_reason(tmp_path):
    shutil.copytree(MODULES, tmp_path / "modules", ignore=shutil.ignore_patterns("pyservice"))
    bad = tmp_path / "modules/store/module.yaml"
    bad.write_text(bad.read_text().replace("group: infra", "group: nonsense"))
    run = tiltfile_result(modules=tmp_path / "modules")
    assert run.returncode != 0
    assert "reason: schema-violation" in run.stderr


def test_dependency_on_a_disabled_module_is_refused(tmp_path):
    shutil.copytree(MODULES, tmp_path / "modules", ignore=shutil.ignore_patterns("pyservice"))
    worker = tmp_path / "modules/worker/module.yaml"
    worker.write_text(worker.read_text().replace("[infra/store]", "[infra/store, infra/mirror]"))
    assert tiltfile_result("ci", modules=tmp_path / "modules").returncode == 0
    run = tiltfile_result("dev", modules=tmp_path / "modules")
    assert run.returncode != 0
    assert "module worker depends on" in run.stderr


def live_update_steps(profile):
    run = tiltfile_result(profile)
    assert run.returncode == 0, run.stderr
    manifests = json.loads(run.stdout)["Manifests"]
    steps = {}
    for m in manifests:
        specs = [t["LiveUpdateSpec"] for t in m["ImageTargets"] or []]
        if specs:
            steps[m["Name"]] = [k for k in ("syncs", "execs") if any(s.get(k) for s in specs)]
    return steps


def test_only_the_dev_profile_live_updates_a_module_that_opts_in():
    assert live_update_steps("dev") == {"pyservice": ["syncs", "execs"]}
    for profile in set(PROFILES) - {"dev", "minimal"}:
        assert live_update_steps(profile) == {"pyservice": []}


def test_soak_enables_the_ci_modules_with_no_live_update_step():
    assert set(selection("soak")[0]) == set(selection("ci")[0])
    assert live_update_steps("soak") == {"pyservice": []}
