"""The compile task path: stage the source bundle, fetch its locked dependencies, build the
component offline, and collect the component and its build info."""

import asyncio
import shutil
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError
from wft_artifactclient.errors import ArtifactError
from wft_artifactclient.staging import collect_outputs, stage_inputs, upload_tail
from wft_compile.build import EXIT_CODES
from wft_compile.limits import classify
from wft_events import format_time
from wft_events.models import ProducerRef
from wft_keys import attempt_identity
from wft_workflow.contract import failure_classes
from wft_workflow.models import REASON_MAX, FailureDetails, LogTailReference, TaskInput, TaskResult

from .. import read_request
from ..runner import Child, Exit
from . import Context

STUBBED = ()

# Kept back from the activity's start-to-close timeout for the uploads after the build.
UPLOAD_RESERVE_S = 30.0
# How long a step may run past the deadline it was given before the runner stops it.
LATE_S = 5.0
# The task's own directory of the scratch root, emptied around each task.
TASK = "task"
WHEELS = "wheels"


class _Failed(Exception):
    def __init__(self, cls: str, reason: str):
        super().__init__(reason)
        self.cls = cls
        self.reason = reason[:REASON_MAX]


async def run(task: TaskInput, context: Context) -> TaskResult:
    info = activity.info()
    started_at = format_time(info.started_time)
    left = (info.started_time + info.start_to_close_timeout - datetime.now(UTC)).total_seconds()
    end = time.monotonic() + left
    deadline = end - UPLOAD_RESERVE_S
    request = read_request(task.request)
    producer = ProducerRef(
        job=request.jobid,
        task=request.taskid,
        attempt=attempt_identity(task.task_attempt, task.resume_counter),
    )
    where = (producer, request.tenant, task.batch_id, task.seed)
    source = task.params.source
    tail = b""
    work = context.scratch / TASK
    shutil.rmtree(work, ignore_errors=True)
    try:
        try:
            staged = await asyncio.to_thread(
                stage_inputs, context.store, [source], work, _left(deadline)
            )
            bundle = staged.paths[source.name]
            wheels = work / WHEELS
            fetch = ["-m", "wft_compile.limits", "fetch", "--bundle", str(bundle)]
            fetch += ["--find-links", context.mirror, "--out", str(wheels)]
            build = ["-m", "wft_compile", "build", "--bundle", str(bundle)]
            build += ["--entry", task.params.entry, "--out", str(work / "output")]
            build += ["--find-links", str(wheels)]
            for step in (fetch, build):
                ended, step_tail = await _step([sys.executable, *step], work, deadline)
                tail += step_tail
                if ended is None or ended.code != 0:
                    raise _failure(" ".join(step[1:3]), ended, step_tail)
            collected = await asyncio.to_thread(
                collect_outputs, context.store, work, "compile", *where, deadline=_left(end)
            )
        except (_Failed, ArtifactError) as failure:
            cls = failure.cls if isinstance(failure, _Failed) else failure.code
            deterministic = failure_classes()[cls].deterministic is True
            try:
                log_tail = await _upload_tail(context, tail, where, end)
            except ArtifactError:
                log_tail = None
            details = FailureDetails(
                deterministic=deterministic,
                reason=failure.reason,
                started_at=started_at,
                checkpoint=None,
                log_tail=log_tail,
            )
            raise ApplicationError(
                details.reason, details, type=cls, non_retryable=deterministic
            ) from failure
        log_tail = await _upload_tail(context, tail, where, end, collected.undeclared)
        return TaskResult(outputs=collected.references, log_tail=log_tail, started_at=started_at)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _step(argv: list[str], cwd: Path, deadline: float) -> tuple[Exit | None, bytes]:
    """Runs one step with `--timeout` of the time left, and its exit, or None when the runner
    stopped it for running past the deadline. Any other end of the wait, a cancel among them,
    stops the step's whole process group."""
    child = await Child.start([*argv, "--timeout", f"{_left(deadline):.3f}"], cwd)
    try:
        async with asyncio.timeout(_left(deadline) + LATE_S):
            async for _ in child.lines():
                pass
    except TimeoutError:
        await child.stop()
        return None, child.tail
    except BaseException:
        await child.stop()
        raise
    return await child.wait(), child.tail


def _failure(step: str, ended: Exit | None, tail: bytes) -> _Failed:
    """The class of a failed step: the OOM fact first, then the library's own codes, and anything
    else, more likely a fault of the platform than of the source, as worker_shutdown."""
    if ended is None:
        return _Failed("compile_timeout", f"compile_timeout: {step} ran past the deadline")
    if ended.oom:
        return _Failed("compile_oom", f"compile_oom: {step} ended after an OOM kill")
    if ended.signal is not None:
        name = signal.Signals(ended.signal).name
        return _Failed("worker_shutdown", f"worker_shutdown: compile-signal: {step} got {name}")
    text = tail.decode(errors="replace")
    if ended.code in EXIT_CODES.values():
        classified = classify(ended.code, text)
        return _Failed(classified.cls, classified.reason)
    lines = [line for line in text.splitlines() if line.strip()]
    detail = lines[-1].strip() if lines else f"{step} exit {ended.code}"
    return _Failed("worker_shutdown", f"worker_shutdown: compile-library-error: {detail}")


async def _upload_tail(context: Context, tail: bytes, where, end: float, undeclared=()):
    reference = await asyncio.to_thread(
        upload_tail, context.store, tail, *where, undeclared=undeclared, deadline=_left(end)
    )
    return LogTailReference.model_validate(reference.model_dump())


def _left(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)
