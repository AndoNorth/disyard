"""The database client against a fake connection, so no database is needed."""

from datetime import UTC, datetime

from dsy_acceptance.clients import postgres

JOB = "3a9617fa-df7c-5760-a644-42d559e0f433"


class FakeConn:
    def __init__(self, rows=()):
        self.rows, self.read_only, self.executed = rows, None, []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


def test_connect_is_read_only_as_the_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("DSY_STATE_DIR", str(tmp_path))
    (tmp_path / "appdb.credential").write_text("s3cret\n")
    seen, conn = [], FakeConn()

    def connect(dsn, **kwargs):
        seen.append((dsn, kwargs))
        return conn

    monkeypatch.setattr(postgres.psycopg, "connect", connect)
    assert postgres.connect("127.0.0.1:5433") is conn
    assert seen == [
        (
            "postgresql://postgres@127.0.0.1:5433/postgres",
            {"password": "s3cret", "autocommit": True},
        )
    ]
    assert conn.read_only is True


def test_job_is_the_row_of_the_jobstate_library(monkeypatch):
    monkeypatch.setattr(postgres, "get_job", lambda conn, job_id: (conn, job_id))
    assert postgres.job("conn", JOB) == ("conn", JOB)


def test_events_are_rows_of_the_job_in_stored_order():
    when = datetime(2026, 11, 20, 9, tzinfo=UTC)
    rows = [
        (f"{JOB}#a", when, "dsy.job.accepted", "dispatch", "acme", JOB, None, None, "{}"),
        (f"{JOB}#b", when, "dsy.job.completed", "workflow", "acme", JOB, None, None, "{}"),
    ]
    conn = FakeConn(rows)
    found = postgres.events(conn, JOB)
    assert [e.type for e in found] == ["dsy.job.accepted", "dsy.job.completed"]
    assert conn.executed[0][1] == (JOB,)
