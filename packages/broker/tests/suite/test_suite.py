"""The checks of the regression suite against a broker container, a delivery held across a
connection that the broker closes, and the suite's commands as an operator runs them."""

import re
import subprocess
import sys

import pytest
from wdf_broker import suite
from wdf_broker.models import BrokerUnavailable, Outcome
from wdf_broker.testing.container import broker_container

pytestmark = pytest.mark.integration

QUEUE = suite.intake_queue()
GOLDEN = suite.golden()[suite.ACCEPTED]


@pytest.fixture(scope="module")
def broker(t1):
    with broker_container(t1) as broker:
        yield broker


def passes(run, check) -> None:
    check(run)
    assert run.failed == []
    assert run.passed > 0


def test_golden_vectors_round_trip(run):
    passes(run, suite.golden_vectors_round_trip)


def test_replay_byte_identical(run):
    passes(run, suite.replay_byte_identical)


def test_replay_strips_death_annotations(run):
    passes(run, suite.replay_strips_death_annotations)


def test_poison_vectors_dead_letter_malformed(run):
    passes(run, suite.poison_vectors_dead_letter_malformed)


def test_full_queue_rejects(run):
    passes(run, suite.full_queue_rejects)


def test_negative_control_fails(run):
    passes(run, suite.negative_control_fails)


def close_connections(broker, user: str) -> None:
    # The JSON formatter prints a pid in a form that close_connection does not take.
    rows = broker.ctl("list_connections", "pid", "user", "--no-table-headers").splitlines()
    pids = [pid for pid, name in (row.split() for row in rows) if name == user]
    assert pids, f"no connection of {user}"
    for pid in pids:
        broker.ctl("close_connection", pid, "regression suite")


def test_held_delivery_across_connection_drop(broker, open_as):
    open_as("dispatch").publish(QUEUE, GOLDEN)
    consumer = open_as("admission").consumer(QUEUE)
    (held,) = consumer.pull(1, suite.TIMEOUT_S)
    assert held.delivery_count == 0
    close_connections(broker, "admission")
    with pytest.raises(BrokerUnavailable):
        consumer.pull(1, suite.TIMEOUT_S)
    with pytest.raises(BrokerUnavailable):
        held.ack()

    # A lost held delivery counts as a failed one, so the next pull sees its count raised by one.
    consumer = open_as("admission").consumer(QUEUE)
    (back,) = consumer.pull(1, suite.TIMEOUT_S)
    assert (back.body, back.delivery_count) == (GOLDEN, 1)

    # A lost last delivery dead-letters as delivery_limit, with no application reason.
    while not back.last:
        back.retry()
        (back,) = consumer.pull(1, suite.TIMEOUT_S)
    close_connections(broker, "admission")
    (dead,) = open_as("operator").consumer(suite.DLQ).pull(1, suite.TIMEOUT_S)
    assert dead.body == GOLDEN
    assert dead.annotations["x-last-death-reason"] == "delivery_limit"
    assert "x-opt-wdf-reason" not in dead.annotations
    dead.ack()


def quiet(broker) -> None:
    """Waits until the metrics endpoint shows the queues of the suite empty with no consumer, since
    it reports the test before late."""
    for queue in (QUEUE, suite.DLQ):
        counts = suite.wait_counts(broker.metrics, queue, lambda c: not any(c.values()))
        assert not any(counts.values()), f"{queue} is not quiet: {counts}"


def suite_args(broker, credentials_dir) -> list[str]:
    return [
        "--address",
        broker.address,
        "--metrics",
        broker.metrics,
        "--credentials-dir",
        str(credentials_dir),
    ]


def test_check_command_exits_zero(broker, credentials_dir):
    quiet(broker)
    run = subprocess.run(
        [sys.executable, "-m", "wdf_broker.suite", *suite_args(broker, credentials_dir), "check"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    print(run.stdout, run.stderr, sep="")
    assert run.returncode == 0
    assert re.search(r"^assertions: (\d+)/\1 passed$", run.stdout, re.MULTILINE)


def test_check_refuses_a_busy_queue(broker, open_as, credentials_dir, capsys):
    quiet(broker)
    assert open_as("dispatch").publish(QUEUE, GOLDEN) is Outcome.ACCEPTED
    assert suite.wait_counts(broker.metrics, QUEUE, lambda c: c["ready"] == 1)["ready"] == 1
    assert suite.main([*suite_args(broker, credentials_dir), "check"]) == 1
    assert "refused" in capsys.readouterr().err


def reason(annotations: dict) -> str:
    return annotations.get("x-opt-wdf-reason") or annotations["x-last-death-reason"]


def test_tool_commands(broker, open_as, credentials_dir, capsys):
    args = suite_args(broker, credentials_dir)
    quiet(broker)

    assert suite.main([*args, "fill", "--count", "3"]) == 0
    assert capsys.readouterr().out == f"published 3 of 3 to {QUEUE}\n"
    assert suite.wait_counts(broker.metrics, QUEUE, lambda c: c["ready"] == 3)["ready"] == 3
    assert suite.main([*args, "count"]) == 0
    assert f"{QUEUE} ready=3 unacked=0\n" in capsys.readouterr().out

    assert suite.main([*args, "drain", "--count", "3"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        *(f"{n} delivery_count=0 bytes={len(GOLDEN)} golden={suite.ACCEPTED}" for n in (1, 2, 3)),
        "drained 3 of 3",
    ]

    assert suite.main([*args, "deadletter"]) == 0
    printed = [line.split()[0] for line in capsys.readouterr().out.splitlines()]
    expected = ["MALFORMED", "UNSUPPORTED_VERSION", "ADMISSION_FAILED", "delivery_limit"]
    assert printed == expected
    dead = open_as("operator").consumer(suite.DLQ).pull(4, suite.TIMEOUT_S)
    assert sorted(reason(d.annotations) for d in dead) == sorted(expected)
    for delivery in dead:
        delivery.ack()
