"""Rows seeded and published as the dispatch API would, a login of admission's group, the
configuration of a service under test, and a dev server with a rig that runs the job workflow on
it, whose `emit` writes the real job row and whose fake pool sits behind the service's prefix."""

import asyncio
import os
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from temporalio import activity
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.client import Client
from temporalio.common import WorkerDeploymentVersion
from temporalio.runtime import Runtime
from temporalio.worker import Worker, WorkerDeploymentConfig
from wft_admission.service import Config, prometheus
from wft_events import build_event, to_bytes
from wft_eventstore import insert_events
from wft_jobdoc.plan_models import Plan
from wft_jobstate import JobRow, insert_accepted, mark_published
from wft_temporal import event_data_converter, new_traceparent, tracing_interceptor
from wft_workflow import versioning
from wft_workflow.activities import Emitter
from wft_workflow.contract import naming
from wft_workflow.placement import StaticPlacer, place_activity
from wft_workflow.service import control_queue
from wft_workflow.testing import Fail, FakeControl, FakePool, Succeed, connect
from wft_workflow.testing.control import Emit, Emitted, LoadedPlan

os.environ.setdefault(naming().workflow.build_id_env, "admission-test")
BUILD_ID = os.environ[naming().workflow.build_id_env]

from wft_workflow.job import JobWorkflow  # noqa: E402

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MANIFEST = {
    "uri": f"s3://wft-sources/sha256/9f/{SHA}",
    "sha256": SHA,
    "size": 2048,
    "name": "manifest.json",
    "kind": "manifest",
}
PREFIX = "t-admission-"
TYPES = {
    "keyword": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "datetime": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
}
# A plan of one checkpoint-capable simulate task with no retry, so a failed attempt pauses the job.
PLAN = LoadedPlan(
    plan=Plan.model_validate(
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
                    "params": {"workload": "pi", "args": {"n": 1000}, "seed": 7},
                }
            ],
        }
    ),
    max_history_events=10_000,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def accepted_event(job_id: str):
    return build_event(
        "job.accepted",
        source="/wft/dispatch",
        tenant="acme",
        jobid=job_id,
        priorityclass="standard",
        jobclass="short",
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
def submit(db):
    """Submits n jobs as the dispatch API does: the rows and their accepted events in one
    transaction, then each publish and its receipt. Returns the job ids in publish order."""

    def submitted(publish, n: int) -> list[str]:
        events = [accepted_event(str(uuid.uuid4())) for _ in range(n)]
        with db.transaction():
            insert_accepted(db, [row_of(event) for event in events])
            insert_events(db, events)
        for event in events:
            publish(to_bytes(event))
            mark_published(db, event.jobid)
        return [event.jobid for event in events]

    return submitted


@pytest.fixture
def counts(db):
    """The number of rows in any of the statuses given."""
    return lambda *statuses: db.execute(
        "SELECT count(*) FROM accepted_job WHERE status = ANY(%s)", (list(statuses),)
    ).fetchone()[0]


@pytest.fixture
def status(db):
    return lambda job_id: db.execute(
        "SELECT status FROM accepted_job WHERE job_id = %s", (job_id,)
    ).fetchone()[0]


@pytest.fixture
def prefix() -> str:
    """The pool queue prefix of the services under test, behind which the rig's pool sits."""
    return PREFIX


@pytest.fixture
def admission_dsn(appdb, db):
    """The DSN of a fresh login in `wft_admission`, the group of admission's service login."""
    user = f"admission_{time.time_ns()}"
    db.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE wft_admission").format(
            sql.Identifier(user)
        )
    )
    yield make_conninfo(appdb, user=user, password="standin")
    db.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (user,))
    db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
    db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


@pytest.fixture(scope="module")
def metrics_port() -> int:
    return _free_port()


@pytest.fixture
def free_port():
    """Finds a loopback port that nothing listens on."""
    return _free_port


@pytest.fixture(scope="module")
def runtime(metrics_port) -> Runtime:
    """One Temporal runtime per module, serving the metrics of every service it runs."""
    return prometheus(f"127.0.0.1:{metrics_port}")


@pytest.fixture
def config(admission_dsn):
    """The configuration of a service under test, with no broker or Temporal behind it until a
    test names them."""

    def make(**changes) -> Config:
        values = {
            "temporal_address": "127.0.0.1:1",
            "namespace": naming().namespace,
            "dsn": admission_dsn,
            "broker_address": "fake",
            "broker_password": "standin",
            "profile": "ci",
            "metrics_address": "unused",
        }
        return Config(**{**values, **changes})

    return make


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
    database, FakeControl serving PLAN, and a FakePool for the simulate pool behind PREFIX, whose
    task fails with a checkpoint on its first attempt for a job in `pausing` and succeeds
    otherwise."""

    def __init__(self, client: Client, dsn: str):
        self.emitter = GatedEmitter(dsn)
        self.pausing: set[str] = set()
        self.executor = ThreadPoolExecutor(8)
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
                activities=FakeControl(plans={MANIFEST["uri"]: PLAN}).control_activities(),
            ),
            FakePool(
                client,
                "simulate",
                self._script,
                queue=PREFIX + naming().pools["simulate"],
            ),
        ]

    def _script(self, task):
        if task.request.jobid in self.pausing and task.task_attempt == 1:
            return Fail("timeout", checkpoint=True)
        return Succeed()

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
