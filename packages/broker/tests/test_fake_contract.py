"""FakeBroker passes the conformance test, the conformance test fails on a fake that breaks the
settle or publish semantics, and the fake loses held deliveries and withholds outcomes as RabbitMQ
4.4.0 did in the S4b spike."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from wft_broker.models import BrokerUnavailable, Outcome, PublishTimeout
from wft_broker.testing import FakeBroker, broker_contract
from wft_broker.testing import fake as fake_module
from wft_broker.testing.conformance import CHECKS

QUEUE = "intake.acme.standard"


def connect_to(broker: FakeBroker):
    return lambda user: broker.connect()


@pytest.mark.parametrize("check", CHECKS)
def test_fake_contract_passes(check):
    assert broker_contract(connect_to(FakeBroker()), checks=[check]) == [check]


def test_fake_contract_passes_every_check_on_one_broker():
    assert broker_contract(connect_to(FakeBroker())) == list(CHECKS)


def _over_deliver(self, n, timeout_s):
    with self._broker._lock:
        return self._take(n + 1)


def _dead_letter_without_reason(self, reason, detail):
    if reason not in self._consumer._broker._codes:
        raise ValueError(reason)
    self._settle((True, True, {}))


def _accept_unknown_keys(original):
    def publish(self, routing_key, body, timeout_s, live):
        if routing_key not in self.queues:
            return Outcome.ACCEPTED
        return original(self, routing_key, body, timeout_s, live)

    return publish


BROKEN = {
    "over-delivers on one credit": (fake_module.Consumer, "pull", _over_deliver),
    "retries with delivery-failed=false": (
        fake_module.Delivery,
        "retry",
        lambda self: self._settle((False, False, {})),
    ),
    "dead-letters without the reason annotation": (
        fake_module.Delivery,
        "dead_letter",
        _dead_letter_without_reason,
    ),
    "accepts a publish to an unknown routing key": (
        FakeBroker,
        "_publish",
        _accept_unknown_keys(FakeBroker._publish),
    ),
}
# The first check that each broken fake fails, and why.
FAILS_AT = {
    "over-delivers on one credit": "one_message_per_credit: one credit with two messages ready "
    "gave 2 deliveries",
    "retries with delivery-failed=false": "retry_counts_and_dead_letters_after_the_limit: "
    "after 1 retries the delivery count is 0",
    "dead-letters without the reason annotation": "dead_letter_carries_the_reason: "
    "the dead letter carries",
    "accepts a publish to an unknown routing key": "publish_outcomes: a publish to an unknown "
    "routing key was Outcome.ACCEPTED",
}


@pytest.mark.parametrize("broken", BROKEN)
def test_fake_contract_fails_a_broken_fake(monkeypatch, broken):
    monkeypatch.setattr(*BROKEN[broken])
    with pytest.raises(AssertionError, match=f"^{re.escape(FAILS_AT[broken])}"):
        broker_contract(connect_to(FakeBroker()))


def test_fake_restart_raises_on_old_connections_and_counts_held_deliveries():
    broker = FakeBroker()
    old = broker.connect()
    for _ in range(2):
        assert old.publish(QUEUE, b"m") is Outcome.ACCEPTED
    consumer = old.consumer(QUEUE)
    held = consumer.pull(1, 1.0)[0]
    broker.restart()
    with pytest.raises(BrokerUnavailable):
        old.publish(QUEUE, b"m")
    with pytest.raises(BrokerUnavailable):
        consumer.pull(1, 1.0)
    with pytest.raises(BrokerUnavailable):
        held.ack()
    new = broker.connect().consumer(QUEUE)
    assert [d.delivery_count for d in new.drain(5, 1.0)] == [1, 0]


def test_fake_close_dead_letters_a_held_last_delivery():
    broker = FakeBroker()
    connection = broker.connect()
    connection.publish(QUEUE, b"m")
    consumer = connection.consumer(QUEUE)
    for _ in range(5):
        consumer.pull(1, 1.0)[0].retry()
    assert consumer.pull(1, 1.0)[0].last
    connection.close()
    dead = broker.connect().consumer("intake.dlq").drain(5, 1.0)
    assert [d.annotations["x-last-death-reason"] for d in dead] == ["delivery_limit"]


def test_fake_alarm_times_out_a_publish_and_the_connection_stays_usable():
    broker = FakeBroker()
    connection = broker.connect()
    broker.alarm(True)
    with pytest.raises(PublishTimeout):
        connection.publish(QUEUE, b"m", timeout_s=0.05)
    broker.alarm(False)
    assert connection.publish(QUEUE, b"m") is Outcome.ACCEPTED


def test_fake_publish_from_threads_gives_one_outcome_each():
    broker = FakeBroker()
    connection = broker.connect()
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda i: connection.publish(QUEUE, b"%d" % i), range(64)))
    assert outcomes == [Outcome.ACCEPTED] * 64
    assert len(connection.consumer(QUEUE).drain(100, 1.0)) == 64
