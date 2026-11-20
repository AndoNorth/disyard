"""The control routes: cancel, terminate, pause or resume one job, or every job of a batch.

Each job is decided and written in its own transaction on a connection of the control login, in
autocommit mode. The row is read with `lock_job`, the control table decides on it, and the request,
the moves, the pending patch and the events of that decision are written before the commit, so
admission's claim either committed first or waits and then finds the row moved. A job that never
started has no workflow, so control records its moves itself. Every other request waits for the
retrier to signal the workflow, or for admission to send a resume, and no signal is sent here.

Every job is scoped to the tenant of the path: a job or a batch of another tenant is not found.
"""

import re
from collections.abc import Iterator, Mapping
from datetime import timedelta
from typing import Any

from psycopg import Connection
from wft_events import Event, build_event
from wft_events.models import UUID_PATTERN
from wft_eventstore import insert_events
from wft_jobstate import (
    JobRow,
    Lost,
    get_job,
    lock_job,
    request_action,
    set_pending_patch,
    transition,
)
from wft_keys import batch_id as batch_id_of

from .control_table import ASK_GATEWAY, Decision, Patch, decide, view
from .models import (
    PAGE_MAX,
    Action,
    ControlAnswer,
    ControlRequest,
    ControlSummary,
    EntryError,
    SummaryLine,
)
from .ports import TemporalUnavailable, WorkflowGateway

SOURCE = "/wft/control"
# The jobs of a batch are read in pages that serve only the paging: each job is decided on its own
# lock, so a batch call never holds more than one row.
PAGE = PAGE_MAX


class ControlRefused(Exception):
    """A control call, or one job of a batch call, answered with a reason code of the contract and
    nothing written."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def control_job(
    conn: Connection,
    gateway: WorkflowGateway,
    tenant: str,
    job_id: str,
    action: Action,
    request: ControlRequest,
    patch: Mapping[str, Any] | None = None,
) -> ControlAnswer:
    """Applies `action` to one job and answers once its transaction has committed.

    `patch` is the stored manifest reference of the template that a patched resume sends, which the
    caller stores before this call. Raises ControlRefused.
    """
    if request.template is not None and action != "resume":
        raise ControlRefused("TEMPLATE_NOT_ALLOWED", f"a {action} takes no template")
    if request.template is not None and patch is None:
        raise ValueError("a patched resume stores its manifest first and passes it as patch")
    if not re.fullmatch(UUID_PATTERN, job_id):
        raise ControlRefused("JOB_NOT_FOUND", f"tenant {tenant} has no job {job_id}")
    with conn.transaction():
        row = lock_job(conn, job_id)
        if row is None or row.tenant != tenant:
            raise ControlRefused("JOB_NOT_FOUND", f"tenant {tenant} has no job {job_id}")
        return _apply(conn, gateway, row, action, request.reason, patch)


def control_batch(
    conn: Connection,
    gateway: WorkflowGateway,
    tenant: str,
    batch_key: str,
    action: Action,
    request: ControlRequest,
) -> Iterator[ControlAnswer | EntryError | SummaryLine]:
    """Applies `action` to every job of a batch in job id order, and yields each job's answer after
    its transaction has committed, then one summary line.

    Raises ControlRefused before the first line, for a template or a batch the tenant does not have.
    """
    if request.template is not None:
        raise ControlRefused("TEMPLATE_NOT_ALLOWED", "a batch control call takes no template")
    batch = batch_id_of(tenant, batch_key)
    first = _page(conn, tenant, batch, None)
    if not first:
        raise ControlRefused("BATCH_NOT_FOUND", f"tenant {tenant} has no batch {batch_key}")
    return _batch_lines(conn, gateway, tenant, batch, first, action, request)


def _batch_lines(conn, gateway, tenant, batch, page, action, request):
    counts = dict.fromkeys(ControlSummary.model_fields, 0)
    while page:
        for job_id, submission_key in page:
            try:
                answer = control_job(conn, gateway, tenant, job_id, action, request)
            except ControlRefused as refused:
                counts["refused"] += 1
                yield EntryError(
                    job_id=job_id,
                    submission_key=submission_key,
                    code=refused.code,
                    message=refused.message,
                )
                continue
            counts[answer.outcome] += 1
            yield answer
        page = _page(conn, tenant, batch, page[-1][0])
    yield SummaryLine(summary=ControlSummary(**counts))


def _page(conn: Connection, tenant: str, batch: str, after: str | None) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT job_id::text, submission_key FROM accepted_job"
        " WHERE tenant = %(tenant)s AND batch_id = %(batch)s"
        " AND (%(after)s::uuid IS NULL OR job_id > %(after)s::uuid)"
        " ORDER BY job_id LIMIT %(limit)s",
        {"tenant": tenant, "batch": batch, "after": after, "limit": PAGE},
    ).fetchall()


def _apply(
    conn: Connection,
    gateway: WorkflowGateway,
    row: JobRow,
    action: Action,
    reason: str | None,
    patch: Mapping[str, Any] | None,
) -> ControlAnswer:
    decision = decide(
        row.status, _has_workflow(gateway, row), row.requested_action, action, _patch_of(row, patch)
    )
    if decision.outcome == "refused":
        outstanding = f" with {row.requested_action} outstanding" if row.requested_action else ""
        raise ControlRefused(
            decision.refusal, f"a {action} of a job at {row.status}{outstanding} is refused"
        )
    _write(conn, row, action, decision, reason, patch)
    after = get_job(conn, row.job_id)
    return ControlAnswer(
        job_id=row.job_id,
        action=action,
        outcome=decision.outcome,
        status=after.status,
        view=view(after.status, after.requested_action),
        control_seq=after.control_seq,
    )


def _has_workflow(gateway: WorkflowGateway, row: JobRow) -> bool | None:
    """Whether the job has a workflow, where its status leaves it open, and None when Temporal does
    not answer. The status decides it everywhere else."""
    if row.status not in ASK_GATEWAY:
        return None
    try:
        return gateway.exists(row.job_id)
    except TemporalUnavailable:
        return None


def _patch_of(row: JobRow, patch: Mapping[str, Any] | None) -> Patch:
    if patch is None:
        return "none"
    pending = row.pending_patch_ref
    return "same" if pending is not None and pending["sha256"] == patch["sha256"] else "other"


def _write(conn, row, action, decision: Decision, reason, patch) -> None:
    """Writes what the decision names, in its order, inside the caller's transaction.

    The row is locked, so no guarded write can lose: one that does raises and rolls back.
    """
    seq = row.control_seq
    if decision.request:
        seq = request_action(conn, row.job_id, action).control_seq
    status = row.status
    for move in decision.moves:
        moved = transition(conn, row.job_id, status, move.to, row_only=move.row_only)
        if isinstance(moved, Lost):
            raise RuntimeError(f"the locked job {row.job_id} moved to {moved.status}")
        status = move.to
    if decision.set_patch:
        lost = set_pending_patch(conn, row.job_id, seq, None if patch is None else dict(patch))
        if isinstance(lost, Lost):
            raise RuntimeError(f"the locked job {row.job_id} lost its resume at {lost.status}")
    if decision.events:
        [now] = conn.execute("SELECT now()").fetchone()
        insert_events(conn, _events(row, decision.events, seq, reason, now))


def _events(row: JobRow, names, seq: int, reason: str | None, now) -> list[Event]:
    """The events of one decision, each with the request's sequence (so its id is unique within the
    control stage), and a microsecond apart in decision order, so the history orders a request
    before the move it caused."""
    return [
        build_event(
            name,
            source=SOURCE,
            tenant=row.tenant,
            jobid=row.job_id,
            priorityclass=row.priority_class,
            jobclass=row.job_class,
            traceparent=row.traceparent,
            tracestate=row.tracestate,
            time=now + timedelta(microseconds=offset),
            data={"reason": reason} if name.endswith(".requested") else {},
            sequence=seq,
        )
        for offset, name in enumerate(names)
    ]
