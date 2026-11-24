"""The admission cycle and its intake: each cycle plans its grants from the snapshot of the rows and
the queues that may hold work, pulls one message per queue grant, claims and starts the job it names
and settles the message after the start.

The message only names the job, and the row wins where they disagree. A message refused by the
event contract or by the tenancy profile is dead-lettered at once, with a `job.dead.lettered` event
when it names a job of that tenant. An outage releases the message uncounted and opens the breaker
of its dependency, and any other error is a counted retry, dead-lettered `ADMISSION_FAILED` on the
last delivery the limit allows. While a dependency breaker is open a cycle only probes it.
"""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

from dsy_broker.client import Connection, Consumer, Delivery
from dsy_broker.contract import topology
from dsy_broker.models import BrokerUnavailable, TenancyProfile, TopologyError
from dsy_broker.topology import intake_queues
from dsy_compat import read_event
from dsy_events import Event, EventError, to_event_row
from dsy_eventstore import insert_events
from dsy_jobstate import JobRow, get_job
from dsy_stages import Facts
from psycopg import Connection as Database
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

from . import stage
from .breaker import INTERNAL, Breakers, dependency_of
from .claim import RETRIED, Skipped, claim
from .policy import Caps, Queue, Readmission, Resume, Snapshot, Turn, plan

log = logging.getLogger(__name__)

ACCEPTED = "dev.dsy.job.accepted.v1"
# Estimates: the grants of one cycle, the wait after a cycle that settled no message, and the time
# an empty queue stays out of the waiting queues.
MAX_GRANTS = 50
IDLE_S = 1.0
EMPTY_BACKOFF_S = 1.0
# A missing queue is tried again every this many cycles.
MISSING_EVERY = 10
# A drain answers at once on an empty queue, so this bounds only a broker that does not answer.
PULL_TIMEOUT_S = 5.0
DETAIL_MAX = topology().annotations.detail_max_chars
# The attributes that name a job, which a refused message needs for its `job.dead.lettered`.
IDENTITY = ("tenant", "jobid", "priorityclass", "jobclass", "traceparent")
# The dead-letter reasons that carry an event, those of `job.dead.lettered` 1.0.
EVENT_REASONS = ("MALFORMED", "UNSUPPORTED_VERSION")

Start = Callable[[JobRow, Event], Awaitable[Literal["started", "duplicate"]]]
Other = Resume | Readmission
Settled = Literal["empty", "released", "settled", "stop"]


@dataclass(frozen=True)
class Work:
    """What the rest of the service brings to a cycle: the snapshot of the rows with the resumes
    and re-admissions that wait, for the cycle number given, and the grant of one of them, which
    returns True when it took a slot."""

    snapshot: Callable[[int], Awaitable[tuple[Snapshot, list[Other]]]]
    grant: Callable[[Other], Awaitable[bool]]


def temporal_probe(client: Client) -> Callable[[], Awaitable[object]]:
    """The probe of the Temporal breaker: a description of the client's namespace."""
    request = DescribeNamespaceRequest(namespace=client.namespace)
    return lambda: client.workflow_service.describe_namespace(request)


def _detail(token: str, text: str) -> str:
    return f"{token}: {text}"[:DETAIL_MAX]


def _identity(body: bytes) -> SimpleNamespace | None:
    """The identity attributes of a message the event contract refused, when it holds them all."""
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    who = {name: document.get(name) for name in IDENTITY}
    if not all(isinstance(value, str) for value in who.values()):
        return None
    return SimpleNamespace(**who)


class Intake:
    """The cycle of one admission process over the intake queues of a tenancy profile.

    `connect_broker` and `connect_db` open a broker connection and an autocommit database
    connection, `start` starts a claimed job's workflow and `probe_temporal` probes Temporal.
    `counts` holds the counters of the admission metrics by name and label, and `blocked` the job
    class each queue waits on.
    """

    def __init__(
        self,
        profile: TenancyProfile,
        caps: Caps,
        *,
        connect_broker: Callable[[], Connection],
        connect_db: Callable[[], Database],
        start: Start,
        probe_temporal: Callable[[], Awaitable[object]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self.profile = profile
        self.caps = caps
        self.connect_broker = connect_broker
        self.connect_db = connect_db
        self.start = start
        self.clock = clock
        self.probes = {
            "temporal": probe_temporal,
            "database": self._probe_database,
            "broker": self._probe_broker,
        }
        pairs = [(t, c) for t in profile.tenants for c in profile.priority_classes]
        self.queues = dict(zip(intake_queues(profile), pairs, strict=True))
        self.breakers = Breakers()
        self.counts: Counter[tuple[str, ...]] = Counter()
        self.blocked: dict[str, str] = {}
        self.cycles = 0
        self.turn = Turn()
        self._broker: Connection | None = None
        self._consumers: dict[str, Consumer] = {}
        self._db: Database | None = None
        self._empty_until: dict[str, float] = {}
        self._missing: set[str] = set()

    def waiting(self, now: float) -> list[Queue]:
        """The queues that may hold work: neither empty within the last backoff nor missing, except
        that every MISSING_EVERY-th cycle tries the missing ones again."""
        retry_missing = self.cycles % MISSING_EVERY == 0
        return [
            Queue(name, tenant, cls, self.blocked.get(name))
            for name, (tenant, cls) in self.queues.items()
            if self._empty_until.get(name, 0.0) <= now
            and (name not in self._missing or retry_missing)
        ]

    async def cycle(self, work: Work) -> float:
        """Runs one cycle and returns the seconds to wait before the next."""
        self.cycles += 1
        now = self.clock()
        if dependency := self.breakers.open_dependency():
            return await self._probe(dependency, now)
        internal = self.breakers.of[INTERNAL]
        if internal.open and now < internal.due:
            return internal.due - now
        try:
            return await self._grants(work, now, probe=internal.open)
        except Exception as error:
            dependency = dependency_of(error)
            if dependency is None:
                raise
            log.warning("%s outage, its breaker opens: %r", dependency, error)
            self._outage(dependency)
            return self.breakers.of[dependency].due - self.clock()

    def close(self) -> None:
        """Closes the connections. No message is held between cycles."""
        self._drop_broker()
        self._drop_db()

    async def _grants(self, work: Work, now: float, probe: bool) -> float:
        snapshot, others = await work.snapshot(self.cycles)
        waiting = [*others, *self.waiting(now)]
        grants = plan(snapshot, self.caps, self.profile.priority_classes, waiting, turn=self.turn)
        internal = self.breakers.of[INTERNAL]
        taken: Counter[str] = Counter()
        for (_, job_class), n in (*snapshot.live.items(), *snapshot.granted.items()):
            taken[job_class] += n
        # A probe is the first grant that reaches a job, past the queues found empty.
        done, settled, skip = [], False, set()
        for grant in grants[:MAX_GRANTS]:
            if not isinstance(grant, Queue):
                if await work.grant(grant):
                    taken[grant.job_class] += 1
                    settled = True
                done.append(grant)
                if probe:
                    break
                continue
            if grant.name in skip:
                continue
            outcome = await self._pull(grant, taken, probe)
            done.append(grant)
            settled = settled or outcome == "settled"
            if outcome == "stop" or (probe and outcome != "empty"):
                break
            if outcome != "settled":
                skip.add(grant.name)
        self.turn = self.turn.after(done)
        if probe and settled:
            internal.close()
        elif internal.open:
            if probe:
                internal.wait(now)
            return internal.due - now
        return 0.0 if settled else IDLE_S

    async def _pull(self, queue: Queue, taken: Counter[str], probe: bool) -> Settled:
        """Pulls one message of the queue and handles it in full."""
        consumer = await self._consumer(queue.name)
        if consumer is None:
            return "empty"
        got = await asyncio.to_thread(consumer.drain, 1, PULL_TIMEOUT_S)
        if not got:
            self._empty_until[queue.name] = self.clock() + EMPTY_BACKOFF_S
            return "empty"
        delivery = got[0]
        try:
            outcome = await self._handle(queue, delivery, taken)
        except Exception as error:
            dependency = dependency_of(error)
            if dependency is not None:
                if dependency != "broker":
                    await self._release(delivery)
                raise
            return await self._failed(delivery, error, probe)
        self.breakers.succeeded()
        return outcome

    async def _handle(self, queue: Queue, delivery: Delivery, taken: Counter[str]) -> Settled:
        try:
            event = read_event(delivery.body)
        except EventError as error:
            return await self._dead_letter(
                delivery,
                error.dlq_reason,
                _detail(error.reason, error.detail),
                _identity(delivery.body),
            )
        if event.type != ACCEPTED:
            return await self._dead_letter(
                delivery, "MALFORMED", _detail("wrong-type", event.type), event
            )
        if event.tenant not in self.profile.tenants:
            return await self._dead_letter(
                delivery, "MALFORMED", _detail("unknown-tenant", event.tenant), None
            )
        if event.priorityclass not in self.profile.priority_classes:
            return await self._dead_letter(
                delivery, "MALFORMED", _detail("unknown-class", event.priorityclass), None
            )
        if (event.tenant, event.priorityclass) != (queue.tenant, queue.priority_class):
            pair = f"{event.tenant}.{event.priorityclass} on {queue.name}"
            return await self._dead_letter(
                delivery, "MALFORMED", _detail("queue-mismatch", pair), event
            )
        return await self._admit(queue, delivery, event, taken)

    async def _admit(
        self, queue: Queue, delivery: Delivery, event: Event, taken: Counter[str]
    ) -> Settled:
        db = await self._database()
        row = await asyncio.to_thread(get_job, db, event.jobid)
        fresh = row is not None and row.status == "accepted"
        if fresh and taken[row.job_class] >= self.caps.job_class.get(row.job_class, 0):
            await asyncio.to_thread(delivery.release)
            self.blocked[queue.name] = row.job_class
            return "released"
        claimed = await asyncio.to_thread(claim, db, event.jobid)
        if isinstance(claimed, Skipped):
            if claimed.reason in RETRIED:
                return await self._retry(delivery, _detail(claimed.reason, event.jobid), event)
            await asyncio.to_thread(delivery.ack)
            self.counts["dsy_admission_skipped_total", claimed.reason] += 1
            return "settled"
        row = claimed.row
        if fresh:
            taken[row.job_class] += 1
        message = (event.tenant, event.priorityclass, event.jobclass)
        if (row.tenant, row.priority_class, row.job_class) != message:
            self.counts["dsy_admission_row_message_mismatch_total",] += 1
            log.warning("job %s: the message says %s, the row wins", row.job_id, message)
        try:
            started = await self.start(row, claimed.accepted)
        except Exception:
            self.counts["dsy_admission_start_failed_total",] += 1
            raise
        await asyncio.to_thread(delivery.ack)
        if started == "duplicate":
            self.counts["dsy_admission_duplicates_total",] += 1
        else:
            self.counts["dsy_admission_admitted_total", row.priority_class, row.job_class] += 1
        self.blocked.pop(queue.name, None)
        return "settled"

    async def _failed(self, delivery: Delivery, error: Exception, probe: bool) -> Settled:
        """Settles a message whose handling failed with an error of admission's own."""
        token = type(error).__name__
        log.warning("a message of %s failed: %r", delivery.queue, error)
        now = self.clock()
        if probe:
            await self._release(delivery)
            self.breakers.of[INTERNAL].fail(now)
            return "stop"
        if self.breakers.failed(token, delivery.body, now):
            log.error("%s failed 5 messages in a row, the internal breaker opens", token)
            await self._release(delivery)
            return "stop"
        return await self._retry(delivery, _detail(token, str(error)), _identity(delivery.body))

    async def _retry(self, delivery: Delivery, detail: str, who) -> Settled:
        """A counted retry, or the dead letter `ADMISSION_FAILED` with no event on the last
        delivery, which leaves the row as it is so a replay admits it."""
        if not delivery.last:
            await asyncio.to_thread(delivery.retry)
            return "settled"
        job = who.jobid if who else "no job"
        log.error("job %s dead-lettered ADMISSION_FAILED on its last delivery: %s", job, detail)
        return await self._dead_letter(delivery, "ADMISSION_FAILED", detail, None)

    async def _dead_letter(self, delivery: Delivery, reason: str, detail: str, who) -> Settled:
        if reason in EVENT_REASONS and who is not None and self._names_job(who):
            db = await self._database()
            await asyncio.to_thread(self._record, db, who, reason, detail)
        await asyncio.to_thread(delivery.dead_letter, reason, detail)
        self.counts["dsy_admission_dead_lettered_total", reason] += 1
        return "settled"

    def _names_job(self, who) -> bool:
        try:
            uuid.UUID(who.jobid)
        except ValueError:
            return False
        return (
            who.tenant in self.profile.tenants
            and who.priorityclass in self.profile.priority_classes
            and who.jobclass in self.caps.job_class
        )

    def _record(self, db: Database, who, reason: str, detail: str) -> None:
        """Writes `job.dead.lettered` for a job of the message's tenant, once per event id."""
        row = get_job(db, who.jobid)
        if row is None or row.tenant != who.tenant:
            return
        refused = {"dlq_reason": reason, "detail": detail}
        facts = Facts(
            time=datetime.now(UTC), emit=("job.dead.lettered",), data={"job.dead.lettered": refused}
        )
        try:
            [event] = stage.admission(who, facts)
            key = to_event_row(event)[0]
        except EventError as error:
            log.warning(
                "job %s: no dead-letter event, its attributes are refused: %r", who.jobid, error
            )
            return
        with db.transaction():
            if not db.execute("SELECT 1 FROM event WHERE event_id = %s", (key,)).fetchone():
                insert_events(db, [event])

    async def _release(self, delivery: Delivery) -> None:
        try:
            await asyncio.to_thread(delivery.release)
        except BrokerUnavailable as error:
            log.warning("broker outage, its breaker opens: %r", error)
            self._outage("broker")

    async def _consumer(self, queue: str) -> Consumer | None:
        """The consumer of a queue, or None when the queue is missing."""
        if queue not in self._consumers:
            if self._broker is None:
                self._broker = await asyncio.to_thread(self.connect_broker)
            try:
                self._consumers[queue] = await asyncio.to_thread(self._broker.consumer, queue)
            except TopologyError as error:
                self._missing.add(queue)
                self.counts["dsy_admission_queue_missing_total",] += 1
                log.error(
                    "queue %s is missing, tried again every %d cycles: %s",
                    queue,
                    MISSING_EVERY,
                    error,
                )
                return None
            self._missing.discard(queue)
        return self._consumers[queue]

    async def _database(self) -> Database:
        if self._db is None:
            self._db = await asyncio.to_thread(self.connect_db)
        return self._db

    async def _probe(self, dependency: str, now: float) -> float:
        breaker = self.breakers.of[dependency]
        if now < breaker.due:
            return breaker.due - now
        try:
            await self.probes[dependency]()
        except Exception as error:
            log.warning("the %s probe failed: %r", dependency, error)
            breaker.fail(now)
            return breaker.due - now
        log.info("the %s probe succeeded, its breaker closes", dependency)
        breaker.close()
        return 0.0

    async def _probe_database(self) -> None:
        self._drop_db()
        db = await asyncio.to_thread(self.connect_db)
        await asyncio.to_thread(db.execute, "SELECT 1")
        self._db = db

    async def _probe_broker(self) -> None:
        self._drop_broker()
        self._broker = await asyncio.to_thread(self.connect_broker)

    def _outage(self, dependency: str) -> None:
        if dependency == "broker":
            self._drop_broker()
        elif dependency == "database":
            self._drop_db()
        self.breakers.of[dependency].fail(self.clock())

    def _drop_broker(self) -> None:
        broker, self._broker, self._consumers = self._broker, None, {}
        if broker is not None:
            try:
                broker.close()
            except BrokerUnavailable:
                pass

    def _drop_db(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            db.close()
