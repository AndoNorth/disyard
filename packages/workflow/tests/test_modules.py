"""The workflow modules: the loader over faked entry points, and the service loop that runs the
workers of the modules beside the built-in ones."""

import asyncio
import importlib.util
import logging
import socket
from datetime import timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from dsy_servicekit.lifecycle import stopping
from dsy_workflow import modules, service
from dsy_workflow.contract import naming
from dsy_workflow.modules import Context
from dsy_workflow.service import DRAIN, Config, serve
from dsy_workflow.testing import FakeControl, FakePool, Succeed, connect
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

LIMIT_S = 120
QUEUE = "dsy-modules-test"

# The helpers of the service suite, which owns the plan and the way a job is started.
_spec = importlib.util.spec_from_file_location(
    "service_fixtures", Path(__file__).parent / "service/conftest.py"
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


def points(*named: tuple[str, str]) -> list[EntryPoint]:
    return [EntryPoint(name, value, modules.GROUP) for name, value in named]


def fake_entry_points(monkeypatch, found: list[EntryPoint]) -> None:
    monkeypatch.setattr(modules, "entry_points", lambda group: found)


def workers(ctx: Context) -> list[Worker]:
    return []


def test_the_modules_come_sorted_by_name(monkeypatch):
    fake_entry_points(
        monkeypatch, points(("b", f"{__name__}:workers"), ("a", f"{__name__}:workers"))
    )
    assert [name for name, _ in modules.load()] == ["a", "b"]


def test_a_repeated_name_skips_every_entry_point_of_it(monkeypatch, caplog):
    fake_entry_points(
        monkeypatch,
        points(
            ("twice", f"{__name__}:workers"),
            ("once", f"{__name__}:workers"),
            ("twice", "os:getcwd"),
        ),
    )
    assert [name for name, _ in modules.load()] == ["once"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert all("twice" in r.getMessage() for r in errors)


def test_a_module_that_fails_to_import_is_skipped(monkeypatch, caplog):
    fake_entry_points(
        monkeypatch,
        points(("broken", "no_such_module_anywhere:workers"), ("fine", f"{__name__}:workers")),
    )
    assert [name for name, _ in modules.load()] == ["fine"]
    assert [r.levelno for r in caplog.records if "broken" in r.getMessage()] == [logging.ERROR]


def test_the_hidden_group_loads_no_module(monkeypatch):
    fake_entry_points(monkeypatch, [])
    assert modules.load() == []
    assert service.build_modules(Context(None, "b", DRAIN)) == []


class Sleeps:
    """A module worker that is a stand-in for a Worker."""

    def __init__(self, dies: bool):
        self.dies = dies
        self.shut_down = asyncio.Event()

    async def run(self):
        if self.dies:
            raise RuntimeError("module worker died")
        await self.shut_down.wait()

    async def shutdown(self):
        self.shut_down.set()


def test_a_module_that_raises_in_construction_is_skipped(monkeypatch, caplog):
    def raises(ctx):
        raise RuntimeError("cannot build")

    fine = Sleeps(dies=False)
    monkeypatch.setattr(service, "load", lambda: [("bad", raises), ("good", lambda ctx: [fine])])
    assert service.build_modules(Context(None, "b", DRAIN)) == [("good", fine)]
    assert [r.levelno for r in caplog.records if "bad" in r.getMessage()] == [logging.ERROR]


def _config(temporal: str, build_id: str, monkeypatch) -> Config:
    monkeypatch.setenv(naming().workflow.build_id_env, build_id)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return Config(
        temporal_address=temporal,
        namespace=naming().namespace,
        build_id=build_id,
        metrics_address=f"127.0.0.1:{port}",
        workflow_dsn="postgresql://unused",
        artifacts_dsn="postgresql://unused",
        store_endpoint="http://127.0.0.1:1",
        workflow_credentials="/unused",
        indexer_credentials="/unused",
    )


async def _job_succeeds(temporal: str, build_id: str, control: FakeControl) -> str:
    client = await connect(temporal)
    async with FakePool(client, "compile", lambda task: Succeed()):
        return await (await fixtures._start_job(client, control, build_id)).result()


def _serve(config: Config, stop: asyncio.Event, control: FakeControl) -> asyncio.Task:
    return asyncio.create_task(
        serve(config, control.control_activities(), control.emit, fixtures.fake_pass, stop=stop)
    )


@pytest.mark.integration
def test_jobs_run_with_no_module(temporal, monkeypatch):
    fake_entry_points(monkeypatch, [])

    async def body():
        # The first connect to a fresh dev server can be reset, and testing.connect retries it.
        await connect(temporal)
        control = FakeControl()
        with stopping() as stop:
            task = _serve(_config(temporal, "nomodule", monkeypatch), stop, control)
            try:
                return await _job_succeeds(temporal, "nomodule", control)
            finally:
                stop.set()
                await task

    assert asyncio.run(asyncio.wait_for(body(), LIMIT_S)) == "succeeded"


@pytest.mark.integration
def test_a_bad_module_does_not_keep_job_workflow_from_running(temporal, monkeypatch, caplog):
    def raises(ctx):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(service, "load", lambda: [("bad", raises)])

    async def body():
        # The first connect to a fresh dev server can be reset, and testing.connect retries it.
        await connect(temporal)
        control = FakeControl()
        with stopping() as stop:
            task = _serve(_config(temporal, "badmodule", monkeypatch), stop, control)
            try:
                return await _job_succeeds(temporal, "badmodule", control)
            finally:
                stop.set()
                await task

    assert asyncio.run(asyncio.wait_for(body(), LIMIT_S)) == "succeeded"
    assert [r.levelno for r in caplog.records if "bad" in r.getMessage()] == [logging.ERROR]


@pytest.mark.integration
def test_a_module_worker_that_dies_later_does_not_end_the_host(temporal, monkeypatch, caplog):
    dying = Sleeps(dies=True)
    monkeypatch.setattr(service, "load", lambda: [("dying", lambda ctx: [dying])])

    async def body():
        # The first connect to a fresh dev server can be reset, and testing.connect retries it.
        await connect(temporal)
        control = FakeControl()
        with stopping() as stop:
            task = _serve(_config(temporal, "dying", monkeypatch), stop, control)
            try:
                status = await _job_succeeds(temporal, "dying", control)
                assert not task.done()
                return status
            finally:
                stop.set()
                await task

    assert asyncio.run(asyncio.wait_for(body(), LIMIT_S)) == "succeeded"
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "dying" in record.getMessage()
    assert "module worker died" in str(record.exc_info[1])


@workflow.defn(name="ModulesTestWorkflow")
class SlowWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            "modules_test_slow", start_to_close_timeout=timedelta(minutes=1)
        )


@pytest.mark.integration
def test_an_activity_in_flight_at_sigterm_finishes_inside_drain(temporal, monkeypatch):
    started = asyncio.Event()

    @activity.defn(name="modules_test_slow")
    async def slow() -> str:
        started.set()
        await asyncio.sleep(3)
        return "done"

    def module_workers(ctx: Context) -> list[Worker]:
        return [
            Worker(
                ctx.client,
                task_queue=QUEUE,
                workflows=[SlowWorkflow],
                activities=[slow],
                workflow_runner=UnsandboxedWorkflowRunner(),
                graceful_shutdown_timeout=ctx.drain,
            )
        ]

    monkeypatch.setattr(service, "load", lambda: [("slow", module_workers)])

    async def body():
        client = await connect(temporal)
        control = FakeControl()
        with stopping() as stop:
            task = _serve(_config(temporal, "drain", monkeypatch), stop, control)
            handle = await client.start_workflow(
                SlowWorkflow.run, id="modules-test-drain", task_queue=QUEUE
            )
            await asyncio.wait_for(started.wait(), 60)
            stop.set()
            await task
        history = await handle.fetch_history()
        return [EventType.Name(event.event_type) for event in history.events]

    events = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert "EVENT_TYPE_ACTIVITY_TASK_COMPLETED" in events
