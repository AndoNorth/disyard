"""The dead-letter tool against a broker container: the reasons it lists, a pass that moves nothing,
replay with its upgrade, its refusal and its order of publish and settle, purge, show and the
selectors."""

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest
from wdf_broker import client, dlq
from wdf_broker.client import Credentials, connect
from wdf_broker.contract import topology
from wdf_broker.models import BrokerUnavailable, Outcome
from wdf_broker.testing.container import broker_container
from wdf_compat import Registry
from wdf_events.profile import CONTRACT_DIR

pytestmark = pytest.mark.integration

QUEUE = "intake.acme.standard"
DLQ = "intake.dlq"
LIMIT = topology().intake_policy.definition.delivery_limit
# Above the longest delayed retry, 1 s times the delivery count before a dead letter.
TIMEOUT_S = 10
GOLDEN = (CONTRACT_DIR / "vectors/golden/job.accepted/1.0.json").read_bytes()
JOB = json.loads(GOLDEN)["jobid"]
WRONG_MAJOR = (CONTRACT_DIR / "vectors/poison/wrong-major.json").read_bytes()
NOT_JSON = (CONTRACT_DIR / "vectors/poison/not-json.json").read_bytes()


def jobid(n: int) -> str:
    return f"{JOB[:-1]}{n}"


def job(n: int) -> bytes:
    """The golden event of a job of its own, whose id ends in n."""
    document = json.loads(GOLDEN)
    document["jobid"] = document["subject"] = jobid(n)
    return json.dumps(document).encode()


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
    for queue in (QUEUE, DLQ):
        consumer = operator.consumer(queue)
        while got := consumer.drain(100, TIMEOUT_S):
            for delivery in got:
                delivery.ack()
    operator.close()


@pytest.fixture
def run(broker, tmp_path, capsys):
    """Runs the tool as the operator and returns its exit code, stdout lines and stderr."""
    (tmp_path / "operator.password").write_text(broker.passwords["operator"])

    def run(*argv: str, **kwargs) -> tuple[int, list[str], str]:
        common = ["--address", broker.address, "--credentials-dir", str(tmp_path)]
        code = dlq.main([*argv, *common], **kwargs)
        out, err = capsys.readouterr()
        return code, out.splitlines(), err

    return run


@pytest.fixture
def dead(open_as):
    """Dead-letters one message per call, with an application reason and its detail, or by six
    retries when no reason is given."""
    publisher, consumer = open_as("dispatch"), open_as("admission").consumer(QUEUE)

    def dead(body: bytes, reason: str | None = None, detail: str = "") -> None:
        assert publisher.publish(QUEUE, body) is Outcome.ACCEPTED
        for _ in range(LIMIT + 1 if reason is None else 1):
            (delivery,) = consumer.pull(1, TIMEOUT_S)
            if reason is None:
                delivery.retry()
            else:
                delivery.dead_letter(reason, detail)

    return dead


@pytest.fixture
def held(broker):
    """Reads the body and delivery count of every message of a queue, in queue order, and leaves
    them in place, on a connection of its own that it closes, as an operator's pass would."""

    def held(queue: str = DLQ) -> list[tuple[bytes, int]]:
        operator = connect(broker.address, Credentials("operator", broker.passwords["operator"]))
        try:
            browse = queue == DLQ
            consumer = operator.browser(queue) if browse else operator.consumer(queue)
            got = consumer.drain(100, TIMEOUT_S)
            for delivery in got:
                delivery.released() if browse else delivery.release()
            return [(d.body, d.delivery_count) for d in got]
        finally:
            operator.close()

    return held


def fields(line: str) -> list[str]:
    """The fields of a list line other than the death time."""
    parts = line.split()
    return parts[:7] + parts[8:]


def test_dlq_list_reasons(run, dead, monkeypatch):
    dead(NOT_JSON, "MALFORMED", "not-json: list test")
    dead(WRONG_MAJOR, "UNSUPPORTED_VERSION", "unsupported-major: list test")
    dead(job(1), "ADMISSION_FAILED", "no-row: list test")
    dead(job(2))
    code, out, _ = run("list")
    assert code == 0
    assert [fields(line) for line in out[:4]] == [
        ["1", "MALFORMED", "not-json", "-", "-", "-", QUEUE],
        ["2", "UNSUPPORTED_VERSION", "unsupported-major", JOB, "acme", "standard", QUEUE],
        ["3", "ADMISSION_FAILED", "no-row", jobid(1), "acme", "standard", QUEUE],
        ["4", "delivery_limit", "-", jobid(2), "acme", "standard", QUEUE],
    ]
    died = datetime.strptime(out[0].split()[7], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - died) < timedelta(minutes=5)
    assert out[4:] == [
        "count ADMISSION_FAILED 1",
        "count MALFORMED 1",
        "count UNSUPPORTED_VERSION 1",
        "count delivery_limit 1",
    ]
    monkeypatch.setattr(dlq, "RETENTION", timedelta(days=-1))
    code, out, _ = run("list", "--limit", "1")
    assert out[0].endswith(" stale")


def test_dlq_list_moves_nothing(run, dead, held):
    for n in range(3):
        dead(job(n), "ADMISSION_FAILED", f"no-row: message {n}")
    before = held()
    assert [count for _, count in before] == [1, 1, 1]
    first = run("list")
    assert first[0] == 0
    # Passes that select only some messages, the head among them, leave the order as it was.
    for partial in (
        ["list", "--limit", "1"],
        ["show", "--job", jobid(1)],
        ["list", "--limit", "2"],
    ):
        run(*partial)
        assert run("list") == first, partial
    assert held() == before


def test_dlq_replay_upgrades_previous_major(run, dead, open_as, held, tmp_path):
    # A registry where the pinned major of job.accepted is the previous one, and its upgrade only
    # renames the type.
    schemas = tmp_path / "schemas/job.accepted"
    schemas.mkdir(parents=True)
    for version in ("1.0", "2.0"):
        shutil.copy(CONTRACT_DIR / "schemas/job.accepted/1.0.json", schemas / f"{version}.json")
    today = datetime.now(UTC).date().isoformat()

    def upgrade(document: dict) -> dict:
        return {
            **document,
            "type": "dev.wdf.job.accepted.v2",
            "dataschema": "urn:wdf:schema:job.accepted:2.0",
        }

    registry = Registry(
        tmp_path / "schemas",
        {("job.accepted", 1): "2026-01-01", ("job.accepted", 2): today},
        {("job.accepted", 1): upgrade},
    )
    dead(GOLDEN, "ADMISSION_FAILED", "no-row: upgrade test")
    code, out, _ = run("replay", registry=registry)
    assert (code, len(out)) == (0, 2)
    (delivery,) = open_as("admission").consumer(QUEUE).pull(1, TIMEOUT_S)
    expected = upgrade(json.loads(GOLDEN))
    assert delivery.body == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    delivery.ack()
    assert held() == []


def test_dlq_replay_refuses_never(run, dead, held):
    dead(NOT_JSON, "MALFORMED", "not-json: refuse test")
    # A valid event dead-lettered for a pair outside the profile, which replays after the fix.
    dead(GOLDEN, "MALFORMED", "unknown-tenant: refuse test")
    dead(job(1), "ADMISSION_FAILED", "no-row: refuse test")
    code, out, err = run("replay", "--reason", "MALFORMED")
    assert (code, out) == (1, [])
    assert err.count("never recovers") == 2
    assert len(held()) == 3

    code, out, err = run("replay")
    assert code == 1
    assert [line.split()[:2] for line in out] == [
        ["3", "ADMISSION_FAILED"],
        ["count", "ADMISSION_FAILED"],
    ]
    assert held() == [(NOT_JSON, 1), (GOLDEN, 1)]

    code, out, err = run("replay", "--force")
    assert code == 1
    assert "does not upgrade" in err
    assert out[-1] == "count MALFORMED 1"
    assert held() == [(NOT_JSON, 1)]
    bodies = [body for body, _ in held(QUEUE)]
    assert bodies == [job(1), GOLDEN]


def test_dlq_replay_publish_before_settle(run, dead, open_as, held, monkeypatch):
    dead(GOLDEN, "ADMISSION_FAILED", "no-row: settle test")
    publisher = open_as("dispatch")
    n = 0
    while publisher.publish(QUEUE, b"%d" % n) is Outcome.ACCEPTED:
        n += 1
    # A publish that the full intake queue rejects leaves the dead letter where it was.
    code, out, err = run("replay")
    assert (code, out) == (1, [])
    assert "the publish was rejected" in err
    assert held() == [(GOLDEN, 1)]
    consumer = open_as("admission").consumer(QUEUE)
    while got := consumer.drain(100, TIMEOUT_S):
        for delivery in got:
            delivery.ack()

    # A crash after the publish and before the settle leaves the message on both queues.
    publish = client.Connection.publish

    def crash(self, *args, **kwargs):
        publish(self, *args, **kwargs)
        raise BrokerUnavailable("crash after the publish")

    monkeypatch.setattr(client.Connection, "publish", crash)
    code, out, err = run("replay")
    assert (code, out) == (3, [])
    assert "crash after the publish" in err
    monkeypatch.undo()
    assert [body for body, _ in held()] == [GOLDEN]
    assert held(QUEUE) == [(GOLDEN, 0)]

    # With room on the queue, the replay acks the dead letter and the intake copy is unchanged.
    code, out, _ = run("replay")
    assert code == 0
    assert held() == []
    assert held(QUEUE) == [(GOLDEN, 0), (GOLDEN, 0)]


def test_dlq_purge_needs_yes(run, dead, held):
    dead(NOT_JSON, "MALFORMED", "not-json: purge test")
    dead(job(1), "ADMISSION_FAILED", "no-row: purge test")
    code, out, err = run("purge")
    assert (code, out) == (2, [])
    assert "--yes" in err
    assert len(held()) == 2
    code, out, _ = run("purge", "--reason", "MALFORMED", "--yes")
    assert code == 0
    assert [line.split()[:2] for line in out] == [["1", "MALFORMED"], ["count", "MALFORMED"]]
    assert held() == [(job(1), 1)]


def test_dlq_show_and_selectors(run, dead):
    for n in range(3):
        dead(job(n), "ADMISSION_FAILED" if n else "MALFORMED", f"no-row: message {n}")
    code, out, _ = run("list", "--limit", "1")
    assert code == 0
    assert [line.split()[:2] for line in out] == [["1", "MALFORMED"], ["count", "MALFORMED"]]

    code, out, _ = run("list", "--job", jobid(2))
    assert [line.split()[:4] for line in out] == [
        ["3", "ADMISSION_FAILED", "no-row", jobid(2)],
        ["count", "ADMISSION_FAILED", "1"],
    ]
    code, out, _ = run("list", "--reason", "ADMISSION_FAILED", "--limit", "2")
    assert [line.split()[0] for line in out] == ["2", "count"]

    code, out, _ = run("show", "--job", jobid(2))
    assert code == 0
    assert out[:4] == [
        "position: 3",
        "reason: ADMISSION_FAILED",
        "detail: no-row: message 2",
        f"jobid: {JOB[:-1]}2",
    ]
    assert "annotation x-opt-wdf-reason: ADMISSION_FAILED" in out
    assert f"annotation x-first-death-queue: {QUEUE}" in out
    assert out[-1] == f"body: {job(2).decode()}"
    code, out, err = run("show", "--reason", "UNSUPPORTED_VERSION")
    assert (code, out, err) == (0, [], "no dead letter matches\n")


def test_dlq_usage_and_unreachable(tmp_path):
    (tmp_path / "operator.password").write_text("unused")
    common = ["--credentials-dir", str(tmp_path)]
    assert dlq.main(["browse", "--address", "127.0.0.1:1", *common]) == 2
    assert dlq.main(["list", "--limit", "0", "--address", "127.0.0.1:1", *common]) == 2
    assert dlq.main(["list", "--address", "127.0.0.1:1", "--credentials-dir", "/nowhere"]) == 2
    assert dlq.main(["list", "--address", "127.0.0.1:1", *common]) == 3
