"""The rendered Prometheus configuration and the manifests of both S.1 modules.

No cluster is needed: each module's config.py is loaded by path, and `promtool` checks the rendered
configuration beside the real rule files.
"""

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from dsy_obs.targets import JOBS

ROOT = Path(__file__).resolve().parents[4]
OBS = ROOT / "deploy/obs"


def load(module: str):
    spec = importlib.util.spec_from_file_location(
        module.replace("-", "_") + "_config", OBS / module / "config.py"
    )
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config


prometheus = load("prometheus")
kube_state_metrics = load("kube-state-metrics")
MODULES = {"prometheus": prometheus, "kube-state-metrics": kube_state_metrics}


def objects(config) -> dict:
    return {(o["kind"], o["metadata"]["name"]): o for o in config.render()}


def deployment(config) -> dict:
    return objects(config)[("Deployment", config.NAME)]


def test_jobs_of_the_rendered_configuration_equal_the_targets_the_gate_reads():
    jobs = [job["job_name"] for job in prometheus.scrape_config()["scrape_configs"]]
    assert len(jobs) == len(set(jobs))
    assert set(jobs) == JOBS


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
def test_promtool_checks_the_rendered_configuration_beside_the_rule_files(tmp_path):
    token = tmp_path / "token"
    token.write_text("t")
    config = yaml.dump(prometheus.scrape_config()).replace(prometheus.TOKEN, str(token))
    (tmp_path / "prometheus.yml").write_text(config)
    shutil.copytree(prometheus.RULES, tmp_path / "rules", ignore=shutil.ignore_patterns("tests"))
    run = subprocess.run(
        ["promtool", "check", "config", str(tmp_path / "prometheus.yml")],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "rule files" in run.stdout


@pytest.mark.parametrize("name", MODULES)
def test_deployments_of_both_modules_recreate_their_pod(name):
    spec = deployment(MODULES[name])["spec"]
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["replicas"] == 1


@pytest.mark.parametrize("name", MODULES)
def test_pod_runs_on_the_obs_pool_inside_the_resources_of_its_manifest(name):
    config = MODULES[name]
    pod = deployment(config)["spec"]["template"]["spec"]
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
    assert {p["name"] for p in container["ports"]} >= {"metrics"}


def test_prometheus_pod_is_selected_by_the_label_the_worker_pools_allow():
    template = deployment(prometheus)["spec"]["template"]
    assert template["metadata"]["labels"]["app.kubernetes.io/name"] == "prometheus"


def test_prometheus_keeps_the_retention_cap_and_receiver_the_design_sets():
    [container] = deployment(prometheus)["spec"]["template"]["spec"]["containers"]
    assert "--storage.tsdb.retention.time=10d" in container["args"]
    assert "--storage.tsdb.retention.size=4GB" in container["args"]
    assert "--web.enable-otlp-receiver" in container["args"]
    claim = objects(prometheus)[("PersistentVolumeClaim", "prometheus-data")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "5Gi"
    # The cap leaves room in the volume for the write-ahead log and head, which it does not count.
    # Prometheus reads GB as GiB, so the 4GB flag is a 4 GiB cap.
    cap_gib, volume_gib = 4, 5
    assert cap_gib < volume_gib * 0.9


def test_rules_are_mounted_from_a_config_map_and_their_checksum_rolls_the_pod(
    monkeypatch, tmp_path
):
    files = {p.name for p in prometheus.RULES.glob("*.yaml")}
    assert files
    assert set(objects(prometheus)[("ConfigMap", "prometheus-rules")]["data"]) == files

    def checksum() -> str:
        return deployment(prometheus)["spec"]["template"]["metadata"]["annotations"][
            "checksum/rules"
        ]

    before = checksum()
    assert before == checksum()
    (tmp_path / "extra.yaml").write_text("groups: []\n")
    monkeypatch.setattr(prometheus, "RULES", tmp_path)
    assert checksum() != before


def test_configuration_checksum_rolls_the_pod_when_a_job_changes(monkeypatch):
    before = deployment(prometheus)["spec"]["template"]["metadata"]["annotations"]
    monkeypatch.setattr(prometheus, "SCRAPE_INTERVAL", "15s")
    after = deployment(prometheus)["spec"]["template"]["metadata"]["annotations"]
    assert after["checksum/config"] != before["checksum/config"]
    assert after["checksum/rules"] == before["checksum/rules"]


def test_pod_scrape_keeps_annotated_pods_and_named_metrics_ports():
    [job] = [
        j
        for j in prometheus.scrape_config()["scrape_configs"]
        if j["job_name"] == "kubernetes-pods"
    ]
    [keep] = [r for r in job["relabel_configs"] if r.get("action") == "keep"]
    regex = re.compile(keep["regex"])
    assert regex.fullmatch("true;")
    assert regex.fullmatch(";metrics")
    assert regex.fullmatch(";prometheus")
    assert not regex.fullmatch(";grpc")
    assert not regex.fullmatch("false;http")


def test_kube_state_metrics_serves_the_state_the_rules_read():
    resources = {
        r
        for rule in objects(kube_state_metrics)[("ClusterRole", "dsy-kube-state-metrics")]["rules"]
        for r in rule["resources"]
    }
    assert {"nodes", "pods", "deployments"} <= resources
    assert all(
        rule["verbs"] == ["list", "watch"]
        for rule in objects(kube_state_metrics)[("ClusterRole", "dsy-kube-state-metrics")]["rules"]
    )
    [container] = deployment(kube_state_metrics)["spec"]["template"]["spec"]["containers"]
    [flag] = [a for a in container["args"] if a.startswith("--resources=")]
    assert set(flag.removeprefix("--resources=").split(",")) == resources


def test_every_object_is_in_the_obs_namespace_or_cluster_scoped():
    for config in MODULES.values():
        for obj in config.render():
            if obj["kind"].startswith("ClusterRole"):
                assert "namespace" not in obj["metadata"]
            else:
                assert obj["metadata"]["namespace"] == "dsy-obs"
