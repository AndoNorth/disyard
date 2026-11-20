"""Fixtures of the control tests that run the job workflow on a dev server: rows seeded at accepted
and claimed as admission would, a login of the workflow group, and a rig whose JobWorkflow emits
either into the job row in Postgres, through the real emit activity, or into FakeControl.

The job is one checkpoint-capable simulate task that runs until it is interrupted, so a pause
withdraws it and a cancel or terminate ends it.
"""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig
from wft_events import Event, build_event
from wft_events.models import JobParams
from wft_jobstate import JobRow, get_job, insert_accepted, transition
from wft_keys import batch_id, job_id, workflow_id
from wft_plan import Plan
from wft_temporal import new_traceparent
from wft_workflow.contract import naming
from wft_workflow.placement import StaticPlacer, place_activity
from wft_workflow.testing import FakeControl, FakePool, Interruptible
from wft_workflow.testing.control import LoadedPlan

os.environ.setdefault(naming().workflow.build_id_env, "test-build")
BUILD_ID = os.environ[naming().workflow.build_id_env]

from wft_workflow import versioning  # noqa: E402
from wft_workflow.activities.emit import Emitter  # noqa: E402
from wft_workflow.job import JobWorkflow  # noqa: E402
from wft_workflow.taskpath import control_queue  # noqa: E402

TENANT = "acme"
SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MANIFEST = {
    "uri": f"s3://wft-sources/sha256/9f/{SHA}",
    "sha256": SHA,
    "size": 2048,
    "name": "manifest.json",
    "kind": "manifest",
}
PLAN = Plan.model_validate(
    {
        "plan_version": 1,
        "profile": "ci",
        "max_parallel": 1,
        "parameters": {},
        "tasks": [
            {
                "id": "sim",
                "type": "simulate",
                "pool": "simulate",
                "depends_on": [],
                "inputs": [],
                "cpu_milli": 250,
                "memory_mib": 384,
                "run_timeout_s": 600,
                "retries": 1,
                "checkpoint": True,
                "allow_partial": False,
                "params": {"workload": "pi", "args": {"n": 1000}, "seed": 7},
            }
        ],
    }
)
WAIT_S = 60


@pytest.fixture
def workflow_dsn(appdb, db):
    """The DSN of a fresh login in `wft_workflow`, which the real emit activity writes as."""
    user = f"workflow_{time.time_ns()}"
    db.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE wft_workflow").format(
            sql.Identifier(user)
        )
    )
    yield make_conninfo(appdb, user=user, password="standin")
    db.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (user,))
    db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


@pytest.fixture
def control_conn(logins):
    """A connection of the control login, which the control routes run on."""
    with psycopg.connect(logins["control"], autocommit=True) as connection:
        yield connection


def new_row(key: str, index: int = 0) -> JobRow:
    """The row of a job of the batch `batch` under the submission key `key`."""
    return JobRow(
        job_id=job_id(TENANT, key),
        tenant=TENANT,
        submission_key=key,
        batch_id=batch_id(TENANT, "batch"),
        priority_class="standard",
        job_class="short",
        manifest_ref=MANIFEST,
        schema_version="urn:wft:schema:job.accepted:1.0",
        traceparent=new_traceparent(),
        seed=index,
        parameters={},
        fingerprint=SHA,
    )


@pytest.fixture
def seed(db) -> Callable[[int], list[JobRow]]:
    """Inserts the rows of `count` jobs at accepted and returns them, under keys of their own, so
    no workflow of an earlier test on the same server shares their job ids."""

    def insert(count: int = 1) -> list[JobRow]:
        batch_key = str(uuid.uuid4())
        rows = [new_row(f"{batch_key}/{index}", index) for index in range(count)]
        insert_accepted(db, rows)
        return rows

    return insert


def accepted_of(row: JobRow) -> Event:
    """The accepted event of the row, as admission rebuilds it to start the job."""
    return build_event(
        "job.accepted",
        source="/wft/dispatch",
        tenant=row.tenant,
        jobid=row.job_id,
        priorityclass=row.priority_class,
        jobclass=row.job_class,
        traceparent=row.traceparent,
        tracestate=row.tracestate,
        time=row.created_at or datetime.now(UTC),
        data={
            "manifest": row.manifest_ref,
            "batch_id": row.batch_id,
            "submission_key": row.submission_key,
        },
    )


async def start_workflow(
    client: Client, row: JobRow, task_queue: str | None = None
) -> WorkflowHandle:
    """Starts the job's workflow as admission would, on the workflow queue unless `task_queue`
    names one that no worker polls."""
    return await client.start_workflow(
        JobWorkflow.run,
        args=[accepted_of(row), JobParams(seed=row.seed, parameters=row.parameters)],
        id=workflow_id(row.job_id),
        task_queue=task_queue or naming().workflow.queue,
    )


async def start(client: Client, db, row: JobRow, task_queue: str | None = None) -> WorkflowHandle:
    """Claims the seeded row from accepted to admitted, then starts its workflow."""
    transition(db, row.job_id, "accepted", "admitted")
    return await start_workflow(client, get_job(db, row.job_id), task_queue)


async def until(check: Callable[[], object], what: str, timeout_s: float = WAIT_S):
    """Waits until `check`, which may block and so runs on a thread, returns a true value, and
    returns that value."""
    deadline = time.monotonic() + timeout_s
    while not (value := await asyncio.to_thread(check)):
        assert time.monotonic() < deadline, f"no {what} within {timeout_s:.0f} s"
        await asyncio.sleep(0.2)
    return value


@asynccontextmanager
async def rig(client: Client, workflow_dsn: str | None = None) -> AsyncIterator[FakeControl]:
    """Runs the workflow worker of the test build, made current, the control activities of
    FakeControl and a simulate pool whose every attempt runs until it is interrupted.

    With `workflow_dsn` the emit activity is the real one, which moves the job row and writes its
    events in Postgres, and otherwise it is FakeControl's, which holds one row in memory.
    """
    control = FakeControl()
    control.plans[MANIFEST["uri"]] = LoadedPlan(plan=PLAN, max_history_events=10_000)
    emit = Emitter(workflow_dsn).emit if workflow_dsn else control.emit
    with ThreadPoolExecutor(4) as local:
        workers = [
            Worker(
                client,
                task_queue=naming().workflow.queue,
                workflows=[JobWorkflow],
                activities=[place_activity(StaticPlacer()), emit],
                activity_executor=local,
                deployment_config=WorkerDeploymentConfig(
                    version=WorkerDeploymentVersion(naming().workflow.deployment, BUILD_ID),
                    use_worker_versioning=True,
                ),
            ),
            Worker(client, task_queue=control_queue(), activities=control.control_activities()),
            FakePool(client, "simulate", lambda task: Interruptible()),
        ]
        async with AsyncExitStack() as stack:
            for worker in workers:
                await stack.enter_async_context(worker)
            await versioning.set_current(client, BUILD_ID)
            yield control


@pytest.fixture
def kit() -> SimpleNamespace:
    """The helpers above, for the tests of this directory, which the importlib import mode of the
    suite keeps from importing this module."""
    return SimpleNamespace(
        new_row=new_row,
        start=start,
        start_workflow=start_workflow,
        until=until,
        rig=rig,
    )
