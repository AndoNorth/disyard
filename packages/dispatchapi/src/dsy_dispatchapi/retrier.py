"""The control retrier and the resume finisher: the loops that carry a control request recorded on
the row to the workflow, or finish it on the row when the workflow is gone.

A control route records a request and commits, and only then can the signal be sent, so a request
can be left undelivered by a crash or a Temporal outage. The retrier signals every outstanding
`cancel`, `terminate` or `pause` whose delivery is not recorded, records the delivery once the
server accepted the signal, and keeps trying a row, backing off, while its action is outstanding. A
signal sent again after a crash before the delivery was recorded is a no-op for the workflow.
`resume` is never signalled: admission sends it.

A job at `paused` or `resume-pending` whose workflow Temporal no longer holds cannot take a signal,
so the retrier ends a cancel or terminate of it on the row, and the resume finisher returns a
resume of it to `accepted`, from where admission starts the job again.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from dsy_events import from_bytes
from dsy_eventstore import insert_events
from dsy_jobstate import JobRow, lock_job, mark_delivered, transition
from dsy_keys import cloudevent_id
from dsy_workflow.models import ControlSignal
from psycopg import Connection

from .control import SOURCE, _events
from .ports import NoWorkflow, TemporalUnavailable, WorkflowGateway

log = logging.getLogger(__name__)

# The cadence and the size of a delivery pass and of a finisher pass, and the backoff of a row
# whose signal failed (estimates).
DELIVER_EVERY_S = 1.0
FINISH_EVERY_S = 60.0
BATCH = 100
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 60.0
# The statuses at which a job can have a workflow, and the two of them that admission's claim never
# reaches, where the workflow may be gone.
WITH_WORKFLOW = ("admitted", "running", "paused", "resume-pending")
STOPPED = ("paused", "resume-pending")
ENDS = {"cancel": "cancelled", "terminate": "terminated"}

SELECT_UNDELIVERED = """
SELECT job_id::text, status, requested_action, control_seq FROM accepted_job
WHERE requested_action IN ('cancel', 'terminate', 'pause') AND control_delivered_at IS NULL
AND status = ANY(%(statuses)s) AND NOT job_id = ANY(%(waiting)s::uuid[])
ORDER BY job_id LIMIT %(limit)s
"""
SELECT_RESUME_PENDING = """
SELECT job_id::text, control_seq FROM accepted_job
WHERE requested_action = 'resume' AND status = 'resume-pending'
AND (%(after)s::uuid IS NULL OR job_id > %(after)s::uuid)
ORDER BY job_id LIMIT %(limit)s
"""
SELECT_EVENT = "SELECT body FROM event WHERE job_id = %s::uuid AND event_id = %s"


@dataclass
class Backoff:
    """When a row is tried next, and the delay that follows a failure then."""

    due: float
    delay: float


class Retrier:
    """Runs the delivery pass every DELIVER_EVERY_S and the finisher pass every FINISH_EVERY_S.

    Each pass opens its own connections, one of the control login and, to read the reason of a
    requested event, one of the reader login, so a pass after a database outage starts afresh.
    """

    def __init__(
        self,
        control_dsn: str,
        reader_dsn: str,
        gateway: WorkflowGateway,
        batch: int = BATCH,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.control_dsn = control_dsn
        self.reader_dsn = reader_dsn
        self.gateway = gateway
        self.batch = batch
        self.clock = clock
        self.backoff: dict[str, Backoff] = {}
        # The last job id the finisher read, from which its next pass reads on.
        self.after: str | None = None

    def run(self, stop: threading.Event) -> None:
        """Runs both passes until `stop` is set. A pass that fails is logged, and the next one runs
        on its cadence."""
        finish_due = self.clock()
        while not stop.is_set():
            _logged("delivery", self.deliver)
            if self.clock() >= finish_due:
                finish_due = self.clock() + FINISH_EVERY_S
                _logged("finisher", self.finish)
            stop.wait(DELIVER_EVERY_S)

    def deliver(self) -> int:
        """One delivery pass over up to `batch` rows that are not backing off, and returns how many
        requests it delivered or finished. The first Temporal outage ends the pass, since it fails
        every call."""
        now = self.clock()
        waiting = [job for job, wait in self.backoff.items() if wait.due > now]
        done = 0
        with (
            psycopg.connect(self.control_dsn, autocommit=True) as conn,
            psycopg.connect(self.reader_dsn, autocommit=True) as reader,
        ):
            rows = conn.execute(
                SELECT_UNDELIVERED,
                {
                    "statuses": list(WITH_WORKFLOW),
                    "waiting": waiting,
                    "limit": self.batch,
                },
            ).fetchall()
            for job_id, status, action, seq in rows:
                try:
                    done += self._deliver(conn, reader, job_id, status, action, seq)
                except NoWorkflow:
                    self._back_off(job_id)
                except TemporalUnavailable as error:
                    self._back_off(job_id)
                    log.warning("Temporal is unavailable, the pass ends: %s", error)
                    break
        # A row that is due but was not selected was delivered or ended elsewhere, or fell beyond
        # the batch, and drops its backoff, so the table of waiting rows stays bounded.
        selected = {row[0] for row in rows}
        self.backoff = {
            job: wait for job, wait in self.backoff.items() if wait.due > now or job in selected
        }
        return done

    def _deliver(self, conn, reader, job_id: str, status: str, action: str, seq: int) -> int:
        if status in STOPPED and action in ENDS and not self.gateway.exists(job_id):
            return self._end_on_row(conn, job_id, status, action, seq)
        reason = _reason(reader, job_id, action, seq)
        self.gateway.signal(job_id, action, ControlSignal(control_seq=seq, reason=reason))
        mark_delivered(conn, job_id, seq)
        self.backoff.pop(job_id, None)
        return 1

    def _back_off(self, job_id: str) -> None:
        last = self.backoff.get(job_id)
        delay = BACKOFF_FIRST_S if last is None else min(last.delay * 2, BACKOFF_MAX_S)
        self.backoff[job_id] = Backoff(due=self.clock() + delay, delay=delay)

    def _end_on_row(self, conn: Connection, job_id: str, status: str, action: str, seq: int) -> int:
        """Ends a stopped job whose workflow is gone by the move its cancel or terminate asks for,
        with its event, unless the row changed since it was read."""
        with conn.transaction():
            row = lock_job(conn, job_id)
            if not _still(row, status, action, seq):
                return 0
            transition(conn, job_id, status, ENDS[action])
            insert_events(conn, _events(row, [f"job.{ENDS[action]}"], seq, None, _now(conn)))
        self.backoff.pop(job_id, None)
        return 1

    def finish(self) -> int:
        """One finisher pass over up to `batch` rows at `resume-pending` with the resume
        outstanding, read in job id order from where the last pass stopped and from the first row
        again once the end is reached, and returns how many it returned to `accepted`. The first
        Temporal outage ends the pass, which the next one starts from the same row."""
        done = 0
        with psycopg.connect(self.control_dsn, autocommit=True) as conn:
            rows = conn.execute(
                SELECT_RESUME_PENDING, {"after": self.after, "limit": self.batch}
            ).fetchall()
            for job_id, seq in rows:
                try:
                    if not self.gateway.exists(job_id):
                        done += _back_to_accepted(conn, job_id, seq)
                except TemporalUnavailable as error:
                    log.warning("Temporal is unavailable, the finisher pass ends: %s", error)
                    return done
                self.after = job_id
        if len(rows) < self.batch:
            self.after = None
        return done


def _logged(name: str, step: Callable[[], int]) -> None:
    try:
        step()
    except Exception:
        log.exception("the %s pass failed", name)


def _back_to_accepted(conn: Connection, job_id: str, seq: int) -> int:
    """The row-only move of a resumed job whose workflow is gone back to `accepted`, with
    `job.resumed`, unless the row changed since it was read."""
    with conn.transaction():
        row = lock_job(conn, job_id)
        if not _still(row, "resume-pending", "resume", seq):
            return 0
        transition(conn, job_id, "resume-pending", "accepted", row_only=True)
        insert_events(conn, _events(row, ["job.resumed"], seq, None, _now(conn)))
    return 1


def _still(row: JobRow | None, status: str, action: str, seq: int) -> bool:
    return row is not None and (row.status, row.requested_action, row.control_seq) == (
        status,
        action,
        seq,
    )


def _now(conn: Connection):
    return conn.execute("SELECT now()").fetchone()[0]


def _reason(reader: Connection, job_id: str, action: str, seq: int) -> str | None:
    """The reason of the stored requested event of the request, or None once the history has
    dropped it."""
    name = f"job.{action}.requested"
    key = f"{SOURCE}#{cloudevent_id(job_id, '', '', name, seq)}"
    found = reader.execute(SELECT_EVENT, (job_id, key)).fetchone()
    return None if found is None else from_bytes(found[0].encode()).data.get("reason")
