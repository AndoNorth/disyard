"""The Temporal client connects once to namespace wdf, with the workflows' payload converter."""

import asyncio

from temporalio.client import Client
from wdf_acceptance.clients import temporal
from wdf_temporal import event_data_converter


def test_connect_names_namespace_wdf(monkeypatch):
    calls = []

    async def connect(address, **kwargs):
        calls.append((address, kwargs))
        return "client"

    monkeypatch.setattr(Client, "connect", connect)
    assert asyncio.run(temporal.connect("127.0.0.1:7233")) == "client"
    assert calls == [
        ("127.0.0.1:7233", {"namespace": "wdf", "data_converter": event_data_converter})
    ]
