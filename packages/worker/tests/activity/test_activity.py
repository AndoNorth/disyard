"""The activity outside Temporal: the failure class of an error the task path lets through, the
TMPDIR it empties, the heartbeat details of a beat, and the worker stage it reads through."""

import asyncio

import pytest
from dsy_artifactclient.errors import ArtifactNotFound, StoreUnavailable
from dsy_stages import Facts, run_stage_contract
from dsy_worker.activity import heartbeat_details, path_of, run_task
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

STARTED = "2026-11-15T00:00:00.000000Z"


def run(task):
    return asyncio.run(ActivityEnvironment().run(run_task, task))


@pytest.mark.parametrize(
    ("error", "deterministic"),
    [(StoreUnavailable("down"), False), (ArtifactNotFound("gone"), True)],
)
def test_artifact_error_is_its_code(context, task, path, error, deterministic):
    async def fails(task, context):
        raise error

    path(fails)
    with pytest.raises(ApplicationError) as raised:
        run(task)
    assert raised.value.type == error.code
    assert raised.value.non_retryable is deterministic
    [details] = raised.value.details
    assert details.reason == error.reason
    assert details.deterministic is deterministic
    assert details.log_tail is not None


def test_failure_class_of_the_path_passes_through(context, task, path):
    failure = ApplicationError("guest_failed: scripted", type="guest_failed", non_retryable=True)

    async def fails(task, context):
        raise failure

    path(fails)
    with pytest.raises(ApplicationError) as raised:
        run(task)
    assert raised.value is failure


def test_tmp_emptied_at_start_and_scratch_root_kept(context, task, path, succeed, tmp_path):
    """A stopped child's temporary files are gone when the next task starts, and what the scratch
    root keeps across tasks, the module cache among it, is left to the task paths."""
    kept = context.scratch / "modules" / "component"
    kept.parent.mkdir(parents=True)
    kept.write_text("compiled")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "left").write_text("by a stopped child")
    seen = {}

    async def runs(task, given):
        seen["tmp"] = list((tmp_path / "tmp").iterdir())
        seen["kept"] = kept.exists()
        return succeed(task, given)

    path(runs)
    run(task)
    assert seen == {"tmp": [], "kept": True}
    assert kept.read_text() == "compiled"


def test_each_task_starts_with_no_beat(context, task, path, succeed):
    beats = []

    async def runs(task, context):
        beats.append(context.beat)
        context.beat = {"progress": 0.5, "step": 1, "steps": 2}
        return succeed(task, context)

    path(runs)
    run(task)
    run(task)
    assert beats == [None, None]


@pytest.mark.parametrize(
    ("beat", "progress"),
    [
        (None, None),
        ({"progress": 0.5, "step": 1, "steps": 2}, 0.5),
        ({"progress": 0.5}, None),
        ({"progress": 2.0, "step": 1, "steps": 2}, None),
    ],
)
def test_heartbeat_details_of_a_beat(beat, progress):
    details = heartbeat_details(beat, STARTED)
    assert details.progress == progress
    assert details.checkpoint is None
    assert details.started_at == STARTED


def test_stage_contract_through_dispatch():
    def stage(inbound, facts: Facts) -> list:
        path_of(inbound)
        return []

    run_stage_contract("worker", stage)
