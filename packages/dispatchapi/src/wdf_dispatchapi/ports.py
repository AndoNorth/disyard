"""The port through which control and the reads reach a job's workflow.

Handlers are threads, so the port is synchronous, and an implementation over the asynchronous
Temporal client bridges to its own loop. A job's workflow is `wdf_keys.workflow_id(job_id)`.
"""

from typing import Literal, Protocol, get_args

from wdf_workflow.models import ControlSignal, StatusQuery

# The signals of naming.yaml that the API sends. Admission alone sends `resume`.
SignalName = Literal["cancel", "terminate", "pause"]
SIGNALS: tuple[str, ...] = get_args(SignalName)


class NoWorkflow(LookupError):
    """The job has no workflow that can take the call: none was started, it was purged, or, for a
    signal, it has closed."""


class TemporalUnavailable(Exception):
    """Temporal did not answer in time, so nothing is known about the workflow."""


class WorkflowGateway(Protocol):
    def exists(self, job_id: str) -> bool:
        """Whether Temporal holds the job's workflow, open or closed.

        Raises TemporalUnavailable.
        """
        ...

    def signal(self, job_id: str, name: SignalName, signal: ControlSignal) -> None:
        """Sends a control signal and returns once the server accepted it.

        Raises ValueError for a name outside SIGNALS, `resume` included, before any call, and
        NoWorkflow or TemporalUnavailable.
        """
        ...

    def status(self, job_id: str) -> StatusQuery:
        """The answer of the workflow's `status` query.

        Raises NoWorkflow or TemporalUnavailable, which covers a query that times out.
        """
        ...
