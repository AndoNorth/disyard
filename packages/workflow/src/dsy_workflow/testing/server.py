"""The Temporal dev server of a test and the client that reaches it."""

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager

from dsy_temporal import event_data_converter, tracing_interceptor
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.client import Client

from ..contract import naming

ATTRIBUTE_TYPES = {
    "keyword": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "datetime": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
}

# The build of a test run that sets none, which the workflow reads from the environment.
TEST_BUILD = "test-build"


def build_id() -> str:
    """The build id of the process, `DSY_BUILD_ID` when it is set and a fixed one when it is not,
    which is then set so the workflow code reads the same one.

    Call it before the first import of `dsy_workflow.job`."""
    return os.environ.setdefault(naming().workflow.build_id_env, TEST_BUILD)


@contextmanager
def dev_server(
    t1, *, namespace: str | None = None, db_filename: str | None = None, search_attributes=False
) -> Iterator[str]:
    """Starts a dev server in a container of the T1 session and yields its address, the container
    being gone afterwards.

    The namespace is that of naming.yaml, or none for the server's own when `namespace` is "".
    `db_filename` keeps the server's state in that file of the container, which survives a restart
    of the server where memory does not. `search_attributes` registers those of naming.yaml.
    The fixture that wraps it chooses the scope, one server per module or per test.
    """
    space = naming().namespace if namespace is None else namespace
    command = ["server", "start-dev", "--ip", "0.0.0.0", "--headless"]
    if space:
        command += ["--namespace", space]
    if db_filename:
        command += ["--db-filename", db_filename]
    with t1.container("temporalio/temporal", ports={0: 7233}, command=tuple(command)) as container:
        container.wait_ready(0)
        address = f"127.0.0.1:{container.host_ports[0]}"
        # The port opens before gRPC answers, and a client that connects in between is reset.
        asyncio.run(connect(address, namespace=space or "default"))
        if search_attributes:
            asyncio.run(register_search_attributes(address, space))
        yield address


async def register_search_attributes(address: str, namespace: str | None = None) -> None:
    """Registers the search attributes of naming.yaml on the server at `address`."""
    space = naming().namespace if namespace is None else namespace
    server = await connect(address, namespace=space)
    await server.operator_service.add_search_attributes(
        AddSearchAttributesRequest(
            namespace=space,
            search_attributes={
                name: ATTRIBUTE_TYPES[kind] for name, kind in naming().search_attributes.items()
            },
        )
    )


async def connect(address: str, attempts: int = 120, **options) -> Client:
    """A client of the namespace of naming.yaml with the payload converter of dsy_temporal, once
    the server answers. `options` replace the defaults of `Client.connect`."""
    options = {
        "namespace": naming().namespace,
        "data_converter": event_data_converter,
        **options,
    }
    for _ in range(attempts - 1):
        try:
            return await Client.connect(address, **options)
        except RuntimeError:
            await asyncio.sleep(0.5)
    return await Client.connect(address, **options)


async def client(address: str, attempts: int = 120, **options) -> Client:
    """`connect` with the tracing interceptor of dsy_temporal, as the services carry it."""
    return await connect(address, attempts, **{"interceptors": [tracing_interceptor()], **options})
