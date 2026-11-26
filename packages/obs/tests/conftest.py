"""The `cluster` marker: a test that reads a real backend of a running stack, run only when a
`-m` expression names the marker, so the unit run never reaches for a cluster."""

import pytest
from dsy_obs.testing import FakeObs


def pytest_collection_modifyitems(config, items):
    if "cluster" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="reads a running stack, selected with -m cluster")
    for item in items:
        if "cluster" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fake():
    with FakeObs() as obs:
        yield obs
