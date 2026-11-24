"""An in-memory broker behind the API of dsy_broker.client, for the publisher and the consumer of
the intake queues at T0, with the semantics that RabbitMQ 4.4.0 showed and none of its delays."""

import threading
from collections import deque
from dataclasses import dataclass, field

from ..contract import reasons, topology
from ..models import BrokerUnavailable, Outcome, PublishTimeout, TopologyError
from ..tenancy import load


@dataclass
class _Message:
    body: bytes
    annotations: dict[str, object]
    delivery_count: int = 0


@dataclass
class _Queue:
    limit: int | None
    # A message settled back goes ahead of every message never delivered, in the order it came back.
    back: deque[_Message] = field(default_factory=deque)
    ready: deque[_Message] = field(default_factory=deque)

    def __len__(self) -> int:
        return len(self.back) + len(self.ready)

    def take(self) -> _Message:
        return (self.back or self.ready).popleft()


class FakeBroker:
    """The topology of a tenancy profile: one intake queue per tenant and priority class, and the
    dead-letter queue, with credit, delivery counts, the delivery limit, max-length with
    reject-publish, and dead-lettering with the annotations of the settle calls.

    restart() and alarm(on) stand in for a broker restart and a memory alarm.
    """

    def __init__(self, profile: str = "ci"):
        spec = topology()
        policy = spec.intake_policy.definition
        tenancy = load(profile)
        self._max_length = policy.max_length.value
        self._exchange = spec.intake_exchange.name
        self._dead_letter_exchange = spec.dead_letter_exchange.name
        self._dlq = spec.dead_letter_queue.name
        self._annotations = spec.annotations
        self._codes = {r.code for r in reasons().reasons if r.source == "application"}
        template = spec.intake_queue.template
        self.queues = {
            template.replace("<tenant>", tenant).replace("<class>", cls): _Queue(
                policy.delivery_limit
            )
            for tenant in tenancy.tenants
            for cls in tenancy.priority_classes
        }
        self.queues[self._dlq] = _Queue(limit=None)
        self._lock = threading.Condition()
        self._connections: list[Connection] = []
        self._alarm = False

    def connect(self, address: str | None = None, credentials: object = None) -> "Connection":
        """Opens a connection, as dsy_broker.client.connect does, with any address and user."""
        with self._lock:
            connection = Connection(self)
            self._connections.append(connection)
            return connection

    def alarm(self, on: bool) -> None:
        """Raises or clears a memory alarm, which withholds the outcome of every publish."""
        with self._lock:
            self._alarm = on
            self._lock.notify_all()

    def restart(self) -> None:
        """Loses every open connection, as a broker restart does, and keeps every message."""
        with self._lock:
            for connection in self._connections:
                connection._lose()
            self._connections.clear()
            self._lock.notify_all()

    def _publish(self, routing_key: str, body: bytes, timeout_s: float, live) -> Outcome:
        with self._lock:
            if not self._lock.wait_for(lambda: not self._alarm or not live(), timeout_s):
                raise PublishTimeout(f"no outcome within {timeout_s} s")
            if not live():
                raise BrokerUnavailable("the connection is lost")
            queue = self.queues.get(routing_key)
            if queue is None or routing_key == self._dlq:
                return Outcome.UNROUTABLE
            # RabbitMQ rejects a publish only once the queue is already above its max-length.
            if len(queue) > self._max_length:
                return Outcome.REJECTED
            annotations = {"x-exchange": self._exchange, "x-routing-key": routing_key}
            queue.ready.append(_Message(bytes(body), annotations))
            self._lock.notify_all()
            return Outcome.ACCEPTED

    def _settle_back(
        self,
        queue: str,
        message: _Message,
        failed: bool,
        undeliverable: bool,
        annotations: dict[str, object],
    ) -> None:
        """Settles a delivery `modified`, or loses it, which counts as a failed delivery."""
        message.annotations.update(annotations)
        message.delivery_count += failed
        limit = self.queues[queue].limit
        if undeliverable:
            self._dead_letter(queue, message, "rejected")
        elif limit is not None and message.delivery_count > limit:
            self._dead_letter(queue, message, "delivery_limit")
        else:
            self.queues[queue].back.append(message)
        self._lock.notify_all()

    def _dead_letter(self, queue: str, message: _Message, reason: str) -> None:
        death = {"queue": queue, "reason": reason, "exchange": message.annotations["x-exchange"]}
        annotations = dict(message.annotations)
        for key, value in death.items():
            annotations.setdefault(f"x-first-death-{key}", value)
            annotations[f"x-last-death-{key}"] = value
        annotations["x-exchange"] = self._dead_letter_exchange
        self.queues[self._dlq].ready.append(_Message(message.body, annotations))


class Connection:
    """One connection, whose publish may be shared between threads."""

    def __init__(self, broker: FakeBroker):
        self._broker = broker
        self._live = True
        self._held: list[Delivery] = []

    def publish(self, routing_key: str, body: bytes, timeout_s: float = 5.0) -> Outcome:
        """Publishes to the intake exchange, as dsy_broker.client does."""
        return self._broker._publish(routing_key, body, timeout_s, lambda: self._live)

    def consumer(self, queue: str) -> "Consumer":
        with self._broker._lock:
            self._check()
            if queue not in self._broker.queues:
                raise TopologyError(f"amqp:not-found: no queue {queue}")
            return Consumer(self, queue)

    def close(self) -> None:
        """Closes the connection, and every delivery it holds unsettled counts as failed."""
        with self._broker._lock:
            if self._live:
                self._lose()
                self._broker._connections.remove(self)

    def _lose(self) -> None:
        self._live = False
        for delivery in self._held:
            delivery._settled = True
            self._broker._settle_back(delivery.queue, delivery._message, True, False, {})
        self._held.clear()

    def _check(self) -> None:
        if not self._live:
            raise BrokerUnavailable("the connection is lost")


class Consumer:
    """A receiver on one queue, which gets messages only for the credit of a pull or a drain."""

    def __init__(self, connection: Connection, queue: str):
        self._connection = connection
        self._broker = connection._broker
        self.queue = queue

    def pull(self, n: int, timeout_s: float) -> list["Delivery"]:
        """Grants n credit, waits up to timeout_s for n deliveries and returns those that came."""
        with self._broker._lock:
            queue = self._broker.queues[self.queue]
            connection = self._connection
            self._broker._lock.wait_for(lambda: len(queue) >= n or not connection._live, timeout_s)
            return self._take(n)

    def drain(self, n: int, timeout_s: float) -> list["Delivery"]:
        """Grants n credit and returns at once with the deliveries that the queue holds now."""
        with self._broker._lock:
            return self._take(n)

    def _take(self, n: int) -> list["Delivery"]:
        self._connection._check()
        queue = self._broker.queues[self.queue]
        taken = [Delivery(self, queue.take()) for _ in range(min(n, len(queue)))]
        self._connection._held += taken
        return taken


class Delivery:
    """One message held by a consumer until a settle call."""

    def __init__(self, consumer: Consumer, message: _Message):
        self._consumer = consumer
        self._message = message
        self._settled = False
        self.body = message.body
        self.queue = consumer.queue
        self.delivery_count = message.delivery_count
        self.annotations = dict(message.annotations)
        limit = consumer._broker.queues[consumer.queue].limit
        self.last = limit is not None and message.delivery_count == limit

    def ack(self) -> None:
        """Settles `accepted`: the message is gone."""
        self._settle(None)

    def retry(self) -> None:
        """Settles `modified` with delivery-failed, which counts toward the delivery limit."""
        self._settle((True, False, {}))

    def dead_letter(self, reason: str, detail: str) -> None:
        """Settles `modified`, delivery-failed and undeliverable-here, with the two annotations."""
        broker = self._consumer._broker
        if reason not in broker._codes:
            raise ValueError(f"{reason} is not an application code of the reason table")
        if len(detail) > broker._annotations.detail_max_chars:
            raise ValueError(f"the detail is above {broker._annotations.detail_max_chars} chars")
        annotations = {broker._annotations.reason: reason, broker._annotations.detail: detail}
        self._settle((True, True, annotations))

    def release(self) -> None:
        """Settles `modified` without delivery-failed, which counts nothing."""
        self._settle((False, False, {}))

    def _settle(self, modified: tuple[bool, bool, dict[str, object]] | None) -> None:
        broker = self._consumer._broker
        with broker._lock:
            self._consumer._connection._check()
            if self._settled:
                raise RuntimeError("the delivery is already settled")
            self._settled = True
            self._consumer._connection._held.remove(self)
            if modified is not None:
                broker._settle_back(self.queue, self._message, *modified)
