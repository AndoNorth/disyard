"""The manifests and configuration of the Loki and collector modules.

No cluster is needed: each module's config.py is loaded by path.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
OBS = ROOT / "deploy/obs"


def load(module: str):
    spec = importlib.util.spec_from_file_location(f"{module}_config", OBS / module / "config.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config


loki = load("loki")
collector = load("collector")
MODULES = {"loki": loki, "collector": collector}


def objects(config) -> dict:
    return {(o["kind"], o["metadata"]["name"]): o for o in config.render()}


def deployment(config) -> dict:
    return objects(config)[("Deployment", config.NAME)]


@pytest.mark.parametrize("name", MODULES)
def test_deployment_recreates_its_single_pod(name):
    spec = deployment(MODULES[name])["spec"]
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["replicas"] == 1


@pytest.mark.parametrize("name", MODULES)
def test_pod_runs_on_the_obs_pool_inside_the_resources_of_its_manifest(name):
    pod = deployment(MODULES[name])["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"dsy.dev/pool": "obs"}
    assert pod["tolerations"][0]["key"] == "dsy.dev/pool"
    [container] = pod["containers"]
    module = yaml.safe_load((OBS / name / "module.yaml").read_text())
    assert container["resources"] == module["resources"][name]
    assert (
        container["resources"]["requests"]["memory"] == container["resources"]["limits"]["memory"]
    )
    pins = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    assert container["image"].endswith("@" + pins[module["artifact"]["pin"]]["digest"])
    assert "metrics" in {p["name"] for p in container["ports"]}


@pytest.mark.parametrize("name", MODULES)
def test_pod_selected_by_the_label_the_worker_pools_allow(name):
    template = deployment(MODULES[name])["spec"]["template"]
    assert template["metadata"]["labels"]["app.kubernetes.io/name"] == name


@pytest.mark.parametrize("name", MODULES)
def test_configuration_checksum_rolls_the_pod_when_the_configuration_changes(name, monkeypatch):
    config = MODULES[name]
    key = f"{name}_config"
    before = deployment(config)["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    original = getattr(config, key)
    monkeypatch.setattr(config, key, lambda: {**original(), "extra": True})
    after = deployment(config)["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert after != before


@pytest.mark.parametrize("name", MODULES)
def test_every_object_is_in_the_obs_namespace_or_cluster_scoped(name):
    for obj in MODULES[name].render():
        if obj["kind"].startswith("ClusterRole"):
            assert "namespace" not in obj["metadata"]
        else:
            assert obj["metadata"]["namespace"] == "dsy-obs"


def test_loki_keeps_ten_days_and_enforces_them_with_the_compactor():
    config = loki.loki_config()
    assert config["limits_config"]["retention_period"] == "240h"
    assert config["compactor"]["retention_enabled"] is True
    claim = objects(loki)[("PersistentVolumeClaim", "loki-data")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "2Gi"


def test_loki_accepts_structured_metadata_so_correlation_fields_stay_out_of_the_index():
    assert loki.loki_config()["limits_config"]["allow_structured_metadata"] is True
    # Loki indexes only its own default resource attributes unless a list is configured, and
    # configuring one is how a field such as job_id would become a label.
    assert "otlp_config" not in loki.loki_config()["limits_config"]


def test_loki_is_served_on_the_port_the_collector_and_clients_address():
    service = objects(loki)[("Service", "loki")]
    assert [p["port"] for p in service["spec"]["ports"]] == [3100]
    assert collector.LOKI_ENDPOINT == "http://loki.dsy-obs:3100/otlp"


def test_collector_receives_otlp_on_the_ports_the_services_push_to():
    config = collector.collector_config()
    protocols = config["receivers"]["otlp"]["protocols"]
    assert protocols["http"]["endpoint"].endswith(":4318")
    service = objects(collector)[("Service", "collector")]
    assert {p["port"] for p in service["spec"]["ports"]} == {4317, 4318}


def test_collector_pipelines_carry_logs_and_events_to_loki_and_traces_to_tempo():
    pipelines = collector.collector_config()["service"]["pipelines"]
    assert pipelines["logs/otlp"]["receivers"] == ["otlp"]
    assert pipelines["logs/events"]["receivers"] == ["k8s_objects"]
    assert pipelines["logs/otlp"]["exporters"] == pipelines["logs/events"]["exporters"]
    assert pipelines["logs/otlp"]["exporters"] == ["otlp_http/loki"]
    assert pipelines["traces"]["receivers"] == ["otlp"]
    assert pipelines["traces"]["exporters"] == ["otlp_http/tempo"]


def test_collector_drops_traces_while_tempo_is_absent_instead_of_queueing_them():
    exporter = collector.collector_config()["exporters"]["otlp_http/tempo"]
    assert exporter["endpoint"] == "http://tempo.dsy-obs:4318"
    assert exporter["retry_on_failure"] == {"enabled": False}
    assert exporter["sending_queue"] == {"enabled": False}


def test_collector_adds_no_label_beyond_the_service_name_of_the_events():
    config = collector.collector_config()
    processors = {
        name
        for pipeline in config["service"]["pipelines"].values()
        for name in pipeline["processors"]
    }
    assert processors == {"memory_limiter", "batch", "resource/events"}
    [attribute] = config["processors"]["resource/events"]["attributes"]
    assert attribute["key"] == "service.name"
    # Only the events pipeline carries it: a service's own name is never overwritten.
    assert "resource/events" not in config["service"]["pipelines"]["logs/otlp"]["processors"]


def test_collector_memory_limiter_sits_under_the_container_limit():
    limit_mib = int(
        yaml.safe_load((OBS / "collector/module.yaml").read_text())["resources"]["collector"][
            "limits"
        ]["memory"].removesuffix("Mi")
    )
    limiter = collector.collector_config()["processors"]["memory_limiter"]
    assert limiter["limit_mib"] + limiter["spike_limit_mib"] < limit_mib


def test_collector_may_only_watch_events():
    [rule] = objects(collector)[("ClusterRole", "dsy-collector")]["rules"]
    assert rule["resources"] == ["events"]
    assert rule["verbs"] == ["list", "watch"]
