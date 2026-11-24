"""The results push of the smoke gate: with the observability switch on, after the count line, each
row's verdict and seconds go to Prometheus's OTLP receiver as gauges and one annotation tagged
`smoke` goes to Grafana.

The push is bounded and never changes a row, the count line or the exit code: a push that fails
prints one line to stderr and the run ends as it would have.
"""

import base64
import json
import sys
import time
from collections.abc import Sequence
from urllib.request import Request, urlopen

from dsy_clusterkit import state_dir

from .. import report
from . import telemetry

GRAFANA = "http://127.0.0.1:13000"
PUSH_S = 5.0
# The rows of the gate in the order they print, whose position is the `row` label.
ROWS = (
    "cluster-verify",
    "api",
    "broker",
    "temporal",
    "placement",
    "job-submit",
    "job-run",
    "job-result",
    "job-aggregate",
    "dead-letters",
    telemetry.ROW,
)
RUN = "run"


def verdict_of(rows: Sequence[report.Row]) -> str:
    return report.OK if all(row.verdict == report.OK for row in rows) else report.FAILED


def per_row(
    rows: Sequence[report.Row], row_ends: Sequence[float], started: float
) -> dict[str, tuple[str, float]]:
    """Each numbered row's printed verdict and seconds, from the time each row ended."""
    marks = [started, *row_ends]
    measured = {}
    for index, row in enumerate(rows):
        if row.name in ROWS:
            measured[str(ROWS.index(row.name) + 1)] = (row.verdict, marks[index + 1] - marks[index])
    return measured


def _value(value: str) -> dict:
    return {"stringValue": value}


def _point(attributes: dict[str, str], value: float, now_ns: int) -> dict:
    return {
        "attributes": [{"key": k, "value": _value(v)} for k, v in attributes.items()],
        "timeUnixNano": str(now_ns),
        "asDouble": value,
    }


def otlp_body(measured: dict[str, tuple[str, float]], now_ns: int) -> bytes:
    """The two gauges as an OTLP JSON metrics request."""
    verdicts = [_point({"row": r, "verdict": v}, 1.0, now_ns) for r, (v, _) in measured.items()]
    seconds = [_point({"row": r}, s, now_ns) for r, (_, s) in measured.items()]
    metrics = [
        {"name": "dsy_smoke_row_verdict", "gauge": {"dataPoints": verdicts}},
        {"name": "dsy_smoke_row_seconds", "gauge": {"dataPoints": seconds}},
    ]
    resource = {"attributes": [{"key": "service.name", "value": _value("smoke")}]}
    scope = {"scope": {"name": "dsy_acceptance.smoke"}, "metrics": metrics}
    return json.dumps(
        {"resourceMetrics": [{"resource": resource, "scopeMetrics": [scope]}]}
    ).encode()


def _post(url: str, body: bytes, timeout_s: float, headers: dict[str, str]) -> None:
    request = Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    with urlopen(request, timeout=max(timeout_s, 0.1)) as response:
        response.read()


def push(
    switch: str,
    forwarded: telemetry.Forwarded,
    rows: Sequence[report.Row],
    row_ends: Sequence[float],
    started: float,
    batch: str | None,
) -> None:
    """Pushes the run's results when the record is `on`; whatever fails is reported on stderr."""
    if switch != "on":
        return
    verdict = verdict_of(rows)
    now = time.time()
    measured = per_row(rows, row_ends, started)
    measured[RUN] = (verdict, time.monotonic() - started)
    deadline = time.monotonic() + PUSH_S

    def left() -> float:
        return deadline - time.monotonic()

    def gauges() -> None:
        with forwarded(telemetry.NAMESPACE, "svc/prometheus", 9090, timeout_s=PUSH_S) as address:
            body = otlp_body(measured, int(now * 1e9))
            _post(f"http://{address}/api/v1/otlp/v1/metrics", body, left(), {})

    def annotation() -> None:
        credential = (state_dir() / "grafana.credential").read_text().strip()
        login = base64.b64encode(f"admin:{credential}".encode()).decode()
        text = f"smoke {verdict}, batch {batch or 'none'}"
        note = {"time": int(now * 1000), "tags": ["smoke"], "text": text}
        headers = {"Authorization": f"Basic {login}"}
        _post(f"{GRAFANA}/api/annotations", json.dumps(note).encode(), left(), headers)

    # Each part is tried whatever the other did, so the one stderr line names every failure.
    failures = []
    for name, part in (("gauges", gauges), ("annotation", annotation)):
        try:
            part()
        except (Exception, KeyboardInterrupt) as error:
            failures.append(f"{name}: {error or type(error).__name__}")
    if failures:
        print(f"smoke results: the push failed, {'; '.join(failures)}", file=sys.stderr)
