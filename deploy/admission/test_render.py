"""Render tests of the admission module through `tilt alpha tiltfile-result`, run by `just test`.

No cluster and no image build are needed.
A wrapper Tiltfile stands in for the modules it depends on, exports the Tilt profile and enables
only the module's own resources as the root Tiltfile does, and loads the module from a copy of the
files it reads, with a state directory of its own.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from wdf_admission.service import ENV, GLOBAL_CAP_ENV, PREFIX_ENV, READY_ENV

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INPUTS = [
    "deploy/versions.yaml",
    "deploy/rabbitmq/config.py",
    "contracts/topology/topology.yaml",
    "contracts/temporal/naming.yaml",
    "contracts/tenancy/tenancy.yaml",
]
DEPENDS = ["appdb-migrate", "temporal-bootstrap", "rabbitmq"]
SECRETS = ["admission-logins", "admission-broker"]

_spec = importlib.util.spec_from_file_location(
    "rabbitmq_config", ROOT / "deploy/rabbitmq/config.py"
)
rabbitmq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rabbitmq)

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


def tiltfile_result(root: Path, tilt_profile: str | None):
    shutil.copytree(
        HERE, root / "deploy/admission", ignore=shutil.ignore_patterns("test_*", "__pycache__")
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
        + "load_dynamic('deploy/admission/Tiltfile.module')\n"
        + f"config.set_enabled_resources({DEPENDS + ['admission']})\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "WDF_TILT_PROFILE"}
    env |= {
        "KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wdf.yaml"),
        "WDF_STATE_DIR": str(state),
        # logins.py and config.py run in the environment of the tree, which the copy does not hold.
        "UV_PROJECT": str(ROOT),
    }
    if tilt_profile is not None:
        env["WDF_TILT_PROFILE"] = tilt_profile
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
    root = tmp_path_factory.mktemp("render")
    return (*rendered_objects(tiltfile_result(root, "ci")), root)


def container_of(objects) -> dict:
    [container] = objects["Deployment", "admission"]["spec"]["template"]["spec"]["containers"]
    return container


def env_of(objects) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container_of(objects)["env"]}


def test_one_replica_recreated_on_the_infra_pool(rendered):
    _, objects, _ = rendered
    deployment = objects["Deployment", "admission"]
    assert deployment["metadata"]["namespace"] == "wdf-control"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"wdf.dev/pool": "infra"}
    assert pod["terminationGracePeriodSeconds"] == 30


def test_resources_of_the_module(rendered):
    _, objects, _ = rendered
    resources = {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "250m", "memory": "128Mi"},
    }
    assert container_of(objects)["resources"] == resources
    module = yaml.safe_load((HERE / "module.yaml").read_text())
    assert module["resources"]["admission"] == resources


def test_ready_probe_tests_the_ready_file(rendered):
    _, objects, _ = rendered
    probe = container_of(objects)["readinessProbe"]["exec"]["command"]
    assert probe == ["test", "-e", env_of(objects)[READY_ENV]]


def test_service_configuration_from_env_and_secrets(rendered):
    _, objects, _ = rendered
    env = env_of(objects)
    assert env["WDF_TEMPORAL_ADDRESS"] == "temporal-frontend.wdf-infra.svc.cluster.local:7233"
    assert env["WDF_TEMPORAL_NAMESPACE"] == "wdf"
    assert env["WDF_BROKER_ADDRESS"] == "rabbitmq.wdf-infra.svc.cluster.local:5672"
    assert env["WDF_TENANCY_PROFILE"] == "ci"
    assert GLOBAL_CAP_ENV not in env
    assert PREFIX_ENV not in env
    assert container_of(objects)["envFrom"] == [{"secretRef": {"name": s}} for s in SECRETS]
    from_secrets = {key for s in SECRETS for key in objects["Secret", s]["stringData"]}
    assert from_secrets == {"WDF_ADMISSION_DSN", "WDF_BROKER_PASSWORD"}
    assert not from_secrets & set(env)
    assert set(ENV.values()) <= set(env) | from_secrets


def test_broker_secret_from_the_broker_helper(rendered, monkeypatch):
    _, objects, root = rendered
    monkeypatch.setenv("WDF_STATE_DIR", str(root / "state"))
    assert objects["Secret", "admission-broker"]["stringData"] == {
        "WDF_BROKER_PASSWORD": rabbitmq.password("admission")
    }


def test_tenancy_profile_mapped_from_the_tilt_profile(tmp_path):
    _, objects = rendered_objects(tiltfile_result(tmp_path, "full"))
    assert env_of(objects)["WDF_TENANCY_PROFILE"] == "dev"


def test_waits_for_logins_and_its_dependencies(rendered):
    manifests, _, _ = rendered
    assert sorted(manifests["admission"]["ResourceDependencies"]) == sorted(
        DEPENDS + ["admission-logins"]
    )
    assert manifests["admission-logins"]["ResourceDependencies"] == ["appdb-migrate"]
    [image] = manifests["admission"]["ImageTargets"]
    assert image["BuildDetails"]["args"][-1].endswith("deploy/admission/image.sh")


def test_no_tilt_profile_refused(tmp_path):
    run = tiltfile_result(tmp_path, None)
    assert run.returncode != 0
    assert "WDF_TILT_PROFILE is unset" in run.stderr


def test_dockerfile_builds_proton_in_a_build_stage():
    stages = (HERE / "Dockerfile").read_text().split("\nFROM ")[1:]
    assert [s.split("\n")[0] for s in stages] == ["${BASE} AS proton", "${BASE}"]
    build, runtime = stages
    assert "apt-get install -y --no-install-recommends gcc libc6-dev" in build
    wheel = build.split("pip wheel")[1]
    for option in ("--no-binary python-qpid-proton", "--require-hashes", "--no-index"):
        assert option in wheel
    assert "gcc" not in runtime and "apt-get" not in runtime
    assert "COPY --from=proton /wheels" in runtime
    assert "--require-hashes" in runtime
    assert runtime.rstrip().endswith('ENTRYPOINT ["python", "-m", "wdf_admission.service"]')
