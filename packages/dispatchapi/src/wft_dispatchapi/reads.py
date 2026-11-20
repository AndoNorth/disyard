"""The read routes: a job's status, a batch's summary and its pages of jobs, and the depth of the
rows that wait at accepted.

Every read runs on a connection of the reader login, the one group that reads event payloads, and
sees only the rows of the tenant in its path. The status read never fails because Temporal is
down: the workflow's answer is a detail beside the row, which stays the record.
"""

import json
import re
from collections.abc import Mapping
from typing import get_args

import yaml
from wft_events.models import UUID_PATTERN, ResultReference
from wft_eventstore import events_for_job
from wft_jobstate import get_job
from wft_keys import batch_id as batch_id_of
from wft_temporal import trace_id_of

from .models import (
    CONTRACT,
    PAGE_MAX,
    BatchSummary,
    DeadLetter,
    Depth,
    JobLine,
    JobPage,
    JobStatus,
    Status,
)
from .ports import NoWorkflow, TemporalUnavailable, WorkflowGateway

# Each view, with the action it is shown over and the statuses it is shown at.
VIEWS = {
    name: (rule["over"].split(".")[1], frozenset(rule["shown_while"]))
    for name, rule in yaml.safe_load((CONTRACT / "status.yaml").read_text())["views"].items()
}
# The statuses at which a job can have a workflow, so its status query is asked.
QUERIED = frozenset({"admitted", "running", "paused", "resume-pending"})
RESULT_READY = "dev.wft.task.result.ready.v1"
DEAD_LETTERED = "dev.wft.job.dead.lettered.v1"
PAGE_QUERY = frozenset({"status", "after", "limit"})


class ReadRefused(Exception):
    """A read answered with a reason code of the contract and no body of its own."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def view(status: str, requested_action: str | None) -> str | None:
    """The view that an outstanding action shows over the stored status, if any."""
    for name, (action, shown_while) in VIEWS.items():
        if requested_action == action and status in shown_while:
            return name
    return None


def job_status(conn, gateway: WorkflowGateway, tenant: str, job_id: str) -> JobStatus:
    row = get_job(conn, job_id) if re.fullmatch(UUID_PATTERN, job_id) else None
    if row is None or row.tenant != tenant:
        raise ReadRefused("JOB_NOT_FOUND", f"tenant {tenant} has no job {job_id}")
    events = events_for_job(conn, job_id)
    # The events come in time order and the sort is stable, so a task's results stay in time order.
    ready = sorted((e for e in events if e.type == RESULT_READY), key=lambda e: e.task_id)
    dead = [e for e in events if e.type == DEAD_LETTERED]
    detail = detail_error = None
    if row.status in QUERIED:
        try:
            detail = gateway.status(job_id)
        except NoWorkflow:
            detail_error = "no-workflow"
        except TemporalUnavailable:
            detail_error = "unavailable"
    return JobStatus(
        job_id=row.job_id,
        tenant=row.tenant,
        batch_id=row.batch_id,
        submission_key=row.submission_key,
        status=row.status,
        view=view(row.status, row.requested_action),
        requested_action=row.requested_action,
        control_seq=row.control_seq,
        control_delivered=row.control_delivered_at is not None,
        priority_class=row.priority_class,
        job_class=row.job_class,
        published=row.published_at is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
        trace_id=trace_id_of(row.traceparent),
        detail=detail,
        detail_error=detail_error,
        results=[ResultReference(**_data(e)["result"]) for e in ready],
        dead_letter=DeadLetter(**_data(dead[-1]), time=dead[-1].event_time) if dead else None,
    )


def batch_summary(conn, tenant: str, batch_key: str) -> BatchSummary:
    batch = batch_id_of(tenant, batch_key)
    counts = dict(
        conn.execute(
            "SELECT status, count(*) FROM accepted_job WHERE tenant = %s AND batch_id = %s"
            " GROUP BY status",
            (tenant, batch),
        ).fetchall()
    )
    if not counts:
        raise ReadRefused("BATCH_NOT_FOUND", f"tenant {tenant} has no batch {batch_key}")
    return BatchSummary(batch_id=batch, total=sum(counts.values()), counts=counts)


def job_page(conn, tenant: str, batch_key: str, query: Mapping[str, str]) -> JobPage:
    """One page of a batch's jobs in job id order, from the query string of the route."""
    status, after, limit = _page_query(query)
    batch = batch_id_of(tenant, batch_key)
    rows = conn.execute(
        "SELECT job_id::text, submission_key, status, requested_action FROM accepted_job"
        " WHERE tenant = %(tenant)s AND batch_id = %(batch)s"
        " AND (%(status)s::text IS NULL OR status = %(status)s)"
        " AND (%(after)s::uuid IS NULL OR job_id > %(after)s::uuid)"
        " ORDER BY job_id LIMIT %(more)s",
        {"tenant": tenant, "batch": batch, "status": status, "after": after, "more": limit + 1},
    ).fetchall()
    if not rows and not _batch_exists(conn, tenant, batch):
        raise ReadRefused("BATCH_NOT_FOUND", f"tenant {tenant} has no batch {batch_key}")
    jobs = [
        JobLine(job_id=job, submission_key=key, status=at, view=view(at, action))
        for job, key, at, action in rows[:limit]
    ]
    return JobPage(jobs=jobs, next=jobs[-1].job_id if len(rows) > limit else None)


def depth(conn, tenant: str) -> Depth:
    """The rows at accepted, keyed by priority class then job class."""
    # ponytail: the tenant's rows are read through the batch index and filtered on the status, an
    # index on (tenant, status) when #16 measures this read as slow.
    counts: dict[str, dict[str, int]] = {}
    for priority, job_class, count in conn.execute(
        "SELECT priority_class, job_class, count(*) FROM accepted_job"
        " WHERE tenant = %s AND status = 'accepted' GROUP BY priority_class, job_class",
        (tenant,),
    ):
        counts.setdefault(priority, {})[job_class] = count
    return Depth(depth=counts)


def _data(event) -> dict:
    return json.loads(event.body)["data"]


def _batch_exists(conn, tenant: str, batch: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM accepted_job WHERE tenant = %s AND batch_id = %s LIMIT 1", (tenant, batch)
    ).fetchone()
    return found is not None


def _page_query(query: Mapping[str, str]) -> tuple[str | None, str | None, int]:
    if unknown := set(query) - PAGE_QUERY:
        raise ReadRefused("INVALID_QUERY", f"unknown parameters {sorted(unknown)}")
    status, after, limit = query.get("status"), query.get("after"), query.get("limit")
    if status is not None and status not in get_args(Status):
        raise ReadRefused("INVALID_QUERY", f"status {status!r} is not a job status")
    if after is not None and not re.fullmatch(UUID_PATTERN, after):
        raise ReadRefused("INVALID_QUERY", f"after {after!r} is not a job id")
    if limit is None:
        return status, after, PAGE_MAX
    if not re.fullmatch("[0-9]+", limit) or not 1 <= int(limit) <= PAGE_MAX:
        raise ReadRefused("INVALID_QUERY", f"limit {limit!r} is not from 1 to {PAGE_MAX}")
    return status, after, int(limit)
