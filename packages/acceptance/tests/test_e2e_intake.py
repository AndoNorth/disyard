"""The verdicts of the intake negatives against fakes, so no cluster is needed."""

import asyncio

import pytest
from dsy_acceptance.e2e import intake
from temporalio.service import RPCError, RPCStatusCode

JOB = "3a9617fa-df7c-5760-a644-42d559e0f433"
QUEUE = "jobs.intake.e2e.standard"
MALFORMED, UNSUPPORTED = (intake.POISON[n] for n in ("not-json", "wrong-major"))
BODIES = {"not-json": b"this is not json", "wrong-major": b'{"specversion":"9.0"}'}


def accepted(job_id=JOB):
    return [{"index": 0, "outcome": "accepted", "job_id": job_id}, {"summary": {}}]


def duplicate(job_id=JOB):
    return [{"index": 0, "outcome": "duplicate", "job_id": job_id, "status": "accepted"}]


def run_duplicate(first=None, again=None, rows=None, workflows=1):
    answers = iter([first or accepted(), again or duplicate()])
    return intake.duplicate_submission(
        lambda: next(answers), lambda: [JOB] if rows is None else rows, lambda job_id: workflows
    )


def test_a_repeated_key_is_one_job_row_one_workflow_and_a_duplicate_answer():
    assert run_duplicate() == JOB


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"again": accepted()}, "not \\[\\['accepted'\\], \\['duplicate'\\]\\]"),
        ({"again": duplicate("other")}, "not " + JOB),
        ({"rows": [JOB, "other"]}, "2 job rows"),
        ({"rows": []}, "0 job rows"),
        ({"workflows": 2}, "2 workflows"),
        ({"workflows": 0}, "0 workflows"),
    ],
)
def test_a_repeated_key_that_is_not_one_job_fails(kwargs, message):
    with pytest.raises(AssertionError, match=message):
        run_duplicate(**kwargs)


def test_rows_for_key_reads_the_ids_of_the_tenants_key():
    class Conn:
        def execute(self, sql, params):
            self.asked = (sql, params)
            return self

        def fetchall(self):
            return [(JOB,)]

    conn = Conn()
    assert intake.rows_for_key(conn, "e2e", "e2e-abc/0") == [JOB]
    assert conn.asked[1] == ("e2e", "e2e-abc/0")


def letter(position, reason, queue=QUEUE):
    return f"{position} {reason} token - - - {queue} 2026-11-25T09:00:00Z\n"


class Broker:
    """A dead-letter queue that the poison events land in once published, and a purge that empties
    it, which keeps what was done to it."""

    def __init__(self, held="", landing=None, purges=True):
        self.held, self.landing, self.purges = held, landing, purges
        self.published, self.calls = [], []

    def publish(self, body):
        self.published.append(body)
        if len(self.published) == len(BODIES) and self.landing is not None:
            self.held = self.landing
        return True

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "purge":
            assert "--yes" in args
            self.held = self.held if not self.purges else ""
            return ""
        if args[0] == "show":
            reason = args[2]
            sent = BODIES["not-json" if reason == MALFORMED else "wrong-major"]
            return f"reason: {reason}\nbody: {sent.decode()}\n"
        return self.held


LANDED = letter(1, MALFORMED) + letter(2, UNSUPPORTED)


def run_intake(broker, ended="succeeded"):
    def end(job_id):
        if isinstance(ended, Exception):
            raise ended
        return ended

    return intake.malformed_intake(broker, broker.publish, BODIES, lambda: JOB, end, timeout_s=0.2)


def test_both_poison_events_dead_letter_with_their_reasons_and_the_queue_is_purged():
    broker = Broker(landing=LANDED)
    assert run_intake(broker) == JOB
    assert broker.held == ""
    assert broker.calls[-2][0] == "purge"


def test_a_dead_letter_without_its_reason_code_fails_and_still_purges():
    broker = Broker(landing=letter(1, intake.POISON["not-json"]) + letter(2, "-"))
    with pytest.raises(AssertionError, match="reasons"):
        run_intake(broker)
    assert broker.held == ""


def test_an_event_that_never_lands_fails_and_still_purges():
    broker = Broker(landing=None)
    with pytest.raises(TimeoutError, match="the dead letters"):
        run_intake(broker)
    assert ("purge", "--yes") in broker.calls


def test_a_job_held_up_beside_the_poison_events_fails():
    for ended in ("failed", TimeoutError("no end")):
        broker = Broker(landing=LANDED)
        with pytest.raises(AssertionError, match="held up|ended failed"):
            run_intake(broker, ended)
        assert broker.held == ""


def test_a_queue_that_is_not_empty_to_begin_with_is_left_alone():
    broker = Broker(held=letter(1, MALFORMED), landing=LANDED)
    with pytest.raises(AssertionError, match="before N4"):
        run_intake(broker)
    assert broker.published == [] and not any(c[0] == "purge" for c in broker.calls)


def test_a_purge_that_leaves_a_letter_fails():
    broker = Broker(landing=LANDED, purges=False)
    with pytest.raises(AssertionError, match="holds 2 letters after"):
        run_intake(broker)


def test_a_refused_publish_fails_and_purges():
    broker = Broker(landing=LANDED)
    with pytest.raises(AssertionError, match="did not accept not-json"):
        intake.malformed_intake(broker, lambda body: False, BODIES, lambda: JOB, lambda j: "x")
    assert ("purge", "--yes") in broker.calls


class Cluster:
    """The admission Deployment: its replicas, its pods, and every kubectl call made."""

    def __init__(self, replicas=2, restores=True):
        self.replicas, self.restores, self.calls = replicas, restores, []

    def kubectl(self, *args):
        self.calls.append(args)
        if args[0] == "get":
            return str(self.replicas)
        if args[0] == "scale":
            count = int(args[2].removeprefix("--replicas="))
            if count == 0 or self.restores:
                self.replicas = count
        return ""

    def pods(self):
        return [{}] if self.replicas else []


def test_admission_is_held_at_zero_and_restored_to_its_count():
    cluster = Cluster(replicas=2)
    with intake.admission_held(cluster.kubectl, cluster.pods):
        assert cluster.replicas == 0
    assert cluster.replicas == 2


def test_admission_is_restored_when_the_block_fails():
    cluster = Cluster(replicas=2)
    with pytest.raises(RuntimeError, match="inside"):
        with intake.admission_held(cluster.kubectl, cluster.pods):
            raise RuntimeError("inside N5")
    assert cluster.replicas == 2
    assert ("rollout", "status", intake.ADMISSION, "--timeout=120s") in cluster.calls


def test_admission_left_at_zero_by_a_dead_run_is_restored_to_one():
    cluster = Cluster(replicas=0)
    with intake.admission_held(cluster.kubectl, cluster.pods):
        pass
    assert cluster.replicas == 1


def test_a_restore_that_does_not_reach_the_count_fails():
    cluster = Cluster(replicas=2, restores=False)
    with pytest.raises(AssertionError, match="has 0 replicas, not the 2 it had"):
        with intake.admission_held(cluster.kubectl, cluster.pods):
            pass


def test_admission_with_a_pod_that_never_stops_fails_after_restoring(monkeypatch):
    cluster = Cluster(replicas=2)
    monkeypatch.setattr(intake, "UP_S", 0.0)
    monkeypatch.setattr(cluster, "pods", lambda: [{}])
    with pytest.raises(AssertionError, match="still has a pod"):
        with intake.admission_held(cluster.kubectl, cluster.pods):
            pass
    assert cluster.replicas == 2


def cancelled(status="cancelled", outcome="applied", exists=False):
    return intake.cancel_before_admission(
        lambda: JOB,
        lambda job_id: {"outcome": outcome},
        lambda job_id: status,
        lambda job_id: exists,
        timeout_s=0.1,
    )


def test_a_job_cancelled_while_accepted_has_no_workflow():
    assert cancelled() == JOB


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"outcome": "noop"}, "not applied"),
        ({"status": "admitted"}, "reads admitted, not cancelled"),
        ({"exists": True}, "has a workflow"),
    ],
)
def test_a_cancel_that_did_not_stop_the_job_before_admission_fails(kwargs, message):
    with pytest.raises(AssertionError, match=message):
        cancelled(**kwargs)


def test_a_workflow_is_known_by_its_describe_and_not_by_search():
    async def known(job_id):
        return object()

    async def none(job_id):
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def down(job_id):
        raise RPCError("down", RPCStatusCode.UNAVAILABLE, b"")

    assert asyncio.run(intake.workflow_exists(known, JOB)) is True
    assert asyncio.run(intake.workflow_exists(none, JOB)) is False
    with pytest.raises(RPCError):
        asyncio.run(intake.workflow_exists(down, JOB))


def test_parse_letters_reads_the_reason_and_queue_of_each_letter_line():
    listing = letter(1, MALFORMED) + letter(2, UNSUPPORTED) + f"count {MALFORMED} 1\n"
    assert intake.parse_letters(listing) == [
        intake.Letter(MALFORMED, QUEUE),
        intake.Letter(UNSUPPORTED, QUEUE),
    ]
