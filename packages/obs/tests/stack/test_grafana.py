"""The manifests, datasources and credential of the Grafana module.

No cluster is needed: config.py is loaded by path and secrets.sh runs on a temporary directory.
"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "deploy/obs/grafana"


def load():
    spec = importlib.util.spec_from_file_location("grafana_config", MODULE / "config.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config


grafana = load()


def objects() -> dict:
    return {(o["kind"], o["metadata"]["name"]): o for o in grafana.render()}


def deployment() -> dict:
    return objects()[("Deployment", "grafana")]


def container() -> dict:
    [container] = deployment()["spec"]["template"]["spec"]["containers"]
    return container


def env() -> dict:
    return {e["name"]: e for e in container()["env"]}


def datasources() -> dict:
    return {d["uid"]: d for d in grafana.datasources()["datasources"]}


def test_deployment_recreates_its_single_pod():
    spec = deployment()["spec"]
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["replicas"] == 1


def test_pod_runs_on_the_obs_pool_inside_the_resources_of_its_manifest():
    pod = deployment()["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"wft.dev/pool": "obs"}
    assert pod["tolerations"][0]["key"] == "wft.dev/pool"
    module = yaml.safe_load((MODULE / "module.yaml").read_text())
    assert container()["resources"] == module["resources"]["grafana"]
    memory = container()["resources"]
    assert memory["requests"]["memory"] == memory["limits"]["memory"] == "512Mi"
    pins = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    assert container()["image"].endswith("@" + pins[module["artifact"]["pin"]]["digest"])
    assert "metrics" in {p["name"] for p in container()["ports"]}


def test_every_object_is_in_the_obs_namespace():
    for obj in grafana.render():
        assert obj["metadata"]["namespace"] == "wft-obs"


def test_database_lives_on_a_local_volume_so_annotations_survive_a_pod_replacement():
    claim = objects()[("PersistentVolumeClaim", "grafana-data")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"
    pod = deployment()["spec"]["template"]["spec"]
    assert {"name": "data", "persistentVolumeClaim": {"claimName": "grafana-data"}} in pod[
        "volumes"
    ]
    assert {"name": "data", "mountPath": "/var/lib/grafana"} in container()["volumeMounts"]


def test_anonymous_visitors_are_editors_which_is_the_role_that_gives_explore():
    assert env()["GF_AUTH_ANONYMOUS_ENABLED"]["value"] == "true"
    assert env()["GF_AUTH_ANONYMOUS_ORG_ROLE"]["value"] == "Editor"


def test_admin_credential_comes_from_the_secret_and_is_never_in_a_manifest():
    for name in ("GF_SECURITY_ADMIN_USER", "GF_SECURITY_ADMIN_PASSWORD"):
        assert "value" not in env()[name]
        assert env()[name]["valueFrom"]["secretKeyRef"]["name"] == "grafana-admin"
    assert "grafana-admin" not in {name for kind, name in objects() if kind == "Secret"}


def test_service_is_the_nodeport_the_host_port_row_maps():
    rows = yaml.safe_load((ROOT / "deploy/host/ports.yaml").read_text())["ports"]
    [row] = [r for r in rows if r["name"] == "grafana"]
    assert row["host_port"] == 13000
    service = objects()[("Service", "grafana")]
    assert service["spec"]["type"] == "NodePort"
    [port] = service["spec"]["ports"]
    assert (port["port"], port["nodePort"]) == (3000, row["node_port"])
    assert port["targetPort"] == "metrics"


def test_configuration_checksum_rolls_the_pod_when_the_datasources_change(monkeypatch):
    key = "checksum/config"
    before = deployment()["spec"]["template"]["metadata"]["annotations"][key]
    original = grafana.datasources
    monkeypatch.setattr(grafana, "datasources", lambda: {**original(), "extra": True})
    after = deployment()["spec"]["template"]["metadata"]["annotations"][key]
    assert after != before


def test_the_four_datasources_have_the_uids_and_types_the_dashboards_name():
    assert {uid: d["type"] for uid, d in datasources().items()} == {
        "prometheus": "prometheus",
        "loki": "loki",
        "tempo": "tempo",
        "pyroscope": "grafana-pyroscope-datasource",
    }
    assert [d["uid"] for d in datasources().values() if d.get("isDefault")] == ["prometheus"]


def test_every_datasource_a_dashboard_reads_is_provisioned():
    wanted = set()
    for path in (MODULE / "dashboards").glob("*.json"):
        text = path.read_text()
        wanted |= {
            ref["datasource"]["uid"]
            for ref in _refs(json.loads(text))
            if isinstance(ref.get("datasource"), dict)
        }
    # Grafana's own datasource for annotations is built in and is not provisioned.
    assert wanted - {"grafana"} <= set(datasources())


def _refs(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def test_loki_derives_a_trace_link_to_tempo_and_a_workflow_link_to_the_temporal_ui():
    fields = {f["name"]: f for f in datasources()["loki"]["jsonData"]["derivedFields"]}
    assert fields["trace_id"]["datasourceUid"] == "tempo"
    assert fields["trace_id"]["url"] == "${__value.raw}"
    assert fields["job_id"]["url"] == (
        "http://127.0.0.1:18233/namespaces/wft/workflows/job-${__value.raw}"
    )
    # The correlation ids are structured metadata, so both read a label by name.
    for field in fields.values():
        assert (field["matcherType"], field["matcherRegex"]) == ("label", field["name"])


def test_provisioning_file_escapes_the_dollar_names_grafana_expands_from_its_environment():
    text = objects()[("ConfigMap", "grafana-provisioning")]["data"]["datasources.yaml"]
    assert "$${__value.raw}" in text
    assert yaml.safe_load(text.replace("$$", "$")) == grafana.datasources()


def test_the_workflow_page_prefix_is_the_workflow_id_of_a_job():
    from wft_keys import workflow_id

    assert workflow_id("J") == grafana.WORKFLOW_ID_PREFIX + "J"


def test_temporal_ui_link_follows_the_host_port_row():
    rows = yaml.safe_load((ROOT / "deploy/host/ports.yaml").read_text())["ports"]
    [row] = [r for r in rows if r["name"] == "temporal-ui"]
    assert grafana.temporal_workflow_url("W").startswith(f"http://127.0.0.1:{row['host_port']}/")


def test_dashboards_are_provisioned_as_their_files_say():
    files = {p.name: p.read_text() for p in (MODULE / "dashboards").glob("*.json")}
    config = objects()[("ConfigMap", "grafana-dashboards")]["data"]
    assert config == files
    assert sum(len(text.encode()) for text in files.values()) < grafana.CONFIGMAP_LIMIT
    [provider] = grafana.dashboard_provider()["providers"]
    assert provider["options"]["path"] == grafana.DASHBOARDS_PATH
    assert provider["allowUiUpdates"] is False
    assert {"name": "dashboards", "mountPath": grafana.DASHBOARDS_PATH} in container()[
        "volumeMounts"
    ]


def secrets(state: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(MODULE / "secrets.sh"), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "WFT_STATE_DIR": str(state)},
    )


def test_credential_is_generated_once_into_the_state_directory_and_kept(tmp_path):
    first = secrets(tmp_path, "password")
    assert first.returncode == 0
    password = first.stdout.strip()
    assert len(password) >= 32
    file = tmp_path / "grafana.credential"
    assert file.read_text().strip() == password
    assert file.stat().st_mode & 0o777 == 0o600
    assert secrets(tmp_path, "password").stdout.strip() == password


def test_credential_renders_as_the_secret_the_pod_reads(tmp_path):
    password = secrets(tmp_path, "password").stdout.strip()
    secret = yaml.safe_load(secrets(tmp_path).stdout)
    assert secret["kind"] == "Secret"
    assert (secret["metadata"]["name"], secret["metadata"]["namespace"]) == (
        "grafana-admin",
        "wft-obs",
    )
    assert secret["stringData"] == {"admin-user": "admin", "admin-password": password}


def test_secrets_script_refuses_an_unknown_mode(tmp_path):
    assert secrets(tmp_path, "nope").returncode == 2
