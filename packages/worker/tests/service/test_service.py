"""The service as a real process on a dev server and SeaweedFS: the activity contract on every pool
behind a queue prefix of its own, the drain on SIGTERM, the ready file, and the refusal of a
missing or malformed variable."""

import asyncio
import contextlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from dsy_worker.config import ENV, MIRROR_ENV, PREFIX_ENV
from dsy_worker.service import DRAIN
from dsy_workflow.contract import check_failure
from dsy_workflow.models import HeartbeatDetails, RaisedFailure, TaskInput
from dsy_workflow.testing import activity_contract, connect
from dsy_workflow.testing.pool import RunTaskOnce
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

LIMIT_S = 300
# Far more work than any test waits for, beating often.
BURN = {"workload": "cpu_burn", "seed": 7, "args": {"units": 10**9, "heartbeat_every": 1000}}
# Work of a few seconds, which ends well inside the drain.
SHORT = {"workload": "cpu_burn", "seed": 7, "args": {"units": 5000, "heartbeat_every": 100}}


def run(body):
    return asyncio.run(asyncio.wait_for(body, LIMIT_S))


def with_params(task: TaskInput, params: dict) -> TaskInput:
    return TaskInput.model_validate({**task.model_dump(mode="json"), "params": params})


@contextlib.asynccontextmanager
async def started(client: Client, queue: str, task: TaskInput, cancel: bool = False):
    """The workflow of run_task_once started on a queue of its own, for a test that acts while
    the activity runs."""
    once = f"dsy-run-task-once-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=once,
        workflows=[RunTaskOnce],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        yield await client.start_workflow(
            RunTaskOnce.run,
            args=[queue, task, cancel, 5, 300],
            id=f"run-task-once-{uuid.uuid4()}",
            task_queue=once,
        )


async def guest_running(client: Client, handle) -> None:
    """Returns once a heartbeat carries the guest's progress, so the warm-up is over."""
    converter = client.data_converter.payload_converter
    while True:
        for pending in (await handle.describe()).raw_description.pending_activities:
            if pending.heartbeat_details.payloads:
                [beat] = converter.from_payloads(
                    pending.heartbeat_details.payloads, [HeartbeatDetails]
                )
                if beat.progress is not None:
                    return
        await asyncio.sleep(0.2)


def children_of(scratch: Path) -> list[str]:
    """The command lines of the processes that name the worker's scratch root."""
    found = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if str(scratch) in argv:
            found.append(argv)
    return found


@pytest.mark.parametrize("pool", ["compile", "simulate", "aggregate"])
@pytest.mark.integration
@pytest.mark.pair
def test_activity_contract(worker, temporal, cases, pool):
    """The real worker passes the activity contract on its pool's queue behind its prefix, and
    the simulate pool keeps its module cache from one task to the next."""
    given = cases(pool)
    running = worker(pool)

    async def body():
        await activity_contract(await connect(temporal), running.queue, given)

    run(body())
    running.stop()
    assert running.process.wait(DRAIN.total_seconds()) == 0
    assert f"polling {running.queue} in namespace dsy for pool {pool}, unversioned" in (
        running.output()
    )
    if pool == "simulate":
        assert list((running.scratch / "modules").iterdir())


@pytest.mark.integration
@pytest.mark.pair
def test_sigterm_drains(worker, temporal, cases):
    """A task in flight at a SIGTERM runs on to its result, and the worker then exits 0."""
    task = with_params(cases("simulate").succeeds, SHORT)
    running = worker("simulate")

    async def body():
        client = await connect(temporal)
        async with started(client, running.queue, task) as handle:
            await guest_running(client, handle)
            stopped = time.monotonic()
            running.stop()
            outcome = await handle.result()
            return outcome, time.monotonic() - stopped

    outcome, finished = run(body())
    print(f"finished {finished:.2f} s after the SIGTERM")
    assert "result" in outcome, outcome
    assert running.process.wait(DRAIN.total_seconds()) == 0
    assert not running.ready_file.exists()


@pytest.mark.integration
@pytest.mark.pair
def test_sigterm_fails_long_task(worker, temporal, cases):
    """A task still running at the end of the drain is stopped with its children and fails as
    worker_shutdown, retried on the infrastructure budget, with a log tail."""
    task = with_params(cases("simulate").succeeds, BURN)
    running = worker("simulate")

    async def body():
        client = await connect(temporal)
        async with started(client, running.queue, task) as handle:
            await guest_running(client, handle)
            stopped = time.monotonic()
            running.stop()
            outcome = await handle.result()
            return outcome, time.monotonic() - stopped

    outcome, drained = run(body())
    print(f"drain: {drained:.2f} s")
    failure = RaisedFailure.model_validate(
        {"type": outcome["type"], "details": outcome["details"][0]}
    )
    check_failure(failure)
    assert failure.type == "worker_shutdown"
    assert failure.details.deterministic is False
    assert failure.details.reason.startswith("worker_shutdown: drain: ")
    assert failure.details.log_tail is not None
    assert drained >= DRAIN.total_seconds()
    assert running.process.wait(DRAIN.total_seconds()) == 0
    assert children_of(running.scratch) == []


@pytest.mark.integration
@pytest.mark.pair
def test_cancel_during_drain_withdraws(worker, temporal, cases):
    """A server cancel that lands during the drain is a withdrawn attempt, not a shutdown, and
    the worker exits without waiting out the drain."""
    task = with_params(cases("simulate").succeeds, BURN)
    running = worker("simulate")

    async def body():
        client = await connect(temporal)
        async with started(client, running.queue, task, cancel=True) as handle:
            await guest_running(client, handle)
            stopped = time.monotonic()
            running.stop()
            while running.ready_file.exists():
                await asyncio.sleep(0.1)
            await handle.signal(RunTaskOnce.cancel)
            outcome = await handle.result()
            return outcome, time.monotonic() - stopped

    outcome, took = run(body())
    print(f"cancel during drain: {took:.2f} s")
    assert outcome["cancelled"] is True
    assert outcome.get("error", "").startswith("CancelledError"), outcome
    assert running.process.wait(DRAIN.total_seconds()) == 0
    assert took < DRAIN.total_seconds()
    assert children_of(running.scratch) == []


@pytest.mark.integration
@pytest.mark.pair
def test_ready_file_while_polling(worker, temporal, tmp_path):
    """The ready file exists once the worker polls and is gone once it stops, and a namespace
    the server does not know fails the start without it, even when a killed process left one."""
    running = worker("aggregate")
    running.stop()
    assert running.process.wait(DRAIN.total_seconds()) == 0
    assert not running.ready_file.exists()

    environ = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    given = complete(tmp_path) | {ENV["temporal_address"]: temporal, ENV["namespace"]: "nowhere"}
    # Left by a process that was killed, so it never removed its file.
    Path(given[ENV["ready_file"]]).touch()
    done = subprocess.run(
        [sys.executable, "-m", "dsy_worker", "--pool", "aggregate"],
        env=environ | given,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "nowhere" in done.stderr
    assert not Path(given[ENV["ready_file"]]).exists()


def complete(root: Path) -> dict[str, str]:
    return {
        ENV["temporal_address"]: "127.0.0.1:1",
        ENV["namespace"]: "dsy",
        ENV["store_endpoint"]: "http://127.0.0.1:1",
        ENV["store_credentials"]: str(root / "credentials.json"),
        ENV["slot_memory_mib"]: "1024",
        ENV["scratch"]: str(root / "scratch"),
        ENV["ready_file"]: str(root / "ready"),
    }


@pytest.mark.parametrize(
    ("pool", "change", "error"),
    [
        ("simulate", {ENV["scratch"]: ""}, "missing environment: DSY_SCRATCH"),
        ("compile", {}, f"missing environment: {MIRROR_ENV}"),
        ("aggregate", {PREFIX_ENV: "Up-"}, f"{PREFIX_ENV} gives the queue 'Up-dsy-aggregate'"),
        ("nowhere", {}, "argument --pool: invalid choice: 'nowhere'"),
    ],
)
def test_missing_env_exits_2(tmp_path, pool, change, error):
    """A missing or malformed variable, or a pool that does not exist, exits 2 before anything
    connects."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    done = subprocess.run(
        [sys.executable, "-m", "dsy_worker", "--pool", pool],
        env=environ | complete(tmp_path) | change,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2, done.stderr
    assert error in done.stderr
    assert not (tmp_path / "ready").exists()
