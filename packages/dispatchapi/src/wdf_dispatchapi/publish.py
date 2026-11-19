"""The publisher of accepted events: one broker connection as the user `dispatch`, shared by the
submit calls and the reconciler, whose publishes the client runs one at a time.

Only an accepted publish records a receipt, and only the call that set the receipt records
`job.published`. Every other outcome leaves the row to the reconciler. A publish timeout, which an
alarm causes for every publish, or a lost connection, which fails every pending call, ends the run
of publishes in hand, and the next publish after a lost connection connects again, because the
client never reconnects by itself. A missing exchange stops the publisher for good.
"""

import logging
import threading
from collections.abc import Callable, Iterable

import psycopg
from wdf_broker.client import Connection, Credentials
from wdf_broker.contract import topology
from wdf_broker.models import BrokerUnavailable, Outcome, PublishTimeout, TopologyError
from wdf_events import Event, build_event, to_bytes
from wdf_eventstore import insert_events
from wdf_jobstate import mark_published

from .submit import SOURCE

TIMEOUT_S = 5.0

log = logging.getLogger(__name__)


def routing_key(event: Event) -> str:
    """The intake queue of the event's tenant and priority class."""
    template = topology().intake_queue.template
    return template.replace("<tenant>", event.tenant).replace("<class>", event.priorityclass)


def published_event(accepted: Event, time) -> Event:
    """The `job.published` event of an accepted job, at the time of its receipt."""
    return build_event(
        "job.published",
        source=SOURCE,
        tenant=accepted.tenant,
        jobid=accepted.jobid,
        priorityclass=accepted.priorityclass,
        jobclass=accepted.jobclass,
        traceparent=accepted.traceparent,
        tracestate=accepted.tracestate,
        time=time,
        data={},
    )


class Publisher:
    """Publishes accepted events over a connection opened by `connect(address, credentials)`."""

    def __init__(
        self,
        connect: Callable[[str, Credentials], Connection],
        address: str,
        credentials: Credentials,
        timeout_s: float = TIMEOUT_S,
    ):
        self._connect, self.address, self.credentials = connect, address, credentials
        self.timeout_s = timeout_s
        self._connection: Connection | None = None
        self._lock = threading.Lock()
        self.stopped = False

    def publish(self, conn: psycopg.Connection, events: Iterable[Event]) -> bool:
        """Publishes each event in order and records the receipt of each accepted one on `conn`, a
        connection of the dispatch login in autocommit mode.

        Returns False when the run ended early, after which the caller publishes nothing more.
        """
        for event in events:
            if self.stopped:
                return False
            connection = None
            try:
                connection = self._connected()
                outcome = connection.publish(routing_key(event), to_bytes(event), self.timeout_s)
            except PublishTimeout:
                return False
            except BrokerUnavailable as error:
                log.warning("the broker is unavailable, publishing waits: %s", error)
                self._lose(connection)
                return False
            except TopologyError as error:
                log.error("the intake exchange is missing, publishing stops: %s", error)
                self.stopped = True
                return False
            if outcome is Outcome.ACCEPTED:
                self._receipt(conn, event)
            elif outcome is Outcome.UNROUTABLE:
                log.error("job %s: no intake queue %s", event.jobid, routing_key(event))
        return not self.stopped

    def _receipt(self, conn: psycopg.Connection, event: Event) -> None:
        with conn.transaction():
            if (time := mark_published(conn, event.jobid)) is not None:
                insert_events(conn, [published_event(event, time)])

    def _connected(self) -> Connection:
        with self._lock:
            if self._connection is None:
                self._connection = self._connect(self.address, self.credentials)
            return self._connection

    def _lose(self, connection: Connection | None) -> None:
        """Drops the connection that failed, and keeps one that another thread opened since."""
        with self._lock:
            if self._connection is connection:
                self._connection = None
