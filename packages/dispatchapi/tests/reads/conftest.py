"""Rows and events seeded as the submit sequence, control and the workflow would write them, and a
connection of the reader login that every read runs on."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from wdf_events import build_event
from wdf_eventstore import insert_events
from wdf_jobstate import JobRow, insert_accepted
from wdf_keys import batch_id, job_id
from wdf_temporal import new_traceparent

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MANIFEST = {
    "uri": f"s3://wdf-sources/sha256/9f/{SHA}",
    "sha256": SHA,
    "size": 2048,
    "name": "manifest.json",
    "kind": "manifest",
}
EPOCH = datetime(2026, 11, 18, 12, tzinfo=UTC)


@pytest.fixture
def reader(logins):
    with psycopg.connect(logins["reader"], autocommit=True) as connection:
        yield connection


@pytest.fixture
def seed(db):
    """Inserts the rows of `count` jobs of a batch at accepted and returns their rows."""

    def insert(
        batch_key: str = "batch",
        count: int = 1,
        tenant: str = "acme",
        job_class: str = "short",
        start: int = 0,
    ) -> list[JobRow]:
        rows = [
            JobRow(
                job_id=job_id(tenant, f"{batch_key}/{index}"),
                tenant=tenant,
                submission_key=f"{batch_key}/{index}",
                batch_id=batch_id(tenant, batch_key),
                priority_class="standard",
                job_class=job_class,
                manifest_ref=MANIFEST,
                schema_version="urn:wdf:schema:job.accepted:1.0",
                traceparent=new_traceparent(),
                seed=index,
                parameters={"n": index},
                fingerprint=SHA,
            )
            for index in range(start, start + count)
        ]
        insert_accepted(db, rows)
        return rows

    return insert


@pytest.fixture
def record(db):
    """Stores one event of a job's row, `minutes` after a fixed time."""

    def insert(row: JobRow, name: str, data: dict, minutes: int = 0, **attributes) -> None:
        event = build_event(
            name,
            tenant=row.tenant,
            jobid=row.job_id,
            priorityclass=row.priority_class,
            jobclass=row.job_class,
            traceparent=row.traceparent,
            time=EPOCH + timedelta(minutes=minutes),
            data=data,
            **attributes,
        )
        insert_events(db, [event])

    return insert
