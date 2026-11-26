"""Rows seeded and moved as the dispatch and control APIs would, a login of admission's group, a dev
server that knows the search attributes of naming.yaml, and a rig that runs the job workflow on it,
whose `emit` writes the real job row, so a job pauses and resumes in the row admission reads."""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from admission_tests_support import MANIFEST, GatedEmitter, manifest, row_of
from dsy_events import build_event
from dsy_eventstore import insert_events
from dsy_jobdoc.plan_models import Plan
from dsy_jobstate import insert_accepted, mark_published, request_action, transition
from dsy_temporal import new_traceparent
from dsy_workflow import versioning
from dsy_workflow.placement import StaticPlacer
from dsy_workflow.testing import Fail, FakeControl, FakePool, Succeed, build_id, control_worker
from dsy_workflow.testing.control import LoadedPlan
from dsy_workflow.worker import workflow_worker
from temporalio.client import Client

BUILD_ID = build_id()

# The moves that take a freshly claimed row to each status, made as the workflow and control would.
MOVES = {
    "admitted": [],
    "running": [("admitted", "running")],
    "paused": [("admitted", "running"), ("running", "paused")],
    "resume-pending": [
        ("admitted", "running"),
        ("running", "paused"),
        ("paused", "resume-pending"),
    ],
    "succeeded": [("admitted", "running"), ("running", "succeeded")],
}


# The manifest of a patch, whose simulate task runs other args.
PATCH = manifest("b" * 64)
SIMULATE = {"workload": "pi", "args": {"n": 1000}, "seed": 7}
PATCHED = {**SIMULATE, "args": {"n": 2000}}


def one_task(params: dict) -> LoadedPlan:
    """A plan of one checkpoint-capable simulate task with no retry, so the failure of its first
    attempt pauses the job."""
    plan = Plan.model_validate(
        {
            "plan_version": 1,
            "profile": "ci",
            "max_parallel": 8,
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
                    "retries": 0,
                    "checkpoint": True,
                    "allow_partial": False,
                    "params": params,
                }
            ],
        }
    )
    return LoadedPlan(plan=plan, max_history_events=10_000)


def accepted_event(job_id: str, tenant: str = "acme", job_class: str = "short"):
    return build_event(
        "job.accepted",
        source="/dsy/dispatch",
        tenant=tenant,
        jobid=job_id,
        priorityclass="standard",
        jobclass=job_class,
        traceparent=new_traceparent(),
        tracestate="vendor=1",
        time=datetime.now(UTC),
        data={
            "manifest": MANIFEST,
            "batch_id": str(uuid.uuid4()),
            "submission_key": f"key-{job_id}",
        },
    )


@pytest.fixture
def connected(login):
    """Connects fresh logins in `dsy_admission`, the group of admission's service login."""
    return lambda: psycopg.connect(login("dsy_admission"), autocommit=True)


@pytest.fixture
def admission_db(connected):
    with connected() as connection:
        yield connection


@pytest.fixture
def seed(db):
    """Inserts a row and its accepted event in one transaction and marks it published, as the
    submit sequence does, then moves it to `status`, and returns the job id. `published=False`
    stops before the mark."""

    def insert(
        status: str = "accepted",
        tenant: str = "acme",
        job_class: str = "short",
        published: bool = True,
    ) -> str:
        event = accepted_event(str(uuid.uuid4()), tenant, job_class)
        job_id = event.jobid
        with db.transaction():
            insert_accepted(db, [row_of(event)])
            insert_events(db, [event])
        if published:
            mark_published(db, job_id)
        if status == "accepted":
            return job_id
        transition(db, job_id, "accepted", "admitted")
        for expected, to in MOVES[status]:
            if to == "paused":
                request_action(db, job_id, "pause")
            if to == "resume-pending":
                request_action(db, job_id, "resume")
            transition(db, job_id, expected, to)
        return job_id

    return insert


@pytest.fixture
def resume_before_start(db):
    """Pauses and resumes a job that never started, as the control API does with its row-only moves,
    which leaves the row at `accepted` with a control sequence and no outstanding action."""

    def resume(job_id: str) -> None:
        request_action(db, job_id, "pause")
        transition(db, job_id, "accepted", "paused", row_only=True)
        request_action(db, job_id, "resume")
        transition(db, job_id, "paused", "resume-pending")
        transition(db, job_id, "resume-pending", "accepted", row_only=True)

    return resume


@pytest.fixture
def age(db):
    """Sets a row's `updated_at` that long ago, past the trigger that sets it on every update."""

    def set_age(job_id: str, ago: timedelta) -> None:
        with db.transaction():
            db.execute("SET LOCAL session_replication_role = replica")
            db.execute(
                "UPDATE accepted_job SET updated_at = now() - %s WHERE job_id = %s", (ago, job_id)
            )

    return set_age


@pytest.fixture
def admitted_events(db):
    return lambda job_id: db.execute(
        "SELECT count(*) FROM event WHERE job_id = %s AND type = 'dev.dsy.job.admitted.v1'",
        (job_id,),
    ).fetchone()[0]


class Rig:
    """The workers of one test on the dev server: the job workflow with a GatedEmitter on the
    database, FakeControl serving the plans of MANIFEST and PATCH, and a FakePool for the simulate
    pool whose first attempt of a task fails with a checkpoint and whose later ones succeed."""

    patch = PATCH
    patched = PATCHED

    def __init__(self, client: Client, dsn: str):
        self.emitter = GatedEmitter(dsn)
        self.control = FakeControl(
            plans={MANIFEST["uri"]: one_task(SIMULATE), PATCH["uri"]: one_task(PATCHED)}
        )
        self.pool = FakePool(
            client,
            "simulate",
            lambda task: Fail("timeout", checkpoint=True) if task.task_attempt == 1 else Succeed(),
        )
        self.client = client
        self.workers = [
            workflow_worker(client, BUILD_ID, StaticPlacer(), self.emitter.emit),
            control_worker(client, self.control, BUILD_ID),
            self.pool,
        ]

    async def __aenter__(self) -> "Rig":
        for worker in self.workers:
            await worker.__aenter__()
        await versioning.set_current(self.client, BUILD_ID)
        return self

    async def __aexit__(self, *exc) -> None:
        for worker in reversed(self.workers):
            await worker.__aexit__(*exc)


@pytest.fixture
def rig(appdb):
    return lambda client: Rig(client, appdb)
