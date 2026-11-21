"""The simulate task path: stage the component, warm it up once per pod, run the guest under the
slot's cap and the task's deadline, and collect its results."""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError
from wft_artifactclient.errors import ArtifactError
from wft_artifactclient.staging import collect_outputs, stage_inputs, upload_tail
from wft_events import format_time
from wft_events.models import ProducerRef
from wft_keys import attempt_identity
from wft_runtime.classify import RESULTS, Classified, Facts, classify, read_error_file
from wft_runtime.command import INPUT_FILE, OUTPUT, CapRefused, command, warm_up
from wft_runtime.contract import canonical
from wft_runtime.run import _marker, copy_input, input_digest, warmed
from wft_workflow.contract import failure_classes
from wft_workflow.models import REASON_MAX, FailureDetails, LogTailReference, TaskInput, TaskResult

from .. import read_request
from ..runner import Child, Exit
from . import Context

STUBBED = ()

# What the slot keeps back for the worker itself: its peak while it collects a result at the
# staging cap, plus a quarter, in whole 64 MiB steps (spike S11).
WORKER_RESERVE_MIB = 256
# The end of the task's time that is kept for collecting its outputs and uploading its tail. An
# estimate.
UPLOAD_RESERVE = timedelta(seconds=30)
# The shortest guest timeout, so a task whose time is spent still runs and is classified a timeout.
MIN_TIMEOUT_S = 0.001
# Directories of the scratch root: the task's own, emptied around each task, the warm-up's, and
# the module cache, which lives as long as the pod.
TASK = "task"
WARM_UP = "warm-up"
MODULES = "modules"


async def run(task: TaskInput, context: Context) -> TaskResult:
    info = activity.info()
    started_at = format_time(info.started_time)
    # Temporal starts the start-to-close clock when the activity starts, so time spent waiting in
    # the queue never counts against the guest, unlike the request's deadline.
    deadline = info.started_time + info.start_to_close_timeout - UPLOAD_RESERVE
    request = read_request(task.request)
    producer = ProducerRef(
        job=request.jobid,
        task=request.taskid,
        attempt=attempt_identity(task.task_attempt, task.resume_counter),
    )
    where = (producer, request.tenant, task.batch_id, task.seed)
    work = context.scratch / TASK
    shutil.rmtree(work, ignore_errors=True)
    tail, undeclared, outputs = b"", [], []
    try:
        try:
            staged = await asyncio.to_thread(
                stage_inputs, context.store, request.inputs, work, deadline=_left(deadline)
            )
            [component] = staged.paths.values()
            input_file = staged.directory / INPUT_FILE
            input_file.write_text(canonical(task.params.model_dump(mode="json")))
            memory_mib = context.slot_memory_mib - WORKER_RESERVE_MIB
            classified, tail = await _guest(
                component, input_file, work, context, memory_mib, deadline
            )
            if classified.cls == "ok":
                collected = await asyncio.to_thread(
                    collect_outputs,
                    context.store,
                    work,
                    request.task_type,
                    *where,
                    deadline=_left(deadline + UPLOAD_RESERVE),
                )
                outputs, undeclared = collected.references, collected.undeclared
        except ArtifactError as error:
            classified = _artifact(error)
        try:
            log_tail = await asyncio.to_thread(
                upload_tail,
                context.store,
                tail,
                *where,
                undeclared=undeclared,
                deadline=_left(deadline + UPLOAD_RESERVE),
            )
            log_tail = LogTailReference.model_validate(log_tail.model_dump())
        except ArtifactError as error:
            if classified.cls == "ok":
                classified = _artifact(error)
            log_tail = None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if classified.cls == "ok":
        return TaskResult(outputs=outputs, log_tail=log_tail, started_at=started_at)
    details = FailureDetails(
        deterministic=classified.deterministic,
        reason=classified.reason[:REASON_MAX],
        started_at=started_at,
        checkpoint=None,
        log_tail=log_tail,
    )
    raise ApplicationError(
        details.reason, details, type=classified.cls, non_retryable=classified.deterministic
    )


async def _guest(
    component: Path,
    input_file: Path,
    work: Path,
    context: Context,
    memory_mib: int,
    deadline: datetime,
) -> tuple[Classified, bytes]:
    """Runs the guest once the module cache holds the component, and its class and log tail."""
    try:
        # Refused before any warm-up, since no warm-up can make room for the guest.
        command(component, work, memory_mib, None)
    except CapRefused as refused:
        return classify(None, "", None, Facts(cap_refused=str(refused))), b""
    cache = context.scratch / MODULES
    try:
        if not warmed(component, cache):
            await _warm(component, context.scratch / WARM_UP, cache)
        timeout_s = max(_left(deadline), MIN_TIMEOUT_S)
        digest = copy_input(input_file, work)
        (work / OUTPUT).mkdir(parents=True, exist_ok=True)
        argv = command(component, work, memory_mib, timeout_s, cache)
        exit, tail = await _run(argv, work, context)
    except OSError as error:
        return classify(None, "", None, Facts(host_error=str(error))), b""
    results = work / OUTPUT / RESULTS["file"]
    facts = Facts(
        oom_event=exit.oom,
        input_modified=input_digest(work) != digest,
        results_bytes=results.stat().st_size if results.is_file() else None,
    )
    code = -exit.signal if exit.code is None else exit.code
    text = tail.decode(errors="replace")
    return classify(code, text, read_error_file(work), facts), tail


async def _warm(component: Path, scratch: Path, cache: Path) -> None:
    """The warm-up in its own process, recorded as warm only when it succeeds, so a failed one is
    repeated by the next task and this task's guest compiles and reports its own failure."""
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        exit, _ = await _run(warm_up(component, scratch, cache), scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if exit.code == 0:
        marker = _marker(component, cache)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()


async def _run(argv: list[str], cwd: Path, context: Context | None = None) -> tuple[Exit, bytes]:
    """Runs a child to its end, handing each beat it prints to `context` when given, and stops
    its group if the task ends first, as on a cancel."""
    child = await Child.start(argv, cwd)
    try:
        async for _ in child.lines():
            if context is not None:
                context.beat = child.beat
        return await child.wait(), child.tail
    except BaseException:
        await child.stop()
        raise
    finally:
        if context is not None:
            context.beat = None


def _artifact(error: ArtifactError) -> Classified:
    return Classified(error.code, failure_classes()[error.code].deterministic, error.reason)


def _left(deadline: datetime) -> float:
    return (deadline - datetime.now(UTC)).total_seconds()
