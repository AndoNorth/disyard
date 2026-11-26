"""The workers of one workflow test: the workflow worker, the control worker and the pool fakes."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dsy_events import Event
from dsy_jobdoc.plan_models import Plan
from dsy_keys import workflow_id
from dsy_temporal import tracing_interceptor
from temporalio.client import Client
from temporalio.worker import Worker

from ..contract import naming
from ..models import TaskInput
from .builders import JOB_PARAMS, accepted_event
from .control import FakeControl, LoadedPlan
from .pool import FakePool, Succeed

Script = Callable[[TaskInput], Any]
DONE = ("succeeded", "partial", "failed", "cancelled", "terminated")


def control_worker(
    client: Client, control: FakeControl, build_id: str | None = None, **options
) -> Worker:
    """The worker of the control queue of `build_id`, or of the build in the environment, over
    the control activities of `control`."""
    from ..taskpath import control_queue

    queue = naming().control_queue.replace("<build id>", build_id) if build_id else control_queue()
    return Worker(client, task_queue=queue, activities=control.control_activities(), **options)


@dataclass
class Rig:
    """The workers of one test and what they were given."""

    client: Client
    control: FakeControl
    placer: Any
    pools: dict[str, FakePool] = field(default_factory=dict)
    # The pools whose worker starts only when the test serves them.
    idle: dict[str, FakePool] = field(default_factory=dict)

    async def start(
        self, plan: Plan, event: Event | None = None, max_history_events: int = 10_000
    ) -> Any:
        """Starts the job of `plan` as admission would, and returns its handle."""
        from ..job import JobWorkflow

        event = event or accepted_event()
        # FakeControl holds one job row, so the row of a job that ended is claimed again for the
        # next one, as admission claims each new row.
        if self.control.status in DONE:
            self.control.status = "admitted"
        self.control.plans[event.data["manifest"]["uri"]] = LoadedPlan(
            plan=plan, max_history_events=max_history_events
        )
        return await self.client.start_workflow(
            JobWorkflow.run,
            args=[event, JOB_PARAMS],
            id=workflow_id(event.jobid),
            task_queue=naming().workflow.queue,
        )

    async def serve(self, pool: str) -> None:
        """Starts the worker of an idle pool."""
        self.pools[pool] = await self.idle.pop(pool).__aenter__()


class Running:
    """The async context of a Rig: its workers start in order, its build is made current once
    they poll, and they stop in the reverse order, the pools served late first."""

    def __init__(self, this: Rig, workers: list, build_id: str, timeout_s: float):
        self.this = this
        self.workers = workers
        self.build_id = build_id
        self.timeout_s = timeout_s

    async def __aenter__(self) -> Rig:
        from ..versioning import set_current

        for worker in self.workers:
            await worker.__aenter__()
        await set_current(self.this.client, self.build_id, self.timeout_s)
        return self.this

    async def __aexit__(self, *exc) -> None:
        served = [p for p in self.this.pools.values() if p not in self.workers]
        for worker in reversed([*self.workers, *served]):
            await worker.__aexit__(*exc)


def rig(
    client: Client,
    *,
    build_id: str,
    placer: Any,
    control: FakeControl | None = None,
    scripts: dict[str, Script] | None = None,
    idle: frozenset[str] = frozenset(),
    timeout_s: float = 60,
) -> Running:
    """An async context of a Rig whose pools run `scripts`, each succeeding by default, the pools
    of `idle` waiting to be served, and whose workers carry the tracing interceptor of
    dsy_temporal.

    The placer is the caller's, since this package never builds one: the harness owns the one
    place that pairs the workflow worker with StaticPlacer.
    """
    from ..worker import workflow_worker

    scripts = {pool: lambda task: Succeed() for pool in naming().pools} | (scripts or {})
    this = Rig(client, control or FakeControl(), placer)
    workflow = workflow_worker(
        client, build_id, placer, this.control.emit, interceptors=[tracing_interceptor()]
    )
    control_side = control_worker(
        client, this.control, build_id, interceptors=[tracing_interceptor()]
    )
    pools = {pool: FakePool(client, pool, script) for pool, script in scripts.items()}
    this.pools = {pool: fake for pool, fake in pools.items() if pool not in idle}
    this.idle = {pool: fake for pool, fake in pools.items() if pool in idle}
    return Running(this, [workflow, control_side, *this.pools.values()], build_id, timeout_s)
