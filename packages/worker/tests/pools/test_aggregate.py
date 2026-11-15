"""The aggregate task path with SeaweedFS: the aggregate library runs over an index of level 0
results, its progress lines reach the heartbeat state, and each way it ends maps to its class."""

import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from wdf_artifactclient.aggregate import put_index
from wdf_artifactclient.client import ArtifactClient
from wdf_artifactclient.models import IndexEntry
from wdf_events.models import ProducerRef
from wdf_storecheck.s3 import dump_credentials
from wdf_worker.config import ENV
from wdf_worker.pools import Context, aggregate
from wdf_workflow.contract import check_task_input, check_task_result
from wdf_workflow.models import REASON_MAX, TaskInput
from wdf_workloads import main

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[4]
VECTOR = ROOT / "contracts/activity/vectors/task-input/valid/aggregate.json"
ECHO = ROOT / "contracts/workload/vectors/level0/echo/input.json"


@dataclasses.dataclass
class Recording(Context):
    """A context that keeps every beat the path sets, as the activity would heartbeat them."""

    beats: list = dataclasses.field(default_factory=list)

    def __setattr__(self, name, value):
        if name == "beat" and value is not None:
            self.beats.append(value)
        super().__setattr__(name, value)


@pytest.fixture
def context(pool_store, tmp_path, monkeypatch) -> Recording:
    credentials = tmp_path / "store.json"
    dump_credentials(credentials, {"aggregate": pool_store.credentials["aggregate"]})
    monkeypatch.setenv(ENV["store_endpoint"], pool_store.endpoint)
    monkeypatch.setenv(ENV["store_credentials"], str(credentials))
    return Recording(
        store=ArtifactClient(pool_store.endpoint, pool_store.credentials["aggregate"]),
        scratch=tmp_path / "scratch",
        slot_memory_mib=1024,
    )


@pytest.fixture
def task(pool_store, tmp_path):
    """The aggregate task input over the index of level 0 echo results, one per (task, result
    bytes) given, put as the platform puts them."""

    def make(results: dict[str, bytes]) -> TaskInput:
        vector = json.loads(VECTOR.read_text())
        vector["batch_id"] = str(uuid.uuid4())
        request = vector["request"]

        def role(name: str) -> ArtifactClient:
            return ArtifactClient(pool_store.endpoint, pool_store.credentials[name])

        entries = []
        for name, body in sorted(results.items()):
            producer = ProducerRef(job=request["jobid"], task=name, attempt="r0a1")
            reference = role("simulate").put_attempt(
                body, "result", "results.xml", producer, request["tenant"], vector["batch_id"], 0
            )
            entries.append(IndexEntry(task=name, status="succeeded", reference=reference))
        header = {
            "job": request["jobid"],
            "tenant": request["tenant"],
            "task": request["taskid"],
            "partial": "refuse",
        }
        index = put_index(role("indexer"), entries, header)
        request["data"]["inputs"] = [index.model_dump(mode="json")]
        vector["params"] = {"aggregation": "digest_list"}
        given = TaskInput.model_validate(vector)
        check_task_input(given)
        return given

    return make


@pytest.fixture(scope="module")
def echo(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("echo")
    (out / "input.json").write_bytes(ECHO.read_bytes())
    assert main.execute(out / "input.json", out) == 0
    return (out / "results.xml").read_bytes()


def environment(start_to_close_s: float = 300) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        started_time=datetime.now(UTC),
        start_to_close_timeout=timedelta(seconds=start_to_close_s),
    )
    return env


def failure(task, context, env=None) -> ApplicationError:
    with pytest.raises(ApplicationError) as raised:
        asyncio.run((env or environment()).run(aggregate.run, task, context))
    error = raised.value
    [details] = error.details
    assert error.type == details.reason.split(":")[0]
    assert len(details.reason) <= REASON_MAX
    assert details.log_tail is not None
    return error


def test_digest_list_over_two_results(task, context, echo, pool_store):
    given = task({"sim-0": echo, "sim-1": echo})
    result = asyncio.run(environment().run(aggregate.run, given, context))
    check_task_result(result)
    [summary] = result.outputs
    assert summary.kind == "summary"
    reader = ArtifactClient(pool_store.endpoint, pool_store.credentials["workflow"])
    document = json.loads(reader.get_bytes(summary, summary.size))
    assert [digest["task"] for digest in document["digests"]] == ["sim-0", "sim-1"]
    assert document["missing"] == []
    tail = reader.get_bytes(result.log_tail, result.log_tail.size).decode()
    assert "summary: " in tail


def test_malformed_block_aggregate_parse_failed(task, context, echo):
    broken = echo.replace(b'<block id="args" kind="echo">', b'<block kind="echo">')
    error = failure(task({"sim-0": echo, "sim-1": broken}), context)
    assert error.type == "aggregate_parse_failed"
    assert error.non_retryable
    assert error.details[0].deterministic
    assert error.details[0].reason.startswith("aggregate_parse_failed: sim-1: ")


def test_deadline_aggregate_timeout(task, context, echo):
    """No time is left once the upload reserve is kept back, so the runner stops the aggregate."""
    env = environment(aggregate.UPLOAD_RESERVE_S)
    error = failure(task({"sim-0": echo}), context, env)
    assert error.type == "aggregate_timeout"
    assert not error.non_retryable
    assert not error.details[0].deterministic


def test_progress_lines_reach_heartbeat_state(task, context, echo):
    asyncio.run(environment().run(aggregate.run, task({"sim-0": echo, "sim-1": echo}), context))
    steps = [(beat["step"], beat["steps"]) for beat in context.beats]
    assert steps[0] == (1, 2)
    assert steps[-1] == (2, 2)
    assert context.beats[-1] == {"progress": 1.0, "step": 2, "steps": 2}
    # The aggregate has ended, so the upload that follows beats with no progress.
    assert context.beat is None
    assert not (context.scratch / aggregate.TASK).exists()
