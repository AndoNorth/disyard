"""The breakers alone: which errors are outages, the backoff, and the streak of one error class."""

import psycopg
import pytest
from temporalio.service import RPCError, RPCStatusCode
from wft_admission.breaker import MAX_BACKOFF_S, STREAK, Breaker, Breakers, dependency_of
from wft_broker.models import BrokerUnavailable, TopologyError


@pytest.mark.parametrize(
    ("error", "dependency"),
    [
        (RPCError("x", RPCStatusCode.UNAVAILABLE, b""), "temporal"),
        (RPCError("x", RPCStatusCode.DEADLINE_EXCEEDED, b""), "temporal"),
        (RPCError("x", RPCStatusCode.RESOURCE_EXHAUSTED, b""), "temporal"),
        (RPCError("x", RPCStatusCode.INVALID_ARGUMENT, b""), None),
        (psycopg.OperationalError("x"), "database"),
        (psycopg.errors.UniqueViolation("x"), None),
        (BrokerUnavailable("x"), "broker"),
        (TopologyError("x"), None),
        (ValueError("x"), None),
    ],
)
def test_outage_dependency(error, dependency):
    assert dependency_of(error) == dependency


def test_backoff_doubles_to_its_ceiling_and_closes():
    breaker = Breaker()
    waits = []
    for _ in range(7):
        breaker.fail(100.0)
        waits.append(breaker.due - 100.0)
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, MAX_BACKOFF_S, MAX_BACKOFF_S]
    breaker.wait(200.0)
    assert (breaker.due, breaker.open) == (200.0 + MAX_BACKOFF_S, True)
    breaker.close()
    assert not breaker.open
    breaker.fail(300.0)
    assert breaker.due == 301.0


def test_streak_of_distinct_messages_opens_internal():
    breakers = Breakers()
    for n in range(STREAK - 1):
        assert not breakers.failed("ValueError", b"m%d" % n, 0.0)
    # A message failing again is not a new one.
    assert not breakers.failed("ValueError", b"m0", 0.0)
    assert breakers.failed("ValueError", b"last", 0.0)
    assert breakers.gauge() == {"temporal": 0, "database": 0, "broker": 0, "internal": 1}


def test_streak_breaks_on_another_class_or_a_success():
    breakers = Breakers()
    for n in range(STREAK - 1):
        breakers.failed("ValueError", b"m%d" % n, 0.0)
    assert not breakers.failed("KeyError", b"k", 0.0)
    for n in range(STREAK - 2):
        breakers.failed("KeyError", b"k%d" % n, 0.0)
    breakers.succeeded()
    assert not breakers.failed("KeyError", b"after", 0.0)
    assert not breakers.of["internal"].open
