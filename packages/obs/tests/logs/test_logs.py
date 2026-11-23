"""The JSON line, the field filter, `bind`, and the non-blocking OTLP push of `wft_obs.logs`."""

import io
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from wft_events.metrics import registry
from wft_obs import logs

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"
ALLOWED = set(logs.CORE) | set(logs.FIELDS)


@pytest.fixture
def lines(root, monkeypatch):
    """Installs the stdout handler alone into a buffer and returns a reader of its JSON lines."""
    monkeypatch.delenv(logs.ENDPOINT, raising=False)
    out = io.StringIO()
    logs.install("dispatch", stream=out)
    return lambda: [json.loads(line) for line in out.getvalue().splitlines()]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def otlp_handler(root) -> logs.OtlpHandler | None:
    return next((h for h in root.handlers if isinstance(h, logs.OtlpHandler)), None)


def test_each_line_is_one_json_object_with_the_contract_fields(lines):
    log = logging.getLogger("wft.test")
    log.info("plain %s", "line")
    with logs.bind(tenant="t1", job_id="j1", task_id="k1", attempt_id="a1", pool="simulate"):
        log.warning("bound", extra={"event_type": "task.completed", "exit_class": "ok"})
    plain, bound = lines()
    assert set(plain) == set(logs.CORE)
    assert plain["level"] == "INFO" and plain["component"] == "dispatch"
    assert plain["msg"] == "plain line"
    assert plain["ts"].endswith("Z") and "T" in plain["ts"]
    assert bound == bound | {
        "tenant": "t1",
        "job_id": "j1",
        "task_id": "k1",
        "attempt_id": "a1",
        "pool": "simulate",
        "event_type": "task.completed",
        "exit_class": "ok",
        "level": "WARNING",
    }
    assert set(bound) <= ALLOWED


def test_bind_derives_the_trace_ids_from_traceparent(lines):
    with logs.bind(traceparent=TRACEPARENT, job_id="j1"):
        logging.getLogger("wft.test").info("traced")
    [line] = lines()
    assert line["trace_id"] == TRACE_ID and line["span_id"] == SPAN_ID
    assert "traceparent" not in line


@pytest.mark.parametrize("value", ["", "garbage", f"00-{'0' * 32}-{SPAN_ID}-01"])
def test_a_malformed_traceparent_sets_no_trace_ids(lines, value):
    with logs.bind(traceparent=value):
        logging.getLogger("wft.test").info("x")
    [line] = lines()
    assert "trace_id" not in line and "span_id" not in line


@pytest.mark.parametrize("name", ["jobid", "taskid", "attempt", "msg", "component", "anything"])
def test_bind_refuses_a_name_outside_the_contract(name):
    with pytest.raises(TypeError, match=name):
        logs.bind(**{name: "x"})


def test_bind_nests_and_restores(lines):
    log = logging.getLogger("wft.test")
    with logs.bind(job_id="j1"):
        with logs.bind(task_id="k1"):
            log.info("inner")
        log.info("outer")
    log.info("none")
    inner, outer, none = lines()
    assert (inner["job_id"], inner["task_id"]) == ("j1", "k1")
    assert outer["job_id"] == "j1" and "task_id" not in outer
    assert "job_id" not in none


def test_an_extra_key_outside_the_contract_is_not_emitted(lines):
    extra = {"jobid": "j1", "traceparent": TRACEPARENT, "user": "u", "job_id": "j2"}
    logging.getLogger("wft.test").info("x", extra=extra)
    [line] = lines()
    assert set(line) == set(logs.CORE) | {"job_id"}
    assert line["job_id"] == "j2"


def test_an_exception_is_kept_in_msg(lines):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("wft.test").exception("failed")
    [line] = lines()
    assert set(line) == set(logs.CORE)
    assert line["msg"].startswith("failed\n") and "ValueError: boom" in line["msg"]


def test_with_the_endpoint_unset_no_exporter_is_created(root, monkeypatch):
    monkeypatch.delenv(logs.ENDPOINT, raising=False)
    created = []
    monkeypatch.setattr(logs.OtlpHandler, "__init__", lambda *a, **k: created.append(a))
    logs.install("dispatch", stream=io.StringIO())
    assert not created and otlp_handler(root) is None


def test_a_refusing_endpoint_never_blocks_and_counts_the_drops(monkeypatch, root):
    """`monkeypatch` comes first so the short close still applies when `root` closes the handler."""
    monkeypatch.setenv(logs.ENDPOINT, f"http://127.0.0.1:{free_port()}")
    monkeypatch.setattr(logs, "CLOSE_SECONDS", 0.1)
    out = io.StringIO()
    logs.install("dispatch", stream=out)
    assert otlp_handler(root) is not None
    before = logs.dropped()
    counted = registry.count("wft_obs_log_dropped_total", component="dispatch")
    log = logging.getLogger("wft.test")
    started = time.monotonic()
    for i in range(10_000):
        log.info("line %d", i)
    elapsed = time.monotonic() - started
    assert elapsed < 2, f"10,000 lines took {elapsed:.2f}s"
    written = [json.loads(line)["msg"] for line in out.getvalue().splitlines()]
    assert sum(msg.startswith("line ") for msg in written) == 10_000
    assert logs.dropped() - before >= 10_000 - logs.QUEUE_SIZE - logs.BATCH_SIZE
    exported = registry.count("wft_obs_log_dropped_total", component="dispatch") - counted
    assert exported == logs.dropped() - before


class Receiver(ThreadingHTTPServer):
    """An OTLP HTTP log receiver on 127.0.0.1 that keeps each decoded request."""

    def __init__(self):
        self.requests: list[ExportLogsServiceRequest] = []
        self.received = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                request = ExportLogsServiceRequest()
                request.ParseFromString(body)
                receiver.requests.append((self.path, request))
                receiver.received.set()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


def test_the_push_carries_service_name_and_the_fields_as_attributes(root, monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv(logs.ENDPOINT, f"http://127.0.0.1:{receiver.server_address[1]}")
        logs.install("admission", stream=io.StringIO())
        with logs.bind(job_id="j1", traceparent=TRACEPARENT):
            logging.getLogger("wft.test").info("pushed", extra={"user": "u", "pool": "compile"})
        otlp_handler(root).close()
        receiver.shutdown()
    [(path, request)] = receiver.requests
    assert path == "/v1/logs"
    [resource_logs] = request.resource_logs
    resource = {a.key: a.value.string_value for a in resource_logs.resource.attributes}
    assert resource["service.name"] == "admission"
    [record] = [r for scope in resource_logs.scope_logs for r in scope.log_records]
    assert record.body.string_value == "pushed"
    assert record.severity_text == "INFO"
    attributes = {a.key: a.value.string_value for a in record.attributes}
    assert attributes == {
        "job_id": "j1",
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "pool": "compile",
    }


def test_records_of_the_export_path_are_not_pushed(root, monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv(logs.ENDPOINT, f"http://127.0.0.1:{receiver.server_address[1]}")
        logs.install("admission", stream=io.StringIO())
        logging.getLogger("opentelemetry.exporter").warning("retrying")
        logging.getLogger("urllib3.connectionpool").warning("retrying")
        otlp_handler(root).close()
        receiver.shutdown()
    assert receiver.requests == []
