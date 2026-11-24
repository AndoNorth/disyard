"""The one read client of the observability stack, over the HTTP API of each backend.

Each call takes the base address of its backend, such as `http://127.0.0.1:19090`, and returns the
part of the answer a caller reads. Times are Unix seconds.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

TIMEOUT_S = 5.0


class QueryError(Exception):
    """A backend did not answer, answered with an error status, or answered with no data."""


def prom(address: str, expr: str) -> list[dict]:
    """The instant vector of a PromQL expression: each item has `metric` (its labels) and `value`
    (a `[time, "value"]` pair)."""
    return _prometheus(address, "/api/v1/query", {"query": expr})["result"]


def targets(address: str) -> list[dict]:
    """The active scrape targets of Prometheus: each item has `labels` (its `job` among them) and
    `health` (`up`, `down` or `unknown`)."""
    return _prometheus(address, "/api/v1/targets", {"state": "active"})["activeTargets"]


def loki(address: str, query: str, start: float, end: float) -> list[dict]:
    """The log streams of a LogQL query between two times: each item has `stream` (its labels) and
    `values` (`[nanoseconds, line]` pairs, or with structured metadata a third item)."""
    params = {"query": query, "start": _ns(start), "end": _ns(end), "direction": "forward"}
    answer = _get(address, "/loki/api/v1/query_range", params)
    if answer.get("status") != "success":
        raise QueryError(f"loki: {answer}")
    return answer["data"]["result"]


def trace(address: str, trace_id: str) -> dict:
    """The trace of a hex trace id from Tempo, as OTLP JSON. An unknown id raises `QueryError`."""
    return _get(address, f"/api/traces/{quote(trace_id)}", {})


def profiled(address: str, start: float, end: float) -> list[str]:
    """The `service_name` values of the profiles Pyroscope holds between two times."""
    body = {"name": "service_name", "start": _ms(start), "end": _ms(end)}
    answer = _call(
        Request(
            f"{address.rstrip('/')}/querier.v1.QuerierService/LabelValues",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )
    return sorted(answer.get("names", []))


def _prometheus(address: str, path: str, params: dict) -> dict:
    answer = _get(address, path, params)
    if answer.get("status") != "success":
        raise QueryError(f"prometheus {path}: {answer}")
    return answer["data"]


def _get(address: str, path: str, params: dict) -> dict:
    query = f"?{urlencode(params)}" if params else ""
    return _call(Request(f"{address.rstrip('/')}{path}{query}"))


def _call(request: Request) -> dict:
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as e:
        raise QueryError(f"{request.full_url}: HTTP {e.code} {e.read()[:200]!r}") from e
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        raise QueryError(f"{request.full_url}: {e}") from e


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


def _ms(seconds: float) -> int:
    return int(seconds * 1000)
