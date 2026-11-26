"""The `appdb`, `db` and `login` fixtures, a pytest plugin registered by the package entry point."""

import contextlib
import itertools
import time

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .runner import apply, discover

PASSWORD = "dsy"
IMAGE = "timescale/timescaledb"
TELEMETRY_OFF = ("-c", "timescaledb.telemetry_level=off")


def _connect(port: int, dbname: str = "postgres", timeout: float = 90) -> psycopg.Connection:
    """The published port accepts connections before the server is up, so retry until it answers."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="postgres",
                password=PASSWORD,
                dbname=dbname,
                autocommit=True,
            )
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="module")
def appdb_server(t1):
    """One pinned TimescaleDB container for a test module, yielding its published port."""
    with t1.container(
        IMAGE, ports={0: 5432}, command=TELEMETRY_OFF, env={"POSTGRES_PASSWORD": PASSWORD}
    ) as container:
        _connect(container.host_ports[0]).close()
        yield container.host_ports[0]


@pytest.fixture
def empty_db(appdb_server):
    """A fresh database with nothing applied, yielding its DSN."""
    names = itertools.count()
    with _connect(appdb_server) as admin:
        name = f"t{time.time_ns()}_{next(names)}"
        admin.execute(f'CREATE DATABASE "{name}"')
    yield f"postgresql://postgres:{PASSWORD}@127.0.0.1:{appdb_server}/{name}"


@pytest.fixture
def appdb(empty_db):
    """A fresh database with every installed migration applied, yielding its DSN."""
    apply(empty_db, discover())
    return empty_db


@pytest.fixture
def db(appdb):
    """A superuser autocommit connection to the `appdb` database."""
    with psycopg.connect(appdb, autocommit=True) as connection:
        yield connection


@contextlib.contextmanager
def logins(db, dsn: str):
    """A factory of fresh group logins, each dropped on exit despite leaked connections."""
    users: list[str] = []

    def login(group: str) -> str:
        user = f"standin_{group}_{time.time_ns()}_{len(users)}"
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE {}").format(
                sql.Identifier(user), sql.Identifier(group)
            )
        )
        users.append(user)
        return make_conninfo(dsn, user=user, password="standin")

    try:
        yield login
    finally:
        for user in users:
            db.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (user,)
            )
            db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
            db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


@pytest.fixture
def login(appdb, db):
    """`login(group)` returns the DSN of a fresh login in a group role, dropped after the test."""
    with logins(db, appdb) as factory:
        yield factory
