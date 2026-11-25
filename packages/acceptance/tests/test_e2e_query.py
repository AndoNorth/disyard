"""The verdicts of scenario H2 against canned answers: no cluster, and the real backends' lag is
a few polls of a fake."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from dsy_acceptance.e2e import query
from dsy_acceptance.e2e.baseline import Baseline
from dsy_acceptance.smoke.telemetry import COMPILE, LOGGED, TRACED, log_query
from dsy_obs.testing.fake import FakeObs
from temporalio.api.common.v1 import Payload, Payloads
from temporalio.api.history.v1 import HistoryEvent

JOB = "3a9617fa-df7c-5760-a644-42d559e0f433"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
RESULTS = ("a-sim", "b-sim", "agg")
VECTORS = Path(__file__).parents[3] / "contracts/events/vectors/golden"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(query, "SETTLE_S", 0.2)
    monkeypatch.setattr(query, "EVERY_S", 0.02)


def golden(name: str) -> dict:
    return json.loads((VECTORS / name / "1.0.json").read_text())


def ready(task: str) -> dict:
    event = deepcopy(golden("task.result.ready"))
    event["taskid"], event["subject"] = task, f"{JOB}/{task}"
    event["id"] = f"ready-{task}"
    return event


def row(event: dict) -> SimpleNamespace:
    return SimpleNamespace(event_id=f"{event['source']}#{event['id']}", body=json.dumps(event))


def history_event(carried: dict) -> HistoryEvent:
    """A history event whose activity input is a JSON payload holding the event."""
    payload = Payload(metadata={"encoding": b"json/plain"}, data=json.dumps([carried]).encode())
    event = HistoryEvent()
    event.activity_task_scheduled_event_attributes.input.CopyFrom(Payloads(payloads=[payload]))
    return event


STORED = [golden("job.started"), *map(ready, RESULTS)]


def span_trace(services) -> dict:
    return {
        "batches": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": s}}]},
                "scopeSpans": [{"spans": [{"name": "x"}]}],
            }
            for s in services
        ]
    }


def lines(components) -> dict:
    return {log_query(c, JOB): [{"stream": {}, "values": [["1", "a line"]]}] for c in components}


@pytest.fixture
def obs():
    with FakeObs(traces={TRACE: span_trace(TRACED)}, streams=lines((*LOGGED, COMPILE))) as fake:
        yield fake


def counter(*values: float) -> Baseline:
    """A baseline of 5 whose later reads are `values`, the last repeated."""
    reads = iter([5, *values])
    last = [5.0]

    def read() -> float:
        last[0] = next(reads, last[0])
        return last[0]

    taken = Baseline("completed", read)
    taken.take()
    return taken


def job(completed: Baseline | None = None) -> query.Job:
    return query.Job(JOB, RESULTS, 0.0, True, completed or counter(6))


def drive(obs, scenario=None, stored=STORED, carried=STORED, status=None):
    query.run(
        scenario or job(),
        status=lambda: status or {"status": "succeeded", "trace_id": TRACE},
        rows=lambda: [row(e) for e in stored],
        history=lambda: [history_event(e) for e in carried],
        tempo=obs.url,
        loki=obs.url,
    )


def test_a_healthy_job_passes(obs):
    drive(obs)


def test_a_counter_that_rose_by_two_means_the_baseline_was_not_quiet(obs):
    with pytest.raises(AssertionError, match="completed rose by 2, above the 1 asked"):
        drive(obs, job(counter(7)))


def test_a_counter_that_did_not_rise_fails_with_the_change_seen(obs):
    with pytest.raises(AssertionError, match="completed rose by 0 in 0 s, not 1"):
        drive(obs, job(counter(5)))


def test_an_event_that_fails_its_schema_names_the_row(obs):
    broken = deepcopy(STORED[1])
    broken["data"]["result"]["size"] = "big"
    with pytest.raises(AssertionError, match=r"event row .* is not a valid CloudEvent: bad-data"):
        drive(obs, stored=[STORED[0], broken, *STORED[2:]])


def test_an_event_in_the_history_that_fails_its_schema_names_the_position(obs):
    broken = deepcopy(STORED[1])
    broken["dataschema"] = "urn:dsy:schema:job.started:1.0"
    with pytest.raises(AssertionError, match="event in history event 2 is not a valid CloudEvent"):
        drive(obs, carried=[STORED[0], broken])


def test_a_missing_result_ready_event_is_named(obs):
    with pytest.raises(AssertionError, match="events in Postgres still missing: no result ready"):
        drive(obs, stored=[e for e in STORED if e.get("taskid") != "agg"])


def test_a_result_ready_event_missing_from_the_history_is_named(obs):
    with pytest.raises(AssertionError, match="events in history still missing: no result ready"):
        drive(obs, carried=STORED[:2])


def test_a_trace_without_the_dispatch_span_is_named(obs):
    obs.traces[TRACE] = span_trace(["admission", "workflow"])
    with pytest.raises(AssertionError, match=f"no span of dispatch in trace {TRACE}"):
        drive(obs)


def test_an_unknown_trace_names_every_traced_service(obs):
    del obs.traces[TRACE]
    with pytest.raises(AssertionError) as caught:
        drive(obs)
    assert all(f"no span of {s}" in str(caught.value) for s in TRACED)


def test_a_component_without_a_log_line_is_named(obs):
    del obs.streams[log_query("admission", JOB)]
    with pytest.raises(AssertionError, match="log lines of the job still missing: no log line of "):
        drive(obs)


def test_the_compile_worker_is_not_asked_when_the_compile_hit_the_cache(obs):
    del obs.streams[log_query(COMPILE, JOB)]
    drive(obs, SimpleNamespace(**{**job().__dict__, "compiled": False}))


def test_a_job_that_is_not_succeeded_is_named(obs):
    with pytest.raises(AssertionError, match="status is 'running', not 'succeeded'"):
        drive(obs, status={"status": "running", "trace_id": TRACE})


def test_a_lagging_backend_is_polled_until_it_shows_the_job(obs):
    asked = iter([[], [], STORED])
    query.run(
        job(),
        status=lambda: {"status": "succeeded", "trace_id": TRACE},
        rows=lambda: [row(e) for e in next(asked, STORED)],
        history=lambda: [history_event(e) for e in STORED],
        tempo=obs.url,
        loki=obs.url,
    )
