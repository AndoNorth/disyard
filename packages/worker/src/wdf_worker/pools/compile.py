"""The compile task path: stage the source bundle, fetch its locked dependencies, build the
component offline, and collect the component and its build info."""

from wdf_workflow.models import TaskInput, TaskResult

from . import Context

STUBBED = ("run",)


async def run(task: TaskInput, context: Context) -> TaskResult:
    raise NotImplementedError("not implemented: owned by lane T")
