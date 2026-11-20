"""The single-job and batch control routes on the control login, over rows seeded in Postgres, with
Temporal faked. A claim is played by the guarded move that admission's claim makes."""

import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.types.json import Jsonb
from wft_dispatchapi import control
from wft_dispatchapi.control import ControlRefused, control_batch, control_job
from wft_dispatchapi.models import ControlAnswer, ControlRequest, EntryError, SummaryLine
from wft_events import from_bytes
from wft_eventstore import events_for_job
from wft_jobstate import JobRow, Lost, get_job, insert_accepted, transition
from wft_keys import batch_id, cloudevent_id, job_id
from wft_stages import Facts, run_stage_contract
from wft_temporal import new_traceparent

pytestmark = pytest.mark.integration

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MANIFEST = {
    "uri": f"s3://wft-sources/sha256/9f/{SHA}",
    "sha256": SHA,
    "size": 2048,
    "name": "manifest.json",
    "kind": "manifest",
}
PATCH = {**MANIFEST, "sha256": "1" * 64, "uri": f"s3://wft-sources/sha256/11/{'1' * 64}"}
PLAIN = ControlRequest()
LOCK_WAIT_S = 10


@pytest.fixture
def conn(logins):
    """A connection of the control login, which every control call runs on."""
    with psycopg.connect(logins["control"], autocommit=True) as connection:
        yield connection


@pytest.fixture
def seed(db):
    """Inserts the rows of `count` jobs of a batch at accepted and returns their rows."""

    def insert(batch_key: str = "batch", count: int = 1, tenant: str = "acme") -> list[JobRow]:
        rows = [
            JobRow(
                job_id=job_id(tenant, f"{batch_key}/{index}"),
                tenant=tenant,
                submission_key=f"{batch_key}/{index}",
                batch_id=batch_id(tenant, batch_key),
                priority_class="standard",
                job_class="short",
                manifest_ref=MANIFEST,
                schema_version="urn:wft:schema:job.accepted:1.0",
                traceparent=new_traceparent(),
                seed=index,
                parameters={"n": index},
                fingerprint=SHA,
            )
            for index in range(count)
        ]
        insert_accepted(db, rows)
        return rows

    return insert


def start(db, row: JobRow, gateway=None, to: str = "running") -> None:
    """Moves the row as admission and the workflow would, and starts its workflow in `gateway`."""
    transition(db, row.job_id, "accepted", "admitted")
    if to != "admitted":
        transition(db, row.job_id, "admitted", "running")
    if to == "paused":
        transition(db, row.job_id, "running", "paused")
    if gateway is not None:
        gateway.start(row.job_id)


def refused(code: str):
    return pytest.raises(ControlRefused, match=f"^{code}: ")


def name_of(event_type: str) -> str:
    return event_type.removeprefix("dev.wft.").removesuffix(".v1")


def history(db, row: JobRow) -> list[tuple[str, int]]:
    """The job's control events in history order, as (type, sequence by the id rule) pairs."""
    found = []
    for stored in events_for_job(db, row.job_id):
        event = from_bytes(stored.body.encode())
        assert event.source == "/wft/control"
        name = name_of(event.type)
        [seq] = [s for s in range(64) if event.id == cloudevent_id(row.job_id, "", "", name, s)]
        found.append((name, seq))
    return found


def control_row(db, row: JobRow) -> tuple:
    found = get_job(db, row.job_id)
    return found.status, found.requested_action, found.control_seq


def wait_for_a_lock_wait(db) -> None:
    """Returns once another session of the database waits on a row lock."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while not db.execute(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE wait_event_type = 'Lock' AND datname = current_database()"
    ).fetchone()[0]:
        assert time.monotonic() < deadline, "nothing waited on the lock"
        time.sleep(0.05)


def test_cancel_and_pause_before_start_are_row_only(conn, db, seed, gateway):
    cancelled, paused = seed(count=2)
    answer = control_job(
        conn, gateway, "acme", cancelled.job_id, "cancel", ControlRequest(reason="operator")
    )
    assert answer == ControlAnswer(
        job_id=cancelled.job_id,
        action="cancel",
        outcome="applied",
        status="cancelled",
        view=None,
        control_seq=1,
    )
    assert control_row(db, cancelled) == ("cancelled", None, 1)
    assert get_job(db, cancelled.job_id).terminal_at is not None
    assert history(db, cancelled) == [("job.cancel.requested", 1), ("job.cancelled", 1)]
    [requested, _] = events_for_job(db, cancelled.job_id)
    assert from_bytes(requested.body.encode()).data == {"reason": "operator"}

    answer = control_job(conn, gateway, "acme", paused.job_id, "pause", PLAIN)
    assert (answer.outcome, answer.status, answer.view) == ("applied", "paused", None)
    assert control_row(db, paused) == ("paused", None, 1)
    # A job that never started resumes straight back to accepted, for admission to claim again.
    answer = control_job(conn, gateway, "acme", paused.job_id, "resume", PLAIN)
    assert (answer.outcome, answer.status, answer.control_seq) == ("applied", "accepted", 2)
    assert control_row(db, paused) == ("accepted", None, 2)
    assert history(db, paused) == [
        ("job.pause.requested", 1),
        ("job.paused", 1),
        ("job.resume.requested", 2),
        ("job.resumed", 2),
    ]
    assert gateway.signals == []


def test_requested_outcomes_wait_for_the_workflow(conn, db, seed, gateway):
    running, paused, unknown = seed(count=3)
    start(db, running, gateway)
    answer = control_job(conn, gateway, "acme", running.job_id, "cancel", PLAIN)
    assert (answer.outcome, answer.status, answer.view) == ("requested", "running", "cancelling")
    assert control_row(db, running) == ("running", "cancel", 1)
    assert history(db, running) == [("job.cancel.requested", 1)]

    start(db, paused, gateway, to="paused")
    db.execute(
        "UPDATE accepted_job SET pending_patch_ref = %s WHERE job_id = %s",
        (Jsonb(PATCH), paused.job_id),
    )
    answer = control_job(conn, gateway, "acme", paused.job_id, "resume", PLAIN)
    assert (answer.outcome, answer.status) == ("requested", "resume-pending")
    assert control_row(db, paused) == ("resume-pending", "resume", 1)
    # A resume without a patch clears the pending one, so the workflow keeps the plan in force.
    assert get_job(db, paused.job_id).pending_patch_ref is None
    assert history(db, paused) == [("job.resume.requested", 1)]

    # Without Temporal a paused job's cancel is still requested, for the retrier to finish.
    start(db, unknown, gateway, to="paused")
    gateway.available = False
    answer = control_job(conn, gateway, "acme", unknown.job_id, "cancel", PLAIN)
    assert (answer.outcome, answer.status, answer.view) == ("requested", "paused", "cancelling")
    assert gateway.signals == []


def test_refusals_write_nothing(conn, db, seed, gateway):
    running, paused = seed(count=2)
    start(db, running, gateway)
    start(db, paused, gateway, to="paused")
    gateway.available = False
    with refused("TEMPORAL_UNAVAILABLE"):
        control_job(conn, gateway, "acme", paused.job_id, "resume", PLAIN)
    with refused("NOT_PAUSED"):
        control_job(conn, gateway, "acme", running.job_id, "resume", PLAIN)
    with refused("TEMPLATE_NOT_ALLOWED"):
        control_job(conn, gateway, "acme", running.job_id, "cancel", ControlRequest(template={}))
    assert control_row(db, running) == ("running", None, 0)
    assert control_row(db, paused) == ("paused", None, 0)
    assert history(db, running) == history(db, paused) == []


def test_patch_reference_is_written_with_the_resume(conn, db, seed, gateway):
    (row,) = seed()
    start(db, row, gateway, to="paused")
    patched = ControlRequest(template={"patched": True})
    answer = control_job(conn, gateway, "acme", row.job_id, "resume", patched, patch=PATCH)
    assert (answer.outcome, answer.status) == ("requested", "resume-pending")
    assert get_job(db, row.job_id).pending_patch_ref == PATCH
    again = control_job(conn, gateway, "acme", row.job_id, "resume", patched, patch=PATCH)
    assert (again.outcome, again.control_seq) == ("noop", 1)
    with refused("PATCH_PENDING"):
        control_job(conn, gateway, "acme", row.job_id, "resume", patched, patch=MANIFEST)


def test_cancel_racing_the_claim_still_cancels(appdb, conn, db, seed, gateway):
    claimed, waiting = seed(count=2)
    # The claim committed before the lock, so the job is to have a workflow, which takes the cancel.
    transition(db, claimed.job_id, "accepted", "admitted")
    answer = control_job(conn, gateway, "acme", claimed.job_id, "cancel", PLAIN)
    assert (answer.outcome, answer.status, answer.view) == ("requested", "admitted", "cancelling")

    def claim():
        with psycopg.connect(appdb, autocommit=True) as claimer:
            return transition(claimer, waiting.job_id, "accepted", "admitted")

    # The claim waits on the lock that the cancel holds, then finds the row cancelled and loses.
    with ThreadPoolExecutor(1) as pool:
        with conn.transaction():
            answer = control_job(conn, gateway, "acme", waiting.job_id, "cancel", PLAIN)
            claimed_late = pool.submit(claim)
            wait_for_a_lock_wait(db)
        assert claimed_late.result(timeout=LOCK_WAIT_S) == Lost("cancelled")
    assert (answer.outcome, answer.status) == ("applied", "cancelled")
    assert control_row(db, waiting) == ("cancelled", None, 1)


def test_pause_and_cancel_racing_on_one_row(logins, conn, db, seed, gateway):
    waiting, running = seed(count=2)
    start(db, running, gateway)

    def other(row: JobRow, action: str):
        with psycopg.connect(logins["control"], autocommit=True) as second:
            try:
                return control_job(second, gateway, "acme", row.job_id, action, PLAIN)
            except ControlRefused as refusal:
                return refusal

    with ThreadPoolExecutor(1) as pool:
        # A cancel that waits on a pause takes over from it once the pause has committed.
        with conn.transaction():
            control_job(conn, gateway, "acme", waiting.job_id, "pause", PLAIN)
            cancel = pool.submit(other, waiting, "cancel")
            wait_for_a_lock_wait(db)
        assert cancel.result(timeout=LOCK_WAIT_S).outcome == "applied"
        # A pause that waits on a cancel is refused once the cancel has committed.
        with conn.transaction():
            control_job(conn, gateway, "acme", running.job_id, "cancel", PLAIN)
            pause = pool.submit(other, running, "pause")
            wait_for_a_lock_wait(db)
        assert pause.result(timeout=LOCK_WAIT_S).code == "STOPPING"
    assert control_row(db, waiting) == ("cancelled", None, 2)
    assert history(db, waiting) == [
        ("job.pause.requested", 1),
        ("job.paused", 1),
        ("job.cancel.requested", 2),
        ("job.cancelled", 2),
    ]
    assert control_row(db, running) == ("running", "cancel", 1)
    assert history(db, running) == [("job.cancel.requested", 1)]


def test_event_ids_follow_d5(conn, db, seed, gateway):
    (row,) = seed()
    for action in ("pause", "resume", "pause", "terminate"):
        control_job(conn, gateway, "acme", row.job_id, action, PLAIN)
    stored = [from_bytes(e.body.encode()) for e in events_for_job(db, row.job_id)]
    expected = [
        ("job.pause.requested", 1),
        ("job.paused", 1),
        ("job.resume.requested", 2),
        ("job.resumed", 2),
        ("job.pause.requested", 3),
        ("job.paused", 3),
        ("job.terminate.requested", 4),
        ("job.terminated", 4),
    ]
    assert [(name_of(e.type), e.id) for e in stored] == [
        (name, cloudevent_id(row.job_id, "", "", name, seq)) for name, seq in expected
    ]
    assert {(e.source, e.jobid, e.tenant, e.traceparent) for e in stored} == {
        ("/wft/control", row.job_id, "acme", row.traceparent)
    }


def test_batch_scope_answers_every_job(monkeypatch, conn, db, seed, gateway):
    monkeypatch.setattr(control, "PAGE", 2)
    rows = seed(count=5)
    (elsewhere,) = seed(batch_key="other")
    by_id = {row.job_id: row for row in rows}
    ordered = sorted(by_id)
    running, ended, paused = (by_id[job] for job in ordered[1:4])
    start(db, running, gateway)
    control_job(conn, gateway, "acme", ended.job_id, "cancel", PLAIN)
    control_job(conn, gateway, "acme", paused.job_id, "pause", PLAIN)

    lines = []
    for line in control_batch(conn, gateway, "acme", "batch", "pause", PLAIN):
        # Each job's answer arrives after its transaction committed, as another session sees it.
        if not isinstance(line, SummaryLine):
            assert get_job(db, line.job_id).status == (
                line.status if isinstance(line, ControlAnswer) else "cancelled"
            )
        lines.append(line)
    *jobs, summary = lines
    assert [line.job_id for line in jobs] == ordered
    assert [
        (line.outcome, line.status, line.view) if isinstance(line, ControlAnswer) else line.code
        for line in jobs
    ] == [
        ("applied", "paused", None),
        ("requested", "running", "pausing"),
        "ALREADY_ENDED",
        ("noop", "paused", None),
        ("applied", "paused", None),
    ]
    assert jobs[2] == EntryError(
        job_id=ended.job_id,
        submission_key=ended.submission_key,
        code="ALREADY_ENDED",
        message=jobs[2].message,
    )
    assert summary.summary.model_dump() == {"applied": 2, "requested": 1, "noop": 1, "refused": 1}
    assert control_row(db, elsewhere) == ("accepted", None, 0)

    with refused("BATCH_NOT_FOUND"):
        control_batch(conn, gateway, "acme", "absent", "cancel", PLAIN)
    with refused("TEMPLATE_NOT_ALLOWED"):
        control_batch(conn, gateway, "acme", "batch", "resume", ControlRequest(template={}))


def test_another_tenants_job_is_not_found(conn, db, seed, gateway):
    (row,) = seed(tenant="acme")
    for job in (row.job_id, "not-a-job-id", job_id("other", "batch/0")):
        with refused("JOB_NOT_FOUND"):
            control_job(conn, gateway, "other", job, "cancel", PLAIN)
    with refused("BATCH_NOT_FOUND"):
        control_batch(conn, gateway, "other", "batch", "cancel", PLAIN)
    assert control_row(db, row) == ("accepted", None, 0)
    assert history(db, row) == []


def test_stage_contract_control(conn, db, seed, gateway):
    rows = seed(count=6)
    actions = (("cancel",), ("terminate",), ("pause", "resume"))
    for row, steps in zip(rows[:3], actions, strict=True):
        for action in steps:
            control_job(conn, gateway, "acme", row.job_id, action, PLAIN)
    for row, action in zip(rows[3:], ("pause", "terminate", "resume"), strict=True):
        start(db, row, gateway, to="paused" if action == "resume" else "running")
        control_job(conn, gateway, "acme", row.job_id, action, PLAIN)
    emitted = [from_bytes(e.body.encode()) for row in rows for e in events_for_job(db, row.job_id)]
    assert {name_of(e.type) for e in emitted} == {
        f"job.{name}" for name in ("cancelled", "terminated", "paused", "resumed")
    } | {f"job.{action}.requested" for action in ("cancel", "terminate", "pause", "resume")}
    run_stage_contract("control", lambda inbound, facts: emitted, [(None, Facts())])
