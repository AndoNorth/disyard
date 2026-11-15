"""The `run_task` activity of every pool: it dispatches on the task type to the pool's task path,
heartbeats while the path runs, and turns every end into a TaskResult or a failure class."""

from temporalio import activity
from wdf_workflow.contract import naming
from wdf_workflow.models import TaskInput, TaskResult

STUBBED = ("run_task",)

[POOL_ACTIVITY] = naming().activities.pool


@activity.defn(name=POOL_ACTIVITY.name)
async def run_task(task: TaskInput) -> TaskResult:
    raise NotImplementedError("not implemented: owned by lane A")
