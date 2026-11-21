"""The row snapshot of one cycle and the two re-admission scans.

The snapshot reads the counts the policy takes from the job rows, so a restart needs no recovery
and a paused job frees its slot the moment its row says so. Every tenth cycle it also reads the two
kinds of row that no intake message will admit: a job resumed before it started, whose message is
gone, and a row left at `admitted` with no workflow by a crash, an outage or a failed start.
"""

import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from psycopg import Connection, OperationalError
from psycopg.rows import dict_row
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from wft_jobstate import JobRow
from wft_jobstate.jobs import READ_COLUMNS
from wft_keys import workflow_id

from . import policy
from .claim import Claimed, claim
from .start import RPC_TIMEOUT, start

LIVE = ("admitted", "running")
# Above the start call's timeout, so a start still in flight is never taken for a stuck row.
STUCK_AGE = timedelta(seconds=60)
DESCRIBES_PER_SCAN = 50
# How long a workflow found for a stuck row keeps that row out of the scan. The id is never reused
# while Temporal keeps the workflow, so the answer stays true at least that long.
REMEMBER_S = 3600.0
BACKOFF_FIRST_S = 60.0
BACKOFF_LAST_S = 3600.0
# The re-admissions a scan reads per tenant and priority class, oldest first, which is more than the
# policy grants one tenant before the next scan reads them again.
READMISSIONS_PER_TENANT = 50
OUTAGE = (
    RPCStatusCode.UNAVAILABLE,
    RPCStatusCode.DEADLINE_EXCEEDED,
    RPCStatusCode.RESOURCE_EXHAUSTED,
)

COUNTS = (
    "SELECT status, tenant, job_class, count(*) AS n FROM accepted_job"
    " WHERE status IN ('admitted', 'running', 'paused', 'resume-pending')"
    " GROUP BY status, tenant, job_class"
)
RESUMES = (
    f"SELECT {READ_COLUMNS} FROM accepted_job"
    " WHERE status = 'resume-pending' AND requested_action = 'resume' ORDER BY updated_at"
)
# Only the row-only path through `resume-pending` back to `accepted` leaves a published row at
# `accepted` with a control sequence and no outstanding action.
READMISSIONS = (
    "SELECT job_id::text, tenant, priority_class, job_class, updated_at FROM ("
    " SELECT *, row_number() OVER (PARTITION BY tenant, priority_class ORDER BY updated_at) AS n"
    " FROM accepted_job WHERE status = 'accepted' AND published_at IS NOT NULL"
    " AND control_seq > 0 AND requested_action IS NULL"
    ") AS waiting WHERE n <= %(limit)s ORDER BY updated_at"
)
STUCK = (
    f"SELECT {READ_COLUMNS} FROM accepted_job"
    " WHERE status = 'admitted' AND updated_at < now() - %(age)s"
    " AND job_id <> ALL(%(skip)s::uuid[]) ORDER BY updated_at LIMIT %(limit)s"
)


@dataclass(frozen=True)
class Rows:
    """What one cycle reads from the job rows."""

    # Rows in `admitted` or `running`, by tenant and job class.
    live: dict[tuple[str, str], int]
    paused: dict[str, int]
    resume_pending: int
    # Rows in `resume-pending` whose resume is requested, oldest first.
    resumes: list[JobRow]
    # The scans, empty on a cycle that does not run them.
    readmissions: list[policy.Readmission]
    stuck: list[JobRow]

    def counts(self, granted: dict[tuple[str, str], int] | None = None) -> policy.Snapshot:
        """The policy's view, with the resumes granted and still `resume-pending` held as live."""
        opened = sum(self.live.values()) + sum(self.paused.values()) + self.resume_pending
        return policy.Snapshot(live=self.live, open=opened, granted=granted or {})


@dataclass(frozen=True)
class Restarted:
    """What the scan did with a stuck row: `started` or `duplicate` once its workflow is started,
    `left` when one runs, `stale-admitted` when one is closed, `failed` when the start failed, or
    the skip reason of a row that moved on since the snapshot."""

    job_id: str
    outcome: str


async def readmit(conn: Connection, client: Client, job_id: str, prefix: str = "") -> str:
    """Admits a granted re-admission as a message would: the claim, then the start. A claim that
    skips returns its reason, and a start that fails leaves the row at `admitted` for the scan."""
    claimed = claim(conn, job_id)
    if not isinstance(claimed, Claimed):
        return claimed.reason
    return await start(client, claimed.row, claimed.accepted, prefix)


def _outage(error: BaseException) -> bool:
    return isinstance(error, OperationalError) or (
        isinstance(error, RPCError) and error.status in OUTAGE
    )


class Scanner:
    """Reads the snapshot, and restarts the stuck rows it finds, remembering which rows have a
    workflow and which failed to start so neither fills the next scans."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        # Each job id kept out of the stuck scan, with the clock time it may come back.
        self._until: dict[str, float] = {}
        self._failures: Counter[str] = Counter()

    def _kept_out(self) -> list[str]:
        now = self._clock()
        for job_id in [j for j, until in self._until.items() if until <= now]:
            del self._until[job_id]
        return list(self._until)

    def snapshot(self, conn: Connection, scans: bool = False) -> Rows:
        """Reads the cycle's rows in one repeatable read, so a row that moves between the
        statements is counted once. `scans` also reads the two scan predicates."""
        live: Counter[tuple[str, str]] = Counter()
        paused: Counter[str] = Counter()
        resume_pending = 0
        readmissions: list[policy.Readmission] = []
        stuck: list[JobRow] = []
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            for row in cur.execute(COUNTS):
                if row["status"] in LIVE:
                    live[(row["tenant"], row["job_class"])] += row["n"]
                elif row["status"] == "paused":
                    paused[row["job_class"]] += row["n"]
                else:
                    resume_pending += row["n"]
            resumes = [JobRow(**row) for row in cur.execute(RESUMES)]
            if scans:
                readmissions = [
                    policy.Readmission(**row)
                    for row in cur.execute(READMISSIONS, {"limit": READMISSIONS_PER_TENANT})
                ]
                stuck = [
                    JobRow(**row)
                    for row in cur.execute(
                        STUCK,
                        {"age": STUCK_AGE, "skip": self._kept_out(), "limit": DESCRIBES_PER_SCAN},
                    )
                ]
        return Rows(dict(live), dict(paused), resume_pending, resumes, readmissions, stuck)

    async def restart(
        self, conn: Connection, client: Client, stuck: Sequence[JobRow], prefix: str = ""
    ) -> list[Restarted]:
        """Starts each stuck row whose workflow does not exist, with no grant, because its
        `admitted` status already holds its slot.

        An outage of Temporal or the database raises, for the breaker, and leaves the remaining
        rows to a later scan. Any other failure of one row backs that row off.
        """
        done = []
        for row in stuck:
            try:
                outcome = await self._restart(conn, client, row, prefix)
            except Exception as error:
                if _outage(error):
                    raise
                self._back_off(row.job_id)
                outcome = "failed"
            done.append(Restarted(row.job_id, outcome))
        return done

    async def _restart(self, conn: Connection, client: Client, row: JobRow, prefix: str) -> str:
        try:
            found = await client.get_workflow_handle(workflow_id(row.job_id)).describe(
                rpc_timeout=RPC_TIMEOUT
            )
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
            outcome = await readmit(conn, client, row.job_id, prefix)
        else:
            # Only a terminate from outside the workflow's code closes it with its row at admitted.
            running = found.status == WorkflowExecutionStatus.RUNNING
            outcome = "left" if running else "stale-admitted"
        self._failures.pop(row.job_id, None)
        self._until[row.job_id] = self._clock() + REMEMBER_S
        return outcome

    def _back_off(self, job_id: str) -> None:
        delay = min(BACKOFF_FIRST_S * 2 ** self._failures[job_id], BACKOFF_LAST_S)
        self._failures[job_id] += 1
        self._until[job_id] = self._clock() + delay
