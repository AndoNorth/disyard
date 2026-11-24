"""A control decision on the locked row cannot interleave with a claim."""

import threading
import uuid

import psycopg
import pytest
from dsy_jobstate import (
    JobRow,
    Lost,
    Moved,
    get_job,
    insert_accepted,
    lock_job,
    request_action,
    transition,
)

pytestmark = pytest.mark.integration
SHA = "a" * 64


@pytest.fixture
def job(appdb):
    row = JobRow(
        job_id=str(uuid.uuid4()),
        tenant="acme",
        submission_key="key-0",
        batch_id=str(uuid.uuid4()),
        priority_class="standard",
        manifest_ref={
            "uri": f"s3://dsy-sources/sha256/aa/{SHA}",
            "sha256": SHA,
            "size": 7,
            "name": "m",
            "kind": "manifest",
        },
        schema_version="dsy.job.accepted/3",
        traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        seed=3,
        parameters={"a": 1},
        fingerprint="b" * 64,
    )
    with psycopg.connect(appdb, autocommit=True) as setup:
        insert_accepted(setup, [row])
    return row.job_id


def test_lock_job_returns_the_row_or_none(appdb, job):
    with psycopg.connect(appdb) as conn:
        assert lock_job(conn, job) == get_job(conn, job)
        assert lock_job(conn, str(uuid.uuid4())) is None


def test_a_claim_waits_for_lock_job_and_then_sees_the_committed_status(appdb, job):
    results = {}
    control = psycopg.connect(appdb)
    admission = psycopg.connect(appdb)
    try:
        assert lock_job(control, job).status == "accepted"

        def claim():
            results["claim"] = transition(admission, job, "accepted", "admitted")
            admission.commit()

        thread = threading.Thread(target=claim)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive(), "the claim waits for the lock of the control decision"
        request_action(control, job, "cancel")
        results["cancel"] = transition(control, job, "accepted", "cancelled", row_only=True)
        control.commit()
        thread.join(timeout=30)
    finally:
        control.close()
        admission.close()
    assert results["cancel"] == Moved("cancelled")
    assert results["claim"] == Lost("cancelled")


def test_a_claim_committed_before_lock_job_is_what_the_lock_reads(appdb, job):
    with psycopg.connect(appdb, autocommit=True) as admission:
        assert transition(admission, job, "accepted", "admitted") == Moved("admitted")
    with psycopg.connect(appdb) as control:
        assert lock_job(control, job).status == "admitted"
