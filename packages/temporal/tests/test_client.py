"""`connect` builds the client recipe of the services."""

import asyncio
import socket
import urllib.request

import pytest
from dsy_temporal import event_data_converter
from dsy_temporal.client import connect
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.service import RPCError


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_lazy_returns_without_a_server():
    client = asyncio.run(asyncio.wait_for(connect("127.0.0.1:1", "default", lazy=True), 10))
    assert client.namespace == "default"
    assert client.data_converter is event_data_converter
    assert any(isinstance(i, TracingInterceptor) for i in client.config()["interceptors"])


@pytest.mark.integration
def test_client_carries_the_recipe_serves_runtime_metrics_and_checks_the_namespace(t1):
    metrics_port = free_port()
    with t1.container(
        "temporalio/temporal",
        ports={0: 7233},
        command=("server", "start-dev", "--ip", "0.0.0.0", "--headless"),
    ) as container:
        container.wait_ready(0)
        address = f"127.0.0.1:{container.host_ports[0]}"
        runtime = Runtime(
            telemetry=TelemetryConfig(
                metrics=PrometheusConfig(bind_address=f"127.0.0.1:{metrics_port}")
            )
        )

        async def run():
            client = None
            for _ in range(120):
                try:
                    client = await connect(
                        address, "default", runtime=runtime, check_namespace=True
                    )
                    break
                except RuntimeError:
                    await asyncio.sleep(0.5)
            assert client is not None, "the Temporal server did not answer"
            with pytest.raises(RPCError):
                await connect(address, "no-such-namespace", runtime=runtime, check_namespace=True)
            return client

        client = asyncio.run(run())
        assert client.data_converter is event_data_converter
        assert any(isinstance(i, TracingInterceptor) for i in client.config()["interceptors"])
        body = urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/metrics", timeout=10).read()
        assert b"temporal_request" in body
