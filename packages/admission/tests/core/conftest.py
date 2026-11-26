"""Rows seeded as the dispatch API would, and a login of admission's group."""

import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from admission_tests_support import MANIFEST, row_of
from dsy_events import build_event
from dsy_eventstore import insert_events
from dsy_jobstate import insert_accepted, mark_published
from dsy_temporal import new_traceparent


def accepted_event(
    job_id: str, tenant: str = "acme", priorityclass: str = "standard", jobclass: str = "short"
):
    return build_event(
        "job.accepted",
        source="/dsy/dispatch",
        tenant=tenant,
        jobid=job_id,
        priorityclass=priorityclass,
        jobclass=jobclass,
        traceparent=new_traceparent(),
        tracestate="vendor=1",
        time=datetime.now(UTC),
        data={
            "manifest": MANIFEST,
            "batch_id": str(uuid.uuid4()),
            "submission_key": f"key-{job_id}",
        },
    )


@pytest.fixture
def admission_dsn(login):
    """The DSN of a fresh login in `dsy_admission`, the group of admission's service login."""
    return login("dsy_admission")


@pytest.fixture
def admission_db(admission_dsn):
    with psycopg.connect(admission_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def seed_event(db):
    """A row and its accepted event, inserted in one transaction and then marked published, as the
    submit sequence does, and the event. `published=False` stops before the mark."""

    def insert(published: bool = True, tenant: str = "acme", jobclass: str = "short"):
        event = accepted_event(str(uuid.uuid4()), tenant=tenant, jobclass=jobclass)
        with db.transaction():
            insert_accepted(db, [row_of(event)])
            insert_events(db, [event])
        if published:
            mark_published(db, event.jobid)
        return event

    return insert


@pytest.fixture
def seed(seed_event):
    """The job id of a row seeded by `seed_event`."""
    return lambda published=True: seed_event(published).jobid


@pytest.fixture
def claimed_row():
    """A row as a claim leaves it, held in memory only, for a start that needs no database."""
    return lambda: row_of(accepted_event(str(uuid.uuid4()))).model_copy(
        update={"status": "admitted", "created_at": datetime.now(UTC)}
    )


@pytest.fixture
def events(db):
    """The types and sources of a job's stored events, in order."""
    return lambda job_id: db.execute(
        "SELECT type, source FROM event WHERE job_id = %s ORDER BY event_time, event_id", (job_id,)
    ).fetchall()


@pytest.fixture
def accepted_event_of():
    """Builds an accepted event as dispatch would, for a job that may have no row."""
    return accepted_event
