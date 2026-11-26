"""The runtime's endpoint serves the event registry through the bridge, with the catalogue's
buckets."""

import re
import urllib.request
from pathlib import Path

import pytest
import yaml
from dsy_events import Event, to_bytes
from dsy_events.metrics import registry
from dsy_events.profile import CONTRACT_DIR
from dsy_obs import metrics
from dsy_servicekit.testing import free_port

ROOT = Path(__file__).resolve().parents[4]
CATALOGUE = yaml.safe_load((ROOT / "contracts/instrumentation/metrics.yaml").read_text())
EVENT = CONTRACT_DIR / "vectors/accepted/valid/unknown-minor.json"
OWN = {"dsy_test_wait_seconds": [0.5, 2.0]}


@pytest.fixture(scope="module")
def served():
    """One runtime for the module, since its endpoint stays bound for the life of the process,
    with the bridge removed again so no other test's registry calls reach it."""
    port = free_port()
    runtime = metrics.runtime(f"127.0.0.1:{port}", OWN)
    before = list(registry.listeners)
    metrics.export_registry(runtime.metric_meter, "dispatch")
    yield runtime, f"http://127.0.0.1:{port}/metrics"
    registry.listeners[:] = before


def _scrape(url: str) -> str:
    return urllib.request.urlopen(url, timeout=5).read().decode()


def _bounds(text: str, name: str) -> list[float]:
    found = re.findall(rf'^{name}_bucket{{[^}}]*le="([^"]+)"', text, re.MULTILINE)
    return [float(bound) for bound in found if bound != "+Inf"]


def test_the_endpoint_serves_a_registry_counter(served):
    _, url = served

    registry.inc("dsy_event_rejected_total", reason="schema-invalid")
    registry.inc("dsy_event_rejected_total", reason="schema-invalid")

    assert re.search(
        r'^dsy_event_rejected_total{[^}]*reason="schema-invalid"[^}]*} 2$',
        _scrape(url),
        re.MULTILINE,
    )


def test_the_endpoint_serves_event_bytes_after_to_bytes_with_the_catalogues_buckets(served):
    _, url = served

    encoded = to_bytes(Event.model_validate_json(EVENT.read_text()))

    text = _scrape(url)
    assert re.search(r"^dsy_event_bytes_count(?:{[^}]*})? 1$", text, re.MULTILINE)
    assert re.search(rf"^dsy_event_bytes_sum(?:{{[^}}]*}})? {len(encoded)}$", text, re.MULTILINE)
    assert _bounds(text, "dsy_event_bytes") == CATALOGUE["metrics"]["dsy_event_bytes"]["buckets"]


def test_every_registry_histogram_takes_the_catalogues_buckets_and_an_emitter_its_own(served):
    runtime, url = served
    runtime.metric_meter.create_histogram_float("dsy_test_wait_seconds").record(1.0)
    for name in registry.histograms:
        if name != "dsy_event_bytes":
            registry.observe(name, 1)

    text = _scrape(url)
    for name, histogram in registry.histograms.items():
        assert _bounds(text, name) == CATALOGUE["metrics"][name]["buckets"] == histogram.bounds
    assert _bounds(text, "dsy_test_wait_seconds") == OWN["dsy_test_wait_seconds"]


@pytest.mark.parametrize(
    "component", CATALOGUE["metrics"]["dsy_obs_log_dropped_total"]["labels"]["component"]["values"]
)
def test_the_drop_counters_are_served_at_zero_for_each_component_before_any_drop(served, component):
    runtime, url = served
    before = list(registry.listeners)
    metrics.export_registry(runtime.metric_meter, component)
    registry.listeners[:] = before

    text = _scrape(url)
    for name in metrics.DROP_COUNTERS:
        assert re.search(rf'^{name}{{[^}}]*component="{component}"[^}}]*}} 0$', text, re.MULTILINE)


def test_a_drop_increments_the_served_drop_counter(served):
    _, url = served

    registry.inc("dsy_obs_span_dropped_total", component="dispatch")

    assert re.search(
        r'^dsy_obs_span_dropped_total{[^}]*component="dispatch"[^}]*} 1$',
        _scrape(url),
        re.MULTILINE,
    )
