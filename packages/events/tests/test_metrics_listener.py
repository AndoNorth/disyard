"""The listener hook of the metrics registry."""

import pytest
from dsy_events import Event, to_bytes
from dsy_events.metrics import Registry, registry
from dsy_events.profile import CONTRACT_DIR

ACCEPTED = CONTRACT_DIR / "vectors/accepted/valid"


def test_a_listener_receives_each_call_once():
    metrics = Registry()
    calls = []
    metrics.listeners.append(lambda *call: calls.append(call))

    metrics.inc("dsy_event_rejected_total", reason="schema-invalid")
    metrics.inc("dsy_event_size_warnings_total")
    metrics.observe("dsy_event_bytes", 300)

    assert calls == [
        ("dsy_event_rejected_total", 1, {"reason": "schema-invalid"}),
        ("dsy_event_size_warnings_total", 1, {}),
        ("dsy_event_bytes", 300, {}),
    ]


def test_every_listener_hears_a_call_and_the_registry_still_records_it():
    metrics = Registry()
    first, second = [], []
    metrics.listeners += [lambda *call: first.append(call), lambda *call: second.append(call)]

    metrics.inc("dsy_event_size_warnings_total")
    metrics.observe("dsy_event_bytes", 2048)

    assert (
        first == second == [("dsy_event_size_warnings_total", 1, {}), ("dsy_event_bytes", 2048, {})]
    )
    assert metrics.count("dsy_event_size_warnings_total") == 1
    assert metrics.histograms["dsy_event_bytes"].count == 1


def test_a_refused_call_reaches_no_listener():
    metrics = Registry()
    calls = []
    metrics.listeners.append(lambda *call: calls.append(call))

    with pytest.raises(ValueError):
        metrics.inc("dsy_event_rejected_total")

    assert calls == []


def test_the_shared_registry_tells_a_listener_of_a_serialised_event(monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "listeners", [lambda *call: calls.append(call)])
    event = Event.model_validate_json((ACCEPTED / "unknown-minor.json").read_text())

    encoded = to_bytes(event)

    assert calls == [("dsy_event_bytes", len(encoded), {})]
