"""The aggregate task path: run the aggregate library over the index of its inputs, and collect
its summary."""

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from datetime import UTC, datetime

from dsy_aggregate.reduce import SUMMARY_NAME
from dsy_aggregate.run import EXIT_CODES
from dsy_artifactclient.errors import ArtifactError
from dsy_artifactclient.staging import collect_outputs, upload_tail
from dsy_events import format_time
from dsy_events.models import ProducerRef
from dsy_keys import attempt_identity
from dsy_workflow.contract import failure_classes
from dsy_workflow.models import REASON_MAX, FailureDetails, LogTailReference, TaskInput, TaskResult
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import read_request
from ..config import ENV
from ..runner import Child
from . import Context

STUBBED = ()

# Kept back from the activity's start-to-close timeout for the uploads after the aggregate.
UPLOAD_RESERVE_S = 30.0
# The store role of the pool, the one identity its credentials file holds.
ROLE = "aggregate"
REASON = "reason: "
# The task's own directory of the scratch root, emptied around each task.
TASK = "task"
# The directory of the task's own where the aggregate writes its summary.
OUTPUT = "output"
CLASSES = {code: cls for cls, code in EXIT_CODES.items()}


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
    [index] = [reference for reference in request.inputs if reference["kind"] == "index"]
    work = context.scratch / TASK
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        index_file = work / "index.json"
        index_file.write_text(json.dumps(index))
        argv = [sys.executable, "-m", "dsy_aggregate", "run", "--index", str(index_file)]
        argv += ["--tenant", request.tenant, "--aggregation", task.params.aggregation]
        argv += ["--args", json.dumps(task.params.args.model_dump(exclude_none=True))]
        argv += ["--out", str(work / OUTPUT)]
        # The pod's own store identity, which the service reads from the same variables.
        argv += ["--endpoint", os.environ[ENV["store_endpoint"]]]
        argv += ["--credentials", os.environ[ENV["store_credentials"]], "--role", ROLE]
        child = await Child.start(argv, work)
        try:
            try:
                async with asyncio.timeout(_left(deadline)):
                    async for _ in child.lines():
                        context.beat = child.beat
            except TimeoutError:
                await child.stop()
                raise _Failed(
                    "aggregate_timeout", "aggregate_timeout: ran past the deadline"
                ) from None
            except BaseException:
                await child.stop()
                raise
            finally:
                context.beat = None
            ended = await child.wait()
            if ended.code != 0:
                raise _failure(ended, child.tail)
            collected = await asyncio.to_thread(
                collect_outputs, context.store, work, "aggregate", *where, deadline=_left(end)
            )
        except (_Failed, ArtifactError) as failure:
            cls = failure.cls if isinstance(failure, _Failed) else failure.code
            deterministic = failure_classes()[cls].deterministic is True
            try:
                log_tail = await _upload_tail(context, child.tail, where, end)
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
        log_tail = await _upload_tail(context, child.tail, where, end, collected.undeclared)
        count_summary(context, work / OUTPUT)
        return TaskResult(outputs=collected.references, log_tail=log_tail, started_at=started_at)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def count_summary(context: Context, out_dir) -> None:
    """Counts the result files the summary in `out_dir` was reduced from, which are its inputs
    less the missing ones, and their rows."""
    if context.metrics is None:
        return
    summary = json.loads((out_dir / SUMMARY_NAME).read_text())
    context.metrics.aggregated(summary["inputs"] - len(summary["missing"]), summary["rows"])


def _failure(ended, tail: bytes) -> _Failed:
    """The class of a failed aggregate: the OOM fact first, then the library's own codes, and
    anything else, a usage error or a kill the worker did not send, as worker_shutdown."""
    if ended.oom:
        return _Failed("aggregate_oom", "aggregate_oom: ended after an OOM kill")
    if ended.signal is not None:
        name = signal.Signals(ended.signal).name
        return _Failed("worker_shutdown", f"worker_shutdown: aggregate-signal: got {name}")
    lines = [line.strip() for line in tail.decode(errors="replace").splitlines() if line.strip()]
    reasons = [line.removeprefix(REASON) for line in lines if line.startswith(REASON)]
    if ended.code in CLASSES:
        cls = CLASSES[ended.code]
        return _Failed(cls, reasons[-1] if reasons else f"{cls}: exit {ended.code}")
    detail = lines[-1] if lines else f"exit {ended.code}"
    return _Failed("worker_shutdown", f"worker_shutdown: aggregate-library-error: {detail}")


async def _upload_tail(context: Context, tail: bytes, where, end: float, undeclared=()):
    reference = await asyncio.to_thread(
        upload_tail, context.store, tail, *where, undeclared=undeclared, deadline=_left(end)
    )
    return LogTailReference.model_validate(reference.model_dump())


def _left(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)
