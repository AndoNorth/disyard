"""The row snapshot and the two re-admission scans, over admission's login and a dev server."""

import asyncio
import statistics
import threading
import time
from datetime import timedelta

import pytest
from dsy_admission import policy
from dsy_admission.claim import Claimed, claim
from dsy_admission.scan import Restarted, Scanner, readmit
from dsy_admission.start import start
from dsy_jobstate import insert_accepted, transition
from dsy_keys import workflow_id
from dsy_stateops import bench
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

pytestmark = [pytest.mark.integration, pytest.mark.pair]

LIMIT_S = 120
STUCK = timedelta(seconds=120)
# Past the namespace's 24 h retention, after which Temporal has purged any earlier workflow.
PURGED = timedelta(hours=25)
CAPS = policy.Caps(
    open=100, global_=1, quotas={"acme": 100}, job_class={"short": 100}, reserve_interval=5
)
QUEUE = policy.Queue("intake.acme.standard", "acme", "standard")


def run(body):
    return asyncio.run(asyncio.wait_for(body(), LIMIT_S))


async def workflow_status(c, job_id: str) -> WorkflowExecutionStatus | None:
    try:
        return (await c.get_workflow_handle(workflow_id(job_id)).describe()).status
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return None
        raise


def claimed(conn, job_id: str) -> Claimed:
    found = claim(conn, job_id)
    assert isinstance(found, Claimed)
    return found


def test_live_count_from_rows(admission_db, seed):
    seed("admitted")
    seed("running")
    seed("running", job_class="long")
    seed("running", tenant="globex")
    seed("paused")
    seed("paused", job_class="long")
    resumed = seed("resume-pending")
    seed("accepted")
    seed("succeeded")

    rows = Scanner().snapshot(admission_db)

    assert rows.live == {("acme", "short"): 2, ("acme", "long"): 1, ("globex", "short"): 1}
    assert rows.paused == {"short": 1, "long": 1}
    assert rows.resume_pending == 1
    assert [row.job_id for row in rows.resumes] == [resumed]
    assert (rows.readmissions, rows.stuck) == ([], [])
    assert rows.counts({("acme", "short"): 1}) == policy.Snapshot(
        live=rows.live, open=7, granted={("acme", "short"): 1}
    )


def test_paused_row_frees_slot(admission_db, seed, db):
    job_id = seed("running")
    scanner = Scanner()
    before = scanner.snapshot(admission_db).counts()
    assert policy.plan(before, CAPS, ["standard"], [QUEUE]) == []

    transition(db, job_id, "running", "paused")
    after = scanner.snapshot(admission_db).counts()

    assert (before.open, after.open) == (1, 1)
    assert policy.plan(after, CAPS, ["standard"], [QUEUE]) == [QUEUE]


def test_resumed_before_start_readmitted(admission_db, seed, resume_before_start, status, client):
    job_id = seed()
    resume_before_start(job_id)
    scanner = Scanner()
    rows = scanner.snapshot(admission_db, scans=True)
    assert [r.job_id for r in rows.readmissions] == [job_id]
    assert scanner.snapshot(admission_db).readmissions == []

    [grant] = policy.plan(rows.counts(), CAPS, ["standard"], rows.readmissions)

    async def body():
        c = await client()
        return await readmit(admission_db, c, grant.job_id), await workflow_status(c, job_id)

    assert run(body) == ("started", WorkflowExecutionStatus.RUNNING)
    assert status(job_id) == "admitted"
    after = scanner.snapshot(admission_db, scans=True)
    assert (after.live, after.readmissions) == ({("acme", "short"): 1}, [])


def test_resumed_before_start_without_receipt_left(admission_db, seed, resume_before_start):
    job_id = seed(published=False)
    resume_before_start(job_id)
    assert Scanner().snapshot(admission_db, scans=True).readmissions == []


def test_stuck_admitted_started(admission_db, seed, age, admitted_events, client):
    job_id = seed()
    claimed(admission_db, job_id)
    age(job_id, STUCK)
    fresh = seed()
    claimed(admission_db, fresh)
    scanner = Scanner()
    rows = scanner.snapshot(admission_db, scans=True)
    assert [row.job_id for row in rows.stuck] == [job_id]

    async def body():
        c = await client()
        restarted = await scanner.restart(admission_db, c, rows.stuck)
        return restarted, await workflow_status(c, job_id), await workflow_status(c, fresh)

    assert run(body) == ([Restarted(job_id, "started")], WorkflowExecutionStatus.RUNNING, None)
    assert admitted_events(job_id) == 1
    assert scanner.snapshot(admission_db, scans=True).stuck == []


def test_stuck_admitted_with_workflow_left(admission_db, seed, age, client):
    job_id = seed()
    found = claimed(admission_db, job_id)
    age(job_id, STUCK)
    scanner = Scanner()

    async def body():
        c = await client()
        await start(c, found.row, found.accepted)
        run_id = (await c.get_workflow_handle(workflow_id(job_id)).describe()).run_id
        rows = scanner.snapshot(admission_db, scans=True)
        restarted = await scanner.restart(admission_db, c, rows.stuck)
        return restarted, run_id, (await c.get_workflow_handle(workflow_id(job_id)).describe())

    restarted, run_id, after = run(body)
    assert restarted == [Restarted(job_id, "left")]
    assert after.run_id == run_id
    assert scanner.snapshot(admission_db, scans=True).stuck == []


def test_old_stuck_admitted_started(admission_db, seed, age, client):
    job_id = seed()
    claimed(admission_db, job_id)
    age(job_id, PURGED)
    scanner = Scanner()
    rows = scanner.snapshot(admission_db, scans=True)

    async def body():
        c = await client()
        return await scanner.restart(admission_db, c, rows.stuck), await workflow_status(c, job_id)

    assert run(body) == ([Restarted(job_id, "started")], WorkflowExecutionStatus.RUNNING)


def test_closed_workflow_counted_stale(admission_db, seed, age, client):
    job_id = seed()
    found = claimed(admission_db, job_id)
    age(job_id, STUCK)
    scanner = Scanner()

    async def body():
        c = await client()
        await start(c, found.row, found.accepted)
        await c.get_workflow_handle(workflow_id(job_id)).terminate(reason="from outside")
        rows = scanner.snapshot(admission_db, scans=True)
        return await scanner.restart(admission_db, c, rows.stuck), await workflow_status(c, job_id)

    assert run(body) == (
        [Restarted(job_id, "stale-admitted")],
        WorkflowExecutionStatus.TERMINATED,
    )
    assert scanner.snapshot(admission_db, scans=True).stuck == []


def test_describe_capped_per_scan(admission_db, seed, age, client):
    jobs = []
    for _ in range(60):
        jobs.append(seed())
        claimed(admission_db, jobs[-1])
        age(jobs[-1], STUCK)
    scanner = Scanner()

    async def body():
        c = await client()
        scans = []
        for _ in range(3):
            rows = scanner.snapshot(admission_db, scans=True)
            scans.append(await scanner.restart(admission_db, c, rows.stuck))
        return scans

    first, second, third = run(body)
    assert [len(first), len(second), len(third)] == [50, 10, 0]
    assert {r.job_id for r in first + second} == set(jobs)
    assert {r.outcome for r in first + second} == {"started"}


def test_describe_skips_remembered_rows(admission_db, seed, age, client):
    remembered = []
    for _ in range(50):
        remembered.append(seed())
        claimed(admission_db, remembered[-1])
        age(remembered[-1], STUCK * 2)
    job_id = seed()
    claimed(admission_db, job_id)
    age(job_id, STUCK)
    scanner = Scanner()

    async def body():
        c = await client()
        for found in remembered:
            row = claimed(admission_db, found)
            await start(c, row.row, row.accepted)
        rows = scanner.snapshot(admission_db, scans=True)
        first = await scanner.restart(admission_db, c, rows.stuck)
        rows = scanner.snapshot(admission_db, scans=True)
        return first, await scanner.restart(admission_db, c, rows.stuck)

    first, second = run(body)
    assert [r.job_id for r in first] == remembered
    assert {r.outcome for r in first} == {"left"}
    assert second == [Restarted(job_id, "started")]


def test_scan_and_message_race_once(
    login, seed, resume_before_start, admitted_events, status, client
):
    job_id = seed()
    resume_before_start(job_id)
    with login() as scan_db, login() as message_db:
        [grant] = Scanner().snapshot(scan_db, scans=True).readmissions
        barrier = threading.Barrier(2)
        outcomes = []

        def scan():
            async def body():
                c = await client()
                barrier.wait()
                return await readmit(scan_db, c, grant.job_id)

            outcomes.append(run(body))

        def message():
            async def body():
                c = await client()
                barrier.wait()
                found = claimed(message_db, job_id)
                return await start(c, found.row, found.accepted)

            outcomes.append(run(body))

        threads = [threading.Thread(target=scan), threading.Thread(target=message)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(LIMIT_S)

    assert sorted(outcomes) == ["duplicate", "started"]
    assert (status(job_id), admitted_events(job_id)) == ("admitted", 1)


class FailingStart:
    """A client whose workflows never exist and whose starts fail with `status`."""

    def __init__(self, status: RPCStatusCode):
        self.status = status

    def get_workflow_handle(self, _):
        return self

    async def describe(self, **_):
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def start_workflow(self, *_, **__):
        raise RPCError("refused", self.status, b"")


def test_failed_start_backed_off(admission_db, seed, age):
    job_id = seed()
    claimed(admission_db, job_id)
    age(job_id, STUCK)
    now = [0.0]
    scanner = Scanner(clock=lambda: now[0])
    failing = FailingStart(RPCStatusCode.INVALID_ARGUMENT)

    def scan() -> list[Restarted]:
        rows = scanner.snapshot(admission_db, scans=True)
        return run(lambda: scanner.restart(admission_db, failing, rows.stuck))

    assert scan() == [Restarted(job_id, "failed")]
    now[0] = 59
    assert scan() == []
    now[0] = 60
    assert scan() == [Restarted(job_id, "failed")]
    now[0] = 60 + 119
    assert scan() == []
    now[0] = 60 + 120
    assert scan() == [Restarted(job_id, "failed")]

    now[0] = 60 + 120 + 240
    rows = scanner.snapshot(admission_db, scans=True)
    outage = FailingStart(RPCStatusCode.UNAVAILABLE)
    with pytest.raises(RPCError):
        run(lambda: scanner.restart(admission_db, outage, rows.stuck))
    assert [row.job_id for row in scanner.snapshot(admission_db, scans=True).stuck] == [job_id]


def test_snapshot_on_100k_rows(admission_db, db):
    jobs = 100_000
    started = time.monotonic()
    for numbers in bench.chunks(jobs):
        with db.transaction():
            insert_accepted(db, [bench.job_row(i) for i in numbers])
    seeded_s = time.monotonic() - started
    # One row in ten is live, one in a hundred paused, and one in a thousand stuck at `admitted`,
    # set directly, as no guarded move takes a row past `accepted` in bulk.
    with db.transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute(
            "UPDATE accepted_job SET status = CASE"
            " WHEN seed % 1000 = 0 THEN 'admitted' WHEN seed % 100 = 0 THEN 'paused'"
            " WHEN seed % 10 = 0 THEN 'running' END,"
            " updated_at = now() - interval '1 hour' WHERE seed % 10 = 0"
        )
    db.execute("ANALYZE accepted_job")
    scanner = Scanner()

    def timed(scans: bool) -> tuple[float, object]:
        begun = time.perf_counter()
        rows = scanner.snapshot(admission_db, scans=scans)
        return time.perf_counter() - begun, rows

    timed(False)
    cycles = [timed(False) for _ in range(20)]
    scan_cycles = [timed(True) for _ in range(5)]
    rows = cycles[0][1]
    assert rows.live == {("bench", "short"): 9_000 + 100}
    assert rows.paused == {"short": 900}
    assert len(scan_cycles[0][1].stuck) == 50
    snapshot_ms = statistics.median(s for s, _ in cycles) * 1000
    scan_ms = statistics.median(s for s, _ in scan_cycles) * 1000
    print(
        f"\nrows: {jobs} seeded in {seeded_s:.1f} s\n"
        f"snapshot: median {snapshot_ms:.1f} ms, max {max(s for s, _ in cycles) * 1000:.1f} ms"
        f" over {len(cycles)} reads\n"
        f"scan cycle: median {scan_ms:.1f} ms over {len(scan_cycles)} reads\n"
        f"threshold: 100 ms, {'revisit' if snapshot_ms > 100 else 'no revisit'}"
    )
