"""What the activity tests share: a store that keeps uploads in memory, a simulate task input, the
context the activity is served with, and a task path the test scripts in place of the pool's own."""

import hashlib
from pathlib import Path

import pytest
from dsy_artifactclient.keys import attempt_key, uri_of
from dsy_artifactclient.staging import upload_tail
from dsy_events import format_time
from dsy_events.models import AttemptReference, ProducerRef
from dsy_keys import attempt_identity
from dsy_worker.activity import serving
from dsy_worker.pools import Context
from dsy_workflow.models import LogTailReference, TaskInput, TaskResult
from temporalio import activity

VECTOR = Path(__file__).resolve().parents[4] / "contracts/activity/vectors/task-input/valid"


class Store:
    """Keeps each attempt upload in `puts` by its key and returns its reference, as
    ArtifactClient.put_attempt does."""

    def __init__(self):
        self.puts: dict[str, bytes] = {}

    def put_attempt(self, source, kind, name, producer, tenant, batch, seed=None, segment=None,
                    deadline=None, progress=None):  # fmt: skip
        key = attempt_key(kind, name, producer, tenant, batch, seed, segment)
        self.puts[key] = source
        return AttemptReference(
            uri=uri_of(kind, key),
            sha256=hashlib.sha256(source).hexdigest(),
            size=len(source),
            name=name,
            kind=kind,
            producer=producer,
        )


def _succeed(task: TaskInput, context: Context) -> TaskResult:
    """The result of a simulate task path that ran: its results and its log tail, uploaded."""
    request = task.request
    producer = ProducerRef(
        job=request.jobid,
        task=request.taskid,
        attempt=attempt_identity(task.task_attempt, task.resume_counter),
    )
    result = context.store.put_attempt(
        b"<results/>", "result", "results.xml", producer, request.tenant, task.batch_id, task.seed
    )
    tail = upload_tail(context.store, b"ran\n", producer, request.tenant, task.batch_id, task.seed)
    return TaskResult(
        outputs=[result],
        log_tail=LogTailReference.model_validate(tail.model_dump()),
        started_at=format_time(activity.info().started_time),
    )


@pytest.fixture
def succeed():
    """Ends a scripted task path as a simulate path that ran does."""
    return _succeed


@pytest.fixture
def task() -> TaskInput:
    return TaskInput.model_validate_json((VECTOR / "simulate.json").read_text())


@pytest.fixture
def context(tmp_path) -> Context:
    """The context of a simulate worker, with its task scratch under the test's own directory and
    its TMPDIR beside it, served for the whole test."""
    context = Context(store=Store(), scratch=tmp_path / "task", slot_memory_mib=384)
    with serving(context, tmp_path / "tmp"):
        yield context


@pytest.fixture
def path(monkeypatch):
    """Sets the simulate task path to the coroutine function given."""
    import dsy_worker.pools.simulate

    return lambda run: monkeypatch.setattr(dsy_worker.pools.simulate, "run", run)
