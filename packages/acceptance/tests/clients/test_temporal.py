"""The Temporal client connects once to namespace dsy, with the workflows' payload converter."""

import asyncio

from dsy_acceptance.clients import temporal
from dsy_temporal import event_data_converter
from temporalio.client import Client


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
