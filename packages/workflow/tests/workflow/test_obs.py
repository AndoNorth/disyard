"""The workflow's own telemetry on a dev server: the job-end series and log line, the log line of a
compile skipped on a cache hit, and the trace id the activity spans of a job carry."""

import asyncio
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once
from temporalio.client import Client
from wft_obs import logs, traces
from wft_temporal import tracing_interceptor
from wft_temporal.trace import stage_span
from wft_workflow.testing import connect

pytestmark = pytest.mark.integration

LIMIT_S = 120


def short(accepted):
    """The accepted event of a job of the `short` class, the value the series are labelled with."""
    event = accepted()
    return event.model_copy(update={"jobclass": "short"})


@pytest.fixture
def json_logs():
    """The JSON lines the root logger writes while the test runs, and the root logger as it was."""
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    stream = io.StringIO()
    logs.install("workflow", stream)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(level)


def test_a_job_end_is_counted_and_logged_and_a_cache_hit_is_logged(
    temporal, rig, plan, fan, accepted, meter, json_logs
):
    async def body():
        client, buffer = await meter(temporal)
        async with rig(client) as this:
            first, second = short(accepted), short(accepted)
            await (await this.start(plan(fan), first)).result()
            await (await this.start(plan(fan), second)).result()
        return first, second, buffer.retrieve_updates()

    first, second, updates = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    labels = {"job_class": "short", "outcome": "succeeded"}
    for name in ("wft_workflow_jobs_total", "wft_workflow_job_seconds"):
        ours = [u for u in updates if u.metric.name == name]
        assert ours, name
        assert all(
            u.attributes["job_class"] == "short" and u.attributes["outcome"] == "succeeded"
            for u in ours
        )
    assert sum(u.value for u in updates if u.metric.name == "wft_workflow_jobs_total") == 2
    assert all(u.value >= 0 for u in updates if u.metric.name == "wft_workflow_job_seconds")
    lines = json_logs()
    ended = [line for line in lines if line["msg"].startswith("job ended")]
    assert {line["job_id"] for line in ended} == {first.jobid, second.jobid}
    assert all(
        f"job_class={labels['job_class']} outcome={labels['outcome']}" in line["msg"]
        for line in ended
    )
    # Only the second job's compile hits the entry the first one recorded.
    [skipped] = [line for line in lines if line["msg"].startswith("task skipped")]
    assert skipped["job_id"] == second.jobid
    assert skipped["task_id"] == "build"
    assert "compile-cache-hit" in skipped["msg"]


class Receiver(ThreadingHTTPServer):
    """An OTLP HTTP receiver on 127.0.0.1 that keeps the spans pushed to it."""

    def __init__(self):
        self.spans: list = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/v1/traces":
                    request = ExportTraceServiceRequest()
                    request.ParseFromString(body)
                    for resource in request.resource_spans:
                        for scope in resource.scope_spans:
                            receiver.spans.extend(scope.spans)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


@pytest.fixture
def pushed(monkeypatch):
    """A receiver that the global tracer provider pushes to, and the provider as it was."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    receiver = Receiver()
    monkeypatch.setenv(traces.ENDPOINT, f"http://127.0.0.1:{receiver.server_port}")
    provider = traces.install("workflow")
    assert isinstance(provider, TracerProvider)
    yield receiver
    provider.shutdown()
    receiver.shutdown()


def test_the_activity_spans_of_a_job_carry_its_trace_id(temporal, rig, plan, fan, accepted, pushed):
    event = short(accepted)
    trace_id = event.traceparent.split("-")[1]

    async def body():
        client = await connect(temporal)
        async with rig(client) as this:
            # Only the client that starts the job carries the trace context into its headers.
            this.client = Client(**{**client.config(), "interceptors": [tracing_interceptor()]})
            with stage_span(trace.get_tracer("wft"), "dispatch", event.traceparent):
                handle = await this.start(plan(fan), event)
            await handle.result()

    asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    trace.get_tracer_provider().force_flush()
    activity_spans = [s for s in pushed.spans if s.name.startswith("RunActivity:")]
    assert activity_spans
    assert {s.trace_id.hex() for s in activity_spans} == {trace_id}
