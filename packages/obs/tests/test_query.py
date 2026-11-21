"""Each call of `wft_obs.query` sends what its backend expects and reads the canned answer of
`FakeObs`."""

import json

import pytest
from wft_obs import query
from wft_obs.query import QueryError

UP = {"metric": {"__name__": "up", "job": "kubelet"}, "value": [1700000000, "1"]}
TARGET = {"labels": {"job": "kubelet"}, "health": "up"}
STREAM = {"stream": {"service_name": "dispatch"}, "values": [["1700000000000000000", "{}"]]}
TRACE = {"batches": [{"resource": {"attributes": []}, "scopeSpans": []}]}


def last(fake, path):
    return [r for r in fake.received if r.path == path][-1]


def test_prom_reads_the_vector_of_its_expression(fake):
    fake.vectors["up"] = [UP]
    assert query.prom(fake.url, "up") == [UP]
    assert last(fake, "/api/v1/query").query == {"query": "up"}


def test_prom_reads_an_empty_vector_for_an_expression_with_no_series(fake):
    assert query.prom(fake.url, "absent_metric") == []


def test_targets_reads_the_active_targets(fake):
    fake.active_targets = [TARGET]
    assert query.targets(fake.url) == [TARGET]
    assert last(fake, "/api/v1/targets").query == {"state": "active"}


def test_loki_reads_the_streams_and_sends_nanoseconds(fake):
    fake.streams['{service_name="dispatch"}'] = [STREAM]
    assert query.loki(fake.url, '{service_name="dispatch"}', 1700000000, 1700000060.5) == [STREAM]
    sent = last(fake, "/loki/api/v1/query_range").query
    assert sent["start"] == "1700000000000000000"
    assert sent["end"] == "1700000060500000000"


def test_trace_reads_the_trace_of_its_id(fake):
    fake.traces["0af7651916cd43dd8448eb211c80319c"] = TRACE
    assert query.trace(fake.url, "0af7651916cd43dd8448eb211c80319c") == TRACE


def test_trace_of_an_unknown_id_raises(fake):
    with pytest.raises(QueryError, match="HTTP 404"):
        query.trace(fake.url, "0" * 32)


def test_profiled_reads_the_service_names_and_sends_milliseconds(fake):
    fake.services = ["wft.workflow", "wft.dispatch"]
    assert query.profiled(fake.url, 1700000000, 1700000300) == ["wft.dispatch", "wft.workflow"]
    sent = json.loads(last(fake, "/querier.v1.QuerierService/LabelValues").body)
    assert sent == {"name": "service_name", "start": 1700000000000, "end": 1700000300000}


def test_an_unreachable_backend_raises():
    with pytest.raises(QueryError):
        query.prom("http://127.0.0.1:9", "up")


def test_fake_records_the_otlp_push_and_the_annotations(fake):
    from urllib.request import Request, urlopen

    for path, body in (
        ("/api/v1/otlp/v1/metrics", b"\x0a\x00"),
        ("/api/annotations", b'{"tags": ["smoke"]}'),
    ):
        with urlopen(Request(f"{fake.url}{path}", data=body, method="POST"), timeout=5) as r:
            assert r.status == 200
    assert fake.pushed == [b"\x0a\x00"]
    assert fake.annotations == [{"tags": ["smoke"]}]
