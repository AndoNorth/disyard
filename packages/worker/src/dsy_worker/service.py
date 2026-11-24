"""The pool worker service: one activity worker on the pool's queue, one task at a time, which
drains on SIGTERM.

The queue is unversioned and the worker passes no deployment config, so it serves the workflows of
every build. On SIGTERM or SIGINT it removes the ready file, stops polling and gives the task in
flight DRAIN to finish. A task still running then is cancelled by the SDK as a worker shutdown,
which the activity stops and fails as `worker_shutdown`. The ready file is created once the worker
polls, and one left by a killed process is removed before the service connects.

The children of a task get TMPDIR under the scratch root, which the activity empties when a task
starts, so a stopped child's temporary files never outlive the next task.
"""

import asyncio
import logging
import os
import signal
import tempfile
from datetime import timedelta

from dsy_artifactclient.client import ArtifactClient
from dsy_obs.metrics import export_registry
from dsy_storecheck.s3 import load_credentials
from dsy_temporal import event_data_converter, tracing_interceptor
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.worker import Worker

from .activity import run_task, serving
from .config import Config
from .metrics import Metrics, runtime_of
from .pools import Context

STUBBED = ()

log = logging.getLogger(__name__)

# How long a task in flight at a stop may run on before it is stopped. An estimate.
DRAIN = timedelta(seconds=30)
# The directory of the scratch root that is the TMPDIR of the worker and its children.
TMP = "tmp"


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    """Serves `run_task` on the pool's queue until `stop` is set or the process gets SIGTERM or
    SIGINT, then drains. A namespace the server does not know fails before the ready file is
    created."""
    config.ready_file.unlink(missing_ok=True)
    tmp = config.scratch / TMP
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = tempfile.tempdir = str(tmp)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        runtime = runtime_of(os.environ)
        export_registry(runtime.metric_meter, f"{config.pool}-worker")
        client = await Client.connect(
            config.temporal_address,
            namespace=config.namespace,
            data_converter=event_data_converter,
            interceptors=[tracing_interceptor()],
            runtime=runtime,
        )
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=config.namespace)
        )
        # The store role of each pool is named after the pool.
        credentials = load_credentials(config.store_credentials)[config.pool]
        context = Context(
            store=ArtifactClient(config.store_endpoint, credentials),
            scratch=config.scratch,
            slot_memory_mib=config.slot_memory_mib,
            mirror=config.mirror,
        )
        worker = Worker(
            client,
            task_queue=config.queue,
            activities=[run_task],
            max_concurrent_activities=1,
            graceful_shutdown_timeout=DRAIN,
        )
        with serving(context, tmp, Metrics(runtime.metric_meter, config.pool)):
            log.info(
                "polling %s in namespace %s for pool %s, unversioned",
                config.queue,
                config.namespace,
                config.pool,
            )
            await _run_until(worker, stop, config)
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


async def _run_until(worker: Worker, stop: asyncio.Event, config: Config) -> None:
    running = asyncio.create_task(worker.run())
    stopped = asyncio.create_task(stop.wait())
    config.ready_file.touch()
    try:
        await asyncio.wait([running, stopped], return_when=asyncio.FIRST_COMPLETED)
    finally:
        config.ready_file.unlink(missing_ok=True)
    stopped.cancel()
    log.info("draining for at most %s", DRAIN)
    await worker.shutdown()
    await running
    log.info("drained")
