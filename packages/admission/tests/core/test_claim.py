"""The claim of a row as admission's login, its checks, its event and its lock, and the accepted
event rebuilt from the row."""

import asyncio
import json
import threading
import time
import uuid

import pytest
from temporalio.service import RPCError, RPCStatusCode
from wdf_admission import claim as claim_module
from wdf_admission.claim import Claimed, Skipped, accepted_of, claim
from wdf_admission.start import start
from wdf_events import EventError, format_time, from_bytes
from wdf_jobstate import Lost, get_job, request_action, transition
from wdf_keys import workflow_id

pytestmark = [pytest.mark.integration, pytest.mark.pair]

LIMIT_S = 60
ACCEPTED = ("dev.wdf.job.accepted.v1", "/wdf/dispatch")
ADMITTED = ("dev.wdf.job.admitted.v1", "/wdf/admission")


def run(body):
    return asyncio.run(asyncio.wait_for(body(), LIMIT_S))


async def has_workflow(c, job_id: str) -> bool:
    try:
        await c.get_workflow_handle(workflow_id(job_id)).describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


def test_claim_commits_before_start(admission_db, seed, status, events, client):
    job_id = seed()
    claimed = claim(admission_db, job_id)
    assert isinstance(claimed, Claimed)

    async def body():
        c = await client()
        # Read on the superuser's own connection, so only what the claim committed is seen.
        before = (status(job_id), events(job_id), await has_workflow(c, job_id))
        started = await start(c, claimed.row, claimed.accepted)
        return before, started, await has_workflow(c, job_id)

    before, started, after = run(body)
    assert before == ("admitted", [ACCEPTED, ADMITTED], False)
    assert (started, after) == ("started", True)


def test_claim_writes_admitted_event(admission_db, seed, db):
    job_id = seed()
    claim(admission_db, job_id)
    [body] = db.execute(
        "SELECT body FROM event WHERE job_id = %s AND type = %s", (job_id, ADMITTED[0])
    ).fetchone()
    admitted = from_bytes(body.encode())
    row = get_job(db, job_id)
    assert admitted.source == "/wdf/admission"
    assert admitted.data == {"workflow_id": f"job-{job_id}"}
    assert (admitted.tenant, admitted.jobid, admitted.traceparent) == (
        row.tenant,
        job_id,
        row.traceparent,
    )
    assert json.loads(body)["subject"] == job_id


def test_claim_lost_is_skip(admission_db, seed, status, events, monkeypatch):
    job_id = seed()
    monkeypatch.setattr(claim_module, "transition", lambda *args, **kwargs: Lost("running"))
    assert claim(admission_db, job_id) == Skipped("started")
    assert (status(job_id), events(job_id)) == ("accepted", [ACCEPTED])


@pytest.mark.parametrize(
    ("path", "action", "reason"),
    [
        ((), "pause", "outstanding-action"),
        ((), "cancel", "outstanding-action"),
        (("paused",), None, "outstanding-action"),
        (("paused", "resume-pending"), None, "outstanding-action"),
        (("admitted", "running"), None, "started"),
        (("admitted", "running", "succeeded"), None, "ended"),
        (("cancelled",), None, "ended"),
    ],
)
def test_claim_skips_outstanding_action(admission_db, db, seed, status, path, action, reason):
    job_id = seed()
    held = "accepted"
    for to in path:
        # The flag only permits the moves of a job that never started, and every move here is one.
        transition(db, job_id, held, to, row_only=True)
        held = to
    if action:
        request_action(db, job_id, action)
    assert claim(admission_db, job_id) == Skipped(reason)
    assert status(job_id) == held


def test_claim_refuses_no_receipt(admission_db, seed, status, events):
    job_id = seed(published=False)
    assert claim(admission_db, job_id) == Skipped("no-receipt")
    assert (status(job_id), events(job_id)) == ("accepted", [ACCEPTED])
    assert claim(admission_db, str(uuid.uuid4())) == Skipped("no-row")


def test_claim_locks_row(admission_db, db, seed, status):
    """A control request that holds the row first makes the claim wait, and then wins."""
    job_id = seed()
    result = []
    with db.transaction():
        request_action(db, job_id, "cancel")
        waiting = threading.Thread(target=lambda: result.append(claim(admission_db, job_id)))
        waiting.start()
        time.sleep(1)
        assert waiting.is_alive() and not result
    waiting.join(LIMIT_S)
    assert result == [Skipped("outstanding-action")]
    assert status(job_id) == "accepted"


def test_bad_row_never_claimed(admission_db, db, seed, status, events):
    job_id = seed()
    db.execute(
        'UPDATE accepted_job SET manifest_ref = manifest_ref || \'{"uri": "http://elsewhere"}\''
        " WHERE job_id = %s",
        (job_id,),
    )
    with pytest.raises(EventError):
        claim(admission_db, job_id)
    assert (status(job_id), events(job_id)) == ("accepted", [ACCEPTED])


def test_admitted_row_is_started(admission_db, seed, events, client):
    """A row claimed by a run that died before its start is started by the next claim."""
    job_id = seed()
    first = claim(admission_db, job_id)
    again = claim(admission_db, job_id)
    assert isinstance(again, Claimed) and again.row.status == "admitted"
    assert again.accepted == first.accepted
    assert events(job_id) == [ACCEPTED, ADMITTED]

    async def body():
        c = await client()
        return await start(c, again.row, again.accepted), await has_workflow(c, job_id)

    assert run(body) == ("started", True)


def test_rebuilt_accepted_matches_stored(seed, db):
    job_id = seed()
    [body] = db.execute(
        "SELECT body FROM event WHERE job_id = %s AND type = %s", (job_id, ACCEPTED[0])
    ).fetchone()
    stored = from_bytes(body.encode())
    row = get_job(db, job_id)
    rebuilt = accepted_of(row)
    assert rebuilt.model_dump(exclude={"time"}) == stored.model_dump(exclude={"time"})
    assert rebuilt.time == format_time(row.created_at)
