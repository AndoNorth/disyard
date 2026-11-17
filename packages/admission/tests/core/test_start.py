"""The start of a claimed row on a dev server: the options of naming.yaml, the two arguments, the
queue prefix memo, the trace header and a repeated start."""

import asyncio

import pytest
from temporalio.common import SearchAttributeKey
from temporalio.converter import PayloadConverter
from wdf_admission.claim import accepted_of
from wdf_admission.start import params_of, start
from wdf_events import Event
from wdf_events.models import JobParams
from wdf_keys import workflow_id
from wdf_temporal import trace_id_of
from wdf_workflow.contract import naming
from wdf_workflow.taskpath import QUEUE_PREFIX_MEMO

pytestmark = pytest.mark.integration

LIMIT_S = 60
# The header that the tracing interceptor writes its carrier into.
TRACE_HEADER = "_tracer-data"


def run(body):
    return asyncio.run(asyncio.wait_for(body(), LIMIT_S))


async def started_event(handle):
    async for event in handle.fetch_history_events():
        return event


def test_start_options_match_naming(client, claimed_row):
    plain, prefixed = claimed_row(), claimed_row()
    prefix = "h-0123abcd-"

    async def body():
        c = await client()
        found = {}
        for row, given in ((plain, ""), (prefixed, prefix)):
            assert await start(c, row, accepted_of(row), given) == "started"
            handle = c.get_workflow_handle(workflow_id(row.job_id))
            description = await handle.describe()
            started = (await started_event(handle)).workflow_execution_started_event_attributes
            arguments = await c.data_converter.decode(started.input.payloads, [Event, JobParams])
            found[row.job_id] = (description, await description.memo(), started, arguments)
        return found

    found = run(body)
    assert naming().workflow.type == "JobWorkflow"
    assert naming().workflow.queue == "wdf-workflow"
    for row, memo in ((plain, {}), (prefixed, {QUEUE_PREFIX_MEMO: prefix})):
        description, stored_memo, started, arguments = found[row.job_id]
        assert description.workflow_type == naming().workflow.type
        assert description.task_queue == naming().workflow.queue
        assert description.id == f"job-{row.job_id}" == workflow_id(row.job_id)
        attributes = description.typed_search_attributes
        assert set(naming().search_attributes) == {pair.key.name for pair in attributes}
        expected = {
            "WdfTenant": row.tenant,
            "WdfPriorityClass": row.priority_class,
            "WdfJobClass": row.job_class,
            "WdfJobId": row.job_id,
            "WdfSubmittedAt": row.created_at,
        }
        for name, kind in naming().search_attributes.items():
            key = (
                SearchAttributeKey.for_datetime(name)
                if kind == "datetime"
                else SearchAttributeKey.for_keyword(name)
            )
            assert attributes.get(key) == expected[name], name
        assert stored_memo == memo
        assert arguments == [accepted_of(row), params_of(row)]
        assert arguments[1].class_override is None


def test_start_carries_trace(client, claimed_row):
    row = claimed_row()

    async def body():
        c = await client()
        await start(c, row, accepted_of(row))
        return await started_event(c.get_workflow_handle(workflow_id(row.job_id)))

    first = run(body)
    header = first.workflow_execution_started_event_attributes.header.fields[TRACE_HEADER]
    carrier = PayloadConverter.default.from_payloads([header])[0]
    assert trace_id_of(carrier["traceparent"]) == trace_id_of(row.traceparent)


def test_already_started_is_duplicate(client, claimed_row):
    row = claimed_row()

    async def body():
        c = await client()
        return [await start(c, row, accepted_of(row)) for _ in range(2)]

    assert run(body) == ["started", "duplicate"]
