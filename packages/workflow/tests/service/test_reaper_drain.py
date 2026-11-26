"""The reaper worker of the service drains: a pass in flight at shutdown finishes and reports."""

import asyncio
import threading

import pytest
from dsy_artifactreaper import schedule
from dsy_servicekit.lifecycle import stopping
from dsy_workflow.service import DRAIN, serve
from dsy_workflow.testing import FakeControl, connect
from temporalio.api.enums.v1 import EventType
from temporalio.client import ScheduleOverlapPolicy

LIMIT_S = 120
REPORT = {"ran": True, "objects": 7}


def test_the_reaper_drains_as_long_as_the_service():
    assert schedule.DRAIN == DRAIN


@pytest.mark.integration
def test_a_pass_in_flight_at_shutdown_finishes_inside_drain(temporal, config):
    """The SDK default grace of 0 cancels the activity at once, so its history never holds a
    completion. With DRAIN the pass reports, and the activity completes."""
    started = threading.Event()

    def slow_pass(heartbeat):
        started.set()
        heartbeat()
        threading.Event().wait(3)
        return REPORT

    async def body():
        client = await connect(temporal)
        handle = client.get_schedule_handle(schedule.SCHEDULE_ID)
        control = FakeControl()
        with stopping() as stop:
            service = asyncio.create_task(
                serve(
                    config("reaper-drain"),
                    control.control_activities(),
                    control.emit,
                    slow_pass,
                    stop=stop,
                )
            )
            await schedule.ensure_schedule(client)
            await handle.trigger(overlap=ScheduleOverlapPolicy.SKIP)
            assert await asyncio.to_thread(started.wait, 30)
            stop.set()
            await service
        [execution] = [
            w async for w in client.list_workflows(f"WorkflowType = '{schedule.WORKFLOW}'")
        ]
        history = await client.get_workflow_handle(execution.id).fetch_history()
        return [EventType.Name(event.event_type) for event in history.events]

    events = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert "EVENT_TYPE_ACTIVITY_TASK_COMPLETED" in events
