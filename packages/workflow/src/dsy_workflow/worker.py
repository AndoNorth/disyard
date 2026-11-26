"""The workflow worker of one build, built the same way wherever it runs: the service, the harness
and the test rigs."""

from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import cache

from temporalio.client import Client
from temporalio.common import WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from .contract import naming
from .job import JobWorkflow
from .placement import Placer, place_activity

# The threads of the workflow worker's blocking activities, and so its database connections and
# its calls to the placement service at once.
THREADS = 8
THREAD_PREFIX = "dsy-workflow-activity"


@cache
def executor() -> ThreadPoolExecutor:
    """The one executor of every workflow worker of the process, created on first use."""
    return ThreadPoolExecutor(THREADS, thread_name_prefix=THREAD_PREFIX)


def workflow_worker(
    client: Client, build_id: str, placer: Placer, emit: Callable, **options
) -> Worker:
    """The versioned worker of the workflow queue as build `build_id`, running JobWorkflow, the
    `place` local activity over `placer` and `emit` on the process's executor.

    Every other Worker option passes through `options`, `workflows` included, but never an
    executor, which this module owns.
    """
    return Worker(
        client,
        task_queue=naming().workflow.queue,
        **{"workflows": [JobWorkflow], **options},
        activities=[place_activity(placer), emit],
        activity_executor=executor(),
        max_concurrent_local_activities=THREADS,
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(naming().workflow.deployment, build_id),
            use_worker_versioning=True,
        ),
    )


def control_worker(
    client: Client, build_id: str, activities: Sequence[Callable], executor: Executor, **options
) -> Worker:
    """The worker of build `build_id`'s control queue, running the control `activities` on
    `executor`, one at a time per thread of it. Every other Worker option passes through `options`.
    """
    return Worker(
        client,
        task_queue=naming().control_queue.replace("<build id>", build_id),
        activities=activities,
        activity_executor=executor,
        max_concurrent_activities=THREADS,
        **options,
    )
