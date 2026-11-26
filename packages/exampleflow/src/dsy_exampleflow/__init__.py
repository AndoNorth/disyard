"""The worked example of a workflow module: one workflow and one activity, added to the workflow
service through the entry-point group `dsy.workflow.modules` with no edit of a core package."""

from datetime import timedelta

from dsy_workflow.modules import Context
from temporalio import activity, workflow
from temporalio.worker import Worker

__all__ = ["TASK_QUEUE", "EchoWorkflow", "echo", "workers"]

TASK_QUEUE = "dsy-exampleflow"


@activity.defn
async def echo(text: str) -> str:
    return text


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, text: str) -> dict:
        echoed = await workflow.execute_activity(
            echo, text, start_to_close_timeout=timedelta(seconds=30)
        )
        return {"text": echoed, "length": len(echoed)}


def workers(ctx: Context) -> list[Worker]:
    return [
        Worker(
            ctx.client,
            task_queue=TASK_QUEUE,
            workflows=[EchoWorkflow],
            activities=[echo],
            graceful_shutdown_timeout=ctx.drain,
        )
    ]
