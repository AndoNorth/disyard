"""The negative job scenarios N1 and N2 of level 4: a job ends in the status its failures call for,
with a typed reason, and what the failure left behind is what the platform promises.

Each scenario takes its clients as arguments: `client` is the API of the tenant, and `events`
reads the event rows of a job (a direct read, listed in docs/acceptance-ladder.md). The verdicts are
plain functions of the final status and the events, so a fake can feed them.
"""

import json
import re
from collections.abc import Callable, Sequence

from dsy_workflow.harness import echo_params
from dsy_workflow.harness.cluster import TERMINAL

from ..clients import api
from ..fixtures import bundles, jobs
from . import wait

END_S = 180.0
# `<class>: <detail>`, the shape of every reason the platform writes.
TYPED = re.compile(r"^[A-Za-z][\w-]*: \S")
FAULT = {"workload": "fault", "seed": 7, "args": {"mode": "bad_block"}}


def name_of(type_: str) -> str:
    """`task.failed` from `dev.dsy.task.failed.v1`, or from itself."""
    return re.sub(r"\.v\d+$", "", type_.removeprefix("dev.dsy."))


def of(events: Sequence, type_: str, task: str | None = None) -> list[dict]:
    """The `data` of each event of the type, of the task when one is given, in order, with the
    row's task and attempt beside it."""
    found = []
    for row in events:
        if name_of(row.type) == type_ and (task is None or row.task_id == task):
            data = json.loads(row.body).get("data", {})
            found.append({**data, "task": row.task_id, "attempt": row.attempt})
    return found


def typed(reason: str | None, what: str) -> str:
    if not reason or not TYPED.match(reason):
        raise AssertionError(f"{what} has no typed reason, it says {reason!r}")
    return reason


def results_of(status: dict) -> set[str]:
    return {r["producer"]["task"] for r in status.get("results", [])}


def finished(events: Sequence, task: str) -> bool:
    return bool(of(events, "task.succeeded", task))


def ended_once(events: Sequence, task: str, reason_class: str) -> None:
    """The task failed on its first and only attempt with a reason of the class: no retry ran."""
    failed = of(events, "task.failed", task)
    if len(failed) != 1:
        raise AssertionError(f"{task} has {len(failed)} task.failed events, not one")
    if not typed(failed[0].get("reason"), f"{task} task.failed").startswith(f"{reason_class}:"):
        raise AssertionError(f"{task} failed as {failed[0]['reason']!r}, not {reason_class}")
    tries = of(events, "task.attempt.failed", task)
    if len(tries) != 1 or failed[0]["attempt"] != 1:
        raise AssertionError(
            f"{task} was retried: {len(tries)} failed attempts, "
            f"ended on attempt {failed[0]['attempt']}"
        )


def ran(events: Sequence, task: str) -> bool:
    return any(of(events, kind, task) for kind in ("task.started", "task.succeeded"))


def failed_job(status: dict, events: Sequence, what: str) -> str:
    """The job ended `failed` and its `job.failed` carries a typed reason, which is returned."""
    if status["status"] != "failed":
        raise AssertionError(f"{what} ended {status['status']}, not failed")
    ends = of(events, "job.failed")
    if len(ends) != 1:
        raise AssertionError(f"{what} has {len(ends)} job.failed events, not one")
    return typed(ends[0].get("reason"), f"{what} job.failed")


def check_failing_compile(status: dict, events: Sequence) -> None:
    """Chain b stops on its compile without a retry, chain a finishes and its result is kept, the
    aggregate does not run, and the job ends failed with a typed reason that names chain b."""
    reason = failed_job(status, events, "the job")
    ended_once(events, "b-build", "compile_failed")
    if "b-build" not in reason:
        raise AssertionError(f"the job's reason {reason!r} does not name b-build")
    for task in ("a-build", "a-sim"):
        if not finished(events, task):
            raise AssertionError(f"chain a did not finish: {task} has no task.succeeded")
    if "a-sim" not in results_of(status):
        raise AssertionError("the result of a-sim was not kept in the status")
    if ran(events, "agg") or "agg" in results_of(status):
        raise AssertionError("the aggregate ran after chain b failed")


def check_partial(status: dict, events: Sequence) -> None:
    """With `allow_partial` the aggregate runs over chain a alone and the job ends partial."""
    if status["status"] != "partial":
        raise AssertionError(f"the job ended {status['status']}, not partial")
    if of(events, "job.failed"):
        raise AssertionError("a partial job also wrote job.failed")
    ended_once(events, "b-build", "compile_failed")
    ends = of(events, "job.partial")
    if len(ends) != 1 or "b-build" not in ends[0].get("skipped_tasks", []):
        raise AssertionError(f"job.partial does not name b-build as skipped: {ends}")
    if not finished(events, "agg") or "agg" not in results_of(status):
        raise AssertionError("the aggregate did not run over chain a")
    if "a-sim" not in results_of(status):
        raise AssertionError("the result of a-sim was not kept in the status")


def check_malformed_block(status: dict, events: Sequence) -> None:
    """The simulate ends, the aggregate refuses its block as aggregate_parse_failed naming the
    simulate, once, and the job ends failed with a typed reason."""
    failed_job(status, events, "the job")
    if not finished(events, "sim"):
        raise AssertionError("the simulate did not finish, so the bad block was never written")
    ended_once(events, "agg", "aggregate_parse_failed")
    reason = of(events, "task.failed", "agg")[0]["reason"]
    if not reason.startswith("aggregate_parse_failed: sim"):
        raise AssertionError(f"the parse failure does not name its input sim: {reason!r}")


def run_to_end(client: api.Api, document: dict, batch: str, timeout_s: float) -> dict:
    """Submits one job under `batch` and returns its status once terminal, polled until the
    timeout, which names the last status seen."""
    job_id = client.submit_one(document, batch)
    last = {"status": "unread"}

    def ended() -> dict | None:
        last.update(client.status(job_id))
        return dict(last) if last["status"] in TERMINAL else None

    try:
        return wait(ended, timeout_s, f"job {job_id} to end")
    except TimeoutError:
        raise TimeoutError(
            f"job {job_id} did not end in {timeout_s:.0f} s, last {last['status']}"
        ) from None


def failing_compile(
    client: api.Api,
    events: Callable[[str], Sequence],
    fresh_batch: Callable[[], str],
    timeout_s: float = END_S,
) -> None:
    """N1: the job whose chain b fails its compile ends failed, and the same job with
    `allow_partial` ends partial. Each is its own job under its own batch key."""
    sources = (
        client.upload_source(bundles.variant("a")),
        client.upload_source(bundles.missing_entry()),
    )
    simulates = tuple(echo_params() | {"seed": seed} for seed in (11, 12))
    for allow_partial, check in ((False, check_failing_compile), (True, check_partial)):
        document = jobs.failing_second_chain(sources, simulates, allow_partial)
        status = run_to_end(client, document, fresh_batch(), timeout_s)
        check(status, events(status["job_id"]))


def malformed_block(
    client: api.Api,
    events: Callable[[str], Sequence],
    fresh_batch: Callable[[], str],
    timeout_s: float = END_S,
) -> None:
    """N2: a simulate through the `bad_block` fault mode, whose aggregate fails the parse."""
    source = client.upload_source(bundles.variant("a"))
    status = run_to_end(client, api.template(source, FAULT), fresh_batch(), timeout_s)
    check_malformed_block(status, events(status["job_id"]))
