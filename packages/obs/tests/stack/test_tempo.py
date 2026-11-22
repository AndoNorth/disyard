"""The manifests and configuration of the Tempo module.

No cluster is needed: the module's config.py is loaded by path.
"""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
OBS = ROOT / "deploy/obs"


def load(module: str):
    spec = importlib.util.spec_from_file_location(f"{module}_config", OBS / module / "config.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config


tempo = load("tempo")
collector = load("collector")
grafana = load("grafana")


def objects() -> dict:
    return {(o["kind"], o["metadata"]["name"]): o for o in tempo.render()}


def deployment() -> dict:
    return objects()[("Deployment", "tempo")]


def test_deployment_recreates_its_single_pod():
    spec = deployment()["spec"]
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["replicas"] == 1


def test_pod_runs_on_the_obs_pool_inside_the_resources_of_its_manifest():
    pod = deployment()["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"wft.dev/pool": "obs"}
    assert pod["tolerations"][0]["key"] == "wft.dev/pool"
    [container] = pod["containers"]
    module = yaml.safe_load((OBS / "tempo/module.yaml").read_text())
    assert container["resources"] == module["resources"]["tempo"]
    assert (
        container["resources"]["requests"]["memory"] == container["resources"]["limits"]["memory"]
    )
    pins = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    assert container["image"].endswith("@" + pins[module["artifact"]["pin"]]["digest"])


def test_pod_is_selected_by_the_label_the_worker_pools_allow():
    template = deployment()["spec"]["template"]
    assert template["metadata"]["labels"]["app.kubernetes.io/name"] == "tempo"


def test_container_names_the_port_the_pod_scrape_reads():
    [container] = deployment()["spec"]["template"]["spec"]["containers"]
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    assert ports["metrics"] == 3200


def test_configuration_checksum_rolls_the_pod_when_the_configuration_changes(monkeypatch):
    before = deployment()["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    original = tempo.tempo_config
    monkeypatch.setattr(tempo, "tempo_config", lambda: {**original(), "extra": True})
    after = deployment()["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert after != before


def test_every_object_is_in_the_obs_namespace():
    for obj in tempo.render():
        assert obj["metadata"]["namespace"] == "wft-obs"


def test_container_runs_unprivileged_with_only_its_volume_writable():
    pod = deployment()["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    [container] = pod["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert {m["mountPath"] for m in container["volumeMounts"]} >= {tempo.DATA, "/tmp"}


def test_tempo_keeps_three_days_with_the_key_of_the_worker_that_enforces_them():
    config = tempo.tempo_config()
    assert config["backend_worker"]["compaction"]["block_retention"] == "72h"
    # The scheduler's own key did not delete a block in the pinned version.
    assert "backend_scheduler" not in config
    claim = objects()[("PersistentVolumeClaim", "tempo-data")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "4Gi"


def test_tempo_stores_blocks_and_wal_on_the_mounted_volume():
    trace = tempo.tempo_config()["storage"]["trace"]
    assert trace["backend"] == "local"
    assert trace["local"]["path"].startswith(tempo.DATA + "/")
    assert trace["wal"]["path"].startswith(tempo.DATA + "/")


def test_tempo_reports_no_usage():
    assert tempo.tempo_config()["usage_report"] == {"reporting_enabled": False}


def test_tempo_takes_spans_where_the_collector_sends_them():
    receiver = tempo.tempo_config()["distributor"]["receivers"]["otlp"]["protocols"]["http"]
    port = int(receiver["endpoint"].rsplit(":", 1)[1])
    service = objects()[("Service", "tempo")]
    assert {p["port"] for p in service["spec"]["ports"]} == {3200, port}
    assert collector.TEMPO_ENDPOINT == f"http://tempo.wft-obs:{port}"


def test_tempo_is_served_where_the_grafana_datasource_reads_it():
    [datasource] = [d for d in grafana.datasources()["datasources"] if d["uid"] == "tempo"]
    assert datasource["url"] == "http://tempo.wft-obs:3200"
    service = objects()[("Service", "tempo")]
    assert 3200 in {p["port"] for p in service["spec"]["ports"]}
