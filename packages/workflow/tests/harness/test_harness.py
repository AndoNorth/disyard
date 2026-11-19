"""The harness command line: smoke jobs that load and bind as load_plan would, a T1 restart of the
workflow worker with tasks in flight, and the T1 refusal of the scenarios that read the job row or
the index, and the status query that the counters read, asked again while it times out. Runs on
the real pools start no FakePool and seed the workload library bundle, and every fake run holds
its pool queues behind a prefix of its own. The command ends its process without the interpreter's
teardown."""

import asyncio
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import wdf_clusterkit
from temporalio.api.common.v1 import SearchAttributes
from temporalio.converter import encode_search_attributes
from temporalio.service import RPCError, RPCStatusCode
from wdf_kernel import writer
from wdf_kernel.expected import expected_blocks
from wdf_keys import workflow_id
from wdf_plan import plan_hash, read_plan
from wdf_workflow import versioning
from wdf_workflow.activities.cache import bundle_key
from wdf_workflow.budgets import INFRA_RETRIES
from wdf_workflow.contract import naming
from wdf_workflow.harness import (
    BURN,
    ENTRY,
    REAL_ENTRY,
    SIMULATE_TASKS,
    Counters,
    echo_params,
    infra_retries,
    matches_echo,
    real_jobs,
    run_jobs,
    search_attributes,
    smoke_jobs,
)
from wdf_workflow.harness.__main__ import main
from wdf_workflow.models import StatusQuery
from wdf_workflow.taskpath import QUEUE_PREFIX_MEMO
from wdf_workflow.testing.pool import FakePool
from wdf_workloads import bundle as workloads

LIMIT_S = 300
# The infrastructure budget that the one task of STATUS spent.
SPENT = 2
STATUS = {
    "job_status": "running",
    "pause_reason": None,
    "resume_counter": 0,
    "control_seq": 0,
    "tasks": [
        {
            "id": "sim-1",
            "state": "running",
            "task_attempt": 3,
            "guest_left": 1,
            "infra_left": INFRA_RETRIES - SPENT,
            "reschedules": 0,
            "last_class": None,
            "checkpoint": None,
        }
    ],
}


def harness(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "wdf_workflow.harness", *args],
        capture_output=True,
        text=True,
        timeout=LIMIT_S,
        env={**os.environ, "WDF_BUILD_ID": "harness-test"},
    )


def counters(stdout: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in stdout.splitlines() if ": " in line)


def test_smoke_jobs_load_as_load_plan_reads_them():
    jobs = smoke_jobs(4)
    for job in jobs:
        manifest = job.event.data["manifest"]
        assert plan_hash(read_plan(job.manifest)) == manifest["sha256"]
        tasks = [(t.id, t.type) for t in job.loaded().plan.tasks]
        assert tasks[0] == ("build", "compile") and tasks[-1] == ("agg", "aggregate")
        assert sum(kind == "simulate" for _, kind in tasks) == SIMULATE_TASKS
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "bundle.tar"
            source.write_bytes(job.source)
            assert bundle_key(source, ENTRY) is not None
    # Every second job reuses one source, so its compile can hit the cache.
    assert jobs[0].source == jobs[2].source != jobs[1].source != jobs[3].source


def test_the_drivers_control_namespace_is_the_one_versioning_deploys_to():
    assert wdf_clusterkit.CONTROL == versioning.KUBE_NAMESPACE


def test_start_options_encode_every_search_attribute():
    [job] = smoke_jobs(1)
    encoded = SearchAttributes()
    encode_search_attributes(search_attributes(job.event), encoded)
    assert set(encoded.indexed_fields) == set(naming().search_attributes)


@pytest.mark.parametrize(
    "argv",
    [
        ["--address", "127.0.0.1:1", "restart", "--jobs", "1"],
        ["--address", "127.0.0.1:1", "smoke", "--jobs", "0"],
        ["--address", "127.0.0.1:1", "--kube-context", "kind-wdf", "describe"],
        ["--address", "127.0.0.1:1", "restart", "--target", ","],
        ["--address", "127.0.0.1:1", "smoke", "--queue-prefix", "Bad-"],
        ["--address", "127.0.0.1:1", "smoke", "--queue-prefix", "x" * 120],
        # A T1 run has neither the store nor the index that real pools read.
        ["--address", "127.0.0.1:1", "smoke", "--pools", "real"],
    ],
)
def test_usage_errors_exit_2(argv, capsys):
    try:
        code = main(argv)
    except SystemExit as exit:
        code = exit.code
    assert code == 2


# Runs the harness with a global whose teardown waits for a thread that never ends, as the drop of
# temporalio's runtime at interpreter teardown waits for a blocking thread that the teardown parked
# when it asked for the GIL to hand a result back to the event loop.
WAITS_AT_TEARDOWN = """
import runpy, sys, threading

parked = threading.Thread(target=threading.Event().wait, daemon=True)
parked.start()


class Waits:
    def __del__(self, join=parked.join):
        join()


waits = Waits()
sys.argv = ["harness", *sys.argv[1:]]
runpy.run_module("wdf_workflow.harness", run_name="__main__", alter_sys=True)
"""


def test_harness_exits_without_interpreter_teardown():
    """The harness ends its process once it has printed, so nothing torn down after can hold it."""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            WAITS_AT_TEARDOWN,
            "--address",
            "127.0.0.1:1",
            "smoke",
            "--jobs",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "positive count" in done.stderr


@pytest.mark.integration
def test_harness_restart_worker(temporal):
    """Restarts the workflow worker with simulate tasks in flight, and every job finishes."""
    done = harness("--address", temporal, "restart", "--jobs", "4", "--target", "workflow-worker")
    print(done.stdout, done.stderr, sep="\n")
    assert done.returncode == 0
    printed = counters(done.stdout)
    expected = {
        "finished": "4",
        "failed": "0",
        "infra_retries": "0",
        "restarted": "workflow-worker",
    }
    assert expected.items() <= printed.items()
    assert int(printed["in_flight"]) >= 1


@pytest.mark.integration
def test_fake_run_gets_its_own_prefix(temporal):
    """Each fake run holds its FakePools and its jobs' pool queues behind a fresh prefix, or the one
    --queue-prefix names, and its jobs finish there, which they can only if each job's workflow
    placed its tasks behind the prefix of its memo."""
    prefixes = []
    for extra in ([], [], ["--queue-prefix", "h-mine-"]):
        done = harness("--address", temporal, "smoke", "--jobs", "2", *extra)
        print(done.stdout, done.stderr, sep="\n")
        assert done.returncode == 0
        printed = counters(done.stdout)
        assert {"finished": "2", "failed": "0"}.items() <= printed.items()
        prefixes.append(printed["queue_prefix"])
    fresh, again, named = prefixes
    assert re.fullmatch("h-[0-9a-f]{8}-", fresh) and re.fullmatch("h-[0-9a-f]{8}-", again)
    assert fresh != again
    assert named == "h-mine-"


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["pause-resume", "zombie"])
def test_harness_t1_refuses_row_scenarios(temporal, scenario):
    done = harness("--address", temporal, "smoke", "--jobs", "1", "--scenario", scenario)
    print(f"exit {done.returncode}", done.stderr, sep="\n")
    assert done.returncode == 2
    assert done.stdout == ""
    assert f"refused: {scenario} reads the job row or the index" in done.stderr


class Handle:
    """A workflow handle whose status query times out `timeouts` times before it answers."""

    def __init__(self, timeouts: int, status: RPCStatusCode = RPCStatusCode.CANCELLED):
        self.timeouts = timeouts
        self.status = status
        self.asked = 0

    async def query(self, name, result_type):
        self.asked += 1
        if self.asked <= self.timeouts:
            raise RPCError("Timeout expired", self.status, b"")
        return StatusQuery.model_validate(STATUS)


class Client:
    def __init__(self, handles: dict[str, Handle]):
        self.handles = handles

    def get_workflow_handle(self, workflow: str) -> Handle:
        return self.handles[workflow]


def retried_pool(*job_ids: str) -> SimpleNamespace:
    calls = [
        SimpleNamespace(request=SimpleNamespace(jobid=job_id), task_attempt=2) for job_id in job_ids
    ]
    return SimpleNamespace(calls=calls)


def test_status_query_timeout_is_retried():
    """A status query that times out is asked again, and the counters still come out."""
    slow = Handle(timeouts=2)
    client = Client({workflow_id("slow"): slow})
    counters = Counters()
    asyncio.run(infra_retries(counters, client, {"simulate": retried_pool("slow")}))
    assert slow.asked == 3
    assert counters.infra_retries == SPENT
    assert "status_unanswered" not in counters.extra


def test_status_query_that_never_answers_is_counted():
    client = Client({workflow_id("dead"): Handle(timeouts=10**9), workflow_id("ok"): Handle(0)})
    counters = Counters()
    asyncio.run(infra_retries(counters, client, {"simulate": retried_pool("dead", "ok")}, 0))
    assert counters.infra_retries == SPENT
    assert counters.extra["status_unanswered"] == 1
    assert "status_unanswered: 1" in counters.lines()
    # A count that could not be read fails the run even when every job finished.
    counters.finished = 2
    assert not counters.passed(2)


def test_status_query_other_error_is_raised():
    client = Client({workflow_id("gone"): Handle(1, RPCStatusCode.NOT_FOUND)})
    with pytest.raises(RPCError):
        asyncio.run(infra_retries(Counters(), client, {"simulate": retried_pool("gone")}))


def workload_bundle() -> bytes:
    with tempfile.TemporaryDirectory() as work:
        return workloads.write(Path(work)).read_bytes()


def test_real_pools_seeds_workload_bundle():
    """A job on the real pools compiles the workload library bundle from its entry module, runs
    the level 0 echo on each simulate task and a digest list over their results."""
    source = workload_bundle()
    for job in real_jobs(2, echo_params()):
        assert job.source == source
        tasks = {t.id: t for t in job.loaded().plan.tasks}
        build = tasks["build"].params
        assert build.entry == REAL_ENTRY
        assert build.source.sha256 == hashlib.sha256(source).hexdigest()
        sims = [t.params.model_dump(mode="json") for t in tasks.values() if t.type == "simulate"]
        assert sims == [echo_params()] * SIMULATE_TASKS
        assert tasks["agg"].params.aggregation == "digest_list"
        with tempfile.TemporaryDirectory() as work:
            bundle = Path(work) / "bundle.tar"
            bundle.write_bytes(job.source)
            assert bundle_key(bundle, REAL_ENTRY) is not None


def results_xml(seed: int, args: dict) -> bytes:
    sink = io.BytesIO()
    writer.write(sink, seed, expected_blocks("echo", seed, args))
    return sink.getvalue()


def test_echo_result_is_verified():
    given = echo_params()
    assert matches_echo(results_xml(given["seed"], given["args"]))
    assert not matches_echo(results_xml(given["seed"] + 1, given["args"]))
    assert not matches_echo(results_xml(given["seed"], {**given["args"], "n": 4}))
    assert not matches_echo(b"<results")


class Done:
    """The handle of a job that succeeded, whose status shows sim-1 running."""

    def __init__(self, job_id: str):
        self.id = workflow_id(job_id)

    async def result(self):
        return "succeeded"

    async def query(self, name, result_type):
        return StatusQuery.model_validate(STATUS)


class StartingClient(Client):
    """A client that records each start and answers every status query with STATUS."""

    def __init__(self):
        super().__init__({})
        self.starts: list[dict] = []

    async def start_workflow(self, workflow, args, **options):
        self.starts.append(options)
        return Done(args[0].jobid)

    def get_workflow_handle(self, workflow: str) -> Handle:
        return Handle(0)


class Tier:
    """A tier that seeds nothing, records what it was asked to run and restart, and finds every
    job verified."""

    def __init__(self):
        self.client = StartingClient()
        self.seeded = []
        self.scripts = None
        self.restarted: list[str] = []

    async def seed(self, jobs):
        self.seeded = jobs

    def start_options(self, job):
        return {}

    @asynccontextmanager
    async def running(self, scripts, hold, prefix=""):
        self.scripts = scripts
        yield {}

    async def restart(self, targets):
        self.restarted = targets

    async def verified(self, jobs):
        return len(jobs)


@pytest.fixture
def no_fake(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a run on real pools made a FakePool")

    monkeypatch.setattr(FakePool, "__init__", refuse)


def test_real_pools_starts_no_fake(no_fake):
    tier = Tier()
    counters = asyncio.run(run_jobs(tier, "smoke", 2, None, 1, [], "real", ""))
    assert tier.scripts == {}
    assert [job.source for job in tier.seeded] == [workload_bundle()] * 2
    # With no prefix the jobs run on the contract names, as a job of admission does.
    assert [start["memo"] for start in tier.client.starts] == [None, None]
    assert counters.finished == 2 and counters.extra["verified"] == 2
    assert "queue_prefix" not in counters.extra
    # Real pools show no attempts, so every job's status is read.
    assert counters.infra_retries == 2 * SPENT
    assert counters.passed(2)
    # A job whose results did not all match fails the run.
    counters.extra["verified"] = 1
    assert not counters.passed(2)


def test_real_restart_finds_task_from_status(no_fake):
    tier = Tier()
    counters = asyncio.run(
        run_jobs(tier, "restart", 1, None, 1, ["simulate-worker"], "real", "h-real-")
    )
    assert tier.restarted == ["simulate-worker"]
    assert counters.extra["in_flight"] == 1
    sims = [t.params.model_dump(mode="json") for t in tier.seeded[0].loaded().plan.tasks[1:-1]]
    assert sims == [BURN] * SIMULATE_TASKS
    assert [start["memo"] for start in tier.client.starts] == [{QUEUE_PREFIX_MEMO: "h-real-"}]
    assert "verified" not in counters.extra
