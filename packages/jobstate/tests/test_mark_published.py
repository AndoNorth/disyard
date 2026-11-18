"""The receipt time tells the writer that set it, so only that writer records the event."""

import threading
import uuid

import psycopg
import pytest
from wdf_jobstate import JobNotFound, JobRow, get_job, insert_accepted, mark_published

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
            "uri": f"s3://wdf-sources/sha256/aa/{SHA}",
            "sha256": SHA,
            "size": 7,
            "name": "m",
            "kind": "manifest",
        },
        schema_version="wdf.job.accepted/3",
        traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        seed=3,
        parameters={"a": 1},
        fingerprint="b" * 64,
    )
    with psycopg.connect(appdb, autocommit=True) as setup:
        insert_accepted(setup, [row])
    return row.job_id


def test_mark_published_returns_the_time_only_to_the_call_that_set_it(appdb, job):
    with psycopg.connect(appdb, autocommit=True) as conn:
        first = mark_published(conn, job)
        assert first is not None and first == get_job(conn, job).published_at
        assert mark_published(conn, job) is None
        assert get_job(conn, job).published_at == first


def test_mark_published_of_an_unknown_job_raises(appdb):
    with psycopg.connect(appdb, autocommit=True) as conn, pytest.raises(JobNotFound):
        mark_published(conn, str(uuid.uuid4()))


def test_of_two_concurrent_mark_published_calls_exactly_one_returns_a_time(appdb, job):
    results = {}
    first = psycopg.connect(appdb)
    second = psycopg.connect(appdb)
    try:
        results["first"] = mark_published(first, job)

        def late():
            results["second"] = mark_published(second, job)
            second.commit()

        thread = threading.Thread(target=late)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive(), "the second call waits for the row lock of the first"
        first.commit()
        thread.join(timeout=30)
    finally:
        first.close()
        second.close()
    assert results["first"] is not None
    assert results["second"] is None
