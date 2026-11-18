"""The intake cycle against RabbitMQ and Postgres, with a recording fake for the start: the pull on
credit, the settle after the start, the dead letters and their events, the counted retries, the
head-of-line release of a job class at its cap, and the breakers.

Queue and dead-letter counts are read by polling, because the broker reports its state late.
"""

import asyncio
import json
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from temporalio.service import RPCError, RPCStatusCode
from wdf_admission.intake import IDLE_S, Intake, Work
from wdf_admission.policy import Caps, Resume, Snapshot
from wdf_broker import dlq
from wdf_broker.client import Consumer, Credentials, connect
from wdf_broker.models import Outcome
from wdf_broker.tenancy import load
from wdf_broker.testing.container import broker_container
from wdf_events import build_event, to_bytes
from wdf_jobstate import mark_published, transition

pytestmark = [pytest.mark.integration, pytest.mark.pair]

ACME = "intake.acme.standard"
BETA = "intake.beta.standard"
DLQ = "intake.dlq"
_ci = load("ci")
PROFILE = _ci.model_copy(
    update={"tenants": {"acme": _ci.tenants["acme"], "beta": _ci.tenants["acme"]}}
)
CAPS = Caps(
    open=100,
    global_=10,
    quotas={"acme": 10, "beta": 10},
    job_class={"short": 10, "long": 10},
    reserve_interval=5,
)
DEAD_LETTERED = ("dev.wdf.job.dead.lettered.v1", "/wdf/admission")
# Above the delayed retries of a message's five counted retries, 1 s times each delivery count.
RETRIES_S = 40
TIMEOUT_S = 10


def eventually(probe, expected, timeout_s: float = TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    while (value := probe()) != expected and time.monotonic() < deadline:
        time.sleep(0.2)
    return value


class Clock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Starts:
    """The recording start fake: it records each job it starts, and raises what `fail` returns."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail = lambda row: None
        self.hook = lambda row: None

    async def __call__(self, row, accepted):
        self.calls.append(row.job_id)
        self.hook(row)
        if error := self.fail(row):
            raise error
        return "started"


class Rows:
    """The rest of the service's work: a snapshot of the live counts read from the rows, and the
    waiting resumes, whose grants it records, standing in for their signals."""

    def __init__(self, db):
        self.db = db
        self.snapshots = 0
        self.waiting: list[Resume] = []
        self.granted: list[Resume] = []

    async def snapshot(self, cycle: int):
        self.snapshots += 1
        live = {
            (tenant, job_class): n
            for tenant, job_class, n in self.db.execute(
                "SELECT tenant, job_class, count(*) FROM accepted_job"
                " WHERE status IN ('admitted', 'running') GROUP BY 1, 2"
            )
        }
        return Snapshot(live=live, open=sum(live.values())), list(self.waiting)

    async def grant(self, resume) -> bool:
        self.granted.append(resume)
        return True

    @property
    def work(self) -> Work:
        return Work(self.snapshot, self.grant)


class Broker:
    """The test's side of the broker: publishes as dispatch, counts and reads as operator."""

    def __init__(self, broker):
        self.broker = broker

    def connect(self, user: str):
        return connect(self.broker.address, Credentials(user, self.broker.passwords[user]))

    def publish(self, body, queue: str = ACME) -> None:
        body = body if isinstance(body, bytes) else to_bytes(body)
        publisher = self.connect("dispatch")
        try:
            assert publisher.publish(queue, body) is Outcome.ACCEPTED
        finally:
            publisher.close()

    def counts(self, queue: str = ACME) -> tuple[int, int]:
        """The ready and the unacknowledged messages of a queue."""
        rows = json.loads(
            self.broker.ctl(
                "list_queues",
                "name",
                "messages_ready",
                "messages_unacknowledged",
                "--formatter",
                "json",
            )
        )
        row = next(row for row in rows if row["name"] == queue)
        return row["messages_ready"], row["messages_unacknowledged"]

    def delivery_counts(self, n: int, queue: str = ACME) -> list[int]:
        """The delivery counts of the first n messages of a queue, put back in their order."""
        assert eventually(lambda: self.counts(queue), (n, 0)) == (n, 0)
        operator = self.connect("operator")
        try:
            held = operator.consumer(queue).pull(n, TIMEOUT_S)
            counts = [delivery.delivery_count for delivery in held]
            for delivery in held:
                delivery.release()
        finally:
            operator.close()
        return counts

    def dead_letters(self, n: int) -> list[tuple[str, str]]:
        """The reason and detail of each dead letter, once the queue holds n."""
        assert eventually(lambda: self.counts(DLQ)[0], n, RETRIES_S) == n
        operator = self.connect("operator")
        try:
            held = operator.browser(DLQ).drain(n + 1, TIMEOUT_S)
            letters = [
                (d.annotations.get("x-opt-wdf-reason"), d.annotations.get("x-opt-wdf-detail"))
                for d in held
            ]
            for delivery in held:
                delivery.released()
        finally:
            operator.close()
        return letters


@pytest.fixture(scope="module")
def rabbitmq(t1):
    with broker_container(t1, PROFILE) as broker:
        yield broker


@pytest.fixture
def broker(rabbitmq):
    yield Broker(rabbitmq)
    rabbitmq.ctl("close_all_connections", "test")
    for queue in (ACME, BETA, DLQ):
        rabbitmq.ctl("purge_queue", queue)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def starts():
    return Starts()


@pytest.fixture
def rows(db):
    return Rows(db)


class Probe:
    """A probe fake that fails while `down` is set, and counts its calls."""

    def __init__(self):
        self.down = False
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.down:
            raise RPCError("down", RPCStatusCode.UNAVAILABLE, b"")


@pytest.fixture
def make_intake(broker, admission_dsn, starts, clock):
    made = []

    def make(caps: Caps = CAPS, profile=PROFILE, **kw) -> Intake:
        kw.setdefault("connect_db", lambda: psycopg.connect(admission_dsn, autocommit=True))
        kw.setdefault("probe_temporal", Probe())
        intake = Intake(
            profile,
            caps,
            connect_broker=lambda: broker.connect("admission"),
            start=starts,
            clock=clock,
            **kw,
        )
        made.append(intake)
        return intake

    yield make
    for intake in made:
        intake.close()


@pytest.fixture
def drains(monkeypatch):
    """The drains of each intake queue, counted."""
    counted: Counter[str] = Counter()
    drain = Consumer.drain

    def counting(self, n, timeout_s):
        counted[self.queue] += 1
        return drain(self, n, timeout_s)

    monkeypatch.setattr(Consumer, "drain", counting)
    return counted


def cycle(intake: Intake, rows: Rows) -> float:
    return asyncio.run(intake.cycle(rows.work))


def run_until(intake, rows, clock, done, timeout_s: float = RETRIES_S) -> None:
    """Runs cycles, moving the clock over each wait, until `done()` holds."""
    deadline = time.monotonic() + timeout_s
    while not done():
        assert time.monotonic() < deadline, "the cycles did not get there in time"
        wait = cycle(intake, rows)
        clock.advance(wait)
        if wait:
            time.sleep(0.2)


def edited(event, edit) -> bytes:
    document = json.loads(to_bytes(event))
    edit(document)
    return json.dumps(document).encode()


def unsupported(document) -> None:
    document["type"] = "dev.wdf.job.accepted.v2"
    document["dataschema"] = "urn:wdf:schema:job.accepted:2.0"


def test_ack_after_start(broker, make_intake, rows, starts, clock, seed_event, status):
    event = seed_event()
    broker.publish(event)
    held = []
    starts.hook = lambda row: held.append(eventually(broker.counts, (0, 1)))
    run_until(make_intake(), rows, clock, lambda: starts.calls)
    assert held == [(0, 1)]
    assert eventually(broker.counts, (0, 0)) == (0, 0)
    assert status(event.jobid) == "admitted"


def test_one_message_per_grant(broker, make_intake, rows, starts, clock, seed_event):
    events = [seed_event() for _ in range(3)]
    for event in events:
        broker.publish(event)
    assert eventually(broker.counts, (3, 0)) == (3, 0)
    intake = make_intake(replace(CAPS, global_=1))
    cycle(intake, rows)
    assert starts.calls == [events[0].jobid]
    assert eventually(broker.counts, (2, 0)) == (2, 0)


def test_malformed_dead_lettered(broker, make_intake, rows, clock, seed_event, events, status):
    event = seed_event()
    broker.publish(edited(event, lambda d: d["data"].pop("manifest")))
    intake = make_intake()
    run_until(
        intake, rows, clock, lambda: intake.counts["wdf_admission_dead_lettered_total", "MALFORMED"]
    )
    [(reason, detail)] = broker.dead_letters(1)
    assert (reason, detail.split(": ")[0]) == ("MALFORMED", "bad-data")
    assert events(event.jobid)[-1] == DEAD_LETTERED
    assert status(event.jobid) == "accepted"


def test_unsupported_major_dead_lettered(
    broker, make_intake, rows, clock, seed_event, events, status
):
    event = seed_event()
    broker.publish(edited(event, unsupported))
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "UNSUPPORTED_VERSION"],
    )
    [(reason, detail)] = broker.dead_letters(1)
    assert (reason, detail.split(": ")[0]) == ("UNSUPPORTED_VERSION", "unsupported-major")
    assert events(event.jobid)[-1] == DEAD_LETTERED
    assert status(event.jobid) == "accepted"


def test_dead_letter_event_once(broker, make_intake, rows, clock, seed_event, events):
    event = seed_event()
    body = edited(event, unsupported)
    broker.publish(body)
    broker.publish(body)
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "UNSUPPORTED_VERSION"] == 2,
    )
    assert len(broker.dead_letters(2)) == 2
    assert events(event.jobid).count(DEAD_LETTERED) == 1


def test_nameless_message_no_event(broker, make_intake, rows, clock, seed_event, db):
    event = seed_event()
    broker.publish(b"not json")
    broker.publish(edited(event, lambda d: d.pop("jobid")))
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "MALFORMED"] == 2,
    )
    tokens = [detail.split(": ")[0] for _, detail in broker.dead_letters(2)]
    assert tokens == ["malformed-json", "missing-jobid"]
    assert db.execute(
        "SELECT count(*) FROM event WHERE type = %s", (DEAD_LETTERED[0],)
    ).fetchone() == (0,)


def test_outage_releases_uncounted(broker, make_intake, rows, starts, clock, seed_event, status):
    event = seed_event()
    broker.publish(event)
    starts.fail = lambda row: RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    intake = make_intake()
    wait = cycle(intake, rows)
    assert starts.calls == [event.jobid]
    assert intake.breakers.gauge()["temporal"] == 1
    assert wait == pytest.approx(1.0)
    assert broker.delivery_counts(1) == [0]
    assert status(event.jobid) == "admitted"


def test_last_delivery_admission_failed(
    broker, make_intake, rows, starts, clock, seed_event, events
):
    event = seed_event()
    broker.publish(event)
    starts.fail = lambda row: ValueError("boom")
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "ADMISSION_FAILED"],
    )
    assert broker.dead_letters(1) == [("ADMISSION_FAILED", "ValueError: boom")]
    assert len(starts.calls) == 6
    assert DEAD_LETTERED not in events(event.jobid)
    assert not intake.breakers.of["internal"].open


def test_wrong_type_dead_lettered(broker, make_intake, rows, clock, seed_event, events):
    event = seed_event()
    admitted = build_event(
        "job.admitted",
        source="/wdf/admission",
        tenant=event.tenant,
        jobid=event.jobid,
        priorityclass=event.priorityclass,
        jobclass=event.jobclass,
        traceparent=event.traceparent,
        time=datetime.now(UTC),
        data={"workflow_id": f"job-{event.jobid}"},
    )
    broker.publish(admitted)
    intake = make_intake()
    run_until(
        intake, rows, clock, lambda: intake.counts["wdf_admission_dead_lettered_total", "MALFORMED"]
    )
    assert broker.dead_letters(1) == [("MALFORMED", "wrong-type: dev.wdf.job.admitted.v1")]
    assert events(event.jobid)[-1] == DEAD_LETTERED


def test_unknown_pair_dead_lettered(broker, make_intake, rows, clock, db, accepted_event_of):
    broker.publish(accepted_event_of("3a9617fa-df7c-5760-a644-42d559e0f433", tenant="zeta"))
    broker.publish(accepted_event_of("3a9617fa-df7c-5760-a644-42d559e0f434", priorityclass="gold"))
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "MALFORMED"] == 2,
    )
    assert broker.dead_letters(2) == [
        ("MALFORMED", "unknown-tenant: zeta"),
        ("MALFORMED", "unknown-class: gold"),
    ]
    assert db.execute(
        "SELECT count(*) FROM event WHERE type = %s", (DEAD_LETTERED[0],)
    ).fetchone() == (0,)


def test_queue_mismatch_dead_lettered(broker, make_intake, rows, clock, seed_event, events, status):
    event = seed_event(tenant="beta")
    broker.publish(event, queue=ACME)
    intake = make_intake()
    run_until(
        intake, rows, clock, lambda: intake.counts["wdf_admission_dead_lettered_total", "MALFORMED"]
    )
    assert broker.dead_letters(1) == [("MALFORMED", f"queue-mismatch: beta.standard on {ACME}")]
    assert events(event.jobid)[-1] == DEAD_LETTERED
    assert status(event.jobid) == "accepted"


def test_no_row_retried_then_admission_failed(
    broker, make_intake, rows, starts, clock, db, accepted_event_of
):
    job_id = "3a9617fa-df7c-5760-a644-42d559e0f435"
    broker.publish(accepted_event_of(job_id))
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "ADMISSION_FAILED"],
    )
    assert broker.dead_letters(1) == [("ADMISSION_FAILED", f"no-row: {job_id}")]
    assert starts.calls == []
    assert db.execute(
        "SELECT count(*) FROM event WHERE type = %s", (DEAD_LETTERED[0],)
    ).fetchone() == (0,)


def test_no_receipt_retried_then_admission_failed(
    broker, make_intake, rows, starts, clock, seed_event, events, status
):
    event = seed_event(published=False)
    broker.publish(event)
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "ADMISSION_FAILED"],
    )
    assert broker.dead_letters(1) == [("ADMISSION_FAILED", f"no-receipt: {event.jobid}")]
    assert starts.calls == []
    assert DEAD_LETTERED not in events(event.jobid)
    assert status(event.jobid) == "accepted"


def test_dead_lettered_row_stays_accepted(
    broker, make_intake, rows, starts, clock, seed_event, status, db, tmp_path
):
    event = seed_event(published=False)
    broker.publish(event)
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "ADMISSION_FAILED"],
    )
    broker.dead_letters(1)
    assert status(event.jobid) == "accepted"
    mark_published(db, event.jobid)
    (tmp_path / "operator.password").write_text(broker.broker.passwords["operator"])
    replayed = dlq.main(
        ["replay", "--address", broker.broker.address, "--credentials-dir", str(tmp_path)]
    )
    assert replayed == 0
    run_until(intake, rows, clock, lambda: starts.calls)
    assert starts.calls == [event.jobid]
    assert status(event.jobid) == "admitted"


def test_other_tenant_no_event(broker, make_intake, rows, clock, seed_event, events):
    event = seed_event()

    def as_beta(document):
        unsupported(document)
        document["tenant"] = "beta"

    broker.publish(edited(event, as_beta), queue=BETA)
    intake = make_intake()
    run_until(
        intake,
        rows,
        clock,
        lambda: intake.counts["wdf_admission_dead_lettered_total", "UNSUPPORTED_VERSION"],
    )
    assert [reason for reason, _ in broker.dead_letters(1)] == ["UNSUPPORTED_VERSION"]
    assert DEAD_LETTERED not in events(event.jobid)


def test_systematic_error_opens_breaker(broker, make_intake, rows, starts, clock, seed_event):
    for _ in range(6):
        broker.publish(seed_event())
    assert eventually(broker.counts, (6, 0)) == (6, 0)
    starts.fail = lambda row: ValueError("a bad deploy")
    intake = make_intake()
    wait = cycle(intake, rows)
    assert len(starts.calls) == 5
    assert intake.breakers.gauge()["internal"] == 1
    assert wait == pytest.approx(1.0)
    # Four counted retries, the fifth released uncounted, and the sixth never delivered.
    assert Counter(broker.delivery_counts(6)) == {0: 2, 1: 4}
    assert broker.counts(DLQ) == (0, 0)


def test_internal_breaker_probes_one_grant(broker, make_intake, rows, starts, clock, seed_event):
    for _ in range(3):
        broker.publish(seed_event())
    assert eventually(broker.counts, (3, 0)) == (3, 0)
    starts.fail = lambda row: ValueError("a bad deploy")
    intake = make_intake()
    intake.breakers.of["internal"].fail(clock())
    assert cycle(intake, rows) == pytest.approx(1.0)
    assert (starts.calls, rows.snapshots) == ([], 0)
    clock.advance(1.0)
    assert cycle(intake, rows) == pytest.approx(2.0)
    assert len(starts.calls) == 1
    assert broker.delivery_counts(3) == [0, 0, 0]
    starts.fail = lambda row: None
    clock.advance(2.0)
    assert cycle(intake, rows) == 0.0
    assert len(starts.calls) == 2
    assert intake.breakers.gauge()["internal"] == 0
    cycle(intake, rows)
    assert len(set(starts.calls)) == 3


def test_failure_after_claim_left_admitted(
    broker, make_intake, rows, starts, clock, seed_event, status, events
):
    event = seed_event()
    broker.publish(event)
    starts.fail = lambda row: (
        ValueError("unregistered search attribute") if len(starts.calls) == 1 else None
    )
    intake = make_intake()
    cycle(intake, rows)
    assert status(event.jobid) == "admitted"
    run_until(intake, rows, clock, lambda: len(starts.calls) == 2)
    assert starts.calls == [event.jobid, event.jobid]
    assert eventually(broker.counts, (0, 0)) == (0, 0)
    assert events(event.jobid).count(("dev.wdf.job.admitted.v1", "/wdf/admission")) == 1


def test_broker_breaker_reconnects(broker, make_intake, rows, starts, clock, seed_event, drains):
    intake = make_intake()
    assert cycle(intake, rows) == IDLE_S
    broker.broker.ctl("close_all_connections", "test")
    connections = lambda: broker.broker.ctl("list_connections", "user").strip()  # noqa: E731
    assert eventually(connections, "") == ""
    event = seed_event()
    broker.publish(event)
    clock.advance(IDLE_S)
    assert cycle(intake, rows) == pytest.approx(1.0)
    assert intake.breakers.gauge()["broker"] == 1
    pulled = sum(drains.values())
    assert cycle(intake, rows) == pytest.approx(1.0)
    assert (sum(drains.values()), rows.snapshots) == (pulled, 2)
    clock.advance(1.0)
    assert cycle(intake, rows) == 0.0
    assert intake.breakers.gauge()["broker"] == 0
    run_until(intake, rows, clock, lambda: starts.calls)
    assert starts.calls == [event.jobid]


def test_missing_queue_skipped(broker, make_intake, rows, starts, clock, seed_event):
    tenants = {**PROFILE.tenants, "gamma": PROFILE.tenants["acme"]}
    caps = replace(CAPS, quotas={**CAPS.quotas, "gamma": 10})
    intake = make_intake(caps, PROFILE.model_copy(update={"tenants": tenants}))
    event = seed_event()
    broker.publish(event)
    assert eventually(broker.counts, (1, 0)) == (1, 0)
    cycle(intake, rows)
    assert starts.calls == [event.jobid]
    assert intake.counts["wdf_admission_queue_missing_total",] == 1
    while intake.cycles < 9:
        clock.advance(cycle(intake, rows))
    assert intake.counts["wdf_admission_queue_missing_total",] == 1
    clock.advance(cycle(intake, rows))
    assert intake.counts["wdf_admission_queue_missing_total",] == 2


def test_empty_queue_backed_off(make_intake, rows, clock, drains):
    intake = make_intake()
    assert cycle(intake, rows) == IDLE_S
    assert drains == {ACME: 1, BETA: 1}
    assert cycle(intake, rows) == IDLE_S
    clock.advance(0.5)
    assert cycle(intake, rows) == IDLE_S
    assert drains == {ACME: 1, BETA: 1}
    clock.advance(0.5)
    assert cycle(intake, rows) == IDLE_S
    assert drains == {ACME: 2, BETA: 2}


def test_class_at_cap_released(
    broker, make_intake, rows, starts, clock, seed_event, admission_db, db
):
    from wdf_admission.claim import claim

    holder = seed_event()
    claim(admission_db, holder.jobid)
    first, second = seed_event(), seed_event()
    broker.publish(first)
    broker.publish(second)
    assert eventually(broker.counts, (2, 0)) == (2, 0)
    intake = make_intake(replace(CAPS, job_class={"short": 1, "long": 1}))
    assert cycle(intake, rows) == IDLE_S
    assert starts.calls == []
    assert intake.blocked == {ACME: "short"}
    assert [q.blocked_on for q in intake.waiting(clock()) if q.name == ACME] == ["short"]
    # The released message is back at the head, its delivery count unchanged.
    operator = broker.connect("operator")
    [head] = operator.consumer(ACME).drain(1, TIMEOUT_S)
    assert (json.loads(head.body)["jobid"], head.delivery_count) == (first.jobid, 0)
    head.release()
    operator.close()
    transition(db, holder.jobid, "admitted", "running")
    transition(db, holder.jobid, "running", "succeeded")
    cycle(intake, rows)
    assert starts.calls == [first.jobid]
    assert intake.blocked == {ACME: "short"}
    assert eventually(broker.counts, (1, 0)) == (1, 0)


def test_class_cap_counts_cycle_grants(broker, make_intake, rows, starts, clock, seed_event):
    first, second = seed_event(), seed_event()
    broker.publish(first)
    broker.publish(second)
    assert eventually(broker.counts, (2, 0)) == (2, 0)
    intake = make_intake(replace(CAPS, job_class={"short": 1, "long": 1}))
    cycle(intake, rows)
    assert starts.calls == [first.jobid]
    assert intake.blocked == {ACME: "short"}
    assert eventually(broker.counts, (1, 0)) == (1, 0)


def test_breaker_opens_and_closes(broker, make_intake, rows, starts, clock, seed_event, drains):
    probe = Probe()
    intake = make_intake(probe_temporal=probe)
    rows.waiting = [Resume("r", "standard", "acme", "short", datetime.now(UTC))]
    event = seed_event()
    broker.publish(event)
    assert eventually(broker.counts, (1, 0)) == (1, 0)
    starts.fail = lambda row: RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    assert cycle(intake, rows) == pytest.approx(1.0)
    assert intake.breakers.gauge()["temporal"] == 1
    seen = (rows.snapshots, len(rows.granted), sum(drains.values()), len(starts.calls))
    probe.down = True
    assert cycle(intake, rows) == pytest.approx(1.0)
    clock.advance(1.0)
    assert cycle(intake, rows) == pytest.approx(2.0)
    clock.advance(1.0)
    assert cycle(intake, rows) == pytest.approx(1.0)
    assert probe.calls == 1
    assert (rows.snapshots, len(rows.granted), sum(drains.values()), len(starts.calls)) == seen
    probe.down = False
    starts.fail = lambda row: None
    clock.advance(1.0)
    assert cycle(intake, rows) == 0.0
    assert (probe.calls, intake.breakers.gauge()["temporal"]) == (2, 0)
    cycle(intake, rows)
    assert starts.calls[-1] == event.jobid
    assert eventually(broker.counts, (0, 0)) == (0, 0)


def test_db_breaker_opens_and_closes(
    broker, make_intake, rows, starts, clock, seed_event, drains, admission_dsn, db
):
    down = []

    def connect_db():
        if down:
            raise psycopg.OperationalError("the database is down")
        return psycopg.connect(admission_dsn, autocommit=True)

    intake = make_intake(connect_db=connect_db)
    rows.waiting = [Resume("r", "standard", "acme", "short", datetime.now(UTC))]
    broker.publish(seed_event())
    run_until(intake, rows, clock, lambda: starts.calls)
    event = seed_event()
    broker.publish(event)
    assert eventually(broker.counts, (1, 0)) == (1, 0)
    # The login's sessions end under it, as when the database restarts.
    user = conninfo_to_dict(admission_dsn)["user"]
    db.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (user,))
    down.append(True)
    clock.advance(IDLE_S)
    cycle(intake, rows)
    assert intake.breakers.gauge()["database"] == 1
    assert broker.delivery_counts(1) == [0]
    seen = (rows.snapshots, len(rows.granted), sum(drains.values()))
    clock.advance(1.0)
    assert cycle(intake, rows) == pytest.approx(2.0)
    assert (rows.snapshots, len(rows.granted), sum(drains.values())) == seen
    down.clear()
    clock.advance(2.0)
    assert cycle(intake, rows) == 0.0
    assert intake.breakers.gauge()["database"] == 0
    run_until(intake, rows, clock, lambda: len(starts.calls) == 2)
    assert starts.calls[-1] == event.jobid
