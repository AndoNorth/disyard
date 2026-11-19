"""The client against a broker container: publish outcomes and errors, credit, delivery counts, the
settle calls, the browse of the dead-letter queue, publishes under a memory alarm and from threads,
and the counters of the instrumentation contract."""

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from wdf_broker import client
from wdf_broker import topology as rendered
from wdf_broker.client import Credentials, connect
from wdf_broker.contract import topology
from wdf_broker.models import BrokerUnavailable, Outcome, PublishTimeout, TopologyError
from wdf_broker.testing import FakeBroker, broker_contract
from wdf_broker.testing.conformance import CHECKS
from wdf_broker.testing.container import broker_container
from wdf_events.metrics import registry

pytestmark = pytest.mark.integration

QUEUE = "intake.acme.standard"
DLQ = "intake.dlq"
UNKNOWN = "intake.no-such-tenant.no-such-class"
LIMIT = topology().intake_policy.definition.delivery_limit
MAX_LENGTH = topology().intake_policy.definition.max_length.value
# Above the longest delayed retry, 1 s times the delivery count before a dead letter.
TIMEOUT_S = 10
# Above the drain of a full intake queue plus the longest delayed retry.
EMPTY_TIMEOUT_S = 30


@pytest.fixture(scope="module")
def broker(t1):
    with broker_container(t1) as broker:
        yield broker


@pytest.fixture
def open_as(broker):
    """Opens connections as a broker user, closes them after the test and empties the queues."""
    opened = []

    def open_as(user: str) -> client.Connection:
        connection = connect(broker.address, Credentials(user, broker.passwords[user]))
        opened.append(connection)
        return connection

    yield open_as
    for connection in opened:
        connection.close()
    operator = connect(broker.address, Credentials("operator", broker.passwords["operator"]))
    consumers = [operator.consumer(queue) for queue in (QUEUE, DLQ)]

    def emptied() -> list[int]:
        for consumer in consumers:
            for delivery in consumer.drain(100, TIMEOUT_S):
                delivery.ack()
        return held(broker)

    # A closed connection hands back as failed deliveries what it held unsettled and what it acked
    # too late for the broker to apply, and a failed delivery waits out its delayed retry where no
    # drain sees it. So the queues are empty only once the broker holds nothing, and the operator
    # closes only then, so that none of its own acks is lost.
    assert eventually(emptied, [0, 0], EMPTY_TIMEOUT_S) == [0, 0], "the queues are not empty"
    operator.close()


def eventually(probe, expected, timeout_s: float = TIMEOUT_S):
    """Polls probe until it returns expected, because the broker reports its state late."""
    deadline = time.monotonic() + timeout_s
    while (value := probe()) != expected and time.monotonic() < deadline:
        time.sleep(0.2)
    return value


def ready_and_unacked(broker) -> tuple[int, int]:
    rows = json.loads(
        broker.ctl(
            "list_queues",
            "name",
            "messages_ready",
            "messages_unacknowledged",
            "--formatter",
            "json",
        )
    )
    row = next(row for row in rows if row["name"] == QUEUE)
    return row["messages_ready"], row["messages_unacknowledged"]


def held(broker) -> list[int]:
    """The messages the intake queue and the dead-letter queue hold, ready, delivered and not yet
    settled, waiting out a delayed retry or waiting for the dead-letter worker, read from the state
    of each queue rather than from list_queues, which reports it late."""
    names = ", ".join(f'<<"{queue}">>' for queue in (QUEUE, DLQ))
    total = (
        "[begin"
        ' {ok, Q} = rabbit_amqqueue:lookup(rabbit_misc:r(<<"/">>, queue, Name)),'
        " {ok, {_, N}, _} = rabbit_fifo_client:local_query("
        "amqqueue:get_pid(Q), query_messages_total),"
        f" N end || Name <- [{names}]]."
    )
    return json.loads(broker.ctl("eval", total))


def fill(publisher: client.Connection) -> int:
    """Publishes until the queue rejects, and returns how many publishes were accepted."""
    accepted = 0
    while publisher.publish(QUEUE, b"%d" % accepted) is Outcome.ACCEPTED:
        accepted += 1
        assert accepted <= MAX_LENGTH + 1
    return accepted


def retried_to_last(publisher, consumer) -> client.Delivery:
    """Publishes one message, retries it until its last delivery and returns that delivery."""
    assert publisher.publish(QUEUE, b"last") is Outcome.ACCEPTED
    for _ in range(LIMIT):
        consumer.pull(1, TIMEOUT_S)[0].retry()
    (delivery,) = consumer.pull(1, TIMEOUT_S)
    return delivery


def alarm(broker, on: bool) -> None:
    """Raises a memory alarm by a 1 MB watermark, or clears it with the configured one, and waits
    until the broker reports it."""
    watermark = "1MB" if on else str(topology().memory.high_watermark_mib * 1024 * 1024)
    broker.ctl("set_vm_memory_high_watermark", "absolute", watermark)

    def raised() -> bool:
        alarms = broker.container.exec("rabbitmq-diagnostics", "-q", "alarms").stdout
        return "memory alarm" in alarms.lower()

    assert eventually(raised, on) == on, f"the memory alarm is not {'raised' if on else 'cleared'}"


@contextmanager
def intake_exchange_missing(broker):
    """Deletes the intake exchange, and imports the topology definitions again afterwards."""
    exchange = topology().intake_exchange.name
    # rabbitmqctl has no command that deletes an exchange, and no broker user may configure.
    name = f'rabbit_misc:r(<<"/">>, exchange, <<"{exchange}">>)'
    broker.ctl("eval", f'rabbit_exchange:delete({name}, false, <<"test">>).')
    try:
        yield
    finally:
        broker.ctl(
            "import_definitions", f"{rendered.DEFINITIONS_PATH}/{Path(rendered.TOPOLOGY).name}"
        )

        def bound() -> bool:
            rows = json.loads(broker.ctl("list_bindings", "source_name", "--formatter", "json"))
            return any(row["source_name"] == exchange for row in rows)

        assert eventually(bound, True), f"{exchange} is not bound again"


def test_fake_and_real_pass_contract(broker):
    fake = FakeBroker()
    assert broker_contract(lambda user: fake.connect()) == list(CHECKS)

    def real(user: str) -> client.Connection:
        return connect(broker.address, Credentials(user, broker.passwords[user]))

    assert broker_contract(real, timeout_s=TIMEOUT_S) == list(CHECKS)


def test_publish_outcomes(open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    assert publisher.publish(QUEUE, b"routed") is Outcome.ACCEPTED
    assert publisher.publish(UNKNOWN, b"unroutable") is Outcome.UNROUTABLE
    assert publisher.publish(DLQ, b"unroutable") is Outcome.UNROUTABLE
    (delivery,) = consumer.pull(1, TIMEOUT_S)
    assert (delivery.body, delivery.queue, delivery.delivery_count) == (b"routed", QUEUE, 0)
    delivery.ack()
    # RabbitMQ takes one message over max-length before it rejects.
    assert fill(publisher) == MAX_LENGTH + 1


def test_one_message_per_credit(broker, open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    for body in (b"first", b"second"):
        publisher.publish(QUEUE, body)
    first = consumer.pull(1, TIMEOUT_S)
    assert [d.body for d in first] == [b"first"]
    assert eventually(lambda: ready_and_unacked(broker), (1, 1)) == (1, 1)
    # A pull that gets fewer than its credit drains the rest, so a later message stays ready.
    second = consumer.pull(2, 1.0)
    assert [d.body for d in second] == [b"second"]
    publisher.publish(QUEUE, b"third")
    assert eventually(lambda: ready_and_unacked(broker), (1, 2)) == (1, 2)
    time.sleep(2)
    assert ready_and_unacked(broker) == (1, 2)
    for delivery in first + second:
        delivery.ack()


def test_drain_empty_returns_zero(open_as):
    consumer = open_as("admission").consumer(QUEUE)
    started = time.monotonic()
    assert consumer.drain(5, TIMEOUT_S) == []
    assert time.monotonic() - started < 1.0


def test_retry_counts(open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    publisher.publish(QUEUE, b"retried")
    counts = []
    for _ in range(4):
        (delivery,) = consumer.pull(1, TIMEOUT_S)
        counts.append(delivery.delivery_count)
        delivery.retry()
    assert counts == [0, 1, 2, 3]
    consumer.pull(1, TIMEOUT_S)[0].ack()


def test_dead_letter_on_sixth_delivery(open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    publisher.publish(QUEUE, b"dies")
    counts = []
    for _ in range(LIMIT + 1):
        (delivery,) = consumer.pull(1, TIMEOUT_S)
        counts.append(delivery.delivery_count)
        delivery.retry()
    assert counts == list(range(LIMIT + 1))
    (dead,) = open_as("operator").consumer(DLQ).pull(1, TIMEOUT_S)
    assert dead.body == b"dies"
    assert dead.annotations["x-last-death-reason"] == "delivery_limit"
    assert "x-opt-wdf-reason" not in dead.annotations
    assert consumer.drain(1, TIMEOUT_S) == []
    dead.ack()


def test_reason_in_same_settle(open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    publisher.publish(QUEUE, b"poison")
    consumer.pull(1, TIMEOUT_S)[0].dead_letter("MALFORMED", "not-json: settle test")
    (dead,) = open_as("operator").consumer(DLQ).pull(1, TIMEOUT_S)
    assert dead.body == b"poison"
    expected = {
        "x-opt-wdf-reason": "MALFORMED",
        "x-opt-wdf-detail": "not-json: settle test",
        "x-last-death-reason": "rejected",
        "x-first-death-queue": QUEUE,
    }
    assert {key: dead.annotations.get(key) for key in expected} == expected
    # A dead letter settles delivery-failed, so its copy counts one failed delivery.
    assert dead.delivery_count == 1
    dead.ack()


def test_missing_exchange_is_topology_error(broker, open_as):
    publisher = open_as("dispatch")
    with intake_exchange_missing(broker):
        with pytest.raises(TopologyError, match="amqp:not-found"):
            publisher.publish(QUEUE, b"nowhere")
    assert publisher.publish(QUEUE, b"usable") is Outcome.ACCEPTED


def test_browser_refused_on_intake(open_as):
    publisher, operator = open_as("dispatch"), open_as("operator")
    with pytest.raises(ValueError, match="intake queue"):
        operator.browser(QUEUE)
    publisher.publish(QUEUE, b"browsed")
    open_as("admission").consumer(QUEUE).pull(1, TIMEOUT_S)[0].dead_letter(
        "MALFORMED", "not-json: browse test"
    )
    browser = operator.browser(DLQ)
    for _ in range(3):
        (seen,) = browser.pull(1, TIMEOUT_S)
        assert (seen.body, seen.delivery_count) == (b"browsed", 1)
        seen.released()
    browser.pull(1, TIMEOUT_S)[0].ack()


def test_last_delivery_flagged(open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    publisher.publish(QUEUE, b"last")
    flags = []
    for _ in range(LIMIT):
        (delivery,) = consumer.pull(1, TIMEOUT_S)
        flags.append(delivery.last)
        delivery.retry()
    (last,) = consumer.pull(1, TIMEOUT_S)
    assert flags == [False] * LIMIT
    assert (last.last, last.delivery_count) == (True, LIMIT)
    last.dead_letter("ADMISSION_FAILED", "TimeoutError: last delivery test")
    (dead,) = open_as("operator").consumer(DLQ).pull(1, TIMEOUT_S)
    assert dead.annotations["x-opt-wdf-reason"] == "ADMISSION_FAILED"
    assert not dead.last
    dead.ack()


def test_release_uncounted(open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)
    for body in (b"held", b"behind"):
        publisher.publish(QUEUE, body)
    for _ in range(3):
        (delivery,) = consumer.pull(1, TIMEOUT_S)
        assert (delivery.body, delivery.delivery_count) == (b"held", 0)
        delivery.release()
    consumer.pull(1, TIMEOUT_S)[0].ack()
    assert consumer.pull(1, TIMEOUT_S)[0].body == b"behind"


def test_publish_times_out_under_alarm(broker, open_as):
    publisher = open_as("dispatch")
    assert publisher.publish(QUEUE, b"before") is Outcome.ACCEPTED
    alarm(broker, True)
    try:
        with pytest.raises(PublishTimeout):
            publisher.publish(QUEUE, b"blocked", timeout_s=3)
    finally:
        alarm(broker, False)
    assert publisher.publish(QUEUE, b"after", timeout_s=TIMEOUT_S) is Outcome.ACCEPTED


def test_publish_from_threads(open_as):
    publisher = open_as("dispatch")
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda i: publisher.publish(QUEUE, b"%d" % i), range(64)))
    assert outcomes == [Outcome.ACCEPTED] * 64
    consumer = open_as("admission").consumer(QUEUE)
    got = consumer.pull(64, TIMEOUT_S)
    assert sorted(d.body for d in got) == sorted(b"%d" % i for i in range(64))
    for delivery in got:
        delivery.ack()


def moved(before: Counter) -> dict[str, int]:
    """The counter series that moved since before, as `name{label=value}`."""
    return {
        f"{name}{{{','.join(f'{k}={v}' for k, v in labels)}}}": count - before[(name, labels)]
        for (name, labels), count in registry.counters.items()
        if count != before[(name, labels)]
    }


def test_counters(broker, open_as):
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)

    before = Counter(registry.counters)
    publisher.publish(QUEUE, b"m")
    publisher.publish(UNKNOWN, b"m")
    # A link attached before the exchange went missing stays attached, so a new connection attaches.
    with intake_exchange_missing(broker):
        with pytest.raises(TopologyError):
            open_as("dispatch").publish(QUEUE, b"m")
    assert moved(before) == {
        "wdf_broker_publish_total{outcome=accepted}": 1,
        "wdf_broker_publish_total{outcome=unroutable}": 1,
        "wdf_broker_publish_total{outcome=topology-error}": 1,
    }

    before = Counter(registry.counters)
    consumer.pull(1, TIMEOUT_S)[0].release()
    consumer.pull(1, TIMEOUT_S)[0].retry()
    consumer.pull(1, TIMEOUT_S)[0].ack()
    publisher.publish(QUEUE, b"m")
    consumer.pull(1, TIMEOUT_S)[0].dead_letter("UNSUPPORTED_VERSION", "major: counters test")
    open_as("operator").consumer(DLQ).pull(1, TIMEOUT_S)[0].ack()
    assert moved(before) == {
        "wdf_broker_publish_total{outcome=accepted}": 1,
        "wdf_broker_settle_total{call=release}": 1,
        "wdf_broker_settle_total{call=retry}": 1,
        "wdf_broker_settle_total{call=ack}": 1,
        "wdf_broker_settle_total{call=dead_letter}": 1,
        "wdf_broker_dead_letter_total{reason=UNSUPPORTED_VERSION}": 1,
    }

    last = retried_to_last(publisher, consumer)
    before = Counter(registry.counters)
    last.retry()
    assert moved(before) == {"wdf_broker_settle_total{call=retry}": 1}
    open_as("operator").consumer(DLQ).pull(1, TIMEOUT_S)[0].ack()

    before = Counter(registry.counters)
    accepted = fill(publisher)
    assert moved(before) == {
        "wdf_broker_publish_total{outcome=accepted}": accepted,
        "wdf_broker_publish_total{outcome=rejected}": 1,
    }
    while got := consumer.drain(100, TIMEOUT_S):
        for delivery in got:
            delivery.ack()

    alarm(broker, True)
    try:
        before = Counter(registry.counters)
        with pytest.raises(PublishTimeout):
            publisher.publish(QUEUE, b"m", timeout_s=3)
        assert moved(before) == {"wdf_broker_publish_total{outcome=timeout}": 1}
    finally:
        alarm(broker, False)

    closed = open_as("dispatch")
    closed.close()
    before = Counter(registry.counters)
    with pytest.raises(BrokerUnavailable):
        closed.publish(QUEUE, b"m")
    assert moved(before) == {"wdf_broker_publish_total{outcome=unavailable}": 1}
