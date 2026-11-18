"""The fixtures that the API's test directories share.

Every container is held for one test module, so a module that holds two declares `pair` and no
session ever holds a third: Postgres comes from `appdb` of wdf_migrate, SeaweedFS from `shelf`
and the Temporal dev server from `temporal`. The database logins are fresh members of the four
group roles the service logs in through, the broker is wdf_broker's own FakeBroker, and Temporal
at T0 is FakeGateway.
"""

import asyncio
import time
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from wdf_broker.tenancy import load
from wdf_broker.testing import FakeBroker
from wdf_dispatchapi.testing import FakeGateway
from wdf_storecheck.fixtures import running
from wdf_workflow.contract import naming
from wdf_workflow.testing import connect

ROOT = Path(__file__).resolve().parents[3]
PROFILE = "ci"
# The group role of each service login: the submit sequence, control, index and link rows, and
# reads of event payloads.
GROUPS = {
    "dispatch": "wdf_dispatch",
    "control": "wdf_control",
    "artifacts": "wdf_artifacts",
    "reader": "wdf_reader",
}
ATTRIBUTE_TYPES = {
    "keyword": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "datetime": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
}


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture
def db(appdb):
    """The superuser, which only seeds and reads rows."""
    with psycopg.connect(appdb, autocommit=True) as connection:
        yield connection


@pytest.fixture
def logins(appdb, db) -> dict[str, str]:
    """The DSN of a fresh login in each group role of GROUPS, by its key, dropped after the
    test."""
    stamp = time.time_ns()
    users = {key: f"{key}_{stamp}" for key in GROUPS}
    for key, user in users.items():
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE {}").format(
                sql.Identifier(user), sql.Identifier(GROUPS[key])
            )
        )
    yield {key: make_conninfo(appdb, user=user, password="standin") for key, user in users.items()}
    for user in users.values():
        db.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (user,)
        )
        db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
        db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


@pytest.fixture(scope="module")
def shelf(t1, tmp_path_factory):
    """A SeaweedFS with the buckets and grants of the store contract, one per test module."""
    with running(t1, "seaweedfs", tmp_path_factory.mktemp("shelf")) as started:
        started.store().create_buckets()
        yield started


@pytest.fixture(scope="module")
def temporal(t1) -> str:
    """The address of a dev server with the namespace and search attributes of naming.yaml, one
    per test module."""
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
        address = f"127.0.0.1:{container.host_ports[0]}"

        async def register():
            client = await connect(address)
            await client.operator_service.add_search_attributes(
                AddSearchAttributesRequest(
                    namespace=naming().namespace,
                    search_attributes={
                        name: ATTRIBUTE_TYPES[kind]
                        for name, kind in naming().search_attributes.items()
                    },
                )
            )

        asyncio.run(register())
        yield address


@pytest.fixture(scope="session")
def tenancy():
    """The tenancy profile of the `ci` preset, as the service loads it."""
    return load(PROFILE)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker(PROFILE)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()
