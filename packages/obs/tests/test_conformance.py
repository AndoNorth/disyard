"""The calls of `wft_obs.query` over a backend, `FakeObs` by default and the real backends of a
running stack with `-m cluster`, so the fake answers in the shapes the real APIs answer.

With `-m cluster`, each backend's base address comes from its environment variable, and a test
whose backend has none is skipped, so a stack that has only some backends runs their part.
"""

import os
import time

import pytest
from wft_obs import query
from wft_obs.query import QueryError

ADDRESSES = {
    "prometheus": "WFT_OBS_PROMETHEUS_URL",
    "loki": "WFT_OBS_LOKI_URL",
    "tempo": "WFT_OBS_TEMPO_URL",
    "pyroscope": "WFT_OBS_PYROSCOPE_URL",
}
HEALTH = {"up", "down", "unknown"}


def seed(fake):
    now = time.time()
    fake.vectors["vector(1)"] = [{"metric": {}, "value": [now, "1"]}]
    fake.vectors["up"] = [{"metric": {"__name__": "up", "job": "kubelet"}, "value": [now, "1"]}]
    fake.active_targets = [{"labels": {"job": "kubelet"}, "health": "up"}]
    fake.streams['{service_name=~".+"}'] = [
        {"stream": {"service_name": "dispatch"}, "values": [[str(int(now * 1e9)), "{}"]]}
    ]
    fake.services = ["wft.dispatch"]


@pytest.fixture(params=["fake", pytest.param("cluster", marks=pytest.mark.cluster)])
def address(request):
    """The base address of a named backend."""
    if request.param == "fake":
        fake = request.getfixturevalue("fake")
        seed(fake)
        return lambda backend: fake.url

    def real(backend: str) -> str:
        if not (url := os.environ.get(ADDRESSES[backend])):
            pytest.skip(f"{ADDRESSES[backend]} is not set")
        return url

    return real


def test_prom_answers_an_instant_vector(address):
    assert [item["value"][1] for item in query.prom(address("prometheus"), "vector(1)")] == ["1"]
    for item in query.prom(address("prometheus"), "up"):
        assert "job" in item["metric"]
        assert item["value"][1] in {"0", "1"}


def test_targets_carry_a_job_and_a_health(address):
    for target in query.targets(address("prometheus")):
        assert "job" in target["labels"]
        assert target["health"] in HEALTH


def test_loki_answers_streams_of_timestamped_lines(address):
    now = time.time()
    for stream in query.loki(address("loki"), '{service_name=~".+"}', now - 300, now):
        assert isinstance(stream["stream"], dict)
        for value in stream["values"]:
            assert int(value[0]) > 0
            assert isinstance(value[1], str)


def test_trace_of_an_unknown_id_is_not_found(address):
    with pytest.raises(QueryError, match="HTTP 404"):
        query.trace(address("tempo"), "0" * 31 + "1")


def test_profiled_answers_service_names(address):
    now = time.time()
    names = query.profiled(address("pyroscope"), now - 300, now)
    assert all(isinstance(name, str) for name in names)
