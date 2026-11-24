"""The dead-letter tool of the operator: lists, shows, replays and purges the dead letters of the
intake queues.

Usage: python -m dsy_broker.dlq {list,show,replay,purge} --address HOST:PORT --credentials-dir DIR
       [--reason CODE] [--job ID] [--limit N] [--force] [--yes]
Each command makes one pass over the dead-letter queue: it takes every message the queue holds, up
to PASS_LIMIT, holds them until the pass ends, selects among the first --limit of them, settles the
selected ones, then releases the rest in the order they came, so a pass never sees a message twice,
never moves one it did not select, and leaves the queue in its order.
A pass holds the whole queue even under --limit, because the broker queues a message released on
one link behind the messages never delivered, so releasing only the head would send it to the back.
`list` prints one line per selected message, then a count per reason. `show` prints the first
selected message in full. `replay` republishes each selected message to the intake queue it first
died on, upgraded to the current major, and refuses a reason that never recovers unless --force.
`purge` removes each selected message, and only with --yes.
Exit 0 done, 1 a message was refused or a publish was not accepted, 2 usage, 3 the broker could not
be reached.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dsy_compat import CATALOGUE, Registry, upgrade_for_replay
from dsy_compat.window import DEAD_LETTER_RETENTION_DAYS
from dsy_events import EventError

from .client import Connection, Credentials, Delivery, connect, is_intake_queue
from .contract import reasons, topology
from .models import BrokerUnavailable, Outcome, PublishTimeout, TopologyError

USER = "operator"
DLQ = topology().dead_letter_queue.name
ANNOTATIONS = topology().annotations
RECOVERY = {reason.code: reason.recovery for reason in reasons().reasons}
RETENTION = timedelta(days=DEAD_LETTER_RETENTION_DAYS)
# The most messages one pass holds, and how long it waits for the broker to hand them over.
PASS_LIMIT = 10_000
PASS_TIMEOUT_S = 30
COMMANDS = ("list", "show", "replay", "purge")


class Letter:
    """One dead letter of a pass, with what the operator selects and reads it by."""

    def __init__(self, position: int, delivery: Delivery):
        self.position = position
        self.delivery = delivery
        annotations = delivery.annotations
        self.reason = annotations.get(ANNOTATIONS.reason) or annotations.get(
            "x-last-death-reason", "-"
        )
        self.detail = annotations.get(ANNOTATIONS.detail)
        self.queue = annotations.get("x-first-death-queue")
        deaths = annotations.get("x-opt-deaths")
        times = [int(death["last-time"]) for death in deaths.elements] if deaths else []
        self.died = datetime.fromtimestamp(max(times) / 1000, UTC) if times else None
        try:
            body = json.loads(delivery.body)
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        self.jobid, self.tenant, self.priorityclass = (
            body.get(key) if isinstance(body.get(key), str) else None
            for key in ("jobid", "tenant", "priorityclass")
        )

    def selected(self, args: argparse.Namespace) -> bool:
        return (
            self.position <= (args.limit or PASS_LIMIT)
            and args.reason in (None, self.reason)
            and args.job in (None, self.jobid)
        )

    def line(self) -> str:
        """Position, reason, detail token, job, tenant, class, first-death queue and death time,
        then `stale` once the dead letter is older than its retention."""
        died = self.died.strftime("%Y-%m-%dT%H:%M:%SZ") if self.died else None
        token = self.detail.split(": ", 1)[0] if self.detail else None
        fields = [self.position, self.reason, token, self.jobid, self.tenant]
        fields += [self.priorityclass, self.queue, died]
        line = " ".join("-" if field is None else str(field) for field in fields)
        stale = self.died and datetime.now(UTC) - self.died > RETENTION
        return f"{line} stale" if stale else line

    def full(self) -> str:
        lines = [
            f"position: {self.position}",
            f"reason: {self.reason}",
            f"detail: {self.detail or '-'}",
            f"jobid: {self.jobid or '-'}",
            f"tenant: {self.tenant or '-'}",
            f"priorityclass: {self.priorityclass or '-'}",
            f"first-death queue: {self.queue or '-'}",
            f"died: {self.died.isoformat() if self.died else '-'}",
            f"delivery count: {self.delivery.delivery_count}",
        ]
        lines += [f"annotation {k}: {v}" for k, v in sorted(self.delivery.annotations.items())]
        lines.append(f"body: {self.delivery.body.decode(errors='replace')}")
        return "\n".join(lines)


def replay(connection: Connection, letter: Letter, force: bool, registry: Registry) -> str | None:
    """Republishes one dead letter and acks it once the publish is accepted, and returns why it
    was refused otherwise."""
    if RECOVERY.get(letter.reason) in (None, "never") and not force:
        return f"{letter.reason} never recovers by a replay, and --force replays it anyway"
    if not letter.queue or not is_intake_queue(letter.queue):
        return f"its first-death queue {letter.queue} is not an intake queue"
    try:
        body = upgrade_for_replay(letter.delivery.body, registry)
    except EventError as error:
        return f"the body does not upgrade: {error}"
    try:
        outcome = connection.publish(letter.queue, body)
    except (PublishTimeout, TopologyError) as error:
        return f"the publish failed: {error}"
    if outcome is not Outcome.ACCEPTED:
        return f"the publish was {outcome.value}"
    letter.delivery.ack()
    return None


def one_pass(connection: Connection, args: argparse.Namespace, registry: Registry) -> int:
    held = connection.browser(DLQ).drain(PASS_LIMIT, PASS_TIMEOUT_S)
    kept, counts, status = [], Counter(), 0
    for position, delivery in enumerate(held, 1):
        letter = Letter(position, delivery)
        if not letter.selected(args) or (args.command == "show" and counts):
            kept.append(delivery)
            continue
        if args.command == "replay":
            refused = replay(connection, letter, args.force, registry)
            if refused:
                print(f"refused {letter.line()}: {refused}", file=sys.stderr)
                kept.append(delivery)
                status = 1
                continue
        elif args.command == "purge":
            delivery.ack()
        else:
            kept.append(delivery)
        print(letter.full() if args.command == "show" else letter.line())
        counts[letter.reason] += 1
    for delivery in kept:
        delivery.released()
    if args.command == "show":
        if not counts:
            print("no dead letter matches", file=sys.stderr)
    else:
        for reason, count in sorted(counts.items()):
            print(f"count {reason} {count}")
    return status


def main(argv: list[str], registry: Registry = CATALOGUE) -> int:
    parser = argparse.ArgumentParser(prog="dsy_broker.dlq", exit_on_error=False)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--address", required=True)
    parser.add_argument("--credentials-dir", required=True, type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--job")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--yes", action="store_true")
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return 2
    if args.limit is not None and args.limit < 1:
        print("usage: --limit is at least 1", file=sys.stderr)
        return 2
    if args.command == "purge" and not args.yes:
        print("usage: purge removes dead letters for good, and needs --yes", file=sys.stderr)
        return 2
    try:
        password = (args.credentials_dir / f"{USER}.password").read_text().strip()
    except OSError as error:
        print(f"usage: no password of {USER}: {error}", file=sys.stderr)
        return 2
    try:
        connection = connect(args.address, Credentials(USER, password))
    except BrokerUnavailable as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    try:
        return one_pass(connection, args, registry)
    except BrokerUnavailable as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
