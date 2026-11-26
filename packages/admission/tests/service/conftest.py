"""Rows seeded and published as the dispatch API would, a login of admission's group, the
configuration of a service under test, and a dev server with a rig that runs the job workflow on
it, whose `emit` writes the real job row and whose fake pool sits behind the service's prefix."""

import uuid
from datetime import UTC, datetime

import pytest
from admission_tests_support import MANIFEST, GatedEmitter, row_of
from dsy_admission.service import Config, prometheus
from dsy_events import build_event, to_bytes
from dsy_eventstore import insert_events
from dsy_jobdoc.plan_models import Plan
from dsy_jobstate import insert_accepted, mark_published
from dsy_servicekit.testing import free_port as _free_port
from dsy_temporal import new_traceparent
from dsy_workflow import versioning
from dsy_workflow.contract import naming
from dsy_workflow.placement import StaticPlacer
from dsy_workflow.testing import Fail, FakeControl, FakePool, Succeed, build_id, control_worker
from dsy_workflow.testing.control import LoadedPlan
from dsy_workflow.worker import workflow_worker
from temporalio.client import Client
from temporalio.runtime import Runtime

BUILD_ID = build_id()

PREFIX = "t-admission-"
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


def accepted_event(job_id: str):
    return build_event(
        "job.accepted",
        source="/dsy/dispatch",
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
def prefix() -> str:
    """The pool queue prefix of the services under test, behind which the rig's pool sits."""
    return PREFIX


@pytest.fixture
def admission_dsn(login):
    """The DSN of a fresh login in `dsy_admission`, the group of admission's service login."""
    return login("dsy_admission")


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


class Rig:
    """The workers of one test on the dev server: the job workflow with a GatedEmitter on the
    database, FakeControl serving PLAN, and a FakePool for the simulate pool behind PREFIX, whose
    task fails with a checkpoint on its first attempt for a job in `pausing` and succeeds
    otherwise."""

    def __init__(self, client: Client, dsn: str):
        self.emitter = GatedEmitter(dsn)
        self.pausing: set[str] = set()
        self.client = client
        self.workers = [
            workflow_worker(client, BUILD_ID, StaticPlacer(), self.emitter.emit),
            control_worker(client, FakeControl(plans={MANIFEST["uri"]: PLAN}), BUILD_ID),
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


@pytest.fixture
def rig(appdb):
    return lambda client: Rig(client, appdb)
