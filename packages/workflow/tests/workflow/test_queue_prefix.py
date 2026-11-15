"""The pool queues of a job behind the prefix of its start's memo: its tasks run on FakePools of
the prefixed queues while the FakePools on the contract names take none, and a job with no memo
runs on the contract names as before."""

import asyncio

import pytest
from wdf_events.models import JobParams
from wdf_keys import workflow_id
from wdf_stages.recording import type_name
from wdf_workflow.contract import naming
from wdf_workflow.job import JobWorkflow
from wdf_workflow.taskpath import QUEUE_PREFIX_MEMO
from wdf_workflow.testing import FakePool, Succeed, connect
from wdf_workflow.testing.control import LoadedPlan

pytestmark = pytest.mark.integration

LIMIT_S = 120
PREFIX = "h-1a2b3c4d-"
# The tasks of the fan on each pool.
FAN_CALLS = {"compile": 1, "simulate": 4, "aggregate": 1}
HASH = "b" * 64
# A source of its own for the second job, so its compile does not hit the entry the first recorded.
OTHER = {
    "source": {
        "uri": f"s3://wdf-sources/sha256/bb/{HASH}",
        "sha256": HASH,
        "size": 2048,
        "name": "bundle.tar.gz",
        "kind": "source",
    },
    "entry": "sim",
}


def calls(pools: dict[str, FakePool]) -> dict[str, int]:
    return {pool: len(fake.calls) for pool, fake in pools.items()}


def test_memo_prefix_moves_every_task_to_the_prefixed_queues(temporal, rig, plan, fan, accepted):
    async def body():
        client = await connect(temporal)
        async with rig(client) as this:
            prefixed = {
                pool: FakePool(client, pool, lambda task: Succeed(), queue=PREFIX + queue)
                for pool, queue in naming().pools.items()
            }
            for fake in prefixed.values():
                await fake.__aenter__()
            try:
                event = accepted()
                this.control.plans[event.data["manifest"]["uri"]] = LoadedPlan(
                    plan=plan(fan), max_history_events=10_000
                )
                handle = await client.start_workflow(
                    JobWorkflow.run,
                    args=[event, JobParams(seed=7, parameters={})],
                    id=workflow_id(event.jobid),
                    task_queue=naming().workflow.queue,
                    memo={QUEUE_PREFIX_MEMO: PREFIX},
                )
                await handle.result()
                behind = (calls(this.pools), calls(prefixed))
                # A job started with no memo, as admission starts every job.
                plain = await this.start(plan(fan, compile=OTHER))
                await plain.result()
                after = (calls(this.pools), calls(prefixed))
            finally:
                for fake in prefixed.values():
                    await fake.__aexit__(None, None, None)
            return this, event.jobid, behind, after

    this, job_id, behind, after = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    none = dict.fromkeys(FAN_CALLS, 0)
    assert behind == (none, FAN_CALLS)
    assert after == (FAN_CALLS, FAN_CALLS)
    placed = [
        e.data["queue"]
        for e in this.control.events.values()
        if e.jobid == job_id and type_name(e.type) == "task.placed"
    ]
    assert sorted(placed) == sorted(
        PREFIX + naming().pools[pool] for pool, n in FAN_CALLS.items() for _ in range(n)
    )
