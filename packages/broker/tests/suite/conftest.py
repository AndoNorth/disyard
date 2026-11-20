"""Fixtures of the suite tests over a `broker` fixture of each module: the credentials of its users,
a credentials directory as the broker module writes it, and connections that are closed and queues
that are emptied after each test."""

import pytest
from wft_broker import suite
from wft_broker.client import Credentials, connect


@pytest.fixture
def users(broker) -> dict[str, Credentials]:
    return {user: Credentials(user, password) for user, password in broker.passwords.items()}


@pytest.fixture
def credentials_dir(broker, tmp_path):
    for user, password in broker.passwords.items():
        (tmp_path / f"{user}.password").write_text(password)
    return tmp_path


@pytest.fixture
def open_as(broker, users):
    """Opens connections as a broker user, closes them after the test and empties the queues."""
    opened = []

    def open_as(user: str):
        connection = connect(broker.address, users[user])
        opened.append(connection)
        return connection

    yield open_as
    for connection in opened:
        connection.close()
    operator = connect(broker.address, users["operator"])
    for queue in (suite.intake_queue(), suite.DLQ):
        consumer = operator.consumer(queue)
        while got := consumer.drain(100, suite.TIMEOUT_S):
            for delivery in got:
                delivery.ack()
    operator.close()


@pytest.fixture
def run(broker, users, open_as):
    run = suite.Run(broker.address, users, broker.metrics)
    yield run
    run.close()
