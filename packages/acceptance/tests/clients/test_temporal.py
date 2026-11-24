"""The Temporal client connects once to namespace dsy, with the workflows' payload converter."""

import asyncio
import types

import dsy_keys
from dsy_acceptance.clients import temporal
from dsy_temporal import event_data_converter
from temporalio.client import Client

JOB = "3a9617fa-df7c-5760-a644-42d559e0f433"


def test_connect_names_namespace_dsy(monkeypatch):
    calls = []

    async def connect(address, **kwargs):
        calls.append((address, kwargs))
        return "client"

    monkeypatch.setattr(Client, "connect", connect)
    assert asyncio.run(temporal.connect("127.0.0.1:7233")) == "client"
    assert calls == [
        ("127.0.0.1:7233", {"namespace": "dsy", "data_converter": event_data_converter})
    ]


class FakeHandle:
    def __init__(self, workflow_id):
        self.workflow_id = workflow_id

    async def describe(self):
        return f"described {self.workflow_id}"

    async def fetch_history_events(self):
        for event in ("started", "completed"):
            yield event


class FakeClient:
    def __init__(self, listed=()):
        self.listed, self.queries, self.handles = listed, [], []

    def get_workflow_handle(self, workflow_id):
        self.handles.append(workflow_id)
        return FakeHandle(workflow_id)

    async def list_workflows(self, query):
        self.queries.append(query)
        for workflow_id in self.listed:
            yield types.SimpleNamespace(id=workflow_id)


def test_describe_and_history_read_the_workflow_of_the_job():
    client = FakeClient()
    assert asyncio.run(temporal.describe(client, JOB)) == f"described {dsy_keys.workflow_id(JOB)}"
    assert asyncio.run(temporal.history(client, JOB)) == ["started", "completed"]
    assert client.handles == [dsy_keys.workflow_id(JOB)] * 2


def test_executions_counts_by_workflow_id_in_chunks():
    other = "7d1f0a52-6c3b-5e8d-a1f4-29b7c3e5d061"
    wf, wf_other = dsy_keys.workflow_id(JOB), dsy_keys.workflow_id(other)
    client = FakeClient(listed=[wf, wf, wf_other])
    found = asyncio.run(temporal.executions(client, [JOB, other], chunk=1))
    # Each chunk of one id gets its own query, and the fake answers each with the same list.
    assert len(client.queries) == 2
    assert client.queries[0] == f"WorkflowId IN ('{wf}')"
    assert found == {JOB: 2, other: 1}
