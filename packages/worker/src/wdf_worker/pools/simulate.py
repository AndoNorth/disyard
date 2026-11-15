"""The simulate task path: stage the component, warm it up once per pod, run the guest under the
slot's cap and the task's deadline, and collect its results."""

from wdf_workflow.models import TaskInput, TaskResult

from . import Context

STUBBED = ("run",)


async def run(task: TaskInput, context: Context) -> TaskResult:
    raise NotImplementedError("not implemented: owned by lane T")
