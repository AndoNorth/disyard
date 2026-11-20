"""Render tests of the dispatch module through `tilt alpha tiltfile-result`, run by `just test`.

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
from wft_dispatchapi.service import ENV, LISTEN, LISTEN_ENV
from wft_storecheck.s3 import Creds, dump_credentials

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INPUTS = [
    "deploy/versions.yaml",
    "deploy/host/ports.yaml",
    "deploy/rabbitmq/config.py",
    "contracts/topology/topology.yaml",
    "contracts/tenancy/tenancy.yaml",
    "scripts/storage/store/identities.sh",
]
DEPENDS = ["appdb-migrate", "objectstore-bootstrap", "temporal-bootstrap", "rabbitmq"]
ENV_SECRETS = ["dispatch-logins", "dispatch-broker"]
STORE_ROLES = ("admin", "submit", "workflow", "indexer", "reaper", "compile")
GROUPS = {
    "WFT_DISPATCH_DSN": "wft_dispatch",
    "WFT_CONTROL_DSN": "wft_control",
    "WFT_ARTIFACTS_DSN": "wft_artifacts",
    "WFT_READER_DSN": "wft_reader",
}
CREDENTIALS = "/etc/wft/store/credentials.json"

_spec = importlib.util.spec_from_file_location(
    "rabbitmq_config", ROOT / "deploy/rabbitmq/config.py"
)
rabbitmq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rabbitmq)

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


def tiltfile_result(root: Path, tilt_profile: str | None):
    shutil.copytree(
        HERE, root / "deploy/dispatchapi", ignore=shutil.ignore_patterns("test_*", "__pycache__")
    )
    for f in INPUTS:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, root / f)
    (root / "packages").mkdir()
    (root / "uv.lock").touch()
    state = root / "state"
    state.mkdir()
    dump_credentials(
        state / "objectstore.credentials.json",
        {role: Creds(f"{role}-id", f"{role}-key") for role in STORE_ROLES},
    )
    (root / "Tiltfile").write_text(
        "".join(f"local_resource('{d}', cmd='true')\n" for d in DEPENDS)
        + "load_dynamic('deploy/dispatchapi/Tiltfile.module')\n"
        + f"config.set_enabled_resources({DEPENDS + ['dispatch']})\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "WFT_TILT_PROFILE"}
    env |= {
        "KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wft.yaml"),
        "WFT_STATE_DIR": str(state),
        # logins.py and config.py run in the environment of the tree, which the copy does not hold.
        "UV_PROJECT": str(ROOT),
    }
    if tilt_profile is not None:
        env["WFT_TILT_PROFILE"] = tilt_profile
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


def pod_of(objects) -> dict:
    return objects["Deployment", "dispatch"]["spec"]["template"]["spec"]


def container_of(objects) -> dict:
    [container] = pod_of(objects)["containers"]
    return container


def env_of(objects) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container_of(objects)["env"]}


def test_one_replica_recreated_on_the_infra_pool(rendered):
    _, objects, _ = rendered
    deployment = objects["Deployment", "dispatch"]
    assert deployment["metadata"]["namespace"] == "wft-control"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod = pod_of(objects)
    assert pod["nodeSelector"] == {"wft.dev/pool": "infra"}
    assert pod["terminationGracePeriodSeconds"] == 30


def test_resources_of_the_module(rendered):
    _, objects, _ = rendered
    resources = {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
    }
    assert container_of(objects)["resources"] == resources
    module = yaml.safe_load((HERE / "module.yaml").read_text())
    assert module["resources"]["dispatch"] == resources


def test_two_node_ports_of_the_host_map(rendered):
    _, objects, _ = rendered
    host = yaml.safe_load((ROOT / "deploy/host/ports.yaml").read_text())["ports"]
    node_ports = {row["name"]: row["node_port"] for row in host}
    assert node_ports == {"control-api": 30080, "dispatch-api": 30081}
    ports = {p["name"]: p["containerPort"] for p in container_of(objects)["ports"]}
    for listener, node_port in node_ports.items():
        service = objects["Service", listener]
        assert service["metadata"]["namespace"] == "wft-control"
        assert service["spec"]["type"] == "NodePort"
        assert service["spec"]["selector"] == {"app.kubernetes.io/name": "dispatch"}
        [port] = service["spec"]["ports"]
        assert (port["nodePort"], port["targetPort"]) == (node_port, listener)
        # The container port is the one the service binds when its listen variable is unset.
        assert ports[listener] == port["port"] == int(LISTEN[listener].rsplit(":", 1)[1])
        assert LISTEN_ENV[listener] not in env_of(objects)
    assert ports["metrics"] == int(env_of(objects)["WFT_METRICS_ADDRESS"].rsplit(":", 1)[1])
    # The metrics port has no Service and so no NodePort.
    assert {name for kind, name in objects if kind == "Service"} == set(node_ports)


def test_readiness_asks_the_service(rendered):
    _, objects, _ = rendered
    probe = container_of(objects)["readinessProbe"]
    assert probe["httpGet"] == {"path": "/readyz", "port": "dispatch-api"}
    assert probe["timeoutSeconds"] > 5


def test_read_only_root_with_a_disk_backed_tmp(rendered):
    _, objects, _ = rendered
    container = container_of(objects)
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    volumes = {v["name"]: v for v in pod_of(objects)["volumes"]}
    assert volumes["tmp"]["emptyDir"] == {"sizeLimit": "512Mi"}
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts == {"tmp": "/tmp", "store": str(Path(CREDENTIALS).parent)}


def test_service_configuration_from_env_and_secrets(rendered):
    _, objects, _ = rendered
    env = env_of(objects)
    assert env["WFT_TEMPORAL_ADDRESS"] == "temporal-frontend.wft-infra.svc.cluster.local:7233"
    assert env["WFT_STORE_ENDPOINT"] == "http://objectstore.wft-infra.svc.cluster.local:8333"
    assert env["WFT_SUBMIT_CREDENTIALS"] == CREDENTIALS
    assert env["WFT_BROKER_ADDRESS"] == "rabbitmq.wft-infra.svc.cluster.local:5672"
    assert env["WFT_TENANCY_PROFILE"] == "ci"
    assert container_of(objects)["envFrom"] == [{"secretRef": {"name": s}} for s in ENV_SECRETS]
    from_secrets = {key for s in ENV_SECRETS for key in objects["Secret", s]["stringData"]}
    assert from_secrets == {*GROUPS, "WFT_BROKER_PASSWORD"}
    assert not from_secrets & set(env)
    assert set(ENV.values()) == set(env) | from_secrets


def test_four_logins_each_in_its_group(rendered):
    _, objects, root = rendered
    dsns = objects["Secret", "dispatch-logins"]["stringData"]
    known = json.loads((root / "state/dispatch.logins.json").read_text())
    assert len(known) == len(GROUPS)
    for env, dsn in dsns.items():
        login = dsn.removeprefix("postgresql://").split(":", 1)[0]
        assert dsn == (
            f"postgresql://{login}:{known[login]}@appdb.wft-infra.svc.cluster.local:5432/postgres"
        )
        assert login == "dispatch_" + GROUPS[env].removeprefix("wft_")


def test_store_secret_holds_the_submit_identity_only(rendered):
    _, objects, root = rendered
    held = json.loads(objects["Secret", "dispatch-store"]["stringData"]["credentials.json"])
    every = json.loads((root / "state/objectstore.credentials.json").read_text())
    assert held == {"submit": every["submit"]}


def test_broker_secret_from_the_broker_helper(rendered, monkeypatch):
    _, objects, root = rendered
    monkeypatch.setenv("WFT_STATE_DIR", str(root / "state"))
    assert objects["Secret", "dispatch-broker"]["stringData"] == {
        "WFT_BROKER_PASSWORD": rabbitmq.password("dispatch")
    }


def test_no_credential_in_any_manifest_but_the_secrets(rendered):
    _, objects, root = rendered
    known = json.loads((root / "state/dispatch.logins.json").read_text())
    store = json.loads((root / "state/objectstore.credentials.json").read_text())
    secrets = [
        *known.values(),
        store["submit"]["secret_key"],
        objects["Secret", "dispatch-broker"]["stringData"]["WFT_BROKER_PASSWORD"],
    ]
    for (kind, name), manifest in objects.items():
        if kind == "Secret":
            continue
        text = json.dumps(manifest)
        assert not [s for s in secrets if s in text], f"{kind}/{name} holds a credential"


def test_tenancy_profile_mapped_from_the_tilt_profile(tmp_path):
    _, objects = rendered_objects(tiltfile_result(tmp_path, "full"))
    assert env_of(objects)["WFT_TENANCY_PROFILE"] == "dev"


def test_waits_for_logins_and_its_dependencies(rendered):
    manifests, _, _ = rendered
    assert sorted(manifests["dispatch"]["ResourceDependencies"]) == sorted(
        DEPENDS + ["dispatch-logins"]
    )
    assert manifests["dispatch-logins"]["ResourceDependencies"] == ["appdb-migrate"]
    [image] = manifests["dispatch"]["ImageTargets"]
    assert image["BuildDetails"]["args"][-1].endswith("deploy/dispatchapi/image.sh")


def test_no_tilt_profile_refused(tmp_path):
    run = tiltfile_result(tmp_path, None)
    assert run.returncode != 0
    assert "WFT_TILT_PROFILE is unset" in run.stderr


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
    assert runtime.rstrip().endswith('ENTRYPOINT ["python", "-m", "wft_dispatchapi.service"]')


def test_drops_every_capability_under_the_runtime_seccomp_profile(rendered):
    _, objects, _ = rendered
    pod = pod_of(objects)
    [container] = pod["containers"]
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
