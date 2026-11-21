"""The S4 wrapper over python-qpid-proton, rebuilt from docs/delivery/spikes/broker-client.md.

A blocking API over one proton Container thread per connection. Calls from other threads are
injected into the container thread and wait on a future. Receivers start with credit 0 and only
`pull` (flow) or `drain` grant credit, and the container never reconnects by itself.
"""

import threading
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

from proton import Delivery, Handler, Message, symbol
from proton.reactor import ApplicationEvent, Container, EventInjector

CONTENT_TYPE = "application/cloudevents+json; charset=utf-8"


class BrokerUnavailable(RuntimeError):
    pass


class TopologyError(RuntimeError):
    pass


class PublishTimeout(RuntimeError):
    pass


def sections(raw: bytes) -> dict[int, bytes]:
    """Splits an encoded message into its sections, keyed by descriptor code, each as raw bytes."""
    out, i = {}, 0
    while i < len(raw):
        start = i
        assert raw[i] == 0x00 and raw[i + 1] == 0x53, f"section at {i} is not a small descriptor"
        code = raw[i + 2]
        i = _skip(raw, i + 3)
        out[code] = raw[start:i]
    return out


def _skip(raw: bytes, i: int) -> int:
    fmt = raw[i]
    i += 1
    if fmt == 0x00:  # a described value inside a section
        return _skip(raw, _skip(raw, i))
    width = fmt >> 4
    fixed = {0x4: 0, 0x5: 1, 0x6: 2, 0x7: 4, 0x8: 8, 0x9: 16}
    if width in fixed:
        return i + fixed[width]
    if width in (0xA, 0xC, 0xE):
        return i + 1 + raw[i]
    if width in (0xB, 0xD, 0xF):
        return i + 4 + int.from_bytes(raw[i : i + 4], "big")
    raise ValueError(f"unknown format code {fmt:#x}")


class Received:
    def __init__(self, conn, link, delivery, raw: bytes):
        self._conn, self._link, self._delivery = conn, link, delivery
        self.raw = raw
        self.message = Message()
        self.message.decode(raw)
        self.body = bytes(self.message.body)
        self.delivery_count = self.message.delivery_count
        self.annotations = dict(self.message.annotations or {})

    def _settle(self, kind, failed=False, undeliverable=False, annotations=None):
        def act():
            d = self._delivery
            if kind == Delivery.MODIFIED:
                d.local.failed = failed
                d.local.undeliverable = undeliverable
                if annotations:
                    d.local.annotations = {symbol(k): v for k, v in annotations.items()}
            d.update(kind)
            d.settle()

        self._conn._call(act)

    def ack(self):
        self._settle(Delivery.ACCEPTED)

    def retry(self):
        self._settle(Delivery.MODIFIED, failed=True)

    def dead_letter(self, reason: str, detail: str):
        self._settle(
            Delivery.MODIFIED,
            failed=True,
            undeliverable=True,
            annotations={"x-opt-wft-reason": reason, "x-opt-wft-detail": detail},
        )

    def release(self):
        """Uncounted `modified`, for an outage, a cap, a breaker or a drain."""
        self._settle(Delivery.MODIFIED)

    def released(self):
        """The browse settle of the dead-letter tool."""
        self._settle(Delivery.RELEASED)


class Consumer:
    def __init__(self, conn, address):
        self._conn, self.address = conn, address
        self.inbox: list[Received] = []
        self.cond = threading.Condition()
        self.drained = False
        self.error: Exception | None = None
        self.attached = False
        self.link = conn._call(lambda: conn._create_receiver(self))
        # Credit sent before the broker answers the attach lands on a handle that a refused
        # attach has already detached, and the broker then ends the whole session.
        with self.cond:
            self.cond.wait_for(lambda: self.error or self.attached, timeout=10)
            if self.error:
                raise self.error

    def _wait(self, n, timeout_s, until_drained=False):
        with self.cond:
            self.cond.wait_for(
                lambda: self.error or len(self.inbox) >= n or (until_drained and self.drained),
                timeout=timeout_s,
            )
            if self.error:
                raise self.error
            got, self.inbox = self.inbox[:n], self.inbox[n:]
            return got

    def pull(self, n: int, timeout_s: float) -> list[Received]:
        self._conn._call(lambda: self.link.flow(n))
        return self._wait(n, timeout_s)

    def drain(self, n: int, timeout_s: float) -> list[Received]:
        with self.cond:
            self.drained = False
        self._conn._call(lambda: self.link.drain(n))
        return self._wait(n, timeout_s, until_drained=True)

    def close(self):
        self._conn._call(lambda: self.link.close())


class Connection(Handler):
    """A plain handler: MessagingHandler adds a flow controller and closes the connection on any
    link error, and neither may happen here."""

    def __init__(self, url: str, user: str, password: str):
        super().__init__()
        self._closing = False
        self._injector = EventInjector()
        self._jobs: list = []
        self._lock = threading.Lock()
        self._publish = threading.Lock()
        self._senders: dict[str, object] = {}
        self._pending: dict[object, Future] = {}
        self._consumers: dict[object, Consumer] = {}
        self._link_errors: dict[object, Future] = {}
        self.dead: Exception | None = None
        self._opened = Future()
        self._container = Container(self)
        self._container.selectable(self._injector)
        self._url, self._user, self._password = url, user, password
        self._thread = threading.Thread(target=self._container.run, daemon=True)
        self._thread.start()
        self._call(self._connect)
        try:
            self._opened.result(timeout=15)
        except FutureTimeout:
            raise BrokerUnavailable("connection did not open within 15 s") from None

    def _connect(self):
        self.conn = self._container.connect(
            self._url,
            user=self._user,
            password=self._password,
            allowed_mechs="PLAIN",
            allow_insecure_mechs=True,
            reconnect=False,
        )

    def _call(self, fn):
        if self.dead:
            raise self.dead
        fut = Future()
        with self._lock:
            self._jobs.append((fn, fut))
        self._injector.trigger(ApplicationEvent("job"))
        try:
            return fut.result(timeout=15)
        except FutureTimeout:
            raise self.dead or BrokerUnavailable("container thread did not answer") from None

    def on_job(self, event):
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for fn, fut in jobs:
            try:
                fut.set_result(fn())
            except Exception as exc:
                fut.set_exception(exc)

    def _create_receiver(self, consumer):
        link = self._container.create_receiver(self.conn, consumer.address, name=str(uuid.uuid4()))
        self._consumers[link] = consumer
        return link

    def consumer(self, queue: str) -> Consumer:
        return Consumer(self, f"/queues/{queue}")

    def publish(self, routing_key: str, body: bytes, timeout_s: float = 5.0, address=None) -> str:
        msg = Message(body=body, inferred=True, content_type=CONTENT_TYPE, durable=True)
        return self.publish_raw(routing_key, msg.encode(), timeout_s, address)

    def publish_raw(self, routing_key: str, raw: bytes, timeout_s=5.0, address=None) -> str:
        """Sends encoded sections, as replay does with the bare sections of a dead letter."""
        address = address or f"/exchanges/wft.intake/{routing_key}"
        with self._publish:
            fut = Future()

            def send():
                snd = self._senders.get(address)
                if snd is None:
                    snd = self._senders[address] = self._container.create_sender(
                        self.conn, address, name=str(uuid.uuid4())
                    )
                self._link_errors[snd] = fut
                dlv = snd.delivery(uuid.uuid4().bytes)
                snd.stream(raw)
                snd.advance()
                self._pending[dlv] = fut

            self._call(send)
            try:
                return fut.result(timeout=timeout_s)
            except FutureTimeout:
                raise PublishTimeout(f"no outcome within {timeout_s} s") from None

    def close(self):
        self._closing = True
        if not self.dead:
            self._call(lambda: self.conn.close())
        self._injector.close()
        self._thread.join(timeout=10)

    # Container thread events.

    def on_connection_remote_open(self, event):
        self._opened.set_result(True)

    def on_delivery(self, event):
        d, link = event.delivery, event.link
        if link.is_sender:
            fut = self._pending.pop(d, None)
            if fut and not fut.done():
                fut.set_result(str(d.remote_state.name).lower())
            d.settle()
            return
        if d.partial or not d.readable:
            return
        consumer = self._consumers[link]
        raw = link.recv(d.pending)
        link.advance()
        with consumer.cond:
            consumer.inbox.append(Received(self, link, d, raw))
            consumer.cond.notify_all()

    def on_link_remote_open(self, event):
        consumer = self._consumers.get(event.link)
        if consumer and event.link.remote_source.address:
            with consumer.cond:
                consumer.attached = True
                consumer.cond.notify_all()

    def on_link_flow(self, event):
        consumer = self._consumers.get(event.link)
        if consumer and event.link.credit == 0:
            with consumer.cond:
                consumer.drained = True
                consumer.cond.notify_all()

    def on_link_remote_close(self, event):
        cond = event.link.remote_condition
        err = TopologyError(f"{cond.name}: {cond.description}") if cond else TopologyError("closed")
        if cond and cond.name != "amqp:not-found":
            err = BrokerUnavailable(f"{cond.name}: {cond.description}")
        fut = self._link_errors.pop(event.link, None)
        if fut and not fut.done():
            fut.set_exception(err)
        for addr, snd in list(self._senders.items()):
            if snd is event.link:
                del self._senders[addr]
        consumer = self._consumers.get(event.link)
        if consumer:
            with consumer.cond:
                consumer.error = err
                consumer.cond.notify_all()
        event.link.close()

    def on_session_remote_close(self, event):
        cond = event.session.remote_condition
        self._fail_all(
            f"session closed by broker: {cond.name if cond else 'no condition'}"
            f"{': ' + cond.description if cond and cond.description else ''}"
        )
        event.connection.close()

    def _fail_all(self, why: str):
        self.dead = BrokerUnavailable(why)
        if not self._opened.done():
            self._opened.set_exception(self.dead)
        for fut in list(self._pending.values()) + list(self._link_errors.values()):
            if not fut.done():
                fut.set_exception(self.dead)
        for consumer in self._consumers.values():
            with consumer.cond:
                consumer.error = self.dead
                consumer.cond.notify_all()
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for _, fut in jobs:
            fut.set_exception(self.dead)

    def on_connection_remote_close(self, event):
        cond = event.connection.remote_condition
        self._fail_all(f"connection closed by broker: {cond.name if cond else 'no condition'}")
        event.connection.close()

    def on_transport_error(self, event):
        cond = event.transport.condition
        self._fail_all(f"transport error: {cond.name if cond else 'unknown'}")

    def on_transport_closed(self, event):
        if not self.dead and not self._closing:
            self._fail_all("disconnected")


def connect(address: str, user: str, password: str) -> Connection:
    return Connection(f"amqp://{address}", user, password)
