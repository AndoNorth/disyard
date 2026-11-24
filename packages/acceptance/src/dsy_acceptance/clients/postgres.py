"""Read-only reads of the application database, through the library that owns each table, so the
suite shows what a row holds without any way to write one."""

import psycopg
from dsy_clusterkit import appdb_password
from dsy_eventstore import EventRow, events_for_job
from dsy_jobstate import JobRow, get_job


def connect(address: str) -> psycopg.Connection:
    """A connection to the database at `host:port` as the operator, whose session refuses every
    write."""
    conn = psycopg.connect(
        f"postgresql://postgres@{address}/postgres", password=appdb_password(), autocommit=True
    )
    conn.read_only = True
    return conn


def job(conn: psycopg.Connection, job_id: str) -> JobRow | None:
    """The accepted-job row, or None when the job was never accepted."""
    return get_job(conn, job_id)


def events(conn: psycopg.Connection, job_id: str) -> list[EventRow]:
    """The event rows of the job in the order the facts happened."""
    return events_for_job(conn, job_id)
