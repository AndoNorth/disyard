"""`broker_fixture` yields a ready broker of the profile it is given."""

import pytest
from dsy_broker.testing.fixtures import broker_fixture

pytestmark = pytest.mark.integration

broker = broker_fixture()


def test_the_fixture_yields_a_ready_broker(broker):
    assert broker.queues
    assert broker.ctl("list_queues", "name").split() >= broker.queues[:1]
