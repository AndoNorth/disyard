"""After a control action has reached its end, the job row, the status that the job's latest
lifecycle event projects and the workflow agree.

Each job runs JobWorkflow on a dev server, whose real emit activity moves the row and writes the
events in Postgres. The request goes through the control route, the retrier delivers it, and a
resume is sent by the test with the row's sequence, as admission sends it.
"""

import asyncio

import pytest
import yaml
from dsy_dispatchapi.control import control_job
from dsy_dispatchapi.gateway import TemporalGateway
from dsy_dispatchapi.models import CONTRACT, ControlRequest
from dsy_dispatchapi.retrier import Retrier
from dsy_events import from_bytes
from dsy_eventstore import events_for_job
from dsy_jobstate import get_job
from dsy_stages.recording import type_name
from dsy_workflow.job import JobWorkflow
from dsy_workflow.models import ResumeSignal
from dsy_workflow.testing import connect
from temporalio.client import WorkflowExecutionStatus

pytestmark = [pytest.mark.integration, pytest.mark.pair]

LIMIT_S = 180
PROJECTION = yaml.safe_load((CONTRACT / "status.yaml").read_text())["projection"]
# Where each action ends: the row's status, and whether the workflow is closed by then.
ENDS = {
    "cancel": ("cancelled", True),
    "terminate": ("terminated", True),
    "pause": ("paused", False),
    "resume": ("running", False),
}


def projected(db, job_id: str) -> str | None:
    """The status that the latest event of the job that sets one projects, by the projection of
    the status contract."""
    for stored in reversed(events_for_job(db, job_id)):
        event = from_bytes(stored.body.encode())
        for rule in PROJECTION.get(type_name(event.type), []):
            when = rule.get("when", {})
            if rule["sets"] and all(getattr(event, k) == v for k, v in when.items()):
                return rule["sets"]
    return None


@pytest.mark.parametrize("action", list(ENDS))
def test_row_events_and_workflow_agree(
    temporal, db, seed, logins, control_conn, workflow_dsn, kit, action
):
    [row] = seed(1)
    status, closed = ENDS[action]

    def row_status() -> str:
        return get_job(db, row.job_id).status

    async def request(gateway, retrier, name: str) -> None:
        """Requests `name` through the control route and delivers it as the retrier does."""
        answer = await asyncio.to_thread(
            control_job, control_conn, gateway, "acme", row.job_id, name, ControlRequest()
        )
        assert answer.outcome == "requested"
        await asyncio.to_thread(retrier.deliver)

    async def body():
        client = await connect(temporal)
        async with kit.rig(client, workflow_dsn):
            handle = await kit.start(client, db, row)
            await kit.until(lambda: row_status() == "running", "running row")
            with TemporalGateway(temporal) as gateway:
                retrier = Retrier(logins["control"], logins["reader"], gateway)
                await request(gateway, retrier, "pause" if action == "resume" else action)
                if action == "resume":
                    await kit.until(lambda: row_status() == "paused", "paused row")
                    await request(gateway, retrier, "resume")
                    seq = get_job(db, row.job_id).control_seq
                    await handle.signal(
                        JobWorkflow.resume, ResumeSignal(control_seq=seq, manifest=None)
                    )
                await kit.until(lambda: row_status() == status, f"{status} row")
                if closed:
                    await handle.result()
                    workflow = (await handle.describe()).status
                else:
                    workflow = (await asyncio.to_thread(gateway.status, row.job_id)).job_status
                    await handle.terminate()
        return workflow

    workflow = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert projected(db, row.job_id) == row_status() == status
    if closed:
        assert workflow == WorkflowExecutionStatus.COMPLETED
    else:
        assert workflow == status
