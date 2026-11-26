"""The provider `setup` installs from `OTEL_EXPORTER_OTLP_ENDPOINT`, the spans it pushes, and the
drops it counts when the collector refuses."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from dsy_events.metrics import registry
from dsy_obs import logs, traces
from dsy_obs.setup import setup
from dsy_temporal.trace import current_traceparent, stage_span
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
BLOCKING_CALL_SECONDS = 0.5
BLOCKING_RUN_SECONDS = 10
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Receiver(ThreadingHTTPServer):
    """An OTLP HTTP receiver on 127.0.0.1 that keeps each decoded trace request and answers every
    other signal with an empty success."""

    def __init__(self):
        self.requests: list[ExportTraceServiceRequest] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/v1/traces":
                    request = ExportTraceServiceRequest()
                    request.ParseFromString(body)
                    receiver.requests.append(request)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


def test_with_the_endpoint_unset_no_provider_is_installed_and_spans_stay_no_ops(fresh, monkeypatch):
    monkeypatch.delenv(traces.ENDPOINT, raising=False)
    setup("workflow")
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    with stage_span(trace.get_tracer("dsy"), "stage", TRACEPARENT):
        assert current_traceparent() == TRACEPARENT


def test_a_stage_span_arrives_with_its_parents_trace_id_and_the_service_name(fresh, monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv(traces.ENDPOINT, f"http://127.0.0.1:{receiver.server_address[1]}")
        setup("workflow")
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        with stage_span(trace.get_tracer("dsy"), "stage", TRACEPARENT) as span:
            inside = current_traceparent()
        provider.shutdown()
        receiver.shutdown()
    own = format(span.get_span_context().span_id, "016x")
    assert inside == f"00-{TRACE_ID}-{own}-01" and own != SPAN_ID
    [request] = receiver.requests
    [resource_spans] = request.resource_spans
    resource = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
    assert resource["service.name"] == "workflow"
    [pushed] = [s for scope in resource_spans.scope_spans for s in scope.spans]
    assert pushed.name == "stage"
    assert pushed.trace_id.hex() == TRACE_ID
    assert pushed.parent_span_id.hex() == SPAN_ID


def test_a_refusing_endpoint_never_blocks_and_counts_the_drops(fresh, monkeypatch):
    monkeypatch.setenv(traces.ENDPOINT, f"http://127.0.0.1:{free_port()}")
    monkeypatch.setattr(logs, "CLOSE_SECONDS", 0.1)
    monkeypatch.setattr(traces, "CLOSE_SECONDS", 0.1)
    setup("dispatch")
    tracer = trace.get_tracer("dsy")
    before = traces.dropped()
    counted = registry.count("dsy_obs_span_dropped_total", component="dispatch")
    slowest = 0.0
    started = time.monotonic()
    for _ in range(10_000):
        call = time.monotonic()
        with stage_span(tracer, "stage", TRACEPARENT):
            pass
        slowest = max(slowest, time.monotonic() - call)
    elapsed = time.monotonic() - started
    # A blocking exporter stalls a call for at least the first OTLP retry delay (1s) and the whole
    # run for tens of seconds, so these bounds sit far above scheduler noise and far below blocking.
    assert slowest < BLOCKING_CALL_SECONDS, f"one span call took {slowest:.2f}s"
    assert elapsed < BLOCKING_RUN_SECONDS, f"10,000 spans took {elapsed:.2f}s"
    assert traces.dropped() - before >= 10_000 - traces.QUEUE_SIZE - traces.BATCH_SIZE
    exported = registry.count("dsy_obs_span_dropped_total", component="dispatch") - counted
    assert exported == traces.dropped() - before


def test_setup_is_idempotent_within_a_process(fresh, monkeypatch):
    monkeypatch.delenv(traces.ENDPOINT, raising=False)
    calls = []
    monkeypatch.setattr(traces, "install", calls.append)
    setup("placement")
    setup("placement")
    assert calls == ["placement"]
    with pytest.raises(RuntimeError, match="placement"):
        setup("admission")
