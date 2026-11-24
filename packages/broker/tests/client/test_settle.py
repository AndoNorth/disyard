"""No public call of the client settles `released` or `rejected` on an intake queue, and release()
is the only call that sends `modified` without delivery-failed."""

import inspect
from types import SimpleNamespace

import pytest
from dsy_broker import client
from proton import Delivery as Disposition
from proton import Message

INTAKE = "intake.acme.standard"
ARGS = {"dead_letter": ("ADMISSION_FAILED", "no-row: settle test")}


class Recorded:
    """Stands in for the proton delivery and records how a call settles it."""

    def __init__(self):
        self.local = SimpleNamespace(failed=False, undeliverable=False, annotations=None)
        self.outcome = None
        self.settled = False

    def update(self, outcome):
        self.outcome = outcome

    def settle(self):
        self.settled = True


def settle_with(kind: type[client.Delivery], call: str) -> Recorded:
    connection = SimpleNamespace(_call=lambda fn: fn())
    consumer = SimpleNamespace(_connection=connection, queue=INTAKE)
    recorded = Recorded()
    delivery = kind(consumer, recorded, Message(body=b"{}", inferred=True).encode())
    getattr(delivery, call)(*ARGS.get(call, ()))
    assert recorded.settled, f"{call} did not settle"
    return recorded


def public_calls(kind: type) -> list[str]:
    return [name for name, _ in inspect.getmembers(kind, inspect.isfunction) if name[0] != "_"]


def test_banned_outcomes_not_offered():
    calls = public_calls(client.Delivery)
    assert sorted(calls) == ["ack", "dead_letter", "release", "retry"]
    sent = {}
    for call in calls:
        recorded = settle_with(client.Delivery, call)
        assert recorded.outcome in (Disposition.ACCEPTED, Disposition.MODIFIED), call
        sent[call] = (recorded.outcome, recorded.local.failed, recorded.local.undeliverable)
    assert sent == {
        "ack": (Disposition.ACCEPTED, False, False),
        "retry": (Disposition.MODIFIED, True, False),
        "dead_letter": (Disposition.MODIFIED, True, True),
        "release": (Disposition.MODIFIED, False, False),
    }
    # `released` is offered only by a browser, which is never opened on an intake queue.
    assert public_calls(client.Browsed) == sorted([*calls, "released"])
    with pytest.raises(ValueError, match="intake queue"):
        client.Connection.browser(None, INTAKE)
