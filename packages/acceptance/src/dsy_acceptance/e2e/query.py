"""Scenario H2, step 7 of the acceptance job: what the platform recorded about the job of H1.

Every check takes the answers of a client as arguments, so the verdicts run against canned answers
and the test of the scenario only wires the real clients in. The backends lag, so each part is
polled until it holds, and a failure names what was still missing.
"""

import base64
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from dsy_events.errors import EventError
from dsy_events.profile import check_event
from google.protobuf.json_format import MessageToDict

from ..clients import observability
from ..smoke.telemetry import COMPILE, LOGGED, TRACED, log_query, services_of
from . import wait
from .baseline import Baseline

RESULT_READY = "dev.dsy.task.result.ready.v1"
TASK_REQUESTED = "dev.dsy.task.requested.v1"
# The metric the completed job counter is read from, which has no tenant label.
COMPLETED = 'dsy_workflow_jobs_total{outcome="succeeded"}'
# How long each backend gets to show what the job produced.
SETTLE_S = 90.0
EVERY_S = 2.0
STATUS_SUCCEEDED = "succeeded"


@dataclass
class Job:
    """What H1 leaves for the query step."""

    job_id: str
    # The simulate and aggregate task ids, each of which must have a result ready event.
    results: tuple[str, ...]
    # Unix seconds when H1 began, so every line of the job is later.
    since: float
    # Whether H1's compiles ran on a compile worker, and not from the cache.
    compiled: bool
    # The counter of completed jobs, read before H1 started.
    completed: Baseline


def settled(gaps: Callable[[], list[str]], timeout_s: float, what: str) -> None:
    """Polls `gaps` until it is empty. Raises AssertionError that lists what was still missing."""
    last: list[str] = []

    def done() -> bool:
        nonlocal last
        last = gaps()
        return not last

    try:
        wait(done, timeout_s, what, EVERY_S)
    except TimeoutError:
        raise AssertionError(
            f"after {timeout_s:.0f} s, {what} still missing: {'; '.join(last)}"
        ) from None


def status_gaps(status: dict) -> list[str]:
    """What the status of the job lacks to show the finished job and its trace."""
    gaps = []
    if status.get("status") != STATUS_SUCCEEDED:
        gaps.append(f"status is {status.get('status')!r}, not {STATUS_SUCCEEDED!r}")
    if not status.get("trace_id"):
        gaps.append("the status has no trace id")
    return gaps


def check_event_data(body: str | dict, where: str) -> dict:
    """The event decoded and validated against the profile and its `dataschema`, else an
    AssertionError that names where it was read and why it failed."""
    try:
        event = json.loads(body) if isinstance(body, str) else body
        check_event(event)
    except (EventError, ValueError) as error:
        raise AssertionError(f"event in {where} is not a valid CloudEvent: {error}") from None
    return event


def stored_events(rows: Iterable[Any]) -> list[dict]:
    """The events of the job in Postgres, each validated."""
    return [check_event_data(row.body, f"event row {row.event_id}") for row in rows]


def ready_gaps(events: list[dict], wanted: Iterable[str]) -> list[str]:
    """The tasks among `wanted` with no result ready event."""
    have = {e.get("taskid") for e in events if e["type"] == RESULT_READY}
    return [f"no result ready event of {task}" for task in wanted if task not in have]


def requested_gaps(events: list[dict], wanted: Iterable[str]) -> list[str]:
    """The tasks among `wanted` whose request is not in the history. The history carries the input
    of each task activity as a task.requested event, and the result ready events only reach the
    event table."""
    have = {e.get("taskid") for e in events if e["type"] == TASK_REQUESTED}
    return [f"no task request of {task}" for task in wanted if task not in have]


def _payloads(node: Any) -> Iterable[bytes]:
    """The raw data of every Temporal payload inside a history event."""
    if isinstance(node, dict):
        if "data" in node and "metadata" in node:
            try:
                yield base64.b64decode(node["data"])
            except ValueError:
                return
            return
        for value in node.values():
            yield from _payloads(value)
    elif isinstance(node, list):
        for value in node:
            yield from _payloads(value)


def _events_in(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        if "specversion" in node:
            yield node
            return
        for value in node.values():
            yield from _events_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _events_in(value)
    elif isinstance(node, str) and node.lstrip().startswith("{"):
        try:
            yield from _events_in(json.loads(node))
        except ValueError:
            return


def history_events(history: Iterable[Any]) -> list[dict]:
    """The CloudEvents carried by the payloads of the workflow history, each validated."""
    found = []
    for position, event in enumerate(history, 1):
        for data in _payloads(MessageToDict(event)):
            try:
                decoded = json.loads(data)
            except ValueError:
                continue
            for carried in _events_in(decoded):
                found.append(check_event_data(carried, f"history event {position}"))
    return found


def trace_gaps(tempo: str, trace_id: str | None) -> list[str]:
    """The traced services with no span in the job's trace."""
    if not trace_id:
        return [f"no span of {service}, the job has no trace id" for service in TRACED]
    try:
        found = services_of(observability.trace(tempo, trace_id))
    except observability.QueryError as error:
        return [f"no span of {service} in trace {trace_id}: {error}" for service in TRACED]
    return [f"no span of {s} in trace {trace_id}" for s in TRACED if s not in found]


def log_gaps(loki: str, job: Job, now: float) -> list[str]:
    """The components with no log line for the job."""
    gaps = []
    for component in (*LOGGED, *([COMPILE] if job.compiled else [])):
        try:
            streams = observability.loki(loki, log_query(component, job.job_id), job.since, now)
        except observability.QueryError as error:
            gaps.append(f"no log line of {component}: {error}")
            continue
        if not any(stream["values"] for stream in streams):
            gaps.append(f"no log line of {component}")
    return gaps


def run(
    job: Job,
    status: Callable[[], dict],
    rows: Callable[[], list[Any]],
    history: Callable[[], list[Any]],
    tempo: str,
    loki: str,
) -> None:
    """Asserts every part of H2 for the job of H1, raising AssertionError at the first that does
    not hold within its timeout."""
    seen: dict = {}

    def read_status() -> list[str]:
        seen.update(status())
        return status_gaps(seen)

    settled(read_status, SETTLE_S, "the job's status")
    job.completed.rose_by(1, SETTLE_S)
    settled(lambda: ready_gaps(stored_events(rows()), job.results), SETTLE_S, "events in Postgres")
    settled(
        lambda: requested_gaps(history_events(history()), job.results),
        SETTLE_S,
        "task requests in history",
    )
    settled(lambda: trace_gaps(tempo, seen["trace_id"]), SETTLE_S, "spans of the job's trace")
    settled(lambda: log_gaps(loki, job, time.time()), SETTLE_S, "log lines of the job")
