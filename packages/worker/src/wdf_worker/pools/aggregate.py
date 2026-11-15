"""The aggregate task path: run the aggregate library over the index of its inputs, and collect
its summary."""

from wdf_workflow.models import TaskInput, TaskResult

from . import Context

STUBBED = ("run",)


async def run(task: TaskInput, context: Context) -> TaskResult:
    raise NotImplementedError("not implemented: owned by lane T")
