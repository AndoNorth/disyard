"""The example module, discovered by entry point and served by the real host beside `JobWorkflow`,
with nothing in the workflow package that names it."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from dsy_exampleflow import TASK_QUEUE, EchoWorkflow
from dsy_servicekit.lifecycle import stopping
from dsy_servicekit.testing import free_port
from dsy_workflow import modules
from dsy_workflow.contract import naming
from dsy_workflow.service import Config, serve
from dsy_workflow.testing import FakeControl, FakePool, Succeed, connect

pytestmark = pytest.mark.seam

LIMIT_S = 120
BUILD_ID = "exampleflow"

# The helpers of the workflow service suite, which owns the plan and the way a job is started.
_path = Path(__file__).resolve().parents[2] / "workflow/tests/service/conftest.py"
_spec = importlib.util.spec_from_file_location("workflow_service_fixtures", _path)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


@pytest.fixture(scope="module")
def temporal(t1) -> str:
    from dsy_workflow.testing import dev_server

    with dev_server(t1) as address:
        yield address


def test_the_entry_point_is_discovered():
    assert "exampleflow" in [name for name, _ in modules.load()]


@pytest.mark.integration
def test_the_host_runs_echo_workflow_and_a_level_0_job_in_one_process(temporal, monkeypatch):
    monkeypatch.setenv(naming().workflow.build_id_env, BUILD_ID)
    config = Config(
        temporal_address=temporal,
        namespace=naming().namespace,
        build_id=BUILD_ID,
        metrics_address=f"127.0.0.1:{free_port()}",
        workflow_dsn="postgresql://unused",
        artifacts_dsn="postgresql://unused",
        store_endpoint="http://127.0.0.1:1",
        workflow_credentials="/unused",
        indexer_credentials="/unused",
    )

    async def body():
        # The first connect to a fresh dev server can be reset, and testing.connect retries it.
        client = await connect(temporal)
        control = FakeControl()
        with stopping() as stop:
            task = asyncio.create_task(
                serve(
                    config,
                    control.control_activities(),
                    control.emit,
                    fixtures.fake_pass,
                    stop=stop,
                )
            )
            try:
                async with FakePool(client, "compile", lambda task: Succeed()):
                    job = await fixtures._start_job(client, control, BUILD_ID)
                    echoed = await client.execute_workflow(
                        EchoWorkflow.run, "hello", id="exampleflow-echo", task_queue=TASK_QUEUE
                    )
                    return echoed, await job.result()
            finally:
                stop.set()
                await task

    echoed, status = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert echoed == {"text": "hello", "length": 5}
    assert status == "succeeded"
