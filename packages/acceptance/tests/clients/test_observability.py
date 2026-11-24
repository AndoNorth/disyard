"""The observability client is the obs read client, answering from a canned Prometheus."""

import json

import pytest
from dsy_acceptance.clients import observability


def test_prom_reads_the_instant_vector(serve):
    prometheus = serve()
    body = {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "1"]}]}}
    prometheus.answer("GET", "/api/v1/query", (200, json.dumps(body).encode()))
    assert observability.prom(prometheus.url, "up") == [{"metric": {}, "value": [1, "1"]}]


def test_an_error_status_raises_query_error(serve):
    prometheus = serve()
    prometheus.answer("GET", "/api/v1/query", (500, b"{}"))
    with pytest.raises(observability.QueryError):
        observability.prom(prometheus.url, "up")
