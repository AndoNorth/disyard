"""The workflow worker builder: `place` is a plain function, and the builder runs it on the
process's executor without the caller passing one."""

import asyncio
import inspect
import threading
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime

import pytest
from dsy_events import build_event
from dsy_events.models import JobParams
from dsy_jobdoc.plan_models import Plan
from dsy_keys import workflow_id
from dsy_workflow import versioning
from dsy_workflow.contract import naming
from dsy_workflow.job import JobWorkflow
from dsy_workflow.placement import StaticPlacer, place_activity
from dsy_workflow.taskpath import control_queue
from dsy_workflow.testing import FakeControl, FakePool, Succeed, connect
from dsy_workflow.testing.control import LoadedPlan
from dsy_workflow.worker import THREAD_PREFIX, workflow_worker
from temporalio.worker import Worker

BUILD = "worker-test"
LIMIT_S = 120
HASH = "a" * 64


def test_place_is_not_a_coroutine():
    assert not inspect.iscoroutinefunction(place_activity(StaticPlacer()))


class ThreadRecorder(StaticPlacer):
    """StaticPlacer that records the thread of each call."""

    def __init__(self):
        super().__init__()
        self.threads: list[threading.Thread] = []

    def place(self, request, escalate=False):
        self.threads.append(threading.current_thread())
        return super().place(request, escalate)


def one_compile() -> Plan:
    return Plan.model_validate(
        {
            "plan_version": 1,
            "profile": "ci",
            "max_parallel": 1,
            "parameters": {},
            "tasks": [
                {
                    "id": "build",
                    "type": "compile",
                    "pool": "compile",
                    "depends_on": [],
                    "inputs": [],
                    "cpu_milli": 1000,
                    "memory_mib": 1024,
                    "run_timeout_s": 600,
                    "retries": 1,
                    "allow_partial": False,
                    "checkpoint": False,
                    "params": {
                        "source": {
                            "uri": f"s3://dsy-sources/sha256/aa/{HASH}",
                            "sha256": HASH,
                            "size": 2048,
                            "name": "bundle.tar.gz",
                            "kind": "source",
                        },
                        "entry": "sim",
                    },
                }
            ],
        }
    )


@pytest.mark.integration
def test_workflow_worker_owns_the_executor(temporal, monkeypatch):
    """A worker built with no executor argument runs a job whose `place` runs on a thread of the
    builder's executor, off the event loop's thread."""
    monkeypatch.setenv(naming().workflow.build_id_env, BUILD)
    placer = ThreadRecorder()

    async def body() -> str:
        client = await connect(temporal)
        control = FakeControl()
        control.status = "admitted"
        sha256 = uuid.uuid4().hex * 2
        accepted = build_event(
            "job.accepted",
            source="/dsy/dispatch",
            tenant="acme",
            jobid=str(uuid.uuid4()),
            priorityclass="standard",
            jobclass="simulate",
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            time=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
            data={
                "manifest": {
                    "uri": f"s3://dsy-sources/sha256/{sha256[:2]}/{sha256}",
                    "sha256": sha256,
                    "size": 2048,
                    "name": "manifest.json",
                    "kind": "manifest",
                },
                "batch_id": str(uuid.uuid4()),
                "submission_key": f"sub-{uuid.uuid4()}",
            },
        )
        control.plans[accepted.data["manifest"]["uri"]] = LoadedPlan(
            plan=one_compile(), max_history_events=10_000
        )
        workers = [
            workflow_worker(client, BUILD, placer, control.emit),
            Worker(client, task_queue=control_queue(), activities=control.control_activities()),
            *(FakePool(client, pool, lambda task: Succeed()) for pool in naming().pools),
        ]
        async with AsyncExitStack() as stack:
            for worker in workers:
                await stack.enter_async_context(worker)
            await versioning.set_current(client, BUILD)
            handle = await client.start_workflow(
                JobWorkflow.run,
                args=[accepted, JobParams(seed=7, parameters={})],
                id=workflow_id(accepted.jobid),
                task_queue=naming().workflow.queue,
            )
            return await handle.result()

    assert asyncio.run(asyncio.wait_for(body(), LIMIT_S)) == "succeeded"
    assert placer.threads
    assert all(t.name.startswith(THREAD_PREFIX) for t in placer.threads), placer.threads
    assert threading.main_thread() not in placer.threads
