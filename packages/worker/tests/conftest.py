"""The containers and cases that the worker's tests share: a dev server per test module, the
session's one SeaweedFS, and the contract cases of each pool over it."""

import pytest
from dsy_worker.testing import contract_cases


@pytest.fixture(scope="module")
def temporal(t1) -> str:
    """The address of a dev server with the namespace of naming.yaml, one per test module, which
    is gone before the next module runs."""
    from dsy_workflow.contract import naming

    with t1.container(
        "temporalio/temporal",
        ports={0: 7233},
        command=(
            "server",
            "start-dev",
            "--ip",
            "0.0.0.0",
            "--headless",
            "--namespace",
            naming().namespace,
        ),
    ) as container:
        container.wait_ready(0)
        yield f"127.0.0.1:{container.host_ports[0]}"


@pytest.fixture(scope="module")
def pool_store(store_session):
    """The session's one SeaweedFS with the buckets of the contract, so a module that also holds a
    dev server keeps the session at two containers."""
    store_session.store().create_buckets()
    return store_session


@pytest.fixture
def cases(pool_store):
    """The contract cases of a pool, seeded into the session's store."""
    return lambda pool: contract_cases(pool, pool_store)
