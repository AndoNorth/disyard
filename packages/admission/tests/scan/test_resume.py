"""Resume grants on a dev server: the signal that a paused workflow receives, the slot it holds
until the row leaves `resume-pending`, the resend of an unapplied signal, the recovery after a
restart, and a resume that control's cancel or a missing workflow overtakes."""

import asyncio

import pytest
from dsy_admission.claim import Claimed, claim
from dsy_admission.policy import Caps, Snapshot, plan
from dsy_admission.resume import Resumes, pending
from dsy_admission.start import start
from dsy_jobstate import request_action, transition
from dsy_keys import workflow_id
from dsy_workflow.models import ControlSignal, StatusQuery
from psycopg.types.json import Jsonb
from temporalio.api.enums.v1 import EventType
from temporalio.service import RPCError, RPCStatusCode

pytestmark = [pytest.mark.integration, pytest.mark.pair]

LIMIT_S = 180
SLOT = ("acme", "short")


def run(body):
    return asyncio.run(asyncio.wait_for(body(), LIMIT_S))


async def until(check, every_s: float = 0.2, timeout_s: float = 60):
    """The first truthy answer of `check`, a function or a coroutine function, or
    a `TimeoutError` after `timeout_s`."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        found = check()
        if asyncio.iscoroutine(found):
            found = await found
        if found:
            return found
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"no answer within {timeout_s:.0f} s")
        await asyncio.sleep(every_s)


def status_of(db, job_id: str) -> str:
    return db.execute("SELECT status FROM accepted_job WHERE job_id = %s", (job_id,)).fetchone()[0]


async def paused_job(c, admission_db, db, seed) -> str:
    """A job claimed and started as admission does, whose only task failed with a checkpoint, so
    the workflow paused it and its row reads `paused`."""
    job_id = seed()
    claimed = claim(admission_db, job_id)
    assert isinstance(claimed, Claimed)
    assert await start(c, claimed.row, claimed.accepted) == "started"
    await until(lambda: status_of(db, job_id) == "paused")
    return job_id


def resume_requested(db, job_id: str, patch: dict | None = None) -> int:
    """The resume request of control: the request and the move from `paused` to `resume-pending`
    in one transaction, with the pending patch when one is given, and its sequence."""
    with db.transaction():
        seq = request_action(db, job_id, "resume").control_seq
        if patch is not None:
            db.execute(
                "UPDATE accepted_job SET pending_patch_ref = %s WHERE job_id = %s",
                (Jsonb(patch), job_id),
            )
        transition(db, job_id, "paused", "resume-pending")
    return seq


async def cancel_requested(c, db, job_id: str) -> int:
    """The cancel request of control, and its signal to the job's workflow."""
    seq = request_action(db, job_id, "cancel").control_seq
    await c.get_workflow_handle(workflow_id(job_id)).signal(
        "cancel", ControlSignal(control_seq=seq, reason=None)
    )
    return seq


def never_started(db, seed) -> str:
    """A job paused and resumed before it started, left at `resume-pending` with no workflow."""
    job_id = seed()
    with db.transaction():
        request_action(db, job_id, "pause")
        transition(db, job_id, "accepted", "paused", row_only=True)
    resume_requested(db, job_id)
    return job_id


async def query(c, job_id: str) -> StatusQuery:
    return await c.get_workflow_handle(workflow_id(job_id)).query("status", result_type=StatusQuery)


async def resume_signals(c, job_id: str) -> list[int]:
    """The `control_seq` of each `resume` signal in the workflow's history."""
    found = []
    async for event in c.get_workflow_handle(workflow_id(job_id)).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            signaled = event.workflow_execution_signaled_event_attributes
            if signaled.signal_name == "resume":
                [payload] = signaled.input.payloads
                found.append(
                    c.data_converter.payload_converter.from_payload(payload)["control_seq"]
                )
    return found


def caps(global_: int) -> Caps:
    return Caps(
        open=100,
        global_=global_,
        quotas={"acme": 100},
        job_class={"short": 100, "long": 100},
        reserve_interval=5,
    )


def test_resume_sent_with_row_seq(client, rig, admission_db, db, seed):
    async def body():
        c = await client()
        async with rig(c):
            job_id = await paused_job(c, admission_db, db, seed)
            # Earlier requests of control took the row's sequence on.
            db.execute("UPDATE accepted_job SET control_seq = 6 WHERE job_id = %s", (job_id,))
            seq = resume_requested(db, job_id)
            resumes = Resumes(c)
            [waiting] = resumes.observe(pending(admission_db))
            assert waiting.job_id == job_id
            assert await resumes.grant(waiting)
            await until(lambda: status_of(db, job_id) == "succeeded")
            return seq, await query(c, job_id), await resume_signals(c, job_id)

    seq, status, signals = run(body)
    assert seq == 7
    assert signals == [7]
    assert (status.job_status, status.control_seq, status.resume_counter) == ("succeeded", 7, 1)
    [task] = status.tasks
    assert (task.state, task.task_attempt) == ("succeeded", 2)


def test_resume_carries_patch(client, rig, admission_db, db, seed):
    async def body():
        c = await client()
        async with rig(c) as this:
            job_id = await paused_job(c, admission_db, db, seed)
            seq = resume_requested(db, job_id, this.patch)
            resumes = Resumes(c)
            [waiting] = resumes.observe(pending(admission_db))
            assert await resumes.grant(waiting)
            await until(lambda: status_of(db, job_id) == "succeeded")
            return seq, await query(c, job_id), this.pool.calls, this.patched

    seq, status, calls, patched = run(body)
    assert (status.control_seq, status.resume_counter) == (seq, 1)
    [task] = status.tasks
    assert (task.state, task.task_attempt) == ("succeeded", 2)
    # The attempt after the resume runs the patched plan.
    assert [call.params.args for call in calls] == [{"n": 1000}, patched["args"]]


def test_resume_resent_until_running(client, rig, admission_db, db, seed):
    """A signal whose call failed holds its slot, and the recheck sends it again, since the
    workflow shows the row's sequence unapplied, until the row is `running`."""

    class Lossy(Resumes):
        lost = 1

        async def _send(self, row):
            if self.lost:
                self.lost -= 1
                raise RPCError("the call timed out", RPCStatusCode.DEADLINE_EXCEEDED, b"")
            return await super()._send(row)

    async def body():
        c = await client()
        async with rig(c) as this:
            this.emitter.held = True
            job_id = await paused_job(c, admission_db, db, seed)
            seq = resume_requested(db, job_id)
            resumes = Lossy(c, recheck_s=0)
            [waiting] = resumes.observe(pending(admission_db))
            with pytest.raises(RPCError):
                await resumes.grant(waiting)
            held = dict(resumes.slots())
            unapplied = (await query(c, job_id)).control_seq
            await resumes.recheck()
            await until(async_applied(c, job_id, seq))
            this.emitter.held = False
            await until(lambda: status_of(db, job_id) == "succeeded")
            resumes.observe(pending(admission_db))
            return held, unapplied, await resume_signals(c, job_id), dict(resumes.slots())

    held, unapplied, signals, after = run(body)
    assert held == {SLOT: 1}
    assert unapplied == 0
    assert len(signals) == 1
    assert after == {}


def async_applied(c, job_id: str, seq: int):
    async def check():
        return (await query(c, job_id)).control_seq >= seq

    return check


def test_no_resume_without_request(db, seed, admission_db):
    """Only a `resume-pending` row whose requested action is `resume` waits: not a `paused` row
    with a resume requested, and not a `resume-pending` row with a cancel requested after it."""
    paused = seed()
    with db.transaction():
        request_action(db, paused, "pause")
        transition(db, paused, "accepted", "paused", row_only=True)
    request_action(db, paused, "resume")
    cancelled = never_started(db, seed)
    request_action(db, cancelled, "cancel")
    waiting = never_started(db, seed)

    resumes = Resumes(client=None)
    found = resumes.observe(pending(admission_db))
    assert [resume.job_id for resume in found] == [waiting]
    assert {row.job_id for row in pending(admission_db)} >= {cancelled, waiting}
    assert paused not in {row.job_id for row in pending(admission_db)}


def test_resume_waits_for_free_slot(client, rig, admission_db, db, seed):
    """A waiting resume gets no grant while the global cap is full, gets one once a slot frees,
    and its granted slot keeps the next resume waiting."""

    async def body():
        c = await client()
        async with rig(c) as this:
            this.emitter.held = True
            job_id = await paused_job(c, admission_db, db, seed)
            seq = resume_requested(db, job_id)
            other = never_started(db, seed)
            resumes = Resumes(c)
            waiting = resumes.observe(pending(admission_db))
            full = Snapshot(live={SLOT: 1}, open=3, granted=resumes.slots())
            assert plan(full, caps(1), ["standard"], waiting) == []
            assert (await query(c, job_id)).control_seq == 0
            free = Snapshot(live={}, open=2, granted=resumes.slots())
            [first] = plan(free, caps(1), ["standard"], waiting)
            assert await resumes.grant(first)
            await until(async_applied(c, job_id, seq))
            waiting = resumes.observe(pending(admission_db))
            after = Snapshot(live={}, open=2, granted=resumes.slots())
            blocked = plan(after, caps(1), ["standard"], waiting)
            this.emitter.held = False
            await until(lambda: status_of(db, job_id) == "succeeded")
            return first.job_id, job_id, other, waiting, blocked

    first, job_id, other, waiting, blocked = run(body)
    assert first == job_id
    assert [resume.job_id for resume in waiting] == [other]
    assert blocked == []


def test_terminal_frees_resume_slot(client, rig, admission_db, db, seed):
    """A granted resume whose row control cancels holds its slot until the row reads
    `cancelled`."""

    async def body():
        c = await client()
        async with rig(c) as this:
            this.emitter.held = True
            job_id = await paused_job(c, admission_db, db, seed)
            seq = resume_requested(db, job_id)
            resumes = Resumes(c)
            [waiting] = resumes.observe(pending(admission_db))
            assert await resumes.grant(waiting)
            await until(async_applied(c, job_id, seq))
            resumes.observe(pending(admission_db))
            held = dict(resumes.slots())
            await cancel_requested(c, db, job_id)
            await until(lambda: status_of(db, job_id) == "cancelled")
            resumes.observe(pending(admission_db))
            return held, dict(resumes.slots())

    held, after = run(body)
    assert held == {SLOT: 1}
    assert after == {}


def test_resend_only_when_unapplied(client, rig, admission_db, db, seed):
    """A granted resume whose workflow applied the row's sequence is asked again only once per
    recheck interval and is never signalled again."""
    now = [0.0]

    class Counting(Resumes):
        asked = 0

        async def _status(self, row):
            self.asked += 1
            return await super()._status(row)

    async def body():
        c = await client()
        async with rig(c) as this:
            this.emitter.held = True
            job_id = await paused_job(c, admission_db, db, seed)
            seq = resume_requested(db, job_id)
            resumes = Counting(c, clock=lambda: now[0])
            [waiting] = resumes.observe(pending(admission_db))
            assert await resumes.grant(waiting)
            await until(async_applied(c, job_id, seq))
            asked = []
            for at in (10.0, 29.9, 30.0, 45.0, 60.0):
                now[0] = at
                await resumes.recheck()
                asked.append(resumes.asked)
            signals = await resume_signals(c, job_id)
            this.emitter.held = False
            await until(lambda: status_of(db, job_id) == "succeeded")
            return asked, signals

    asked, signals = run(body)
    assert asked == [0, 0, 1, 1, 2]
    assert len(signals) == 1


def test_restart_counts_applied_resumes(client, rig, admission_db, db, seed, monkeypatch):
    """After a restart, a resume whose workflow applied the row's sequence counts as granted and
    one it did not waits again, and every one counts as granted while the query fails."""

    async def body():
        c = await client()
        async with rig(c) as this:
            this.emitter.held = True
            applied = await paused_job(c, admission_db, db, seed)
            unapplied = await paused_job(c, admission_db, db, seed)
            seq = resume_requested(db, applied)
            resume_requested(db, unapplied)
            before = Resumes(c)
            granted = [r for r in before.observe(pending(admission_db)) if r.job_id == applied]
            assert await before.grant(*granted)
            await until(async_applied(c, applied, seq))

            restarted = Resumes(c)
            await restarted.recover(pending(admission_db))
            recovered = (set(restarted.granted), dict(restarted.slots()))
            waiting = [r.job_id for r in restarted.observe(pending(admission_db))]

            async def failing(self, row):
                raise RPCError("the server is down", RPCStatusCode.UNAVAILABLE, b"")

            monkeypatch.setattr(Resumes, "_status", failing)
            blind = Resumes(c)
            await blind.recover(pending(admission_db))
            monkeypatch.undo()

            this.emitter.held = False
            await until(lambda: status_of(db, applied) == "succeeded")
            restarted.observe(pending(admission_db))
            return applied, unapplied, recovered, waiting, set(blind.granted), restarted

    applied, unapplied, recovered, waiting, blind, restarted = run(body)
    assert recovered == ({applied}, {SLOT: 1})
    assert waiting == [unapplied]
    assert blind == {applied, unapplied}
    # The applied resume's row left `resume-pending`, which frees its slot.
    assert dict(restarted.slots()) == {}


def test_resume_without_workflow_left_to_control(client, server, admission_db, db, seed):
    """A resume of a job that never started finds no workflow: its slot frees, it is counted and
    left out until control moves the row back to `accepted` or its sequence changes."""

    async def body():
        c = await client()
        job_id = never_started(db, seed)
        resumes = Resumes(c)
        [waiting] = resumes.observe(pending(admission_db))
        sent = await resumes.grant(waiting)
        left_out = resumes.observe(pending(admission_db))
        restarted = Resumes(c)
        await restarted.recover(pending(admission_db))
        transition(db, job_id, "resume-pending", "accepted", row_only=True)
        resumes.observe(pending(admission_db))
        return sent, resumes, left_out, restarted

    sent, resumes, left_out, restarted = run(body)
    assert sent is False
    assert (resumes.no_workflow, dict(resumes.slots()), left_out) == (1, {}, [])
    assert resumes.missing == {}
    assert (restarted.no_workflow, restarted.granted) == (1, {})


def test_resume_and_cancel_same_seq_cancel_wins(client, rig, admission_db, db, seed):
    """A resume granted from a row read before control's cancel carries the older sequence, so the
    workflow ignores it and the cancel ends the job."""

    async def body():
        c = await client()
        async with rig(c):
            job_id = await paused_job(c, admission_db, db, seed)
            resumed = resume_requested(db, job_id)
            resumes = Resumes(c)
            [stale] = resumes.observe(pending(admission_db))
            cancelled = await cancel_requested(c, db, job_id)
            assert await resumes.grant(stale)
            await until(lambda: status_of(db, job_id) == "cancelled")
            resumes.observe(pending(admission_db))
            status = await query(c, job_id)
            return resumed, cancelled, status, await resume_signals(c, job_id), resumes

    resumed, cancelled, status, signals, resumes = run(body)
    assert cancelled == resumed + 1
    assert signals == [resumed]
    assert (status.job_status, status.control_seq, status.resume_counter) == (
        "cancelled",
        cancelled,
        0,
    )
    assert dict(resumes.slots()) == {}
