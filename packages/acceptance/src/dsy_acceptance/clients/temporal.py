"""A Temporal client of the namespace of the naming contract, with the payload converter the
workflows use."""

from collections import Counter

import dsy_keys
from dsy_workflow import testing
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, WorkflowExecutionDescription


async def connect(address: str) -> Client:
    """One attempt, so a caller's deadline is not spent on a server that does not answer."""
    return await testing.connect(address, attempts=1)


async def describe(client: Client, job_id: str) -> WorkflowExecutionDescription:
    """The execution of the job's workflow, which raises RPCError when none is known."""
    return await client.get_workflow_handle(dsy_keys.workflow_id(job_id)).describe()


async def history(client: Client, job_id: str) -> list[HistoryEvent]:
    """Every event of the job's workflow history, from the first."""
    handle = client.get_workflow_handle(dsy_keys.workflow_id(job_id))
    return [event async for event in handle.fetch_history_events()]


async def executions(client: Client, job_ids: list[str], chunk: int = 50) -> Counter:
    """How many workflow executions, open or closed, each job has, found by workflow id."""
    ids = {dsy_keys.workflow_id(j): j for j in job_ids}
    names, found = list(ids), Counter()
    for start in range(0, len(names), chunk):
        listed = ", ".join(f"'{name}'" for name in names[start : start + chunk])
        async for execution in client.list_workflows(f"WorkflowId IN ({listed})"):
            found[ids[execution.id]] += 1
    return found
