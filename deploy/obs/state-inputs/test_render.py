"""Render tests of the state-inputs module through `tilt alpha tiltfile-result`, run by `just test`.

No cluster and no image build are needed.
A wrapper Tiltfile stands in for the module it depends on, exports the Tilt profile and the
observability switch, enables only the module's own resources as the root Tiltfile does, and loads
the module from a copy of the files it reads, with a state directory of its own.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from wft_obs.inputs.__main__ import ADDRESS_ENV, DSN_ENV, READY_ENV

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
INPUTS = [
    "deploy/versions.yaml",
    "contracts/topology/topology.yaml",
    "deploy/tilt/obs.star",
]
DEPENDS = ["appdb-migrate"]

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


def tiltfile_result(root: Path, obs: str | None):
    shutil.copytree(
        HERE,
        root / "deploy/obs/state-inputs",
        ignore=shutil.ignore_patterns("test_*", "__pycache__"),
    )
    for f in INPUTS:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, root / f)
    (root / "packages").mkdir()
    (root / "uv.lock").touch()
    state = root / "state"
    state.mkdir()
    (root / "Tiltfile").write_text(
        "".join(f"local_resource('{d}', cmd='true')\n" for d in DEPENDS)
        + "load_dynamic('deploy/obs/state-inputs/Tiltfile.module')\n"
        + f"config.set_enabled_resources({DEPENDS + ['state-inputs']})\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "WFT_OBS"}
    env |= {
        "KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wft.yaml"),
        "WFT_STATE_DIR": str(state),
        # logins.py runs in the environment of the tree, which the copy does not hold.
        "UV_PROJECT": str(ROOT),
    }
    if obs is not None:
        env["WFT_OBS"] = obs
    return subprocess.run(
        ["tilt", "alpha", "tiltfile-result", "-f", str(root / "Tiltfile")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def rendered_objects(run) -> tuple[dict, dict]:
    assert run.returncode == 0, run.stderr
    manifests = {m["Name"]: m for m in json.loads(run.stdout)["Manifests"]}
    objects = {
        (o["kind"], o["metadata"]["name"]): o
        for m in manifests.values()
        for o in yaml.safe_load_all(m["DeployTarget"].get("yaml", ""))
        if o
    }
    return manifests, objects


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    return rendered_objects(tiltfile_result(tmp_path_factory.mktemp("render"), "on"))


def container_of(objects) -> dict:
    [container] = objects["Deployment", "state-inputs"]["spec"]["template"]["spec"]["containers"]
    return container


def env_of(objects) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container_of(objects)["env"]}


def test_one_replica_recreated_on_the_obs_pool(rendered):
    _, objects = rendered
    deployment = objects["Deployment", "state-inputs"]
    assert deployment["metadata"]["namespace"] == "wft-obs"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"wft.dev/pool": "obs"}
    assert pod["tolerations"][0]["key"] == "wft.dev/pool"


def test_resources_of_the_module_with_requests_equal_to_limits(rendered):
    _, objects = rendered
    resources = container_of(objects)["resources"]
    assert resources["requests"] == resources["limits"]
    module = yaml.safe_load((HERE / "module.yaml").read_text())
    assert module["resources"]["state-inputs"] == resources


def test_metrics_port_is_the_one_prometheus_scrapes(rendered):
    _, objects = rendered
    assert container_of(objects)["ports"] == [{"name": "metrics", "containerPort": 9464}]
    assert env_of(objects)[ADDRESS_ENV] == "0.0.0.0:9464"


def test_ready_probe_tests_the_ready_file(rendered):
    _, objects = rendered
    probe = container_of(objects)["readinessProbe"]["exec"]["command"]
    assert probe == ["test", "-e", env_of(objects)[READY_ENV]]


def test_the_dsn_comes_from_the_login_secret_only(rendered):
    _, objects = rendered
    assert container_of(objects)["envFrom"] == [{"secretRef": {"name": "state-inputs-logins"}}]
    assert set(objects["Secret", "state-inputs-logins"]["stringData"]) == {DSN_ENV}
    assert DSN_ENV not in env_of(objects)


def test_telemetry_environment_follows_the_switch(rendered, tmp_path):
    _, objects = rendered
    env = env_of(objects)
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.wft-obs:4318"
    assert env["WFT_PYROSCOPE_ADDRESS"] == "http://pyroscope.wft-obs:4040"
    _, off = rendered_objects(tiltfile_result(tmp_path, "off"))
    assert not {"OTEL_EXPORTER_OTLP_ENDPOINT", "WFT_PYROSCOPE_ADDRESS"} & set(env_of(off))


def test_waits_for_its_login_and_the_migrated_database(rendered):
    manifests, _ = rendered
    assert sorted(manifests["state-inputs"]["ResourceDependencies"]) == sorted(
        DEPENDS + ["state-inputs-logins"]
    )
    assert manifests["state-inputs-logins"]["ResourceDependencies"] == ["appdb-migrate"]
    [image] = manifests["state-inputs"]["ImageTargets"]
    assert image["BuildDetails"]["args"][-1].endswith("deploy/obs/state-inputs/image.sh")


def test_drops_every_capability_under_the_runtime_seccomp_profile(rendered):
    _, objects = rendered
    pod = objects["Deployment", "state-inputs"]["spec"]["template"]["spec"]
    [container] = pod["containers"]
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_dockerfile_installs_hash_pinned_packages_and_runs_the_exporter():
    dockerfile = (HERE / "Dockerfile").read_text()
    assert "--require-hashes" in dockerfile
    assert dockerfile.rstrip().endswith('ENTRYPOINT ["python", "-m", "wft_obs.inputs"]')


def test_the_module_installs_the_stateops_extra():
    assert "--extra stateops" in (HERE / "image.sh").read_text()
