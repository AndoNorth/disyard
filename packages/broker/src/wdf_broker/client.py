"""The broker client: a blocking API over one proton Container thread per connection, for the
publisher and the consumer of the intake queues and for the browse of the dead-letter queue.

Calls from other threads are injected into the container thread and wait on a future. A receiver
starts at credit 0 and gets credit only from a pull or a drain, and a connection never reconnects
by itself: after a lost connection every call raises BrokerUnavailable, and the caller connects
again.
"""

import re
import threading
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import NamedTuple

from proton import Delivery as Disposition
from proton import Handler, Message, symbol
from proton.reactor import ApplicationEvent, Container, EventInjector
from wdf_events.metrics import registry

from .contract import reasons, topology
from .models import BrokerUnavailable, Outcome, PublishTimeout, TopologyError

PUBLISH_ADDRESS = topology().addresses.publish
CONSUME_ADDRESS = topology().addresses.consume
INTAKE_QUEUE = re.compile(topology().intake_policy.pattern)
DELIVERY_LIMIT = topology().intake_policy.definition.delivery_limit
ANNOTATIONS = topology().annotations
APPLICATION_CODES = {r.code for r in reasons().reasons if r.source == "application"}
# How long a call waits for the container thread, and for the broker to open a connection or
# answer an attach.
ANSWER_TIMEOUT_S = 15

OUTCOMES = {
    Disposition.ACCEPTED: Outcome.ACCEPTED,
    Disposition.REJECTED: Outcome.REJECTED,
    Disposition.RELEASED: Outcome.UNROUTABLE,
}
# The outcome label of wdf_broker_publish_total for each error of a publish.
PUBLISH_ERRORS = {
    PublishTimeout: "timeout",
    BrokerUnavailable: "unavailable",
    TopologyError: "topology-error",
}


class Credentials(NamedTuple):
    user: str
    password: str


def connect(address: str, credentials: Credentials) -> "Connection":
    """Opens a connection to the broker at `host:port` as one broker user."""
    return Connection(address, credentials)


def is_intake_queue(queue: str) -> bool:
    return INTAKE_QUEUE.fullmatch(queue) is not None


class Delivery:
    """One message held by a consumer until a settle call.

    `delivery_count` counts the failed deliveries before this one, and `last` is true on an intake
    delivery whose retry dead-letters it.
    """

    def __init__(self, consumer: "Consumer", disposition: Disposition, raw: bytes):
        self._connection = consumer._connection
        self._disposition = disposition
        self._settled = False
        message = Message()
        message.decode(raw)
        self.body = bytes(message.body)
        self.queue = consumer.queue
        self.delivery_count = message.delivery_count
        self.annotations = {str(k): v for k, v in (message.annotations or {}).items()}
        self._intake = is_intake_queue(self.queue)
        self.last = self._intake and self.delivery_count == DELIVERY_LIMIT

    def ack(self) -> None:
        """Settles `accepted`: the message is gone."""
        self._settle("ack", Disposition.ACCEPTED)

    def retry(self) -> None:
        """Settles `modified` with delivery-failed, which counts toward the delivery limit, so a
        retry of the last delivery dead-letters it as delivery_limit."""
        self._settle("retry", Disposition.MODIFIED, failed=True)

    def dead_letter(self, reason: str, detail: str) -> None:
        """Settles `modified`, delivery-failed and undeliverable-here, with the reason and detail
        annotations, so the broker dead-letters the message at once."""
        if reason not in APPLICATION_CODES:
            raise ValueError(f"{reason} is not an application code of the reason table")
        if len(detail) > ANNOTATIONS.detail_max_chars:
            raise ValueError(f"the detail is above {ANNOTATIONS.detail_max_chars} chars")
        # Only an intake queue has a dead-letter exchange, so anywhere else the message is lost.
        if not self._intake:
            raise ValueError(f"{self.queue} is not an intake queue")
        annotations = {ANNOTATIONS.reason: reason, ANNOTATIONS.detail: detail}
        self._settle("dead_letter", Disposition.MODIFIED, True, True, annotations)
        registry.inc("wdf_broker_dead_letter_total", reason=reason)

    def release(self) -> None:
        """Settles `modified` without delivery-failed, which counts nothing and puts the message
        back at the head of its queue: only for a dependency outage, a job class at its cap, the
        consumer's breaker open, or the consumer's drain on shutdown."""
        self._settle("release", Disposition.MODIFIED)

    def _settle(
        self,
        call: str,
        outcome: int,
        failed: bool = False,
        undeliverable: bool = False,
        annotations: dict[str, str] | None = None,
    ) -> None:
        if self._settled:
            raise RuntimeError("the delivery is already settled")

        def settle():
            local = self._disposition.local
            if outcome == Disposition.MODIFIED:
                local.failed = failed
                local.undeliverable = undeliverable
                if annotations:
                    local.annotations = {symbol(k): v for k, v in annotations.items()}
            self._disposition.update(outcome)
            self._disposition.settle()

        self._connection._call(settle)
        self._settled = True
        if self._intake:
            registry.inc("wdf_broker_settle_total", call=call)


class Browsed(Delivery):
    """A delivery of a browse, which may also go back untouched."""

    def released(self) -> None:
        """Settles `released`, which counts nothing and keeps the order of the queue when a pass
        settles its deliveries in the order it got them."""
        self._settle("released", Disposition.RELEASED)


class Consumer:
    """A receiver on one queue, owned by the thread that opened it, which gets messages only for
    the credit of a pull or a drain."""

    _delivery = Delivery

    def __init__(self, connection: "Connection", queue: str):
        self._connection = connection
        self.queue = queue
        self._inbox: list[Delivery] = []
        self._cond = threading.Condition()
        self._drained = False
        self._attached = False
        self._error: Exception | None = None
        address = CONSUME_ADDRESS.replace("<queue>", queue)
        self._link = connection._call(lambda: connection._create_receiver(self, address))
        # Credit sent before the broker answers the attach can land on a handle that a refused
        # attach has already detached, and the broker then ends the whole session.
        with self._cond:
            self._cond.wait_for(lambda: self._error or self._attached, ANSWER_TIMEOUT_S)
            if self._error:
                raise self._error
            if not self._attached:
                raise BrokerUnavailable(f"no answer to the attach of {queue}")

    def pull(self, n: int, timeout_s: float) -> list[Delivery]:
        """Grants n credit, waits up to timeout_s for n deliveries and returns those that came,
        draining any credit left so that none is held past the call."""
        self._connection._call(lambda: self._link.flow(n))
        got = self._wait(n, timeout_s)
        if len(got) < n:
            got += self._drain(0, ANSWER_TIMEOUT_S, n - len(got))
        return got

    def drain(self, n: int, timeout_s: float) -> list[Delivery]:
        """Grants n credit in drain mode and returns with the deliveries that the queue holds now,
        at most n, once the broker answers the drain or timeout_s passes."""
        return self._drain(n, timeout_s, n)

    def _drain(self, credit: int, timeout_s: float, n: int) -> list[Delivery]:
        def drain():
            # Cleared in the container thread, so a flow event handled before it cannot count.
            self._set(drained=False)
            self._link.drain(credit)

        self._connection._call(drain)
        return self._wait(n, timeout_s, until_drained=True)

    def _wait(self, n: int, timeout_s: float, until_drained: bool = False) -> list[Delivery]:
        with self._cond:
            self._cond.wait_for(
                lambda: self._error or len(self._inbox) >= n or (until_drained and self._drained),
                timeout_s,
            )
            if self._error:
                raise self._error
            got, self._inbox = self._inbox[:n], self._inbox[n:]
            return got

    # Container thread events.

    def _received(self, disposition: Disposition, raw: bytes) -> None:
        with self._cond:
            self._inbox.append(self._delivery(self, disposition, raw))
            self._cond.notify_all()

    def _set(self, **state: object) -> None:
        with self._cond:
            for name, value in state.items():
                setattr(self, f"_{name}", value)
            self._cond.notify_all()


class Browser(Consumer):
    """A consumer whose deliveries may go back `released`, for one pass over a queue that is not
    an intake queue."""

    _delivery = Browsed


class Connection(Handler):
    """One AMQP connection, whose publish may be shared between threads and runs one at a time.

    A plain Handler, because proton's MessagingHandler adds a flow controller and closes the
    connection on any link error, and neither may happen here.
    """

    def __init__(self, address: str, credentials: Credentials):
        super().__init__()
        self._credentials = credentials
        self._url = f"amqp://{address}"
        self._closing = False
        self._dead: Exception | None = None
        self._injector = EventInjector()
        self._jobs: list[tuple] = []
        self._jobs_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._senders: dict[str, object] = {}
        self._outcomes: dict[object, Future] = {}
        self._sender_errors: dict[object, Future] = {}
        self._consumers: dict[object, Consumer] = {}
        self._opened: Future = Future()
        self._container = Container(self)
        self._container.selectable(self._injector)
        self._thread = threading.Thread(target=self._container.run, daemon=True)
        self._thread.start()
        try:
            self._call(self._connect)
            self._opened.result(ANSWER_TIMEOUT_S)
        except (BrokerUnavailable, FutureTimeout) as error:
            self._injector.close()
            raise BrokerUnavailable(f"no connection to {address}: {error}") from None

    def publish(self, routing_key: str, body: bytes, timeout_s: float = 5.0) -> Outcome:
        """Publishes the bytes of one event to the intake exchange and returns the outcome."""
        try:
            outcome = self._publish(routing_key, body, timeout_s)
        except tuple(PUBLISH_ERRORS) as error:
            registry.inc("wdf_broker_publish_total", outcome=PUBLISH_ERRORS[type(error)])
            raise
        registry.inc("wdf_broker_publish_total", outcome=outcome.value)
        return outcome

    def consumer(self, queue: str) -> Consumer:
        """Attaches a receiver to a queue, and raises TopologyError when the queue is missing."""
        return Consumer(self, queue)

    def browser(self, queue: str) -> Browser:
        """Attaches a browse receiver to a queue that is not an intake queue."""
        if is_intake_queue(queue):
            raise ValueError(f"{queue} is an intake queue, which is never browsed")
        return Browser(self, queue)

    def close(self) -> None:
        """Closes the connection, and every delivery it holds unsettled counts as failed."""
        if not self._dead:
            self._closing = True
            self._call(lambda: self._connection.close())
            self._dead = BrokerUnavailable("the connection is closed")
        self._injector.close()
        self._thread.join(ANSWER_TIMEOUT_S)

    def _publish(self, routing_key: str, body: bytes, timeout_s: float) -> Outcome:
        message = topology().message
        raw = Message(
            body=body, inferred=True, content_type=message.content_type, durable=message.durable
        ).encode()
        address = PUBLISH_ADDRESS.replace("<routing key>", routing_key)
        with self._publish_lock:
            outcome: Future = Future()

            def send():
                sender = self._senders.get(address)
                if sender is None:
                    sender = self._container.create_sender(
                        self._connection, address, name=str(uuid.uuid4())
                    )
                    self._senders[address] = sender
                self._sender_errors[sender] = outcome
                disposition = sender.delivery(uuid.uuid4().bytes)
                sender.stream(raw)
                sender.advance()
                self._outcomes[disposition] = outcome

            self._call(send)
            try:
                return outcome.result(timeout_s)
            except FutureTimeout:
                raise PublishTimeout(f"no outcome within {timeout_s} s") from None

    def _call(self, fn):
        """Runs fn in the container thread and returns its result."""
        if self._dead:
            raise self._dead
        future: Future = Future()
        with self._jobs_lock:
            self._jobs.append((fn, future))
        self._injector.trigger(ApplicationEvent("job"))
        try:
            return future.result(ANSWER_TIMEOUT_S)
        except FutureTimeout:
            raise self._dead or BrokerUnavailable("the container thread did not answer") from None

    def _connect(self) -> None:
        self._connection = self._container.connect(
            self._url,
            user=self._credentials.user,
            password=self._credentials.password,
            allowed_mechs="PLAIN",
            allow_insecure_mechs=True,
            reconnect=False,
        )

    def _create_receiver(self, consumer: Consumer, address: str):
        link = self._container.create_receiver(self._connection, address, name=str(uuid.uuid4()))
        self._consumers[link] = consumer
        return link

    def _fail_all(self, why: str) -> None:
        self._dead = BrokerUnavailable(why)
        if not self._opened.done():
            self._opened.set_exception(self._dead)
        for future in [*self._outcomes.values(), *self._sender_errors.values()]:
            if not future.done():
                future.set_exception(self._dead)
        for consumer in self._consumers.values():
            consumer._set(error=self._dead)
        with self._jobs_lock:
            jobs, self._jobs = self._jobs, []
        for _, future in jobs:
            future.set_exception(self._dead)

    # Container thread events.

    def on_job(self, event) -> None:
        with self._jobs_lock:
            jobs, self._jobs = self._jobs, []
        for fn, future in jobs:
            try:
                future.set_result(fn())
            except Exception as error:
                future.set_exception(error)

    def on_connection_remote_open(self, event) -> None:
        self._opened.set_result(True)

    def on_delivery(self, event) -> None:
        disposition, link = event.delivery, event.link
        if link.is_sender:
            future = self._outcomes.pop(disposition, None)
            if future and not future.done():
                state = disposition.remote_state
                if state in OUTCOMES:
                    future.set_result(OUTCOMES[state])
                else:
                    future.set_exception(BrokerUnavailable(f"unexpected outcome {state}"))
            disposition.settle()
            return
        if disposition.partial or not disposition.readable:
            return
        raw = link.recv(disposition.pending)
        link.advance()
        self._consumers[link]._received(disposition, raw)

    def on_link_remote_open(self, event) -> None:
        consumer = self._consumers.get(event.link)
        # A refused attach answers with no source before it detaches.
        if consumer and event.link.remote_source.address:
            consumer._set(attached=True)

    def on_link_flow(self, event) -> None:
        consumer = self._consumers.get(event.link)
        if consumer and event.link.credit == 0:
            consumer._set(drained=True)

    def on_link_remote_close(self, event) -> None:
        link, condition = event.link, event.link.remote_condition
        text = f"{condition.name}: {condition.description}" if condition else "detached"
        error = (
            TopologyError(text)
            if condition and condition.name == "amqp:not-found"
            else BrokerUnavailable(text)
        )
        future = self._sender_errors.pop(link, None)
        if future and not future.done():
            future.set_exception(error)
        # proton hands each event a new wrapper of the same link, so links compare by equality.
        self._senders = {a: s for a, s in self._senders.items() if s != link}
        consumer = self._consumers.get(link)
        if consumer:
            consumer._set(error=error)
        link.close()

    def on_session_remote_close(self, event) -> None:
        # A refused permission ends the whole session, and with it every link of the connection.
        condition = event.session.remote_condition
        self._fail_all(
            f"session ended by the broker: {condition.name}: {condition.description}"
            if condition
            else "session ended by the broker"
        )
        event.connection.close()

    def on_connection_remote_close(self, event) -> None:
        condition = event.connection.remote_condition
        self._fail_all(
            f"connection closed by the broker: {condition.name}: {condition.description}"
            if condition
            else "connection closed by the broker"
        )
        event.connection.close()

    def on_transport_error(self, event) -> None:
        condition = event.transport.condition
        self._fail_all(f"transport error: {condition.name if condition else 'unknown'}")

    def on_transport_closed(self, event) -> None:
        if not self._dead and not self._closing:
            self._fail_all("disconnected")
