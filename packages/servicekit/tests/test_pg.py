import socket
import time

import psycopg
import pytest
from dsy_servicekit import pg


@pytest.fixture
def blackhole():
    """A listener that never answers: the handshake completes, the startup packet is never read."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        yield sock.getsockname()[1]


def test_connect_timeout_bounds_a_blackhole(blackhole):
    start = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        pg.connect(f"host=127.0.0.1 port={blackhole} dbname=x", connect_timeout=2)
    assert time.monotonic() - start < 3


@pytest.fixture
def seen(monkeypatch):
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: calls.append((dsn, kw)))
    return calls


@pytest.mark.parametrize("given, want", [(0, 2), (1, 2), (2, 2), (5, 5)])
def test_connect_timeout_floor(seen, given, want):
    pg.connect("dsn", connect_timeout=given)
    assert seen[0][1]["connect_timeout"] == want


def test_defaults(seen):
    pg.connect("dsn")
    assert seen == [("dsn", {"autocommit": False, "connect_timeout": pg.DEFAULT_CONNECT_TIMEOUT_S})]
    assert pg.MIN_CONNECT_TIMEOUT_S == 2


def test_timeouts_become_server_options(seen):
    pg.connect("dsn", autocommit=True, lock_timeout="10s", statement_timeout="1min")
    assert seen[0][1]["options"] == "-c lock_timeout=10s -c statement_timeout=1min"
    assert seen[0][1]["autocommit"] is True
