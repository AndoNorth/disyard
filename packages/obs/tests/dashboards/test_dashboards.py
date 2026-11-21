"""Every provisioned dashboard is valid JSON, reads only the stack's datasources, and no panel query
groups, matches or labels a series by a label on the instrumentation denylist."""

import json
import re
from pathlib import Path

import pytest

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
