"""A broker started from the rendered configuration applies the policies of the topology to every
queue and holds each user of the contract to its permissions, read back from the broker."""

import json
import uuid
from pathlib import Path

import pytest
from proton import Condition, Endpoint, Link, Message, symbol
from proton.reactor import LinkOption
from proton.utils import BlockingConnection
from wft_broker.contract import parse
from wft_broker.contract import topology as spec
from wft_broker.testing.container import broker_container

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[4]
PROFILE = ROOT / "contracts/tenancy/vectors/tenancy/valid/two-tenants.yaml"
INTAKE = "intake.acme.standard"
TIMEOUT_S = 10


@pytest.fixture(scope="module")
def broker(t1):
    """A broker whose profile has two tenants and two classes, so four intake queues."""
    with broker_container(t1, parse("tenancy", PROFILE.read_text()).profiles["ci"]) as broker:
        yield broker


def ctl_json(broker, *args: str) -> list[dict]:
    return json.loads(broker.ctl(*args, "--formatter", "json"))


def test_policies_applied(broker):
    intake = spec().intake_policy.definition
    wft_intake = {
        "delivery-limit": intake.delivery_limit,
        "max-length": intake.max_length.value,
        "overflow": intake.overflow,
        "dead-letter-exchange": intake.dead_letter_exchange,
        "dead-letter-strategy": intake.dead_letter_strategy,
        "delayed-retry-type": intake.delayed_retry_type,
        "delayed-retry-min": intake.delayed_retry_min,
        "delayed-retry-max": intake.delayed_retry_max,
    }
    rows = ctl_json(broker, "list_queues", "name", "type", "policy", "effective_policy_definition")
    queues = {row["name"]: row for row in rows}
    assert set(queues) == {
        "intake.acme.standard",
        "intake.acme.batch",
        "intake.globex-2.standard",
        "intake.globex-2.batch",
        "intake.dlq",
    }
    for name, row in queues.items():
        assert row["type"] == "quorum", name
        if name == "intake.dlq":
            assert row["policy"] == "wft-dlq"
            assert row["effective_policy_definition"] == {"delivery-limit": -1}
        else:
            assert row["policy"] == "wft-intake", name
            assert row["effective_policy_definition"] == wft_intake, name


def _connect(broker, user: str) -> BlockingConnection:
    return BlockingConnection(
        f"amqp://{broker.address}",
        user=user,
        password=broker.passwords[user],
        allowed_mechs="PLAIN",
        timeout=TIMEOUT_S,
    )


def _ended_or_attached(links: list[Link]) -> bool:
    session = links[0].session
    return bool(session.state & Endpoint.REMOTE_CLOSED) or all(
        link.state & Endpoint.REMOTE_ACTIVE for link in links
    )


def attach(broker, user: str, address: str, sender: bool) -> Condition | None:
    """Attaches one link as `user`, and returns the condition that ended its session, or None when
    the broker attached it.

    RabbitMQ refuses a permission by ending the whole session, not by detaching the link.
    """
    connection = _connect(broker, user)
    try:
        create = (
            connection.container.create_sender if sender else connection.container.create_receiver
        )
        link = create(connection.conn, address)
        connection.wait(lambda: _ended_or_attached([link]), msg=f"attaching {address}")
        return link.session.remote_condition
    finally:
        connection.close()


class ManagementPair(LinkOption):
    """One half of the link pair that the management node of RabbitMQ answers on."""

    def apply(self, link: Link) -> None:
        link.properties = {symbol("paired"): True}
        link.snd_settle_mode = Link.SND_SETTLED
        link.rcv_settle_mode = Link.RCV_FIRST
        link.source.address = link.target.address = "/management"


def declare(broker, user: str, queue: str) -> Condition | None:
    """Asks the management node of the broker, over AMQP, to declare a quorum queue as `user`, and
    returns the condition that ended the session, or None when the session stayed open."""
    connection = _connect(broker, user)
    try:
        pair = [
            create(connection.conn, name="management-link-pair", options=ManagementPair())
            for create in (connection.container.create_sender, connection.container.create_receiver)
        ]
        connection.wait(lambda: _ended_or_attached(pair), msg="attaching the management pair")
        sender, receiver = pair
        session = sender.session
        if not session.state & Endpoint.REMOTE_CLOSED:
            receiver.flow(1)
            request = Message(
                id=str(uuid.uuid4()),
                address=f"/queues/{queue}",
                subject="PUT",
                reply_to="$me",
                body={"durable": True, "arguments": {"x-queue-type": "quorum"}},
                inferred=False,
            )
            sender.send(request)
            connection.wait(
                lambda: session.state & Endpoint.REMOTE_CLOSED or receiver.current is not None,
                msg=f"declaring {queue}",
            )
        return session.remote_condition
    finally:
        connection.close()


def assert_refused(condition: Condition | None, access: str, user: str, name: str) -> None:
    assert condition is not None, f"{user} was granted {access} access to {name}"
    assert condition.name == "amqp:unauthorized-access"
    assert f"{access} access to " in condition.description
    assert f"'{name}' in vhost '/' refused for user '{user}'" in condition.description


def test_users_permissions_enforced(broker):
    users = ctl_json(broker, "list_users")
    assert sorted(user["user"] for user in users) == ["admission", "dispatch", "operator"]

    exchange, dlq, declared = "wft.intake", spec().dead_letter_queue.name, "intake.acme.declared"
    publish = f"/exchanges/{exchange}/{INTAKE}"

    assert attach(broker, "dispatch", publish, sender=True) is None
    refused = attach(broker, "dispatch", f"/queues/{INTAKE}", sender=False)
    assert_refused(refused, "read", "dispatch", INTAKE)
    refused = attach(broker, "dispatch", f"/queues/{dlq}", sender=False)
    assert_refused(refused, "read", "dispatch", dlq)

    assert attach(broker, "admission", f"/queues/{INTAKE}", sender=False) is None
    refused = attach(broker, "admission", publish, sender=True)
    assert_refused(refused, "write", "admission", exchange)
    refused = attach(broker, "admission", f"/queues/{dlq}", sender=False)
    assert_refused(refused, "read", "admission", dlq)

    assert attach(broker, "operator", f"/queues/{dlq}", sender=False) is None
    assert attach(broker, "operator", publish, sender=True) is None
    assert_refused(declare(broker, "operator", declared), "configure", "operator", declared)
    assert declared not in {q["name"] for q in ctl_json(broker, "list_queues", "name")}
