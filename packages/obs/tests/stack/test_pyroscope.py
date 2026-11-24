"""The manifests and arguments of the Pyroscope module.

No cluster is needed: the module's config.py is loaded by path.
"""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "deploy/obs/pyroscope"


def load():
    spec = importlib.util.spec_from_file_location("pyroscope_config", MODULE / "config.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config


pyroscope = load()


def objects() -> dict:
    return {(o["kind"], o["metadata"]["name"]): o for o in pyroscope.render()}


def deployment() -> dict:
    return objects()[("Deployment", "pyroscope")]


def container() -> dict:
    [container] = deployment()["spec"]["template"]["spec"]["containers"]
    return container


def test_deployment_recreates_its_single_pod():
    spec = deployment()["spec"]
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["replicas"] == 1


def test_pod_runs_on_the_obs_pool_inside_the_resources_of_its_manifest():
    pod = deployment()["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"dsy.dev/pool": "obs"}
    assert pod["tolerations"][0]["key"] == "dsy.dev/pool"
    module = yaml.safe_load((MODULE / "module.yaml").read_text())
    resources = container()["resources"]
    assert resources == module["resources"]["pyroscope"]
    assert resources["requests"]["memory"] == resources["limits"]["memory"] == "256Mi"
    pins = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    assert container()["image"].endswith("@" + pins[module["artifact"]["pin"]]["digest"])


def test_pod_selected_by_the_label_the_worker_pools_allow():
    labels = deployment()["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "pyroscope"


def test_pod_is_scraped_through_a_port_named_metrics():
    assert {"name": "metrics", "containerPort": 4040} in container()["ports"]


def test_every_object_is_in_the_obs_namespace():
    for obj in pyroscope.render():
        assert obj["metadata"]["namespace"] == "dsy-obs"


def test_profiles_keep_three_days_on_a_local_volume():
    assert "-retention-period=72h" in pyroscope.pyroscope_args()
    claim = objects()[("PersistentVolumeClaim", "pyroscope-data")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "2Gi"
    assert {"name": "data", "mountPath": "/data"} in container()["volumeMounts"]


def test_every_write_lands_on_the_volume_so_the_root_filesystem_stays_read_only():
    args = pyroscope.pyroscope_args()
    assert "-pyroscopedb.data-path=/data" in args
    assert "-storage.filesystem.dir=/data/v2/shared" in args
    assert container()["securityContext"]["readOnlyRootFilesystem"] is True


def test_service_serves_the_port_the_services_push_to_and_grafana_reads():
    service = objects()[("Service", "pyroscope")]
    assert [p["port"] for p in service["spec"]["ports"]] == [4040]
