"""The claim of a job row from `accepted` to `admitted`, committed with its `job.admitted` event
before the workflow starts, and the accepted event rebuilt from the row for that start."""

from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import Connection
from psycopg.rows import dict_row
from wdf_events import Event, build_event, to_bytes
from wdf_eventstore import insert_events
from wdf_jobstate import JobRow, Lost, transition
from wdf_jobstate.jobs import READ_COLUMNS, status_vocabulary
from wdf_stages import Facts

from . import stage
from .start import params_of, search_attributes

# A message whose row is in neither case may still be admitted later, so it is retried and not
# acknowledged: the row may not be visible yet, or dispatch has not yet marked its receipt.
RETRIED = ("no-row", "no-receipt")


@dataclass(frozen=True)
class Claimed:
    """A row at `admitted` whose workflow is to be started with `accepted`."""

    row: JobRow
    accepted: Event


@dataclass(frozen=True)
class Skipped:
    """A row that admission does not start: `started`, `ended` or `outstanding-action`, which are
    acknowledged, or one of RETRIED."""

    reason: str


def accepted_of(row: JobRow) -> Event:
    """The accepted event of the row as dispatch built it, with the row's `created_at` as its time,
    so it equals the stored event in every field but the time."""
    return build_event(
        "job.accepted",
        source="/wdf/dispatch",
        tenant=row.tenant,
        jobid=row.job_id,
        priorityclass=row.priority_class,
        jobclass=row.job_class,
        traceparent=row.traceparent,
        tracestate=row.tracestate,
        time=row.created_at,
        data={
            "manifest": row.manifest_ref,
            "batch_id": row.batch_id,
            "submission_key": row.submission_key,
        },
    )


def _skipped(status: str) -> Skipped:
    if status == "running":
        return Skipped("started")
    if status in status_vocabulary().terminal:
        return Skipped("ended")
    return Skipped("outstanding-action")


def _start_arguments(row: JobRow) -> Event:
    """The accepted event of the row, after every start argument has been built from the row, so a
    row whose content the event schema refuses raises here, before it is claimed."""
    accepted = accepted_of(row)
    to_bytes(accepted)
    params_of(row)
    search_attributes(row)
    return accepted


def claim(conn: Connection, job_id: str) -> Claimed | Skipped:
    """Claims the row in one transaction, on a connection in autocommit mode.

    The row is locked before it is checked, so a control request that commits first wins and one
    that comes after waits for the claim. A row already at `admitted` was claimed by a run that did
    not start it, and is returned to be started with no new event.
    """
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {READ_COLUMNS} FROM accepted_job WHERE job_id = %s FOR UPDATE", (job_id,)
            )
            found = cur.fetchone()
        if found is None:
            return Skipped("no-row")
        row = JobRow(**found)
        if row.status == "admitted":
            return Claimed(row, _start_arguments(row))
        if row.status != "accepted" or row.requested_action is not None:
            return _skipped(row.status)
        if row.published_at is None:
            return Skipped("no-receipt")
        accepted = _start_arguments(row)
        moved = transition(conn, job_id, "accepted", "admitted")
        if isinstance(moved, Lost):
            return _skipped(moved.status)
        facts = Facts(time=datetime.now(UTC), emit=("job.admitted",))
        insert_events(conn, stage.admission(accepted, facts))
    return Claimed(row.model_copy(update={"status": "admitted"}), accepted)
