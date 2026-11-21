"""Render tests of the placement module through `tilt alpha tiltfile-result`, run by `just test`.

No cluster and no image build are needed.
A wrapper Tiltfile enables only the module's own resource as the root Tiltfile does, and loads the
module from a copy of the files it reads.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from wft_placement.service import ENV

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INPUTS = ["deploy/versions.yaml", "contracts/topology/topology.yaml"]
ROLE = "wft-placement-read"

pytestmark = pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    root = tmp_path_factory.mktemp("render")
    shutil.copytree(
        HERE, root / "deploy/placement", ignore=shutil.ignore_patterns("test_*", "__pycache__")
    )
    for f in INPUTS:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, root / f)
    (root / "packages").mkdir()
    (root / "uv.lock").touch()
    (root / "Tiltfile").write_text(
        "load_dynamic('deploy/placement/Tiltfile.module')\n"
        "config.set_enabled_resources(['placement'])\n"
    )
    env = os.environ | {"KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wft.yaml")}
    run = subprocess.run(
        ["tilt", "alpha", "tiltfile-result", "-f", str(root / "Tiltfile")],
        cwd=root,
        env=env,
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


def pod_of(objects) -> dict:
    return objects["Deployment", "placement"]["spec"]["template"]["spec"]


def container_of(objects) -> dict:
    [container] = pod_of(objects)["containers"]
    return container


def env_of(objects) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container_of(objects)["env"]}


def test_one_replica_recreated_on_the_infra_pool(rendered):
    _, objects = rendered
    deployment = objects["Deployment", "placement"]
    assert deployment["metadata"]["namespace"] == "wft-control"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert deployment["spec"]["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/name": "placement"
    }
    assert pod_of(objects)["nodeSelector"] == {"wft.dev/pool": "infra"}


def test_resources_of_the_module(rendered):
    _, objects = rendered
    resources = {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "250m", "memory": "128Mi"},
    }
    assert container_of(objects)["resources"] == resources
    module = yaml.safe_load((HERE / "module.yaml").read_text())
    assert module["resources"] == {"placement": resources}


def test_ready_probe_tests_the_ready_file(rendered):
    _, objects = rendered
    probe = container_of(objects)["readinessProbe"]["exec"]["command"]
    assert probe == ["test", "-e", env_of(objects)[ENV["ready_file"]]]


def test_service_configuration_from_env(rendered):
    _, objects = rendered
    env = env_of(objects)
    assert env == {ENV["listen"]: "0.0.0.0:8080", ENV["ready_file"]: "/tmp/wft-ready"}
    assert "envFrom" not in container_of(objects)


def test_cluster_ip_service_on_8080_and_no_node_port(rendered):
    _, objects = rendered
    service = objects["Service", "placement"]
    assert service["metadata"]["namespace"] == "wft-control"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"app.kubernetes.io/name": "placement"}
    [port] = service["spec"]["ports"]
    assert port == {"name": "http", "port": 8080, "targetPort": "http"}
    assert container_of(objects)["ports"] == [{"name": "http", "containerPort": 8080}]
    assert not any(
        o["kind"] == "Service" and o["spec"].get("type") == "NodePort" for o in objects.values()
    )
    assert not pod_of(objects).get("hostNetwork")


def test_pod_mounts_the_token_of_its_own_service_account(rendered):
    _, objects = rendered
    pod = pod_of(objects)
    assert pod["serviceAccountName"] == "placement"
    assert pod["automountServiceAccountToken"] is True
    account = objects["ServiceAccount", "placement"]
    assert account["metadata"]["namespace"] == "wft-control"


def test_cluster_role_only_gets_and_lists_nodes_and_pods(rendered):
    _, objects = rendered
    assert objects["ClusterRole", ROLE]["rules"] == [
        {"apiGroups": [""], "resources": ["nodes", "pods"], "verbs": ["get", "list"]}
    ]
    binding = objects["ClusterRoleBinding", ROLE]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": ROLE,
    }
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "placement", "namespace": "wft-control"}
    ]
    assert {kind for kind, _ in objects} == {
        "ServiceAccount",
        "ClusterRole",
        "ClusterRoleBinding",
        "Service",
        "Deployment",
    }


def test_only_the_placement_pod_mounts_a_token():
    """Every other service image of the tree runs in a pod that mounts no service account token."""
    services = [p.parent for p in ROOT.glob("deploy/*/Dockerfile") if p.parent != HERE]
    assert {p.name for p in services} >= {"admission", "workflow", "workers"}
    for directory in services:
        sources = [*directory.rglob("Tiltfile.module"), *directory.rglob("*.star")]
        text = "\n".join(s.read_text() for s in sources)
        assert '"containers"' in text, directory
        assert '"automountServiceAccountToken": False' in text, directory
        assert "serviceAccountName" not in text, directory


def test_one_resource_with_its_rbac_and_the_image_of_image_sh(rendered):
    manifests, _ = rendered
    assert list(manifests) == ["placement"]
    [image] = manifests["placement"]["ImageTargets"]
    assert image["BuildDetails"]["args"][-1].endswith("deploy/placement/image.sh")
    assert "env" not in image["BuildDetails"], "the service is unversioned and takes no build id"


def test_dockerfile_runs_the_service_as_non_root():
    dockerfile = (HERE / "Dockerfile").read_text()
    assert "USER 65532:65532" in dockerfile
    assert dockerfile.rstrip().endswith('ENTRYPOINT ["python", "-m", "wft_placement"]')


def test_drops_every_capability_under_the_runtime_seccomp_profile(rendered):
    _, objects = rendered
    pod = pod_of(objects)
    [container] = pod["containers"]
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
