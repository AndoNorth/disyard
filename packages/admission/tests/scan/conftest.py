"""Rows seeded and moved as the dispatch and control APIs would, a login of admission's group, a dev
server that knows the search attributes of naming.yaml, and a rig that runs the job workflow on it,
whose `emit` writes the real job row, so a job pauses and resumes in the row admission reads."""

import asyncio
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from temporalio import activity
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.client import Client
from temporalio.common import WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig
from wdf_events import build_event
from wdf_eventstore import insert_events
from wdf_jobdoc.plan_models import Plan
from wdf_jobstate import JobRow, insert_accepted, mark_published, request_action, transition
from wdf_temporal import event_data_converter, new_traceparent, tracing_interceptor
from wdf_workflow import versioning
from wdf_workflow.activities import Emitter
from wdf_workflow.contract import naming
from wdf_workflow.placement import StaticPlacer, place_activity
from wdf_workflow.service import control_queue
from wdf_workflow.testing import Fail, FakeControl, FakePool, Succeed, connect
from wdf_workflow.testing.control import Emit, Emitted, LoadedPlan

os.environ.setdefault(naming().workflow.build_id_env, "admission-test")
BUILD_ID = os.environ[naming().workflow.build_id_env]

from wdf_workflow.job import JobWorkflow  # noqa: E402

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
TYPES = {
    "keyword": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "datetime": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
}
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


def manifest(sha256: str) -> dict:
    return {
        "uri": f"s3://wdf-sources/sha256/{sha256[:2]}/{sha256}",
        "sha256": sha256,
        "size": 2048,
        "name": "manifest.json",
        "kind": "manifest",
    }


MANIFEST = manifest(SHA)
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
        source="/wdf/dispatch",
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


def row_of(event) -> JobRow:
    return JobRow(
        job_id=event.jobid,
        tenant=event.tenant,
        submission_key=event.data["submission_key"],
        batch_id=event.data["batch_id"],
        priority_class=event.priorityclass,
        job_class=event.jobclass,
        manifest_ref=event.data["manifest"],
        schema_version=event.dataschema,
        traceparent=event.traceparent,
        tracestate=event.tracestate,
        seed=7,
        parameters={"n": 3},
        fingerprint=SHA,
    )


@pytest.fixture
def db(appdb):
    with psycopg.connect(appdb, autocommit=True) as connection:
        yield connection


@pytest.fixture
def login(appdb, db):
    """Connects fresh logins in `wdf_admission`, the group of admission's service login."""
    users = []

    def connected() -> psycopg.Connection:
        user = f"admission_{time.time_ns()}"
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE wdf_admission").format(
                sql.Identifier(user)
            )
        )
        users.append(user)
        return psycopg.connect(make_conninfo(appdb, user=user, password="standin"), autocommit=True)

    yield connected
    for user in users:
        db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
        db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


@pytest.fixture
def admission_db(login):
    with login() as connection:
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
def status(db):
    return lambda job_id: db.execute(
        "SELECT status FROM accepted_job WHERE job_id = %s", (job_id,)
    ).fetchone()[0]


@pytest.fixture
def admitted_events(db):
    return lambda job_id: db.execute(
        "SELECT count(*) FROM event WHERE job_id = %s AND type = 'dev.wdf.job.admitted.v1'",
        (job_id,),
    ).fetchone()[0]


@pytest.fixture(scope="module")
def server(temporal) -> str:
    """The dev server's address, once it knows the search attributes of naming.yaml."""

    async def register():
        client = await connect(temporal)
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                namespace=naming().namespace,
                search_attributes={
                    name: TYPES[kind] for name, kind in naming().search_attributes.items()
                },
            )
        )

    asyncio.run(register())
    return temporal


@pytest.fixture
def client(server):
    """Connects a client set up as the admission service sets it up, inside the test's loop."""

    async def connected() -> Client:
        await connect(server)
        return await Client.connect(
            server,
            namespace=naming().namespace,
            data_converter=event_data_converter,
            interceptors=[tracing_interceptor()],
        )

    return connected


class GatedEmitter(Emitter):
    """The real `emit`, except that while `held` is set the move from `resume-pending` to
    `running` loses as if the row were still `paused`, so a workflow that applied a resume keeps
    its row at `resume-pending` and tries the move again after its backoff."""

    def __init__(self, dsn: str):
        super().__init__(dsn)
        self.held = False

    @activity.defn(name="emit")
    def emit(self, request: Emit) -> Emitted:
        move = request.transition
        if self.held and move and (move.expected, move.to) == ("resume-pending", "running"):
            return Emitted(inserted=0, lost="paused")
        return Emitter.emit(self, request)


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
        self.executor = ThreadPoolExecutor(4)
        self.client = client
        self.workers = [
            Worker(
                client,
                task_queue=naming().workflow.queue,
                workflows=[JobWorkflow],
                activities=[place_activity(StaticPlacer()), self.emitter.emit],
                activity_executor=self.executor,
                deployment_config=WorkerDeploymentConfig(
                    version=WorkerDeploymentVersion(naming().workflow.deployment, BUILD_ID),
                    use_worker_versioning=True,
                ),
            ),
            Worker(
                client,
                task_queue=control_queue(BUILD_ID),
                activities=self.control.control_activities(),
            ),
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
        self.executor.shutdown()


@pytest.fixture
def rig(appdb):
    return lambda client: Rig(client, appdb)
