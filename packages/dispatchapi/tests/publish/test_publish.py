"""Publishing after the answer and the reconciler, against Postgres and SeaweedFS behind the submit
route, with wdf_broker's FakeBroker as the broker."""

import json
import logging
import threading
from collections import Counter
from datetime import timedelta

import pytest
from wdf_broker.contract import topology
from wdf_broker.models import TopologyError
from wdf_dispatchapi import submit as submit_module
from wdf_dispatchapi.reconciler import Reconciler
from wdf_dispatchapi.submit import accepted_event
from wdf_events import from_bytes, to_bytes
from wdf_jobstate import get_job, transition

pytestmark = [pytest.mark.integration, pytest.mark.pair]

ACCEPTED = "dev.wdf.job.accepted.v1"
PUBLISHED = "dev.wdf.job.published.v1"
QUEUE = "intake.acme.standard"
DLQ = topology().dead_letter_queue.name


class Crash(BaseException):
    """A process crash: no handler of the code under test catches it."""


class Hooked:
    """A broker connection that runs `before(n)` and `after(n, outcome)` around its n-th publish,
    counting from 0."""

    def __init__(self, connection, before=None, after=None):
        self.connection, self.before, self.after = connection, before, after
        self.publishes = 0

    def publish(self, routing_key: str, body: bytes, timeout_s: float = 5.0):
        n, self.publishes = self.publishes, self.publishes + 1
        if self.before:
            self.before(n)
        outcome = self.connection.publish(routing_key, body, timeout_s)
        if self.after:
            self.after(n, outcome)
        return outcome


def line(data: dict) -> bytes:
    return json.dumps(data).encode() + b"\n"


def body(template: dict, entries, count: int = 3, batch_key: str = "b", **fields) -> list[bytes]:
    return [line({"batch_key": batch_key, "template": template, **fields}), *entries(count)]


def per_job(db) -> dict[str, tuple[int, int]]:
    """The stored `job.accepted` and `job.published` events of each job."""
    rows = db.execute(
        "SELECT job_id::text, count(*) FILTER (WHERE type = %s),"
        " count(*) FILTER (WHERE type = %s) FROM event GROUP BY job_id",
        (ACCEPTED, PUBLISHED),
    ).fetchall()
    return {job_id: (accepted, published) for job_id, accepted, published in rows}


def receipts(db) -> int:
    return db.execute(
        "SELECT count(*) FROM accepted_job WHERE published_at IS NOT NULL"
    ).fetchone()[0]


def raise_crash(*args, **kwargs):
    raise Crash()


def claim(broker, db) -> Counter:
    """Claims each queued message's job by the guarded move, as admission does, acknowledging a
    message whose job was already claimed, and counts the messages of each job."""
    consumer = broker.connect().consumer(QUEUE)
    seen = Counter()
    while deliveries := consumer.drain(100, 0):
        for delivery in deliveries:
            job_id = from_bytes(delivery.body).jobid
            seen[job_id] += 1
            transition(db, job_id, "accepted", "admitted")
            delivery.ack()
    return seen


STEPS = ["links", "rows", "answer", "publish", "receipt"]


@pytest.mark.parametrize("step", STEPS)
def test_crash_at_each_step_then_resend(
    step, monkeypatch, broker, make_publisher, make_submit, call, template, entries, db, reconcile
):
    """A crash after each step of the submit sequence, then the whole call resent, a reconciler
    pass and a claim of every queued message: every job has one row, one `job.accepted` and one
    `job.published`, is claimed once, and has at most two messages, the extra one acknowledged."""
    hooks = {
        "publish": {"after": lambda n, outcome: raise_crash()},
        "receipt": {"before": lambda n: n == 1 and raise_crash()},
    }.get(step, {})
    publisher = make_publisher(lambda address, credentials: Hooked(broker.connect(), **hooks))
    if step == "links":
        monkeypatch.setattr(submit_module, "insert_accepted", raise_crash)
    elif step == "rows":
        monkeypatch.setattr(submit_module._Call, "_write", raise_crash)
    elif step == "answer":
        monkeypatch.setattr(publisher, "publish", raise_crash)
    with pytest.raises(Crash):
        call(make_submit(publisher), body(template, entries))
    monkeypatch.undo()

    resent = call(make_submit(make_publisher()), body(template, entries))
    reconcile()
    messages = claim(broker, db)

    jobs = {answer["job_id"] for answer in resent[:-1]}
    assert {answer["outcome"] for answer in resent[:-1]} <= {"accepted", "duplicate"}
    assert len(jobs) == 3
    assert per_job(db) == {job_id: (1, 1) for job_id in jobs}
    assert set(messages) == jobs and max(messages.values()) <= 2
    statuses = db.execute("SELECT status, count(*) FROM accepted_job GROUP BY status").fetchall()
    assert statuses == [("admitted", 3)]
    assert len(broker.queues[QUEUE]) == 0 and len(broker.queues[DLQ]) == 0


def test_rejected_and_unroutable_leave_no_receipt(
    broker, make_publisher, make_submit, call, template, entries, tenancy, db, caplog
):
    """A full queue rejects, and a priority class outside the broker topology is unroutable and
    logged as a defect: each submitter is answered `accepted`, and no row has a receipt."""
    filler = broker.connect()
    for _ in range(1001):
        filler.publish(QUEUE, b"{}")
    gold = tenancy.model_copy(update={"priority_classes": ["standard", "gold"]})
    submit = make_submit(make_publisher(), tenancy=gold)

    full = call(submit, body(template, entries, batch_key="full"))
    unroutable = call(submit, body(template, entries, batch_key="gold", priority_class="gold"))

    for answers in (full, unroutable):
        assert [answer["outcome"] for answer in answers[:-1]] == ["accepted"] * 3
    assert receipts(db) == 0
    assert all(published == 0 for _, published in per_job(db).values())
    assert "no intake queue intake.acme.gold" in caplog.text


def test_reconciler_rebuild_equals_stored_bytes(
    make_submit, call, template, entries, db, reconcile, broker
):
    """The rebuild of a row is its stored event byte for byte, so it stores nothing, and the event
    of a row that the history dropped is stored again."""
    answers = call(make_submit(), body(template, entries, count=2))
    kept, dropped = (answer["job_id"] for answer in answers[:-1])

    def stored(job_id: str) -> list[str]:
        return [
            body
            for (body,) in db.execute(
                "SELECT body FROM event WHERE job_id = %s AND type = %s", (job_id, ACCEPTED)
            )
        ]

    rebuilt = {}
    for job_id in (kept, dropped):
        row = get_job(db, job_id)
        rebuilt[job_id] = to_bytes(accepted_event(row, row.created_at)).decode()
        assert stored(job_id) == [rebuilt[job_id]]
    db.execute("DELETE FROM event WHERE job_id = %s AND type = %s", (dropped, ACCEPTED))

    reconcile()

    assert stored(kept) == [rebuilt[kept]]
    assert stored(dropped) == [rebuilt[dropped]]
    assert receipts(db) == 2
    assert sorted(m.body.decode() for m in broker.queues[QUEUE].ready) == sorted(rebuilt.values())


def test_one_published_event_when_reconciler_races_submit(
    broker, make_publisher, make_submit, call, template, entries, db, reconcile
):
    """A reconciler pass that publishes every row before the submit call publishes its first: both
    publishes land, and each job records one `job.published`, by whichever set its receipt."""
    race = {"before": lambda n: n == 0 and reconcile()}
    publisher = make_publisher(lambda address, credentials: Hooked(broker.connect(), **race))

    call(make_submit(publisher), body(template, entries))

    assert len(broker.queues[QUEUE]) == 6
    assert receipts(db) == 3
    assert sorted(per_job(db).values()) == [(1, 1)] * 3


def test_reconciler_skips_a_row_with_a_receipt(
    broker, make_publisher, make_submit, call, template, entries, db, reconcile
):
    """A row published by its submit call, and a row moved off `accepted` before its publish, are
    never published by the reconciler."""
    call(make_submit(make_publisher()), body(template, entries, batch_key="published"))
    answers = call(make_submit(), body(template, entries, count=1, batch_key="cancelled"))
    transition(db, answers[0]["job_id"], "accepted", "cancelled", row_only=True)

    reconcile()

    assert len(broker.queues[QUEUE]) == 3
    assert receipts(db) == 3


def test_reconciler_waits_out_the_grace(
    broker, make_submit, call, template, entries, db, reconcile
):
    """A row younger than the grace is left to its submit call, and published once it is older."""
    call(make_submit(), body(template, entries))

    reconcile(grace=timedelta(seconds=10))
    assert len(broker.queues[QUEUE]) == 0 and receipts(db) == 0

    reconcile()
    assert len(broker.queues[QUEUE]) == 3 and receipts(db) == 3


def test_publish_timeout_ends_the_call_and_the_pass(
    broker, make_publisher, make_submit, call, template, entries, db, reconcile
):
    """Under a memory alarm a submit call and a reconciler pass each stop at their first publish
    timeout with no receipt, and once the alarm clears the next pass publishes every row."""
    hooked = []

    def connect(address, credentials):
        hooked.append(Hooked(broker.connect()))
        return hooked[-1]

    publisher = make_publisher(connect, timeout_s=0.1)
    broker.alarm(True)

    answers = call(make_submit(publisher), body(template, entries))
    assert [answer["outcome"] for answer in answers[:-1]] == ["accepted"] * 3
    assert hooked[0].publishes == 1 and receipts(db) == 0

    reconcile(publisher)
    assert hooked[0].publishes == 2 and receipts(db) == 0

    broker.alarm(False)
    reconcile(publisher)
    assert receipts(db) == 3 and len(broker.queues[QUEUE]) == 3
    assert sorted(per_job(db).values()) == [(1, 1)] * 3


def test_reconnect_after_broker_unavailable(
    broker, make_publisher, make_submit, call, template, entries, db, reconcile
):
    """A restart loses the connection, so the call's first publish after it ends the call's
    publishing, and the next pass connects again and publishes the rows left behind."""
    connects = []

    def connect(address, credentials):
        connects.append(broker.connect())
        return connects[-1]

    publisher = make_publisher(connect)
    submit = make_submit(publisher)
    call(submit, body(template, entries, batch_key="before"))
    assert len(connects) == 1 and receipts(db) == 3

    broker.restart()
    call(submit, body(template, entries, batch_key="after"))
    assert len(connects) == 1 and receipts(db) == 3

    reconcile(publisher)
    assert len(connects) == 2 and receipts(db) == 6
    assert len(broker.queues[QUEUE]) == 6


def test_topology_error_stops_publishing(
    make_publisher, make_submit, call, template, entries, db, logins, caplog
):
    """A missing exchange stops the reconciler's loop by itself and the publisher for good, with
    an error log and no retry, and every row keeps no receipt."""

    class Missing:
        publishes = 0

        def publish(self, routing_key, body, timeout_s=5.0):
            Missing.publishes += 1
            raise TopologyError("amqp:not-found: no exchange wdf.intake")

    publisher = make_publisher(lambda address, credentials: Missing())
    call(make_submit(), body(template, entries, batch_key="first"))

    reconciler = Reconciler(publisher, timedelta(0), interval_s=0.05)
    loop = threading.Thread(target=reconciler.run, args=(logins["dispatch"],))
    with caplog.at_level(logging.ERROR):
        loop.start()
        loop.join(10)
    assert not loop.is_alive()
    assert publisher.stopped and Missing.publishes == 1

    call(make_submit(publisher), body(template, entries, batch_key="second"))
    assert Missing.publishes == 1 and receipts(db) == 0
    assert "the intake exchange is missing" in caplog.text
    assert "the reconciler stops" in caplog.text
