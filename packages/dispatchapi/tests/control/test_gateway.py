"""The Temporal gateway on a dev server, over JobWorkflows whose row FakeControl holds: the
conformance test that FakeGateway passes, a closed workflow, a status query that no worker answers,
and a Temporal that does not answer at all."""

import asyncio
import time
import uuid

import pytest
from wdf_dispatchapi.gateway import TemporalGateway
from wdf_dispatchapi.ports import NoWorkflow, TemporalUnavailable
from wdf_dispatchapi.testing import gateway_contract
from wdf_workflow.models import ControlSignal
from wdf_workflow.testing import connect

LIMIT_S = 180
# Nothing listens there, so every call fails to connect.
NOWHERE = "127.0.0.1:1"
CANCEL = ControlSignal(control_seq=1, reason="sent by the test")


def answers(gateway: TemporalGateway, job: str) -> bool:
    """Whether the job's status query answers, which it does once the plan is loaded."""
    try:
        gateway.status(job)
    except TemporalUnavailable:
        return False
    return True


@pytest.mark.integration
def test_gateway_contract_holds_for_temporal(temporal, kit):
    row = kit.new_row(f"contract/{uuid.uuid4()}")

    async def body():
        client = await connect(temporal)
        async with kit.rig(client):
            await kit.start_workflow(client, row)
            with TemporalGateway(temporal) as gateway:
                await kit.until(lambda: answers(gateway, row.job_id), "status answer")
                await asyncio.to_thread(gateway_contract, gateway, row.job_id)

    asyncio.run(asyncio.wait_for(body(), LIMIT_S))


@pytest.mark.integration
def test_closed_workflow_exists_and_answers_but_takes_no_signal(temporal, kit):
    row = kit.new_row(f"closed/{uuid.uuid4()}")

    async def body():
        client = await connect(temporal)
        async with kit.rig(client):
            handle = await kit.start_workflow(client, row)
            with TemporalGateway(temporal) as gateway:
                await kit.until(lambda: answers(gateway, row.job_id), "status answer")
                await asyncio.to_thread(gateway.signal, row.job_id, "cancel", CANCEL)
                assert await handle.result() == "cancelled"
                assert await asyncio.to_thread(gateway.exists, row.job_id)
                status = await asyncio.to_thread(gateway.status, row.job_id)
                with pytest.raises(NoWorkflow):
                    await asyncio.to_thread(gateway.signal, row.job_id, "cancel", CANCEL)
        return status

    status = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert (status.job_status, status.control_seq) == ("cancelled", 1)


@pytest.mark.integration
def test_status_query_that_no_worker_answers_is_unavailable(temporal, kit):
    """A workflow on a queue that nobody polls exists and takes a signal, and its status query
    gives up at the query timeout."""
    row = kit.new_row(f"unpolled/{uuid.uuid4()}")

    async def body():
        client = await connect(temporal)
        await kit.start_workflow(client, row, task_queue=f"nobody-{uuid.uuid4()}")
        with TemporalGateway(temporal, query_timeout_s=1) as gateway:
            assert await asyncio.to_thread(gateway.exists, row.job_id)
            await asyncio.to_thread(gateway.signal, row.job_id, "pause", CANCEL)
            began = time.monotonic()
            with pytest.raises(TemporalUnavailable):
                await asyncio.to_thread(gateway.status, row.job_id)
            return time.monotonic() - began

    took = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert took < 5


def test_unreachable_temporal_is_unavailable_and_resume_is_refused_first():
    with TemporalGateway(NOWHERE, call_timeout_s=1, query_timeout_s=1) as gateway:
        job = str(uuid.uuid4())
        with pytest.raises(ValueError, match="resume"):
            gateway.signal(job, "resume", CANCEL)
        for call in (
            lambda: gateway.exists(job),
            lambda: gateway.status(job),
            lambda: gateway.signal(job, "pause", CANCEL),
        ):
            with pytest.raises(TemporalUnavailable):
                call()
