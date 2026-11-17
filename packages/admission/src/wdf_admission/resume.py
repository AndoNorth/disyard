"""The resume grants: a paused job whose resume control requested waits at the front of its priority
class, and once granted holds its slot from the `resume` signal until its row leaves
`resume-pending`.

Which resumes were granted is held in memory and never written, so after a restart it is recovered
from the workflows' `status` queries, and a signal that may be lost is sent again only when the
query shows the row's `control_seq` unapplied.
"""

import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import class_row
from temporalio.client import Client, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode
from wdf_keys import workflow_id
from wdf_workflow.contract import naming
from wdf_workflow.models import ResumeSignal, StatusQuery

from .policy import Resume
from .start import RPC_TIMEOUT

# How often a granted resume's workflow is asked whether it applied the signal, an estimate.
RECHECK_S = 30.0
SIGNAL = "resume"


@dataclass(frozen=True)
class Pending:
    """A row in `resume-pending`, whose resume is waiting when `requested_action` is `resume`."""

    job_id: str
    tenant: str
    priority_class: str
    job_class: str
    requested_action: str | None
    control_seq: int
    pending_patch_ref: dict[str, Any] | None
    updated_at: datetime


def pending(conn: Connection) -> list[Pending]:
    """Every row in `resume-pending`, oldest first."""
    with conn.cursor(row_factory=class_row(Pending)) as cur:
        cur.execute(
            "SELECT job_id::text AS job_id, tenant, priority_class, job_class, requested_action,"
            " control_seq, pending_patch_ref, updated_at FROM accepted_job"
            " WHERE status = 'resume-pending' ORDER BY updated_at"
        )
        return cur.fetchall()


@dataclass
class _Granted:
    row: Pending
    # When the workflow was last signalled or asked, on the clock of Resumes.
    checked: float


class Resumes:
    """The granted resumes of one admission process, by job id.

    A workflow that does not exist, which only a job that never started can lack, frees its slot,
    is counted in `no_workflow` and is not signalled again until its row's `control_seq` changes,
    since moving such a row back to `accepted` is control's.
    """

    def __init__(
        self,
        client: Client,
        recheck_s: float = RECHECK_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.recheck_s = recheck_s
        self.clock = clock
        self.granted: dict[str, _Granted] = {}
        self.rows: dict[str, Pending] = {}
        # The control_seq each job had when its workflow was found missing.
        self.missing: dict[str, int] = {}
        self.no_workflow = 0

    def observe(self, rows: Iterable[Pending]) -> list[Resume]:
        """Takes the rows of this cycle, frees the slot of each granted job whose row left
        `resume-pending`, and returns the resumes that wait for a grant."""
        self.rows = {row.job_id: row for row in rows}
        for job_id in self.granted.keys() - self.rows.keys():
            del self.granted[job_id]
        self.missing = {
            job_id: seq
            for job_id, seq in self.missing.items()
            if job_id in self.rows and self.rows[job_id].control_seq == seq
        }
        return [
            Resume(row.job_id, row.priority_class, row.tenant, row.job_class, row.updated_at)
            for row in self.rows.values()
            if row.requested_action == "resume"
            and row.job_id not in self.granted
            and row.job_id not in self.missing
        ]

    def slots(self) -> Counter[tuple[str, str]]:
        """The granted resumes by tenant and job class, which the policy counts as live."""
        return Counter((g.row.tenant, g.row.job_class) for g in self.granted.values())

    async def recover(self, rows: Iterable[Pending]) -> None:
        """Counts as granted, after a restart, each waiting resume that its workflow applied or
        whose workflow does not answer, so a restart never takes the live count past a cap."""
        for row in rows:
            if row.requested_action != "resume":
                continue
            try:
                applied = (await self._status(row)).control_seq >= row.control_seq
            except RPCError as error:
                if error.status == RPCStatusCode.NOT_FOUND:
                    self._missing(row)
                    continue
                applied = True
            except WorkflowQueryFailedError:
                applied = True
            if applied:
                self.granted[row.job_id] = _Granted(row, self.clock())

    async def grant(self, resume: Resume) -> bool:
        """Holds the slot of a waiting resume and signals its workflow, and returns whether the
        workflow exists.

        Any other failure of the signal is raised with the slot still held, and the recheck sends
        it again once the query shows it unapplied.
        """
        row = self.rows[resume.job_id]
        self.granted[row.job_id] = _Granted(row, self.clock())
        return await self._send(row)

    async def recheck(self) -> None:
        """Asks each granted resume's workflow, at most once per `recheck_s`, whether it applied
        the row's `control_seq`, and signals it again when it did not."""
        now = self.clock()
        for granted in list(self.granted.values()):
            if now - granted.checked < self.recheck_s:
                continue
            granted.checked = now
            row = granted.row
            try:
                applied = (await self._status(row)).control_seq >= row.control_seq
            except RPCError as error:
                if error.status == RPCStatusCode.NOT_FOUND:
                    self._missing(row)
                continue
            except WorkflowQueryFailedError:
                continue
            if not applied:
                await self._send(row)

    async def _send(self, row: Pending) -> bool:
        try:
            await self._handle(row).signal(
                SIGNAL,
                ResumeSignal(control_seq=row.control_seq, manifest=row.pending_patch_ref),
                rpc_timeout=RPC_TIMEOUT,
            )
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
            self._missing(row)
            return False
        return True

    async def _status(self, row: Pending) -> StatusQuery:
        return await self._handle(row).query(
            naming().queries[0], result_type=StatusQuery, rpc_timeout=RPC_TIMEOUT
        )

    def _handle(self, row: Pending):
        return self.client.get_workflow_handle(workflow_id(row.job_id))

    def _missing(self, row: Pending) -> None:
        self.granted.pop(row.job_id, None)
        self.missing[row.job_id] = row.control_seq
        self.no_workflow += 1
