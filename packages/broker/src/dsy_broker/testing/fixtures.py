"""A factory of pytest fixtures for a T1 broker, kept out of the package's `__init__` so a T0 user
of the stub never imports pytest."""

import pytest

from .container import Broker, broker_container


def broker_fixture(profile: str = "ci", scope: str = "module"):
    """A fixture named `broker` that yields a running `Broker` of the tenancy profile, one per
    `scope`. Assign the result to `broker` in a conftest."""

    @pytest.fixture(scope=scope, name="broker")
    def broker(t1) -> Broker:
        with broker_container(t1, profile) as running:
            yield running

    return broker
