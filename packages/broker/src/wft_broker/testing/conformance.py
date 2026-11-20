"""The conformance test of the client API: what the publisher and the consumer of the intake queues
rely on, run against FakeBroker at T0 and against the client and a broker container at T1.

Each check starts from empty queues and leaves them empty, and raises AssertionError naming itself.
"""

from collections.abc import Callable

from ..contract import topology
from ..models import Outcome, TopologyError
from ..tenancy import load

UNKNOWN = "intake.no-such-tenant.no-such-class"


class _Run:
    def __init__(self, connect: Callable[[str], object], profile: str, timeout_s: float):
        spec = topology()
        tenancy = load(profile)
        self.queue = spec.intake_queue.template.replace(
            "<tenant>", next(iter(tenancy.tenants))
        ).replace("<class>", tenancy.priority_classes[0])
        self.dlq = spec.dead_letter_queue.name
        self.annotations = spec.annotations
        self.limit = spec.intake_policy.definition.delivery_limit
        self.max_length = spec.intake_policy.definition.max_length.value
        self.timeout_s = timeout_s
        self.connections = [connect(user) for user in ("dispatch", "admission", "operator")]
        self.publisher, consumer, operator = self.connections
        self.intake = consumer.consumer(self.queue)
        self.dead = operator.consumer(self.dlq)
        self.sent = 0

    def publish(self, routing_key: str | None = None) -> tuple[bytes, Outcome]:
        self.sent += 1
        body = b'{"conformance": %d}' % self.sent
        return body, self.publisher.publish(routing_key or self.queue, body, self.timeout_s)

    def put(self) -> bytes:
        body, outcome = self.publish()
        assert outcome is Outcome.ACCEPTED, f"a publish to {self.queue} was {outcome}"
        return body

    def one(self, consumer):
        got = consumer.pull(1, self.timeout_s)
        assert len(got) == 1, f"a pull of one credit on {consumer.queue} gave {len(got)}"
        return got[0]

    def assert_empty(self) -> None:
        for consumer in (self.intake, self.dead):
            left = consumer.drain(1, self.timeout_s)
            assert not left, f"{consumer.queue} still holds a message"

    def close(self) -> None:
        for connection in self.connections:
            connection.close()


def publish_outcomes(run: _Run) -> None:
    body, outcome = run.publish()
    assert outcome is Outcome.ACCEPTED, f"a publish to an intake queue was {outcome}"
    _, outcome = run.publish(UNKNOWN)
    assert outcome is Outcome.UNROUTABLE, f"a publish to an unknown routing key was {outcome}"
    _, outcome = run.publish(run.dlq)
    assert outcome is Outcome.UNROUTABLE, f"a publish to {run.dlq} was {outcome}"
    delivery = run.one(run.intake)
    assert delivery.body == body, "the delivered body differs from the published one"
    assert delivery.queue == run.queue, f"the delivery names the queue {delivery.queue}"
    assert delivery.delivery_count == 0, f"a first delivery counts {delivery.delivery_count}"
    assert not delivery.last, "a first delivery is marked last"
    marked = {run.annotations.reason, run.annotations.detail} & set(delivery.annotations)
    assert not marked, f"a published message carries {sorted(marked)}"
    delivery.ack()


def one_message_per_credit(run: _Run) -> None:
    first, second = run.put(), run.put()
    got = run.intake.pull(1, run.timeout_s)
    assert len(got) == 1, f"one credit with two messages ready gave {len(got)} deliveries"
    rest = run.intake.drain(5, run.timeout_s)
    assert [d.body for d in got + rest] == [first, second], "the deliveries are not in order"
    for delivery in got + rest:
        delivery.ack()


def retry_counts_and_dead_letters_after_the_limit(run: _Run) -> None:
    body = run.put()
    for count in range(run.limit + 1):
        delivery = run.one(run.intake)
        assert delivery.delivery_count == count, (
            f"after {count} retries the delivery count is {delivery.delivery_count}"
        )
        assert delivery.last == (count == run.limit), (
            f"last is {delivery.last} on the delivery of count {count}"
        )
        delivery.retry()
    dead = run.one(run.dead)
    assert dead.body == body, "the dead letter body differs from the published one"
    reason = dead.annotations.get("x-last-death-reason")
    assert reason == "delivery_limit", f"a retried last delivery dead-lettered as {reason}"
    assert run.annotations.reason not in dead.annotations, "a broker dead letter has an app reason"
    dead.ack()


def dead_letter_carries_the_reason(run: _Run) -> None:
    body = run.put()
    delivery = run.one(run.intake)
    try:
        delivery.dead_letter("INVALID_DAG", "no-row: not in the reason table")
    except ValueError:
        pass
    else:
        raise AssertionError("dead_letter took a code outside the reason table")
    delivery.dead_letter("ADMISSION_FAILED", "no-row: conformance")
    dead = run.one(run.dead)
    assert dead.body == body, "the dead letter body differs from the published one"
    expected = {
        run.annotations.reason: "ADMISSION_FAILED",
        run.annotations.detail: "no-row: conformance",
        "x-last-death-reason": "rejected",
        "x-first-death-queue": run.queue,
    }
    found = {key: dead.annotations.get(key) for key in expected}
    assert found == expected, f"the dead letter carries {found}"
    dead.ack()


def release_is_uncounted_and_returns_first(run: _Run) -> None:
    first, second = run.put(), run.put()
    delivery = run.one(run.intake)
    assert delivery.body == first, "the first pull is not the first message"
    delivery.release()
    again = run.one(run.intake)
    assert again.body == first, "a released message does not come back first"
    assert again.delivery_count == 0, f"a release counted, the count is {again.delivery_count}"
    again.ack()
    after = run.one(run.intake)
    assert after.body == second, "the second message is lost"
    after.ack()


def full_queue_rejects(run: _Run) -> None:
    accepted = 0
    while (outcome := run.publish()[1]) is Outcome.ACCEPTED:
        accepted += 1
        assert accepted <= run.max_length + 1, f"{accepted} publishes over max-length accepted"
    assert outcome is Outcome.REJECTED, f"a publish to a full queue was {outcome}"
    # RabbitMQ takes one message over max-length before it rejects.
    assert accepted == run.max_length + 1, f"the queue rejected after {accepted} messages"
    while got := run.intake.drain(100, run.timeout_s):
        for delivery in got:
            delivery.ack()


def missing_queue_is_topology_error(run: _Run) -> None:
    try:
        run.connections[1].consumer(UNKNOWN).pull(1, run.timeout_s)
    except TopologyError:
        pass
    else:
        raise AssertionError(f"a consumer on {UNKNOWN} raised no TopologyError")
    run.put()
    run.one(run.intake).ack()


CHECKS = {
    check.__name__: check
    for check in (
        publish_outcomes,
        one_message_per_credit,
        retry_counts_and_dead_letters_after_the_limit,
        dead_letter_carries_the_reason,
        release_is_uncounted_and_returns_first,
        full_queue_rejects,
        missing_queue_is_topology_error,
    )
}


def broker_contract(
    connect: Callable[[str], object],
    profile: str = "ci",
    timeout_s: float = 10.0,
    checks: list[str] | None = None,
) -> list[str]:
    """Runs the checks, every one by default, and returns the names of those that passed.

    connect(user) opens a connection as the broker user dispatch, admission or operator, and the
    first intake queue of the tenancy profile and the dead-letter queue must start empty.
    timeout_s bounds each wait for a delivery, so it stays above the longest delayed retry.
    """
    passed = []
    for name in checks or list(CHECKS):
        run = _Run(connect, profile, timeout_s)
        try:
            run.assert_empty()
            CHECKS[name](run)
            run.assert_empty()
        except AssertionError as error:
            raise AssertionError(f"{name}: {error}") from error
        finally:
            run.close()
        passed.append(name)
    return passed
