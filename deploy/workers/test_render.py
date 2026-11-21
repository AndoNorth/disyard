"""Render tests of the three pool worker modules through `tilt alpha tiltfile-result`, run by
`just test`.

No cluster and no image build are needed.
A wrapper Tiltfile stands in for the modules they depend on, enables only their own resources as
the root Tiltfile does, and loads them from a copy of the files they read, with a state directory
of its own.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from wft_storecheck.s3 import Creds, dump_credentials
from wft_worker.config import ENV, MIRROR_ENV

ROOT = Path(__file__).resolve().parents[2]
POOLS = ("compile", "simulate", "aggregate")
INPUTS = [
    "deploy/versions.yaml",
    "contracts/topology/topology.yaml",
    "contracts/temporal/naming.yaml",
    "scripts/storage/store/identities.sh",
    "nix/wasm-tools.nix",
    "deploy/tilt/obs.star",
]
DEPENDS = ["temporal-bootstrap", "objectstore-bootstrap", "mirror"]
ROLES = ("admin", "workflow", "compile", "simulate", "aggregate")
SCRATCH_ENV = {
    "WFT_SCRATCH": "/var/wft",
    "WFT_READY_FILE": "/var/wft/ready",
    "HOME": "/var/wft/home",
    "TMPDIR": "/var/wft/tmp",
    "XDG_CACHE_HOME": "/var/wft/cache",
    "UV_CACHE_DIR": "/var/wft/cache/uv",
}

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


def render(tmp_path_factory, obs: str):
    """The manifests and objects of the three pool modules with the observability switch `obs`."""
    root = tmp_path_factory.mktemp(f"render-{obs}")
    shutil.copytree(
        ROOT / "deploy/workers",
        root / "deploy/workers",
        ignore=shutil.ignore_patterns("*.py", "__pycache__"),
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
        {role: Creds(f"{role}-id", f"{role}-key") for role in ROLES},
    )
    (root / "Tiltfile").write_text(
        "".join(f"local_resource('{d}', cmd='true')\n" for d in DEPENDS)
        + "".join(f"load_dynamic('deploy/workers/{p}/Tiltfile.module')\n" for p in POOLS)
        + f"config.set_enabled_resources({DEPENDS + [f'{p}-worker' for p in POOLS]})\n"
    )
    run = subprocess.run(
        ["tilt", "alpha", "tiltfile-result", "-f", str(root / "Tiltfile")],
        cwd=root,
        env=os.environ
        | {
            "KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wft.yaml"),
            "WFT_STATE_DIR": str(state),
            "WFT_OBS": obs,
        },
        capture_output=True,
        text=True,
    )
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
    return render(tmp_path_factory, "off")


@pytest.fixture(scope="module")
def rendered_on(tmp_path_factory):
    return render(tmp_path_factory, "on")


def pod(objects, pool: str) -> dict:
    return objects["Deployment", f"{pool}-worker"]["spec"]["template"]["spec"]


@pytest.mark.parametrize("pool", POOLS)
def test_one_slot_pod_on_the_pool_node(rendered, pool):
    _, objects = rendered
    deployment = objects["Deployment", f"{pool}-worker"]
    assert deployment["metadata"]["namespace"] == "wft-workers"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    spec = pod(objects, pool)
    assert spec["nodeSelector"] == {"wft.dev/pool": pool}
    assert spec["tolerations"] == [
        {"key": "wft.dev/pool", "operator": "Equal", "value": pool, "effect": "NoSchedule"}
    ]
    slot = yaml.safe_load((ROOT / "deploy/presets/slots/ci.yaml").read_text())["pools"][pool]
    module = yaml.safe_load((ROOT / f"deploy/workers/{pool}/module.yaml").read_text())
    [container] = spec["containers"]
    assert container["resources"] == slot == module["resources"][f"{pool}-worker"]
    assert slot["requests"] == slot["limits"]


@pytest.mark.parametrize("pool", POOLS)
def test_secret_holds_the_pool_identity_only(rendered, pool):
    _, objects = rendered
    secret = objects["Secret", f"{pool}-worker-store"]
    held = json.loads(secret["stringData"]["credentials.json"])
    assert held == {pool: Creds(f"{pool}-id", f"{pool}-key")._asdict()}
    volumes = {v["name"]: v for v in pod(objects, pool)["volumes"]}
    assert volumes["store"]["secret"] == {"secretName": f"{pool}-worker-store"}


@pytest.mark.parametrize("pool", POOLS)
def test_environment_has_no_database_prefix_or_build(rendered, pool):
    _, objects = rendered
    [container] = pod(objects, pool)["containers"]
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
    assert "envFrom" not in container
    assert {*ENV.values(), *([MIRROR_ENV] if pool == "compile" else [])} <= set(env)
    assert not [n for n in env if n.startswith("WFT_") and n.endswith("_DSN")]
    assert "WFT_POOL_QUEUE_PREFIX" not in env
    assert not [n for n in env if "BUILD" in n or "DEPLOYMENT" in n]
    assert env["WFT_NAMESPACE"] == "wft"
    assert env["WFT_TEMPORAL_ADDRESS"] == "temporal-frontend.wft-infra.svc.cluster.local:7233"
    assert env["WFT_STORE_ENDPOINT"] == "http://objectstore.wft-infra.svc.cluster.local:8333"
    assert env["WFT_STORE_CREDENTIALS"] == "/etc/wft/store/credentials.json"
    assert env["WFT_SLOT_MEMORY_MIB"] == {
        "resourceFieldRef": {"resource": "limits.memory", "divisor": "1Mi"}
    }
    assert ("WFT_MIRROR" in env) == (pool == "compile")
    assert {k: env[k] for k in SCRATCH_ENV} == SCRATCH_ENV


@pytest.mark.parametrize("pool", POOLS)
def test_disk_scratch_volume_with_its_directories(rendered, pool):
    _, objects = rendered
    spec = pod(objects, pool)
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["scratch"]["emptyDir"] == {"sizeLimit": "1Gi"}
    [init] = spec["initContainers"]
    [container] = spec["containers"]
    for c in (init, container):
        assert {"name": "scratch", "mountPath": "/var/wft"} in c["volumeMounts"]
    assert init["image"] == container["image"] == f"wft-{pool}-worker"
    assert init["command"][:2] == ["mkdir", "-p"]
    assert set(init["command"][2:]) == {
        SCRATCH_ENV[k] for k in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "UV_CACHE_DIR")
    }


@pytest.mark.parametrize("pool", POOLS)
def test_pod_settings_and_readiness(rendered, pool):
    _, objects = rendered
    spec = pod(objects, pool)
    assert spec["automountServiceAccountToken"] is False
    assert spec["terminationGracePeriodSeconds"] == 75
    assert spec["securityContext"]["runAsUser"] == 65532
    assert spec["securityContext"]["runAsNonRoot"] is True
    for c in spec["containers"] + spec["initContainers"]:
        assert c["securityContext"]["readOnlyRootFilesystem"] is True
        assert c["securityContext"]["allowPrivilegeEscalation"] is False
    [container] = spec["containers"]
    assert container["readinessProbe"]["exec"]["command"] == ["test", "-e", "/var/wft/ready"]
    template = objects["Deployment", f"{pool}-worker"]["spec"]["template"]["metadata"]
    assert template["annotations"]["kubectl.kubernetes.io/default-container"] == container["name"]


@pytest.mark.parametrize("pool", POOLS)
def test_egress_only_to_the_platform_endpoints(rendered, pool):
    _, objects = rendered
    policy = objects["NetworkPolicy", f"{pool}-worker"]["spec"]
    assert policy["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": f"{pool}-worker"}}
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in policy
    reached = {
        (
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            json.dumps(peer["podSelector"]["matchLabels"], sort_keys=True),
            port["protocol"],
            port["port"],
        )
        for rule in policy["egress"]
        for peer in rule["to"]
        for port in rule["ports"]
    }
    temporal = {"app.kubernetes.io/component": "frontend", "app.kubernetes.io/name": "temporal"}
    expected = {
        ("kube-system", json.dumps({"k8s-app": "kube-dns"}), "UDP", 53),
        ("kube-system", json.dumps({"k8s-app": "kube-dns"}), "TCP", 53),
        ("wft-infra", json.dumps(temporal, sort_keys=True), "TCP", 7233),
        ("wft-infra", json.dumps({"app": "objectstore"}), "TCP", 8333),
    }
    if pool == "compile":
        expected.add(("wft-infra", json.dumps({"app": "mirror"}), "TCP", 8080))
    assert reached == expected


@pytest.mark.parametrize("pool", POOLS)
def test_waits_for_its_dependencies_and_carries_its_objects(rendered, pool):
    manifests, _ = rendered
    manifest = manifests[f"{pool}-worker"]
    depends = DEPENDS if pool == "compile" else DEPENDS[:2]
    assert sorted(manifest["ResourceDependencies"]) == sorted(depends)
    [image] = manifest["ImageTargets"]
    *_, script, built = image["BuildDetails"]["args"]
    assert script.endswith("deploy/workers/image.sh")
    assert built == pool


OBS_ENV = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.wft-obs:4318",
    "WFT_PYROSCOPE_ADDRESS": "http://pyroscope.wft-obs:4040",
}


def peers(rules: list, key: str) -> set:
    return {
        (
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            json.dumps(peer["podSelector"]["matchLabels"], sort_keys=True),
            port["port"],
        )
        for rule in rules
        for peer in rule[key]
        for port in rule["ports"]
    }


def env_of(objects, pool: str) -> dict:
    [container] = pod(objects, pool)["containers"]
    return {e["name"]: e.get("value") for e in container["env"]}


@pytest.mark.parametrize("pool", POOLS)
def test_metrics_port_is_named_and_the_address_is_set_either_way(rendered, rendered_on, pool):
    for _, objects in (rendered, rendered_on):
        [container] = pod(objects, pool)["containers"]
        assert container["ports"] == [{"name": "metrics", "containerPort": 9464}]
        assert env_of(objects, pool)["WFT_METRICS_ADDRESS"] == "0.0.0.0:9464"


@pytest.mark.parametrize("pool", POOLS)
def test_switch_off_has_no_obs_environment_and_the_policy_is_as_before(rendered, pool):
    _, objects = rendered
    env = env_of(objects, pool)
    assert not set(OBS_ENV) & set(env)
    policy = objects["NetworkPolicy", f"{pool}-worker"]["spec"]
    assert "ingress" not in policy
    assert not [p for p in peers(policy["egress"], "to") if p[0] == "wft-obs"]


@pytest.mark.parametrize("pool", POOLS)
def test_switch_on_adds_the_obs_environment_and_policy_rules(rendered_on, pool):
    _, objects = rendered_on
    env = env_of(objects, pool)
    assert {k: env[k] for k in OBS_ENV} == OBS_ENV
    policy = objects["NetworkPolicy", f"{pool}-worker"]["spec"]
    assert peers(policy["ingress"], "from") == {
        ("wft-obs", json.dumps({"app.kubernetes.io/name": "prometheus"}), 9464)
    }
    obs = {p for p in peers(policy["egress"], "to") if p[0] == "wft-obs"}
    assert obs == {
        ("wft-obs", json.dumps({"app.kubernetes.io/name": "collector"}), 4318),
        ("wft-obs", json.dumps({"app.kubernetes.io/name": "pyroscope"}), 4040),
    }
