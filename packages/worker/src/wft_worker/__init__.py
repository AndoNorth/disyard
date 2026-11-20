"""The pool worker: one activity worker per pool that serves `run_task` by running one child
process at a time.

`read_request` is the one place the worker reads its task request, so the worker stage of
stages.yaml holds for every pool.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Request:
    """What the worker reads of a dev.wft.task.requested.v1 event: the attributes it traces and
    logs with, and the data fields the worker stage declares. `cpu_cores` and `memory_mib` are
    placement's, so the slot is read from the pod and never from the request."""

    id: str
    tenant: str
    jobid: str
    taskid: str
    priorityclass: str
    jobclass: str
    traceparent: str
    task_type: str
    inputs: list[dict[str, Any]]
    resume_checkpoint: dict[str, Any] | None
    deadline: str


def read_request(event) -> Request:
    data = event.data
    return Request(
        id=event.id,
        tenant=event.tenant,
        jobid=event.jobid,
        taskid=event.taskid,
        priorityclass=event.priorityclass,
        jobclass=event.jobclass,
        traceparent=event.traceparent,
        task_type=data["task_type"],
        inputs=data["inputs"],
        resume_checkpoint=data.get("resume_checkpoint"),
        deadline=data["deadline"],
    )
