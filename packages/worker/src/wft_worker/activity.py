"""The `run_task` activity of every pool: it dispatches on the task type to the pool's task path,
heartbeats while the path runs, and turns every end into a TaskResult or a failure class."""

import asyncio
import importlib
import logging
import shutil
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError
from wft_artifactclient.errors import ArtifactError
from wft_artifactclient.staging import upload_tail
from wft_events import format_time
from wft_events.models import ProducerRef
from wft_keys import attempt_identity
from wft_obs import logs
from wft_workflow.contract import failure_classes, naming
from wft_workflow.models import (
    REASON_MAX,
    FailureDetails,
    HeartbeatDetails,
    LogTailReference,
    TaskInput,
    TaskResult,
)

from . import read_request
from .metrics import OK, Metrics
from .pools import Context

STUBBED = ()

log = logging.getLogger(__name__)

[POOL_ACTIVITY] = naming().activities.pool
# The longest wait between two heartbeats, shortened to a third of a shorter heartbeat timeout.
HEARTBEAT_EVERY_S = 10.0

_SERVING: ContextVar[tuple[Context, Path, Metrics | None]] = ContextVar("serving")


@contextmanager
def serving(context: Context, tmp: Path, metrics: Metrics | None = None) -> Iterator[None]:
    """Every `run_task` started inside runs with a copy of `context` of its own. `tmp`, the TMPDIR
    of the task's children, is emptied when a task starts, since a stopped child leaves its files
    behind. The scratch root is not, because it holds what outlives a task, the module cache among
    it, and each task path empties its own directory of it. Each task counts into `metrics`, when
    given."""
    token = _SERVING.set((context, tmp, metrics))
    try:
        yield
    finally:
        _SERVING.reset(token)


@activity.defn(name=POOL_ACTIVITY.name)
async def run_task(task: TaskInput) -> TaskResult:
    """Runs the task and ends with one outcome log line and one count under its exit class, which
    is `ok` or the failure class of the error that ended it."""
    request = read_request(task.request)
    began = time.monotonic()
    exit_class = "cancelled"
    with logs.bind(
        tenant=request.tenant,
        job_id=request.jobid,
        task_id=request.taskid,
        attempt_id=attempt_identity(task.task_attempt, task.resume_counter),
        pool=request.task_type,
        traceparent=request.traceparent,
    ):
        try:
            result = await _run(task)
            exit_class = OK
            return result
        except ApplicationError as error:
            exit_class = error.type or "unclassified"
            raise
        finally:
            seconds = time.monotonic() - began
            metrics = _SERVING.get()[2]
            if metrics is not None:
                metrics.task_ended(exit_class, seconds)
            level = logging.INFO if exit_class == OK else logging.WARNING
            log.log(
                level,
                "task ended: %s after %.1f s",
                exit_class,
                seconds,
                extra={"exit_class": exit_class},
            )


async def _run(task: TaskInput) -> TaskResult:
    served, tmp, metrics = _SERVING.get()
    context = replace(served, beat=None)
    started_at = format_time(activity.info().started_time)
    context.scratch.mkdir(parents=True, exist_ok=True)
    _empty(tmp)
    beating = asyncio.create_task(_heartbeat(context, started_at, metrics))
    try:
        return await path_of(task.request)(task, context)
    except asyncio.CancelledError as error:
        # The SDK cancels a task still running at the end of a drain. Re-raised, that would be
        # reported as a cancel, which withdraws the attempt, so it fails as the shutdown it is.
        cancelled = activity.cancellation_details()
        if cancelled is None or not cancelled.worker_shutdown:
            raise
        reason = "worker_shutdown: drain: the worker stopped before the task ended"
        raise await _failure(context, task, "worker_shutdown", reason, started_at, error) from None
    except ApplicationError:
        raise
    except ArtifactError as error:
        raise await _failure(context, task, error.code, error.reason, started_at, error) from error
    except Exception as error:
        reason = f"worker_shutdown: worker-error: {type(error).__name__}: {error}"
        raise await _failure(context, task, "worker_shutdown", reason, started_at, error) from error
    finally:
        beating.cancel()


def path_of(event):
    """The task path of the request's task type, read as the worker stage declares."""
    task_type = read_request(event).task_type
    if task_type not in naming().pools:
        raise ValueError(f"no task path for the task type {task_type!r}")
    return importlib.import_module(f"{__package__}.pools.{task_type}").run


def heartbeat_details(beat: dict | None, started_at: str) -> HeartbeatDetails:
    """The latest progress beat, or nulls before the first beat or for one that does not fit."""
    if beat is not None:
        try:
            return HeartbeatDetails(
                progress=beat["progress"],
                step=beat["step"],
                steps=beat["steps"],
                checkpoint=None,
                started_at=started_at,
            )
        except (KeyError, ValueError):
            pass
    return HeartbeatDetails(
        progress=None, step=None, steps=None, checkpoint=None, started_at=started_at
    )


async def _heartbeat(context: Context, started_at: str, metrics: Metrics | None) -> None:
    """Beats from the start of the task to its end, whatever the child prints, because a cancel
    reaches the activity only in the reply to a heartbeat. A beat is recorded as the lag since the
    one before and never logged."""
    timeout = activity.info().heartbeat_timeout
    every = min(HEARTBEAT_EVERY_S, timeout.total_seconds() / 3) if timeout else HEARTBEAT_EVERY_S
    last = None
    while True:
        activity.heartbeat(heartbeat_details(context.beat, started_at))
        now = time.monotonic()
        if metrics is not None and last is not None:
            metrics.heartbeat_lag(now - last)
        last = now
        await asyncio.sleep(every)


async def _failure(
    context: Context,
    task: TaskInput,
    failure: str,
    reason: str,
    started_at: str,
    error: Exception,
) -> ApplicationError:
    """The ApplicationError of a failure class, with the error's traceback as the log tail and
    the reason cut to its bound."""
    deterministic = failure_classes()[failure].deterministic is True
    details = FailureDetails(
        deterministic=deterministic,
        reason=reason[:REASON_MAX],
        started_at=started_at,
        checkpoint=None,
        log_tail=await _log_tail(context, task, "".join(traceback.format_exception(error))),
    )
    return ApplicationError(details.reason, details, type=failure, non_retryable=deterministic)


async def _log_tail(context: Context, task: TaskInput, text: str) -> LogTailReference | None:
    """The uploaded tail of the attempt, None when the upload fails."""
    request = task.request
    producer = ProducerRef(
        job=request.jobid,
        task=request.taskid,
        attempt=attempt_identity(task.task_attempt, task.resume_counter),
    )
    try:
        reference = await asyncio.to_thread(
            upload_tail,
            context.store,
            text.encode(),
            producer,
            request.tenant,
            task.batch_id,
            task.seed,
        )
    except Exception:
        return None
    return LogTailReference.model_validate(reference.model_dump())


def _empty(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
