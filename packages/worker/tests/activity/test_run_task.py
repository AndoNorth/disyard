"""The activity on a dev server, run once as the job workflow runs it, with a scripted task path
that starts its child through the runner: heartbeats while the child is silent, a cancel that
stops the child group and withdraws the attempt, and the failure of an error of the worker."""

import asyncio
import contextlib
import time
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from wft_worker.activity import run_task
from wft_worker.runner import Child
from wft_workflow.contract import check_failure
from wft_workflow.models import REASON_MAX, HeartbeatDetails, RaisedFailure
from wft_workflow.testing import connect
from wft_workflow.testing.pool import RunTaskOnce, run_task_once

pytestmark = pytest.mark.integration

PREFIX = "worker_shutdown: worker-error: RuntimeError: "
BEAT = 'HEARTBEAT {"progress":0.5,"step":1,"steps":2}'


def run(body):
    return asyncio.run(asyncio.wait_for(body, 120))


@contextlib.asynccontextmanager
async def served(address: str):
    """A client, and the activity served on a queue of its own."""
    client = await connect(address)
    queue = f"wft-simulate-{uuid.uuid4()}"
    async with Worker(client, task_queue=queue, activities=[run_task], max_concurrent_activities=1):
        yield client, queue


@contextlib.asynccontextmanager
async def started(client: Client, queue: str, task, heartbeat_s: int, cancel: bool = False):
    """The workflow of run_task_once started on its own queue, for a test that acts while the
    activity runs."""
    once = f"wft-run-task-once-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=once,
        workflows=[RunTaskOnce],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        yield await client.start_workflow(
            RunTaskOnce.run,
            args=[queue, task, cancel, heartbeat_s, 300],
            id=f"run-task-once-{uuid.uuid4()}",
            task_queue=once,
        )


def alive(group: int) -> list[int]:
    """The processes of a process group that have not exited."""
    found = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            state, _, pgrp = stat.read_text().rsplit(")", 1)[1].split()[:3]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(pgrp) == group and state not in ("Z", "X"):
            found.append(int(stat.parent.name))
    return found


def test_heartbeats_during_silent_child(temporal, context, task, path, succeed):
    """A child that prints nothing for 20 s outlives four 5 s heartbeat timeouts."""

    async def silent(task, context):
        child = await Child.start(["sleep", "20"], context.scratch)
        assert (await child.wait()).code == 0
        return succeed(task, context)

    path(silent)

    async def body():
        async with served(temporal) as (client, queue):
            return await run_task_once(client, queue, task, heartbeat_s=5)

    outcome = run(body())
    assert "result" in outcome, outcome


def test_cancel_stops_child_and_withdraws(temporal, context, task, path):
    """A cancel stops the child and its grandchild, and the attempt ends as a cancel, with no
    failure class, once the group is gone."""
    groups = []
    running = asyncio.Event()

    async def runs(task, context):
        child = await Child.start(["sh", "-c", "sleep 600 & sleep 600"], context.scratch)
        groups.append(child._process.pid)
        running.set()
        try:
            await child.wait()
        except asyncio.CancelledError:
            await child.stop()
            raise

    path(runs)

    async def body():
        async with (
            served(temporal) as (client, queue),
            started(client, queue, task, heartbeat_s=5, cancel=True) as handle,
        ):
            await running.wait()
            asked = time.monotonic()
            await handle.signal(RunTaskOnce.cancel)
            outcome = await handle.result()
            print(f"cancel latency: {time.monotonic() - asked:.2f} s")
            return outcome

    outcome = run(body())
    assert outcome["cancelled"] is True
    assert outcome.get("error", "").startswith("CancelledError"), outcome
    [group] = groups
    assert alive(group) == []


def test_worker_error_is_worker_shutdown(temporal, context, task, path):
    """An exception of the worker with a message of 10,000 characters fails the attempt as
    worker_shutdown, retried on the infrastructure budget, with a reason within its bound and
    the traceback as the log tail."""

    async def breaks(task, context):
        raise RuntimeError("x" * 10_000)

    path(breaks)

    async def body():
        async with served(temporal) as (client, queue):
            return await run_task_once(client, queue, task)

    outcome = run(body())
    failure = RaisedFailure.model_validate(
        {"type": outcome["type"], "details": outcome["details"][0]}
    )
    check_failure(failure)
    details = failure.details
    assert failure.type == "worker_shutdown"
    assert details.deterministic is False
    assert len(details.reason) <= REASON_MAX
    assert details.reason.startswith(PREFIX + "xxx")
    tail = context.store.puts[details.log_tail.uri.split("/", 3)[3]]
    assert b"RuntimeError" in tail


def test_heartbeat_details_validate(temporal, context, task, path, succeed):
    """Every heartbeat is HeartbeatDetails: nulls before the child's first progress line, and that
    line's progress after it, with no checkpoint and the attempt's start."""

    async def beats(task, context):
        child = await Child.start(["sh", "-c", f"sleep 3; echo '{BEAT}'; sleep 5"], context.scratch)
        async for _ in child.lines():
            context.beat = child.beat
        await child.wait()
        return succeed(task, context)

    path(beats)

    async def body():
        seen: list[HeartbeatDetails] = []
        async with (
            served(temporal) as (client, queue),
            started(client, queue, task, heartbeat_s=3) as handle,
        ):
            converter = client.data_converter.payload_converter
            while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                description = await handle.describe()
                for pending in description.raw_description.pending_activities:
                    if pending.heartbeat_details.payloads:
                        [beat] = converter.from_payloads(
                            pending.heartbeat_details.payloads, [HeartbeatDetails]
                        )
                        seen.append(beat)
                await asyncio.sleep(0.2)
            return seen, await handle.result()

    seen, outcome = run(body())
    assert "result" in outcome, outcome
    assert {beat.progress for beat in seen} == {None, 0.5}
    assert {beat.checkpoint for beat in seen} == {None}
    assert len({beat.started_at for beat in seen}) == 1
