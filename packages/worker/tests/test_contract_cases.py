"""The contract cases of each pool are valid task inputs over objects the store holds, and
FakePool, scripted to succeed and to fail as the cases say a pool worker does, passes the activity
contract with them. Runs on a dev server and SeaweedFS."""

import asyncio
import uuid

import pytest
from wdf_artifactclient.client import ArtifactClient
from wdf_artifactclient.keys import split_uri
from wdf_storecheck.s3 import client as s3_client
from wdf_worker.testing import FAILS
from wdf_workflow.contract import check_task_input
from wdf_workflow.testing import Fail, FakePool, Succeed, activity_contract, connect

pytestmark = [pytest.mark.integration, pytest.mark.pair]


def stored_inputs(task) -> list[str]:
    inputs = [reference["uri"] for reference in task.request.data["inputs"]]
    if task.request.data["task_type"] == "compile":
        inputs.append(task.params.source.uri)
    return inputs


@pytest.mark.parametrize("pool", ["compile", "simulate", "aggregate"])
def test_contract_cases_pass_activity_contract_on_fake_pool(temporal, pool_store, cases, pool):
    given = cases(pool)
    assert given.interrupts is None
    admin = s3_client(pool_store.endpoint, pool_store.credentials["admin"])
    for task in (given.succeeds, given.fails):
        check_task_input(task)
        for uri in stored_inputs(task):
            bucket, key = split_uri(uri)
            admin.head_object(Bucket=bucket, Key=key)
    assert given.succeeds.batch_id == given.fails.batch_id
    assert (given.succeeds.cache_key is not None) == (pool == "compile")

    def script(task):
        return Succeed() if task.task_attempt == given.succeeds.task_attempt else Fail(FAILS[pool])

    async def body():
        client = await connect(temporal)
        store = ArtifactClient(pool_store.endpoint, pool_store.credentials[pool])
        queue = f"wdf-{pool}-{uuid.uuid4()}"
        async with FakePool(client, pool, script, store=store, queue=queue) as fake:
            await activity_contract(client, fake.queue, given)
        return fake.calls

    calls = asyncio.run(body())
    assert [task.task_attempt for task in calls] == [1, 2]
