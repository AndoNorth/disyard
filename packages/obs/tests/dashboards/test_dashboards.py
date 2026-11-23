"""Every provisioned dashboard is valid JSON, reads only the stack's datasources, and no panel query
groups, matches or labels a series by a label on the instrumentation denylist.

The characterization dashboard shows every catalogued histogram on its pinned buckets, and the smoke
dashboard reads the pushed results of the smoke gate and its `smoke` annotations."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
DASHBOARDS = sorted((ROOT / "deploy/obs/grafana/dashboards").glob("*.json"))
SCHEMA = json.loads((ROOT / "contracts/instrumentation/metrics.schema.json").read_text())
DENYLIST = frozenset(SCHEMA["x-label-denylist"])

# The datasource uids Grafana is provisioned with, by type, and Grafana's own for annotations.
DATASOURCES = {
    "prometheus": "prometheus",
    "loki": "loki",
    "tempo": "tempo",
    "pyroscope": "grafana-pyroscope-datasource",
    "grafana": "grafana",
}

MATCHER = re.compile(r"\{([^{}]*)\}")
MATCHER_LABEL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)")
GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)")
LEGEND = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def labels(expr: str, legend: str = "") -> set[str]:
    """The label names a query matches, groups or joins on, and the ones its legend prints."""
    found = {name for body in MATCHER.findall(expr) for name in MATCHER_LABEL.findall(body)}
    found |= {
        name.strip() for body in GROUPING.findall(expr) for name in body.split(",") if name.strip()
    }
    return found | set(LEGEND.findall(legend))


def panels(items):
    for item in items:
        yield item
        yield from panels(item.get("panels", []))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_label_extraction():
    expr = 'sum by (pool, job_id) (rate(x{exit_class=~"a|b", task_id!="1"}[5m])) / on (attempt) y'
    assert labels(expr, "{{ trace_id }}") == {
        "pool",
        "job_id",
        "exit_class",
        "task_id",
        "attempt",
        "trace_id",
    }


def test_dashboards_exist():
    assert DASHBOARDS


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_dashboard_shape(path):
    dashboard = load(path)
    assert dashboard["uid"] == path.stem
    assert dashboard["title"]
    ids = [panel["id"] for panel in panels(dashboard["panels"])]
    assert ids and len(ids) == len(set(ids))


def test_dashboard_uids_and_titles_unique():
    dashboards = [load(path) for path in DASHBOARDS]
    for key in ("uid", "title"):
        values = [dashboard[key] for dashboard in dashboards]
        assert len(values) == len(set(values)), key


def datasources(dashboard: dict):
    for panel in panels(dashboard["panels"]):
        if "datasource" in panel:
            yield panel["title"], panel["datasource"]
        for target in panel.get("targets", []):
            yield panel["title"], target.get("datasource", panel.get("datasource"))
    for annotation in dashboard.get("annotations", {}).get("list", []):
        yield annotation["name"], annotation["datasource"]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_known_datasources(path):
    for title, source in datasources(load(path)):
        assert isinstance(source, dict), f"{title}: datasource must name a uid"
        assert DATASOURCES.get(source.get("uid")) == source.get("type"), f"{title}: {source}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_no_denied_label(path):
    for panel in panels(load(path)["panels"]):
        for target in panel.get("targets", []):
            query = target.get("expr", "") or target.get("query", "")
            denied = labels(query, target.get("legendFormat", "")) & DENYLIST
            assert not denied, f"{panel['title']}: {sorted(denied)}"


CATALOGUE = yaml.safe_load((ROOT / "contracts/instrumentation/metrics.yaml").read_text())["metrics"]


def exprs(name: str) -> list[str]:
    dashboard = load(ROOT / f"deploy/obs/grafana/dashboards/{name}.json")
    return [
        target["expr"]
        for panel in panels(dashboard["panels"])
        for target in panel.get("targets", [])
        if target.get("expr")
    ]


def test_the_g1_dashboards_are_provisioned():
    names = {path.stem for path in DASHBOARDS}
    assert {"fleet", "intake-admission", "paused-jobs", "characterization", "smoke"} <= names


def test_characterization_shows_every_histogram_on_its_buckets():
    text = exprs("characterization")
    for name, metric in CATALOGUE.items():
        if metric["type"] != "histogram":
            continue
        by_le = [e for e in text if f"{name}_bucket" in e and "by (le)" in e]
        assert any("histogram_quantile" in e for e in by_le), f"{name} has no quantile panel"
        assert any("histogram_quantile" not in e for e in by_le), f"{name} has no bucket panel"


def test_smoke_reads_the_pushed_results_and_annotations():
    text = exprs("smoke")
    assert any("wft_smoke_row_seconds" in e for e in text)
    assert any('wft_smoke_row_verdict{row="run"}' in e for e in text)
    assert any('wft_smoke_row_verdict{verdict="FAIL"' in e for e in text)
    smoke = load(ROOT / "deploy/obs/grafana/dashboards/smoke.json")
    tags = [a["target"]["tags"] for a in smoke["annotations"]["list"] if "target" in a]
    assert ["smoke"] in tags


def test_stack_health_covers_every_obs_module():
    modules = [
        yaml.safe_load(p.read_text()) for p in sorted((ROOT / "deploy/obs").glob("*/module.yaml"))
    ]
    namespaces = {module["namespace"] for module in modules}
    assert len(namespaces) == 1, namespaces
    text = exprs("stack-health")
    assert all(
        f'namespace="{ns}"' in e
        for ns in namespaces
        for e in text
        if "kube_" in e or "kubelet_" in e
    )
    # Volume use and memory read the namespace, so a module added to it is covered without an edit.
    assert any("kubelet_volume_stats_used_bytes" in e for e in text)
    assert any("kube_deployment_status_replicas_available" in e for e in text)
    for ingest in (
        "prometheus_tsdb_head_samples_appended_total",
        "loki_distributor_bytes_received_total",
        "tempo_distributor_spans_received_total",
        "pyroscope_distributor_received_compressed_bytes_sum",
        "otelcol_receiver_accepted_spans",
    ):
        assert any(ingest in e for e in text), ingest
