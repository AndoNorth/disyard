"""The dev server, client and Rig of dsy_workflow.testing against a dev server of the T1 session."""

import asyncio

import pytest
from dsy_workflow import testing, versioning
from dsy_workflow.contract import naming
from dsy_workflow.placement import StaticPlacer
from dsy_workflow.testing import FakeControl, dev_server
from temporalio.api.operatorservice.v1 import ListSearchAttributesRequest

pytestmark = pytest.mark.integration

LIMIT_S = 120


@pytest.fixture(scope="module")
def address(t1):
    with dev_server(t1, db_filename="/tmp/temporal.db", search_attributes=True) as address:
        yield address


def test_search_attributes_of_naming_are_registered(address):
    async def body():
        client = await testing.connect(address)
        response = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=naming().namespace)
        )
        return set(response.custom_attributes)

    assert set(naming().search_attributes) <= asyncio.run(asyncio.wait_for(body(), LIMIT_S))


def test_client_carries_the_tracing_interceptor_and_connect_does_not(address):
    async def body():
        return await testing.client(address), await testing.connect(address)

    traced, plain = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert any(type(i).__name__ == "TracingInterceptor" for i in traced.config()["interceptors"])
    assert not plain.config()["interceptors"]
    assert traced.namespace == plain.namespace == naming().namespace


def test_a_rig_runs_a_job_under_the_placer_it_was_given(address):
    build = testing.build_id()

    async def body():
        client = await testing.client(address)
        placer = StaticPlacer()
        async with testing.rig(
            client, build_id=build, placer=placer, control=FakeControl()
        ) as this:
            handle = await this.start(testing.plan(testing.FAN))
            return this, await handle.result()

    this, status = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert status == "succeeded"
    assert this.control.status == "succeeded"
    assert current_build(address) == build


def current_build(address):
    async def body():
        return await versioning.current(await testing.connect(address))

    return asyncio.run(asyncio.wait_for(body(), LIMIT_S))


def test_a_build_that_never_polls_is_never_made_current(address):
    async def body():
        await versioning.set_current(await testing.connect(address), "nobody", timeout_s=1)

    with pytest.raises(TimeoutError, match="never polled"):
        asyncio.run(asyncio.wait_for(body(), LIMIT_S))
