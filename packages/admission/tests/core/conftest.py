"""Rows seeded as the dispatch API would, a login of admission's group, and a dev server that knows
the search attributes the cluster's bootstrap adds."""

import asyncio
import time
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from dsy_events import build_event
from dsy_eventstore import insert_events
from dsy_jobstate import JobRow, insert_accepted, mark_published
from dsy_temporal import event_data_converter, new_traceparent, tracing_interceptor
from dsy_workflow.contract import naming
from dsy_workflow.testing import connect
from psycopg import sql
from psycopg.conninfo import make_conninfo
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.client import Client

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MANIFEST = {
    "uri": f"s3://dsy-sources/sha256/9f/{SHA}",
    "sha256": SHA,
    "size": 2048,
    "name": "manifest.json",
    "kind": "manifest",
}
TYPES = {
    "keyword": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "datetime": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
}


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


def row_of(event) -> JobRow:
    return JobRow(
        job_id=event.jobid,
        tenant=event.tenant,
        submission_key=event.data["submission_key"],
        batch_id=event.data["batch_id"],
        priority_class=event.priorityclass,
        job_class=event.jobclass,
        manifest_ref=event.data["manifest"],
        schema_version=event.dataschema,
        traceparent=event.traceparent,
        tracestate=event.tracestate,
        seed=7,
        parameters={"n": 3},
        fingerprint=SHA,
    )


@pytest.fixture
def db(appdb):
    with psycopg.connect(appdb, autocommit=True) as connection:
        yield connection


@pytest.fixture
def admission_dsn(appdb, db):
    """The DSN of a fresh login in `dsy_admission`, the group of admission's service login."""
    user = f"admission_{time.time_ns()}"
    db.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE dsy_admission").format(
            sql.Identifier(user)
        )
    )
    yield make_conninfo(appdb, user=user, password="standin")
    db.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (user,))
    db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
    db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


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
def status(db):
    return lambda job_id: db.execute(
        "SELECT status FROM accepted_job WHERE job_id = %s", (job_id,)
    ).fetchone()[0]


@pytest.fixture(scope="module")
def server(temporal) -> str:
    """The dev server's address, once it knows the search attributes of naming.yaml."""

    async def register():
        client = await connect(temporal)
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                namespace=naming().namespace,
                search_attributes={
                    name: TYPES[kind] for name, kind in naming().search_attributes.items()
                },
            )
        )

    asyncio.run(register())
    return temporal


@pytest.fixture
def client(server):
    """Connects a client set up as the admission service sets it up, inside the test's loop."""

    async def connected() -> Client:
        await connect(server)
        return await Client.connect(
            server,
            namespace=naming().namespace,
            data_converter=event_data_converter,
            interceptors=[tracing_interceptor()],
        )

    return connected


@pytest.fixture
def accepted_event_of():
    """Builds an accepted event as dispatch would, for a job that may have no row."""
    return accepted_event
