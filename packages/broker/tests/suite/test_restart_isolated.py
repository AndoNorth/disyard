"""A broker restart, each test on a container of its own, which only runs under `-m isolated`."""

import subprocess

import pytest
from wdf_broker import suite
from wdf_broker.models import BrokerUnavailable, Outcome
from wdf_broker.testing.container import _wait_ready, broker_container

pytestmark = [pytest.mark.integration, pytest.mark.isolated]

QUEUE = suite.intake_queue()
GOLDEN = suite.golden()[suite.ACCEPTED]


@pytest.fixture
def broker(t1):
    with broker_container(t1) as broker:
        yield broker


def restart(broker) -> None:
    """Restarts the container, which keeps its data as a pod keeps its volume, and waits until every
    queue answers again."""
    subprocess.run(["docker", "restart", broker.container.id], check=True, capture_output=True)
    _wait_ready(broker)


def test_restart_keeps_messages(broker, open_as):
    bodies = list(suite.golden().values())[:5]
    publisher, admission = open_as("dispatch"), open_as("admission")
    consumer = admission.consumer(QUEUE)
    for body in bodies:
        assert publisher.publish(QUEUE, body) is Outcome.ACCEPTED

    restart(broker)
    with pytest.raises(BrokerUnavailable):
        publisher.publish(QUEUE, GOLDEN)
    with pytest.raises(BrokerUnavailable):
        consumer.pull(1, suite.TIMEOUT_S)
    with pytest.raises(BrokerUnavailable):
        admission.consumer(QUEUE)

    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    got = consumer.pull(len(bodies), suite.TIMEOUT_S)
    assert [(d.body, d.delivery_count) for d in got] == [(body, 0) for body in bodies]
    for delivery in got:
        delivery.ack()
    assert publisher.publish(QUEUE, GOLDEN) is Outcome.ACCEPTED
    (after,) = consumer.pull(1, suite.TIMEOUT_S)
    assert after.body == GOLDEN
    after.ack()


def test_held_delivery_across_restart(broker, open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    assert publisher.publish(QUEUE, b"last") is Outcome.ACCEPTED
    for _ in range(suite.LIMIT):
        consumer.pull(1, suite.TIMEOUT_S)[0].retry()
    (last,) = consumer.pull(1, suite.TIMEOUT_S)
    assert last.last
    assert publisher.publish(QUEUE, GOLDEN) is Outcome.ACCEPTED
    (held,) = consumer.pull(1, suite.TIMEOUT_S)
    assert held.delivery_count == 0

    restart(broker)
    with pytest.raises(BrokerUnavailable):
        held.ack()

    # A held delivery lost to a restart counts as failed, so it comes back with its count raised by
    # one, and a held last delivery dead-letters as delivery_limit.
    consumer = open_as("admission").consumer(QUEUE)
    (back,) = consumer.pull(1, suite.TIMEOUT_S)
    assert (back.body, back.delivery_count) == (GOLDEN, 1)
    back.ack()
    (dead,) = open_as("operator").consumer(suite.DLQ).pull(1, suite.TIMEOUT_S)
    assert dead.body == b"last"
    assert dead.annotations["x-last-death-reason"] == "delivery_limit"
    dead.ack()
    assert consumer.drain(1, suite.TIMEOUT_S) == []
