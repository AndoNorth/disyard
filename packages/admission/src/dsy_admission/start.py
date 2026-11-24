"""The start of a claimed job's workflow, with the start options of the naming contract."""

from datetime import timedelta
from typing import Literal

from dsy_events import Event
from dsy_events.models import JobParams
from dsy_jobstate import JobRow
from dsy_keys import workflow_id
from dsy_temporal import stage_span
from dsy_workflow.contract import naming
from dsy_workflow.taskpath import QUEUE_PREFIX_MEMO
from opentelemetry import trace
from temporalio.client import Client
from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import WorkflowAlreadyStartedError

# Short enough that a drain on SIGTERM finishes the start in progress before the pod is killed.
RPC_TIMEOUT = timedelta(seconds=10)
# The row column behind each search attribute of naming.yaml.
COLUMNS = {
    "DsyTenant": "tenant",
    "DsyPriorityClass": "priority_class",
    "DsyJobClass": "job_class",
    "DsyJobId": "job_id",
    "DsySubmittedAt": "created_at",
}

tracer = trace.get_tracer(__name__)


def params_of(row: JobRow) -> JobParams:
    """The params block of the row, with no class override, so the workflow takes the row's class
    from the accepted event."""
    return JobParams(seed=row.seed, parameters=row.parameters)


def search_attributes(row: JobRow) -> TypedSearchAttributes:
    pairs = []
    for name, kind in naming().search_attributes.items():
        key = SearchAttributeKey.for_keyword
        if kind == "datetime":
            key = SearchAttributeKey.for_datetime
        pairs.append(SearchAttributePair(key(name), getattr(row, COLUMNS[name])))
    return TypedSearchAttributes(pairs)


async def start(
    client: Client, row: JobRow, accepted: Event, prefix: str = ""
) -> Literal["started", "duplicate"]:
    """Starts the job's workflow inside the admission span of the row's trace, with its pool queues
    behind `prefix` when one is given.

    A workflow that already holds the job's id is a repeat of an earlier start, since the id is
    never reused, and is reported as a duplicate.
    """
    with stage_span(tracer, "admission", row.traceparent):
        try:
            await client.start_workflow(
                naming().workflow.type,
                args=[accepted, params_of(row)],
                id=workflow_id(row.job_id),
                task_queue=naming().workflow.queue,
                id_reuse_policy=WorkflowIDReusePolicy[naming().workflow.id_reuse_policy],
                search_attributes=search_attributes(row),
                memo={QUEUE_PREFIX_MEMO: prefix} if prefix else None,
                rpc_timeout=RPC_TIMEOUT,
            )
        except WorkflowAlreadyStartedError:
            return "duplicate"
    return "started"
