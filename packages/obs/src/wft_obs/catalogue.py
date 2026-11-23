"""The series names a dashboard query or a rule expression reads, held against the platform
catalogue of `contracts/instrumentation/metrics.yaml`.

PromQL is read with a tokenizer rather than a parser: strings, label matchers, durations, grouping
clauses and template variables are cut away, and an identifier that is not a function, an operator
word or a keyword is a series name.
"""

import json
import re
from pathlib import Path

import yaml

from wft_obs.query import prom

HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")

# Words that read as an identifier but are PromQL syntax, not a series.
KEYWORDS = frozenset(
    {
        "by", "without", "on", "ignoring", "group_left", "group_right", "and", "or", "unless",
        "bool", "offset", "start", "end", "inf", "nan", "atan2",
        "sum", "min", "max", "avg", "group", "stddev", "stdvar", "count", "count_values",
        "bottomk", "topk", "quantile", "limitk", "limit_ratio",
    }
)  # fmt: skip

# Series Prometheus synthesises itself, which no scrape target emits.
SYNTHETIC = frozenset({"ALERTS", "ALERTS_FOR_STATE", "up"})

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`|\'(?:[^\'\\]|\\.)*\'')
_BRACES = re.compile(r"\{[^{}]*\}")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_VARIABLE = re.compile(r"\$\{?\w+\}?")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
_IDENTIFIER = re.compile(r"(?<![\w:])([A-Za-z_:][A-Za-z0-9_:]*)(\s*\()?")
_NAME_MATCHER = re.compile(r'__name__\s*(?:=|=~)\s*"([^"]*)"')


def series(expr: str) -> set[str]:
    """The series names a PromQL expression selects."""
    names = set(_NAME_MATCHER.findall(expr))
    text = _STRING.sub('""', expr)
    text = _VARIABLE.sub("", text)
    text = _BRACKETS.sub("", text)
    text = _GROUPING.sub("", text)
    # Matchers nest no deeper than one level, so one pass per level of braces is enough.
    for _ in range(3):
        text = _BRACES.sub("", text)
    for name, call in _IDENTIFIER.findall(text):
        if not call and name not in KEYWORDS and not name[0].isdigit():
            names.add(name)
    return names


def dashboard_exprs(path: Path) -> list[str]:
    """The expressions of every Prometheus target and variable of a dashboard."""
    dashboard = json.loads(path.read_text())
    found = []

    def walk(panels):
        for panel in panels:
            for target in panel.get("targets", []):
                source = target.get("datasource") or panel.get("datasource") or {}
                if source.get("type") == "prometheus" and target.get("expr"):
                    found.append(target["expr"])
            walk(panel.get("panels", []))

    walk(dashboard.get("panels", []))
    for variable in dashboard.get("templating", {}).get("list", []):
        query = variable.get("query")
        if (variable.get("datasource") or {}).get("type") == "prometheus" and query:
            found.append(query if isinstance(query, str) else query.get("query", ""))
    return found


def rule_exprs(path: Path) -> list[str]:
    """The expression of every alerting and recording rule of a rule file."""
    groups = yaml.safe_load(path.read_text()).get("groups", [])
    return [rule["expr"] for group in groups for rule in group["rules"]]


def recorded(path: Path) -> set[str]:
    """The series a rule file records, which no catalogue holds."""
    groups = yaml.safe_load(path.read_text()).get("groups", [])
    return {rule["record"] for group in groups for rule in group["rules"] if "record" in rule}


def load(root: Path) -> dict:
    return yaml.safe_load((root / "contracts/instrumentation/metrics.yaml").read_text())


def names(catalogue: dict) -> set[str]:
    """Every series name the catalogue stands for: the exposed names, with the `_bucket`, `_sum`
    and `_count` of each histogram."""
    exposed = set(catalogue["metrics"]) | set(catalogue.get("external", {}))
    kinds = {n: m["type"] for n, m in {**catalogue["metrics"], **catalogue["external"]}.items()}
    return exposed | {
        n + s for n, k in kinds.items() if k == "histogram" for s in HISTOGRAM_SUFFIXES
    }


def uncatalogued(expressions: list[str], catalogue: dict, own: set[str] = frozenset()) -> set[str]:
    """The series the expressions read that the catalogue does not name."""
    read = set().union(*map(series, expressions)) if expressions else set()
    return read - names(catalogue) - SYNTHETIC - own


def absent(address: str, catalogue: dict, window: str = "10m") -> list[str]:
    """The catalogue entries Prometheus holds no sample of within the window, sorted by name.

    The pushed `wft_smoke_*` names are in Prometheus after one smoke run, like every scraped name.

    A histogram is checked by its `_bucket` series, which every observation writes."""
    missing = []
    for name, entry in {**catalogue["metrics"], **catalogue.get("external", {})}.items():
        probe = name + "_bucket" if entry["type"] == "histogram" else name
        if not prom(address, f"count(last_over_time({probe}[{window}]))"):
            missing.append(name)
    return sorted(missing)
