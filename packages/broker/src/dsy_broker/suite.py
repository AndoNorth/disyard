"""The regression suite of the broker: the S4 checks with the golden events of the envelope as
payloads and its poison events as the malformed cases, and the commands that stand in for the
publisher and the consumer, against any broker that runs the topology of the broker contract.

Usage: python -m dsy_broker.suite --address HOST:PORT --metrics URL --credentials-dir DIR COMMAND

  check              runs every check, prints each failed assertion and the assertion count, and
                     refuses to start when the intake queue or intake.dlq holds a message or has a
                     consumer
  fill --count N     publishes N golden job.accepted events
  drain --count N    pulls up to N deliveries, prints each one with its delivery count, and acks it
  count              prints ready and unacked of each queue from the metrics endpoint
  deadletter         dead-letters one message for each reason that a consumer can cause

`--metrics` is the detailed endpoint, `--credentials-dir` holds `<user>.password` of each broker
user, and the intake queue is the one of the tenant and priority class of the golden event.
Exit 0 done, 1 an assertion failed, a check refused to start or a command fell short, 2 usage,
3 the broker or its metrics endpoint could not be reached.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from functools import cache
from pathlib import Path

import dsy_events
from dsy_compat import read_event, upgrade_for_replay
from dsy_compat.vectors import GOLDEN, POISON

from .client import Connection, Credentials, Delivery, connect
from .contract import topology
from .models import BrokerUnavailable, Outcome, TopologyError

ACCEPTED = "job.accepted/1.0.json"
DLQ = topology().dead_letter_queue.name
LIMIT = topology().intake_policy.definition.delivery_limit
MAX_LENGTH = topology().intake_policy.definition.max_length.value
# A routing key that no intake queue can carry, since the queue template has two dots.
UNROUTABLE = "no-such-queue"
MISSING = topology().intake_queue.template.replace("<tenant>", "no-such").replace("<class>", "x")
# A counted retry comes back after at most the longest delayed retry.
TIMEOUT_S = topology().intake_policy.definition.delayed_retry_max / 1000 + 5
# The detailed endpoint lags the queues by several seconds, so a count is polled.
METRICS_TIMEOUT_S = 60
SERIES = {
    "rabbitmq_detailed_queue_messages_ready": "ready",
    "rabbitmq_detailed_queue_messages_unacked": "unacked",
    "rabbitmq_detailed_queue_consumers": "consumers",
}
SAMPLE = re.compile(r'^(\w+)\{[^}]*\bqueue="([^"]*)"[^}]*\} (\S+)')


@cache
def golden() -> dict[str, bytes]:
    """The golden event of each type and minor, keyed by its path under `golden/`."""
    return {
        p.relative_to(GOLDEN).as_posix(): p.read_bytes() for p in sorted(GOLDEN.glob("*/*.json"))
    }


@cache
def poison() -> dict[str, tuple[bytes, dict[str, str]]]:
    """Each poison event with the typed error that reading it raises."""
    return {
        p.stem: (p.read_bytes(), json.loads(p.with_suffix(".expect.json").read_text()))
        for p in sorted(POISON.glob("*.json"))
        if not p.name.endswith(".expect.json")
    }


def intake_queue() -> str:
    """The intake queue of the tenant and priority class of the golden job.accepted event."""
    event = json.loads(golden()[ACCEPTED])
    template = topology().intake_queue.template
    return template.replace("<tenant>", event["tenant"]).replace("<class>", event["priorityclass"])


def credentials(directory: Path) -> dict[str, Credentials]:
    """The credentials of each broker user, from `<user>.password` in `directory`."""
    return {
        user: Credentials(user, (directory / f"{user}.password").read_text().strip())
        for user in topology().users.model_dump()
    }


def queue_metrics(url: str) -> dict[str, dict[str, int]]:
    """Ready, unacked and consumers of each queue, read once from the detailed endpoint."""
    query = urllib.parse.urlencode(
        [("family", "queue_coarse_metrics"), ("family", "queue_consumer_count")]
    )
    with urllib.request.urlopen(f"{url}?{query}", timeout=10) as response:
        return parse_metrics(response.read().decode())


def parse_metrics(text: str) -> dict[str, dict[str, int]]:
    """Ready, unacked and consumers of each queue in a Prometheus text exposition."""
    queues: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        if (match := SAMPLE.match(line)) and match[1] in SERIES:
            counts = queues.setdefault(match[2], dict.fromkeys(SERIES.values(), 0))
            counts[SERIES[match[1]]] = int(float(match[3]))
    return queues


def wait_counts(url: str, queue: str, done: Callable[[dict[str, int]], bool]) -> dict[str, int]:
    """Polls the detailed endpoint until the counts of `queue` satisfy `done`, or for at most
    METRICS_TIMEOUT_S, and returns the last counts read, a missing queue counting zero."""
    deadline = time.monotonic() + METRICS_TIMEOUT_S
    while True:
        counts = queue_metrics(url).get(queue, dict.fromkeys(SERIES.values(), 0))
        if done(counts) or time.monotonic() >= deadline:
            return counts
        time.sleep(0.5)


def death_annotations(annotations: dict) -> list[str]:
    """The annotations that a dead letter carries and a replayed message must not."""
    return sorted(k for k in annotations if "death" in k or k.startswith("x-opt-dsy"))


class Run:
    """One connection per broker user, the intake consumer and the dead-letter consumer, and the
    tally of the assertions that the checks make through them."""

    def __init__(self, address: str, users: dict[str, Credentials], metrics: str):
        self.address = address
        self.users = users
        self.metrics = metrics
        self.queue = intake_queue()
        self.passed = 0
        self.failed: list[str] = []
        self._opened: list[Connection] = []
        self.dispatch = self.open(users["dispatch"])
        self.admission = self.open(users["admission"])
        self.operator = self.open(users["operator"])
        self.intake = self.admission.consumer(self.queue)
        self.dlq = self.operator.consumer(DLQ)

    def open(self, user: Credentials) -> Connection:
        connection = connect(self.address, user)
        self._opened.append(connection)
        return connection

    def check(self, ok: bool, what: str) -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed.append(what)
            print(f"FAIL {what}", flush=True)
        return ok

    def one(self, consumer, what: str) -> Delivery | None:
        got = consumer.pull(1, TIMEOUT_S)
        return got[0] if self.check(len(got) == 1, what) else None

    def publish(self, body: bytes, what: str) -> bool:
        return self.check(self.dispatch.publish(self.queue, body) is Outcome.ACCEPTED, what)

    def replay(self, dead: Delivery) -> Outcome:
        """Republishes a dead letter to its first-death queue, as `dlq replay` does, and settles
        the dead-letter copy only once the publish is accepted."""
        queue = dead.annotations["x-first-death-queue"]
        outcome = self.operator.publish(queue, upgrade_for_replay(dead.body))
        if outcome is Outcome.ACCEPTED:
            dead.ack()
        return outcome

    def through_replay(self, body: bytes, what: str) -> tuple[Delivery, Delivery] | None:
        """Publishes body, dead-letters it, replays it, and returns the dead letter, settled, and
        the replayed delivery, held."""
        self.publish(body, f"{what} published")
        if not (delivery := self.one(self.intake, f"{what} delivered")):
            return None
        delivery.dead_letter("ADMISSION_FAILED", "no-row: regression suite replay")
        if not (dead := self.one(self.dlq, f"{what} dead-lettered")):
            return None
        self.check(self.replay(dead) is Outcome.ACCEPTED, f"{what} replay accepted")
        if not (replayed := self.one(self.intake, f"{what} replayed")):
            return None
        return dead, replayed

    def close(self) -> None:
        for connection in self._opened:
            connection.close()


# The S4 checks, each a function of a Run, run in this order by `check`.


def publish_outcomes(run: Run) -> None:
    body = golden()[ACCEPTED]
    run.publish(body, "1 routable publish accepted")
    outcome = run.dispatch.publish(UNROUTABLE, body)
    run.check(outcome is Outcome.UNROUTABLE, "1 unknown routing key unroutable")
    if delivery := run.one(run.intake, "1 accepted publish delivered"):
        delivery.ack()
    # A refused attach leaves its connection usable, which a connection of its own shows apart.
    probe = run.open(run.users["admission"])
    try:
        probe.consumer(MISSING)
        run.check(False, "1 missing queue is a TopologyError")
    except TopologyError:
        run.check(True, "1 missing queue is a TopologyError")
    run.check(probe.consumer(run.queue).drain(1, TIMEOUT_S) == [], "1 connection usable after it")


def full_queue_rejects(run: Run) -> None:
    body = golden()[ACCEPTED]
    accepted = 0
    while (outcome := run.dispatch.publish(run.queue, body)) is Outcome.ACCEPTED:
        accepted += 1
        if accepted > MAX_LENGTH + 1:
            break
    run.check(outcome is Outcome.REJECTED, "1 full queue rejects a publish")
    # RabbitMQ takes one message over max-length before it rejects.
    run.check(MAX_LENGTH <= accepted <= MAX_LENGTH + 1, f"1 full queue held {accepted}")
    drained = 0
    while got := run.intake.drain(100, TIMEOUT_S):
        drained += len(got)
        for delivery in got:
            delivery.ack()
    run.check(drained == accepted, "1 full queue drains what it accepted")


def one_message_per_credit(run: Run) -> None:
    for _ in range(3):
        run.publish(golden()[ACCEPTED], "2 publish accepted")
    one = run.intake.pull(1, TIMEOUT_S)
    run.check(len(one) == 1, "2 credit 1 gives 1")
    counts = wait_counts(run.metrics, run.queue, lambda c: (c["ready"], c["unacked"]) == (2, 1))
    run.check((counts["ready"], counts["unacked"]) == (2, 1), "2 broker ready 2 unacked 1")
    two = run.intake.pull(2, TIMEOUT_S)
    run.check(len(two) == 2, "2 credit 2 gives 2")
    for delivery in one + two:
        delivery.ack()
    started = time.monotonic()
    run.check(run.intake.drain(5, TIMEOUT_S) == [], "2 drain on empty returns nothing")
    run.check(time.monotonic() - started < 1, "2 drain on empty answers within 1 s")


def retry_counts(run: Run) -> None:
    for settle, expected in (("retry", [0, 1, 2, 3]), ("release", [0, 0, 0, 0])):
        run.publish(golden()[ACCEPTED], f"3 publish for {settle}")
        counts = []
        for _ in expected:
            if not (delivery := run.one(run.intake, f"3 delivered for {settle}")):
                return
            counts.append(delivery.delivery_count)
            getattr(delivery, settle)()
        run.check(counts == expected, f"3 {settle} counts {counts}")
        if delivery := run.one(run.intake, f"3 delivered after {settle}"):
            delivery.ack()


def dead_letter_on_sixth_delivery(run: Run) -> None:
    body = golden()[ACCEPTED]
    run.publish(body, "4 publish accepted")
    counts, last = [], []
    for _ in range(LIMIT + 1):
        if not (delivery := run.one(run.intake, "4 delivered")):
            return
        counts.append(delivery.delivery_count)
        last.append(delivery.last)
        delivery.retry()
    run.check(counts == list(range(LIMIT + 1)), f"4 six deliveries counted {counts}")
    run.check(last == [False] * LIMIT + [True], "4 only the sixth delivery is the last")
    run.check(run.intake.drain(1, TIMEOUT_S) == [], "4 no seventh delivery")
    if dead := run.one(run.dlq, "4 sixth failure dead-lettered"):
        reason = dead.annotations.get("x-last-death-reason")
        run.check(reason == "delivery_limit", f"4 broker reason {reason}")
        run.check("x-opt-dsy-reason" not in dead.annotations, "4 no application reason")
        run.check(dead.body == body, "4 dead letter body identical")
        dead.ack()
    # The reason and the detail travel in the same settle.
    run.publish(body, "4 publish accepted")
    if delivery := run.one(run.intake, "4 delivered"):
        delivery.dead_letter("ADMISSION_FAILED", "no-row: regression suite")
    if dead := run.one(run.dlq, "4 dead_letter moves the message at once"):
        expected = {
            "x-opt-dsy-reason": "ADMISSION_FAILED",
            "x-opt-dsy-detail": "no-row: regression suite",
            "x-last-death-reason": "rejected",
            "x-first-death-queue": run.queue,
        }
        got = {key: dead.annotations.get(key) for key in expected}
        run.check(got == expected, f"4 dead letter annotations {got}")
        dead.ack()


def poison_vectors_dead_letter_malformed(run: Run) -> None:
    for name, (body, expect) in poison().items():
        code = getattr(dsy_events, expect["error"]).dlq_reason
        run.publish(body, f"4 poison {name} published")
        if not (delivery := run.one(run.intake, f"4 poison {name} delivered")):
            continue
        try:
            read_event(delivery.body)
        except dsy_events.EventError as error:
            run.check(error.reason == expect["reason"], f"4 poison {name} raised {error.reason}")
            delivery.dead_letter(error.dlq_reason, f"{error.reason}: {error.detail}"[:1024])
        else:
            run.check(False, f"4 poison {name} raised nothing")
            delivery.ack()
            continue
        if dead := run.one(run.dlq, f"4 poison {name} dead-lettered"):
            reason = dead.annotations.get("x-opt-dsy-reason")
            detail = dead.annotations.get("x-opt-dsy-detail", "")
            run.check(reason == code, f"4 poison {name} dead-lettered as {reason}")
            run.check(detail.startswith(f"{expect['reason']}: "), f"4 poison {name} detail token")
            run.check(dead.body == body, f"4 poison {name} body identical")
            dead.ack()


def golden_vectors_round_trip(run: Run) -> None:
    for path, body in golden().items():
        run.publish(body, f"5 {path} published")
        if delivery := run.one(run.intake, f"5 {path} delivered"):
            run.check(delivery.body == body, f"5 {path} identical at intake")
            delivery.ack()


def replay_byte_identical(run: Run) -> None:
    for path, body in golden().items():
        if got := run.through_replay(body, f"5 {path}"):
            dead, replayed = got
            run.check(dead.body == body, f"5 {path} identical in the dead-letter queue")
            run.check(replayed.body == body, f"5 {path} identical after replay")
            replayed.ack()


def replay_strips_death_annotations(run: Run) -> None:
    if got := run.through_replay(golden()[ACCEPTED], "5 replay"):
        dead, replayed = got
        stale = death_annotations(replayed.annotations)
        run.check(stale == [], f"5 no death or dsy annotation after replay {stale}")
        run.check(dead.annotations.get("x-first-death-queue") == run.queue, "5 first-death queue")
        run.check(replayed.delivery_count == 0, "5 a replay is a new message")
        replayed.ack()


def negative_control_fails(run: Run) -> None:
    """The checks of a replay fail on what they must refuse: the annotations of the dead letter
    itself, and a body with one byte changed."""
    body = golden()[ACCEPTED]
    if got := run.through_replay(body, "5 negative control"):
        dead, replayed = got
        replayed.ack()
        stale = death_annotations(dead.annotations)
        run.check(stale != [], f"5 negative control annotation check fails on {stale}")
        flipped = bytearray(dead.body)
        flipped[-2] ^= 1
        run.check(bytes(flipped) != body, "5 negative control byte check fails on a changed byte")


CHECKS: list[Callable[[Run], None]] = [
    publish_outcomes,
    full_queue_rejects,
    one_message_per_credit,
    retry_counts,
    dead_letter_on_sixth_delivery,
    poison_vectors_dead_letter_malformed,
    golden_vectors_round_trip,
    replay_byte_identical,
    replay_strips_death_annotations,
    negative_control_fails,
]


def check(args, users: dict[str, Credentials]) -> int:
    queue = intake_queue()
    metrics = queue_metrics(args.metrics)
    busy = {q: metrics[q] for q in (queue, DLQ) if any(metrics.get(q, {}).values())}
    if busy:
        print(f"refused: a queue holds a message or has a consumer: {busy}", file=sys.stderr)
        return 1
    run = Run(args.address, users, args.metrics)
    try:
        for each in CHECKS:
            try:
                each(run)
            except Exception as error:
                run.check(False, f"{each.__name__} raised {type(error).__name__}: {error}")
    finally:
        run.close()
    for name in (queue, DLQ):
        counts = wait_counts(args.metrics, name, lambda c: not any(c.values()))
        run.check(not any(counts.values()), f"{name} left empty with no consumer {counts}")
    print(f"assertions: {run.passed}/{run.passed + len(run.failed)} passed")
    return 1 if run.failed else 0


def fill(args, users: dict[str, Credentials]) -> int:
    connection, queue = connect(args.address, users["dispatch"]), intake_queue()
    try:
        outcomes = [connection.publish(queue, golden()[ACCEPTED]) for _ in range(args.count)]
    finally:
        connection.close()
    accepted = outcomes.count(Outcome.ACCEPTED)
    print(f"published {accepted} of {args.count} to {queue}")
    return 0 if accepted == args.count else 1


def drain(args, users: dict[str, Credentials]) -> int:
    connection = connect(args.address, users["admission"])
    names = {body: path for path, body in golden().items()}
    got = 0
    try:
        consumer = connection.consumer(intake_queue())
        while got < args.count and (batch := consumer.drain(min(args.count - got, 100), TIMEOUT_S)):
            for delivery in batch:
                got += 1
                golden_name = names.get(delivery.body, "-")
                print(
                    f"{got} delivery_count={delivery.delivery_count} "
                    f"bytes={len(delivery.body)} golden={golden_name}"
                )
                delivery.ack()
    finally:
        connection.close()
    print(f"drained {got} of {args.count}")
    return 0 if got == args.count else 1


def count(args, users: dict[str, Credentials]) -> int:
    for queue, counts in sorted(queue_metrics(args.metrics).items()):
        print(f"{queue} ready={counts['ready']} unacked={counts['unacked']}")
    return 0


def deadletter(args, users: dict[str, Credentials]) -> int:
    """Dead-letters MALFORMED from a poison event, UNSUPPORTED_VERSION from a previous major,
    ADMISSION_FAILED from a last delivery and delivery_limit from six counted retries, and waits
    until the dead-letter queue holds all four."""
    before = wait_counts(args.metrics, DLQ, lambda c: True)["ready"]
    run = Run(args.address, users, args.metrics)
    try:
        for name in ("not-json", "wrong-major"):
            run.publish(poison()[name][0], f"{name} published")
            if delivery := run.one(run.intake, f"{name} delivered"):
                try:
                    read_event(delivery.body)
                    run.check(False, f"{name} raised nothing")
                    delivery.ack()
                except dsy_events.EventError as error:
                    detail = f"{error.reason}: {error.detail}"[:1024]
                    delivery.dead_letter(error.dlq_reason, detail)
                    print(f"{error.dlq_reason} {detail}")
        for last_settle in ("dead_letter", "retry"):
            run.publish(golden()[ACCEPTED], f"{last_settle} published")
            for _ in range(LIMIT + 1):
                if not (delivery := run.one(run.intake, f"{last_settle} delivered")):
                    break
                if not delivery.last:
                    delivery.retry()
                elif last_settle == "retry":
                    delivery.retry()
                    print("delivery_limit")
                else:
                    delivery.dead_letter("ADMISSION_FAILED", "no-row: regression suite")
                    print("ADMISSION_FAILED no-row: regression suite")
    finally:
        run.close()
    want = before + 4
    held = wait_counts(args.metrics, DLQ, lambda c: c["ready"] >= want)["ready"]
    run.check(held >= want, f"{DLQ} holds {held}, expected at least {want}")
    return 1 if run.failed else 0


COMMANDS = {"check": check, "fill": fill, "drain": drain, "count": count, "deadletter": deadletter}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dsy_broker.suite", exit_on_error=False)
    parser.add_argument("--address", required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--credentials-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("fill").add_argument("--count", required=True, type=int)
    commands.add_parser("drain").add_argument("--count", required=True, type=int)
    commands.add_parser("count")
    commands.add_parser("deadletter")
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return 2
    try:
        users = credentials(args.credentials_dir)
    except OSError as error:
        print(f"usage: {error}", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](args, users)
    except (BrokerUnavailable, OSError) as error:
        print(f"unreachable: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
