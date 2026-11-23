"""The dispatch service's telemetry: a JSON accepted line and one `dispatch.accept` span for each
accepted job, in the trace of the job, pushed by OTLP HTTP to a receiver of the test, and the
`job_id` and trace ids on the line that names a job in the publisher."""

import gzip
import json
import logging
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import wft_obs.setup as obs_setup
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once
from wft_broker.client import Credentials
from wft_broker.testing import FakeBroker
from wft_dispatchapi import service as service_module
from wft_dispatchapi.publish import Publisher, published_event
from wft_dispatchapi.service import routes
from wft_events import build_event
from wft_obs import logs, traces
from wft_obs.setup import setup
from wft_temporal import new_traceparent, trace_id_of

pytestmark = pytest.mark.integration

COUNT = 3


class Receiver:
    """An OTLP HTTP server on a free loopback port that keeps every span it is sent, whatever
    the signal, and answers each push with an empty success."""

    def __init__(self):
        self.spans: list[tuple[dict, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                if self.path == "/v1/traces":
                    outer._keep(data)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _keep(self, data: bytes) -> None:
        request = ExportTraceServiceRequest.FromString(data)
        for resource_spans in request.resource_spans:
            resource = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
            for scope in resource_spans.scope_spans:
                self.spans += [(resource, span) for span in scope.spans]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def receiver():
    made = Receiver()
    yield made
    made.stop()


@pytest.fixture
def fresh(monkeypatch):
    """A process with no global tracer provider and no setup call yet; afterwards shuts down the
    provider the test installed and restores the root logger its `setup` replaced."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(obs_setup, "_installed", None)
    monkeypatch.setattr(logs, "CLOSE_SECONDS", 0.5)
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    installed = trace._TRACER_PROVIDER
    if isinstance(installed, TracerProvider):
        installed.shutdown()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(level)


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_an_accepted_line_and_a_span_for_each_accepted_job(
    start, logins, kit, tmp_path, receiver, fresh, monkeypatch, capsys, gateway
):
    monkeypatch.setenv(traces.ENDPOINT, receiver.url)
    setup("dispatch")
    monkeypatch.setattr(routes, "tracer", trace.get_tracer("wft"))
    service = start(logins, gateway)
    dispatch = service.url("dispatch-api")
    source = kit.upload_bundle(dispatch, tmp_path)

    answers = list(kit.Call(dispatch, kit.batch(source, "obs", COUNT)).answers())
    jobs = {a["job_id"]: a["trace_id"] for a in answers if a.get("outcome") == "accepted"}
    assert len(jobs) == COUNT, answers
    # A resend answers each job as a duplicate, which is not accepted again.
    again = list(kit.Call(dispatch, kit.batch(source, "obs", COUNT)).answers())
    assert [a.get("outcome") for a in again[:COUNT]] == ["duplicate"] * COUNT
    trace.get_tracer_provider().force_flush()

    lines = [x for x in json_lines(capsys.readouterr().out) if x["msg"] == "accepted"]
    assert {x["job_id"]: x["trace_id"] for x in lines} == jobs
    for x in lines:
        assert x["component"] == "dispatch" and x["tenant"] == "acme" and x["span_id"]

    accepts = [(r, s) for r, s in receiver.spans if s.name == "dispatch.accept"]
    assert sorted(s.trace_id.hex() for _, s in accepts) == sorted(jobs.values())
    assert {r["service.name"] for r, _ in accepts} == {"dispatch"}
    by_job = {
        next(a.value.string_value for a in s.attributes if a.key == "jobid"): s.trace_id.hex()
        for _, s in accepts
    }
    assert by_job == jobs

    # The call's own span keeps the links to the jobs of its first chunk.
    submits = [s for _, s in receiver.spans if s.name == "dispatch.submit"]
    assert len(submits) == 2
    linked = {link.trace_id.hex() for s in submits[:1] for link in s.links}
    assert linked == set(jobs.values())


def test_the_line_that_names_a_job_in_the_publisher_carries_its_trace(fresh, capsys):
    setup("dispatch")
    traceparent = new_traceparent()
    event = published_event(
        build_event(
            "job.accepted",
            source="dispatch",
            tenant="acme",
            jobid="7f0c4ad2-0000-4000-8000-000000000001",
            priorityclass="gold",
            jobclass="short",
            traceparent=traceparent,
            time=datetime.now(UTC),
            data={},
        ),
        datetime.now(UTC),
    )
    publisher = Publisher(FakeBroker("ci").connect, "fake", Credentials("dispatch", "standin"))

    assert publisher.publish(None, [event])

    [line] = [x for x in json_lines(capsys.readouterr().out) if "no intake queue" in x["msg"]]
    assert line["level"] == "ERROR" and line["component"] == "dispatch"
    assert line["job_id"] == event.jobid
    assert line["trace_id"] == trace_id_of(traceparent)


def test_main_sets_up_the_component_and_exports_the_registry(monkeypatch):
    calls = []
    monkeypatch.setattr(service_module, "setup", lambda *a, **k: calls.append(("setup", a)))
    monkeypatch.setattr(
        service_module, "export_registry", lambda meter, component: calls.append("export")
    )
    monkeypatch.setattr(service_module, "load", lambda profile: object())
    monkeypatch.setattr(service_module, "run", lambda service: calls.append("run"))
    monkeypatch.setattr(
        service_module,
        "build",
        lambda *_: type("S", (), {"runtime": type("R", (), {"metric_meter": 1})}),
    )
    config = SimpleNamespace(tenancy_profile="ci")
    monkeypatch.setattr(service_module.Config, "from_env", classmethod(lambda cls, env: config))

    assert service_module.main({}) == 0
    assert calls == [("setup", ("dispatch",)), "export", "run"]
