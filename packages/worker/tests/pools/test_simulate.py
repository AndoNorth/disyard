"""The simulate task path with the real wasmtime and the workload component over SeaweedFS: the
level 0 vector, one warm-up per component, and each failure class it reports.

Every task runs in the 512 MiB slot that spike S11 passed, whose guest cap after the worker's
reserve is 128 MiB, the slot of the ci preset. A 384 MiB slot leaves no guest cap at all.
"""

import asyncio
import dataclasses
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from wdf_artifactclient.client import ArtifactClient, RetryPolicy
from wdf_runtime.command import GUEST_INPUT
from wdf_worker.pools import Context, simulate
from wdf_worker.runner import Child
from wdf_worker.testing import contract_cases
from wdf_workflow.contract import failure_classes
from wdf_workflow.harness import matches_echo
from wdf_workflow.models import FailureDetails, TaskInput, TaskResult

pytestmark = pytest.mark.integration

SLOT_MIB = 512
# A slot below the worker's reserve and the guest's overhead.
SMALL_SLOT_MIB = 384
START_TO_CLOSE_S = 300
# Far more work than any test waits for, beating often.
BURN = {"workload": "cpu_burn", "seed": 7, "args": {"units": 10**9, "heartbeat_every": 1000}}
# A result of 33 rows of 1 MiB, which the writer doubles as hex, past the 64 MiB cap of results.xml.
CHURN = {
    "workload": "io_churn",
    "seed": 7,
    "args": {"rows": 33, "row_bytes": 1024 * 1024, "heartbeat_every": 1},
}


@pytest.fixture(scope="module")
def given(pool_store):
    """The simulate contract cases, whose inputs name the workload component in the store."""
    return contract_cases("simulate", pool_store)


@pytest.fixture(scope="module")
def store(pool_store) -> ArtifactClient:
    return ArtifactClient(pool_store.endpoint, pool_store.credentials["simulate"])


@pytest.fixture(scope="module")
def reader(pool_store) -> ArtifactClient:
    """A role that reads what the simulate role puts in wdf-results, which it cannot read back."""
    return ArtifactClient(pool_store.endpoint, pool_store.credentials["aggregate"])


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    """A scratch root whose module cache every test of the module shares, as one pod's does."""
    return tmp_path_factory.mktemp("scratch")


def with_params(task: TaskInput, params: dict) -> TaskInput:
    return TaskInput.model_validate({**task.model_dump(mode="json"), "params": params})


def run(task, store, scratch, slot=SLOT_MIB, seconds=START_TO_CLOSE_S, environment=None):
    """Runs the path as an activity that started now with a start-to-close timeout of `seconds`."""
    environment = environment or ActivityEnvironment()
    environment.info = dataclasses.replace(
        environment.info,
        started_time=datetime.now(UTC),
        start_to_close_timeout=timedelta(seconds=seconds),
    )
    context = Context(store=store, scratch=scratch, slot_memory_mib=slot)
    return asyncio.run(environment.run(simulate.run, task, context))


def failure(task, store, scratch, **options) -> tuple[ApplicationError, FailureDetails]:
    with pytest.raises(ApplicationError) as raised:
        run(task, store, scratch, **options)
    error = raised.value
    [details] = error.details
    print(error.type, details.reason)
    return error, details


def guest_runs(argv) -> bool:
    return any(arg.startswith("max-memory-size=") for arg in argv)


@pytest.fixture
def starts(monkeypatch):
    """Records the argv of every child the path starts."""
    started = []
    original = Child.start

    async def start(argv, cwd):
        started.append(argv)
        return await original(argv, cwd)

    monkeypatch.setattr(simulate.Child, "start", staticmethod(start))
    return started


def test_echo_matches_level0_vector(given, store, reader, scratch):
    result = run(given.succeeds, store, scratch)
    assert isinstance(result, TaskResult)
    [output] = result.outputs
    assert (output.kind, output.name, output.producer.attempt) == ("result", "results.xml", "r0a1")
    assert matches_echo(reader.get_bytes(output, output.size))
    assert result.log_tail.producer == output.producer
    assert not (scratch / simulate.TASK).exists()


def test_first_run_warms_then_runs_warm(given, store, tmp_path, starts):
    for _ in range(2):
        assert isinstance(run(given.succeeds, store, tmp_path), TaskResult)
    print([argv[:4] for argv in starts])
    assert [guest_runs(argv) for argv in starts] == [False, True, True]
    assert len(list((tmp_path / simulate.MODULES / "warm").iterdir())) == 1


@pytest.mark.parametrize(
    "slot, params",
    [
        (SLOT_MIB, {"workload": "fault", "seed": 1, "args": {"mode": "memory_cap"}}),
        (SMALL_SLOT_MIB, BURN),
    ],
    ids=["guest-grows-past-cap", "cap-refused"],
)
def test_guest_memory_cap_deterministic(given, store, scratch, slot, params):
    error, details = failure(with_params(given.succeeds, params), store, scratch, slot=slot)
    assert (error.type, details.deterministic, error.non_retryable) == (
        "guest_memory_cap",
        True,
        True,
    )
    assert details.log_tail is not None


def test_timeout_class(given, store, reader, scratch):
    """A guest that would outrun the task gets what is left after the upload reserve."""
    run(given.succeeds, store, scratch)
    left_s = 3
    seconds = simulate.UPLOAD_RESERVE.total_seconds() + left_s
    started = time.monotonic()
    error, details = failure(with_params(given.succeeds, BURN), store, scratch, seconds=seconds)
    elapsed = time.monotonic() - started
    print(f"timed out in {elapsed:.2f}s")
    assert (error.type, details.deterministic, error.non_retryable) == ("timeout", False, False)
    assert elapsed < left_s + 5
    assert b"HEARTBEAT" in reader.get_bytes(details.log_tail, details.log_tail.size)


def test_output_over_cap_guest_failed(given, store, scratch):
    error, details = failure(with_params(given.succeeds, CHURN), store, scratch)
    assert (error.type, details.deterministic) == ("guest_failed", True)
    assert details.reason.startswith("guest_failed: output-over-cap: ")


def test_input_modified_guest_failed(given, store, scratch, monkeypatch):
    """A guest that leaves /in other than it was given fails, as wasmtime cannot mount it read
    only. The guest's start stands in for it, adding a file beside its input.json."""
    original = Child.start

    async def start(argv, cwd):
        child = await original(argv, cwd)
        if guest_runs(argv):
            (cwd / GUEST_INPUT / "planted").write_text("x")
        return child

    monkeypatch.setattr(simulate.Child, "start", staticmethod(start))
    error, details = failure(given.succeeds, store, scratch)
    assert (error.type, details.deterministic) == ("guest_failed", True)
    assert details.reason == "guest_failed: input-modified: /in changed"


def test_store_unavailable_class(given, pool_store, scratch):
    """A store that refuses every connection fails staging, and the tail cannot go up either."""
    down = ArtifactClient(
        "http://127.0.0.1:1", pool_store.credentials["simulate"], RetryPolicy(tries=1)
    )
    error, details = failure(given.succeeds, down, scratch)
    assert (error.type, details.deterministic, error.non_retryable) == (
        "STORE_UNAVAILABLE",
        False,
        False,
    )
    assert details.log_tail is None


def test_cancel_stops_the_guest(given, store, scratch, starts):
    """A cancel ends the task's guest with its group, and reaches the caller as a cancel."""
    run(given.succeeds, store, scratch)
    starts.clear()
    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(
        environment.info,
        started_time=datetime.now(UTC),
        start_to_close_timeout=timedelta(seconds=START_TO_CLOSE_S),
    )
    context = Context(store=store, scratch=scratch, slot_memory_mib=SLOT_MIB)
    pids = []

    async def cancel_while_the_guest_runs():
        while not any(guest_runs(argv) for argv in starts):
            await asyncio.sleep(0.1)
        await asyncio.sleep(1)
        pids.extend(
            pid
            for pid in map(int, filter(str.isdigit, os.listdir("/proc")))
            if _runs(pid, starts[-1])
        )
        environment.cancel()

    async def body():
        canceller = asyncio.create_task(cancel_while_the_guest_runs())
        try:
            await environment.run(simulate.run, with_params(given.succeeds, BURN), context)
        finally:
            await canceller

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(body())
    assert pids, "no guest was running at the cancel"
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert context.beat is None


def _runs(pid: int, argv: list[str]) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().split(b"\0")[:-1] == [os.fsencode(arg) for arg in argv]
    except OSError:
        return False


def test_every_failure_type_is_a_table_row(
    given, store, pool_store, scratch, tmp_path, monkeypatch
):
    """Each way the path fails raises a class of failure-classes.yaml, with the row's
    deterministic value, non-retryable exactly when deterministic, and valid details."""
    down = ArtifactClient(
        "http://127.0.0.1:1", pool_store.credentials["simulate"], RetryPolicy(tries=1)
    )
    # Each class, with the task, the store and the PATH that provoke it, and run options.
    cases = {
        "guest_failed": (given.fails, store, None, {}),
        "guest_memory_cap": (given.succeeds, store, None, {"slot": SMALL_SLOT_MIB}),
        "timeout": (
            with_params(given.succeeds, BURN),
            store,
            None,
            {"seconds": simulate.UPLOAD_RESERVE.total_seconds() + 1},
        ),
        # A pod whose wasmtime cannot be started.
        "host_error": (given.succeeds, store, str(tmp_path), {}),
        "STORE_UNAVAILABLE": (given.succeeds, down, None, {}),
    }
    rows = failure_classes()
    for expected, (task, client, path, options) in cases.items():
        with monkeypatch.context() as patch:
            if path is not None:
                patch.setenv("PATH", path)
            error, details = failure(task, client, scratch, **options)
        assert error.type == expected
        assert error.type in rows
        assert rows[error.type].deterministic in (details.deterministic, "from-error-file")
        assert error.non_retryable == details.deterministic
        FailureDetails.model_validate(details.model_dump())
