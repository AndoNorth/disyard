"""The publisher and the reconciler against Postgres and the pinned RabbitMQ, publishing as the
broker user `dispatch` with wdf_broker's own client."""

from datetime import timedelta

import psycopg
import pytest
from wdf_broker.client import Credentials, connect
from wdf_broker.testing.container import broker_container
from wdf_dispatchapi.publish import Publisher
from wdf_dispatchapi.reconciler import Reconciler
from wdf_dispatchapi.submit import SCHEMA_VERSION, accepted_event
from wdf_events import to_bytes
from wdf_eventstore import insert_events
from wdf_jobstate import JobRow, get_job, insert_accepted
from wdf_keys import batch_id, job_id
from wdf_temporal import new_traceparent

pytestmark = [pytest.mark.integration, pytest.mark.pair]

QUEUE = "intake.acme.standard"
SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MANIFEST = {
    "uri": f"s3://wdf-sources/sha256/9f/{SHA}",
    "sha256": SHA,
    "size": 2048,
    "name": "manifest.json",
    "kind": "manifest",
}


@pytest.fixture(scope="module")
def rabbitmq(t1):
    with broker_container(t1) as broker:
        yield broker


@pytest.fixture
def dispatch(logins):
    with psycopg.connect(logins["dispatch"], autocommit=True) as connection:
        yield connection


def seed(conn, batch_key: str, count: int, priority_class: str = "standard") -> list[str]:
    """Inserts rows and their accepted events as the submit sequence does, with no receipt."""
    rows = [
        JobRow(
            job_id=job_id("acme", f"{batch_key}/{index}"),
            tenant="acme",
            submission_key=f"{batch_key}/{index}",
            batch_id=batch_id("acme", batch_key),
            priority_class=priority_class,
            job_class="short",
            manifest_ref=MANIFEST,
            schema_version=SCHEMA_VERSION,
            traceparent=new_traceparent(),
            seed=index,
            parameters={"n": index},
            fingerprint=SHA,
        )
        for index in range(count)
    ]
    insert_accepted(conn, rows)
    stored = [get_job(conn, row.job_id) for row in rows]
    insert_events(conn, [accepted_event(row, row.created_at) for row in stored])
    return [row.job_id for row in rows]


def test_publish_rabbitmq(rabbitmq, dispatch, db):
    """A submit call's publish and a reconciler pass each land their rows' stored events on
    `intake.acme.standard` with a receipt and one `job.published`, while a priority class outside
    the topology is unroutable and keeps no receipt."""
    credentials = Credentials("dispatch", rabbitmq.passwords["dispatch"])
    publisher = Publisher(connect, rabbitmq.address, credentials)
    submitted = seed(dispatch, "submitted", 2)
    events = [
        accepted_event(row, row.created_at) for row in (get_job(dispatch, j) for j in submitted)
    ]
    assert publisher.publish(dispatch, events)
    reconciled = seed(dispatch, "reconciled", 3)
    unroutable = seed(dispatch, "gold", 1, priority_class="gold")
    Reconciler(publisher, grace=timedelta(0)).run_pass(dispatch)

    admission = connect(rabbitmq.address, Credentials("admission", rabbitmq.passwords["admission"]))
    try:
        deliveries = admission.consumer(QUEUE).pull(5, 10)
        bodies = sorted(delivery.body for delivery in deliveries)
        for delivery in deliveries:
            delivery.ack()
    finally:
        admission.close()

    stored = db.execute(
        "SELECT body FROM event WHERE type = 'dev.wdf.job.accepted.v1'"
        " AND job_id = ANY(%s::uuid[])",
        (submitted + reconciled,),
    ).fetchall()
    assert bodies == sorted(body.encode() for (body,) in stored)
    published = dict(
        db.execute("SELECT job_id::text, published_at IS NOT NULL FROM accepted_job").fetchall()
    )
    assert published == {**dict.fromkeys(submitted + reconciled, True), unroutable[0]: False}
    count = db.execute(
        "SELECT count(*) FROM event WHERE type = 'dev.wdf.job.published.v1'"
    ).fetchone()[0]
    assert count == 5
    assert to_bytes(events[0]) in bodies
