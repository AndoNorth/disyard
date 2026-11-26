"""An unreachable database fails the retrier and reconciler connects inside a bound.

The database is a listening socket that never accepts: the kernel completes the TCP handshake and
the server never answers, which is what a black-holed Postgres looks like to libpq.
"""

import socket
import time
from contextlib import closing

import psycopg
import pytest
from dsy_dispatchapi.reconciler import Reconciler
from dsy_dispatchapi.retrier import Retrier

LIMIT_S = 4.0


@pytest.fixture
def blackhole_dsn():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield f"postgresql://dispatch@127.0.0.1:{sock.getsockname()[1]}/dsy"


def _fails_within_the_bound(attempt):
    began = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        attempt()
    assert time.monotonic() - began < LIMIT_S


def test_retrier_delivery_connect_is_bounded(blackhole_dsn):
    retrier = Retrier(blackhole_dsn, blackhole_dsn, gateway=None)
    _fails_within_the_bound(retrier.deliver)


def test_retrier_finisher_connect_is_bounded(blackhole_dsn):
    retrier = Retrier(blackhole_dsn, blackhole_dsn, gateway=None)
    _fails_within_the_bound(retrier.finish)


def test_reconciler_pass_gives_up_and_the_loop_goes_on(blackhole_dsn):
    reconciler = Reconciler(publisher=None, interval_s=0.0)
    began = time.monotonic()
    # The first pass fails on its connect, and the stop ends the loop at the wait after it.
    original = reconciler._stop.wait
    reconciler._stop.wait = lambda timeout=None: (reconciler.stop(), original(0))[1]
    reconciler.publisher = type("P", (), {"stopped": False})()
    reconciler.run(blackhole_dsn)
    assert time.monotonic() - began < LIMIT_S
