"""One decision is counted by pool and verdict on the metrics endpoint, an escalation by pool, and
every name is served before the first decision."""

import re
import socket
import threading
import urllib.request

import pytest
from wft_obs import metrics as obs_metrics
from wft_placement import service
from wft_placement.metrics import BUCKETS, Metrics
from wft_workflow.placement import PlacementRequest, ServicePlacer

SIMULATE = PlacementRequest(task_type="simulate", cpu_milli=250, memory_mib=512, run_timeout_s=900)
TOO_BIG = SIMULATE.model_copy(update={"memory_mib": 600})


class _Snapshotter:
    """A snapshotter whose last read, 3 s ago, found no pool capacity."""

    def current(self):
        return None, 3.0, None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def endpoint():
    """One runtime for the module, since its endpoint stays bound for the life of the process."""
    port = _free_port()
    runtime = obs_metrics.runtime(f"127.0.0.1:{port}", BUCKETS)
    # The endpoint closes with its runtime, so the runtime lives as long as the fixture.
    yield Metrics(runtime.metric_meter), f"http://127.0.0.1:{port}/metrics"
    del runtime


def _scrape(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode()


def _value(text: str, name: str, **labels: str) -> float:
    wanted = {f'{k}="{v}"' for k, v in labels.items()}
    for line in text.splitlines():
        match = re.fullmatch(rf"{name}\{{(.*)\}} (\S+)", line)
        if match and wanted <= set(match[1].split(",")):
            return float(match[2])
    raise AssertionError(f"{name}{sorted(wanted)} is not served:\n{text}")


def test_every_name_is_served_at_zero(endpoint):
    _, url = endpoint
    text = _scrape(url)
    assert _value(text, "wft_placement_decisions_total", pool="simulate", verdict="placed") == 0
    assert _value(text, "wft_placement_escalations_total", pool="compile") == 0


def test_one_decision_is_counted_by_pool_and_verdict(endpoint):
    metrics, url = endpoint
    served = service.Service(("127.0.0.1", 0), _Snapshotter(), metrics)
    threading.Thread(target=served.serve, daemon=True).start()
    before = _scrape(url)
    placer = ServicePlacer(served.url)
    placer.place(SIMULATE, escalate=True)
    placer.place(TOO_BIG)
    text = _scrape(url)

    def gained(name: str, **labels: str) -> float:
        return _value(text, name, **labels) - _value(before, name, **labels)

    assert gained("wft_placement_decisions_total", pool="simulate", verdict="placed") == 1
    assert gained("wft_placement_decisions_total", pool="simulate", verdict="unschedulable") == 1
    assert gained("wft_placement_escalations_total", pool="simulate") == 1
    assert gained("wft_placement_decisions_total", pool="compile", verdict="placed") == 0
    served.stop()
    assert "wft_placement_place_seconds_bucket" in text
    assert _value(text, "wft_placement_snapshot_age_seconds") == 3


def test_the_metrics_address_comes_from_the_environment():
    env = {"KUBERNETES_SERVICE_HOST": "10.96.0.1"}
    assert service.Config.from_env(env).metrics_address == "0.0.0.0:9464"
    env["WFT_METRICS_ADDRESS"] = "127.0.0.1:1"
    assert service.Config.from_env(env).metrics_address == "127.0.0.1:1"
