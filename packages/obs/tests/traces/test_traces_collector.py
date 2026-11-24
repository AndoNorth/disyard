"""One span pushed through the pinned collector image, which writes it by its file exporter."""

import json
import subprocess
import time

import pytest
from dsy_obs import logs, traces
from dsy_obs.setup import setup
from dsy_temporal.trace import stage_span
from opentelemetry import trace

pytestmark = pytest.mark.integration

IMAGE = "otel/opentelemetry-collector-contrib"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
# The file exporter writes to the container's stdout, read back with `docker logs`, because the
# image has no shell to read a file with.
CONFIG = """
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  file:
    path: /dev/stdout
    flush_interval: 100ms
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [file]
"""


def exported(container_id: str) -> list[dict]:
    """The spans the file exporter has written so far, each with its resource attributes."""
    out = subprocess.run(
        ["docker", "logs", container_id], capture_output=True, text=True, check=True
    ).stdout
    spans = []
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        for resource_spans in json.loads(line).get("resourceSpans", []):
            resource = resource_spans["resource"]["attributes"]
            for scope in resource_spans["scopeSpans"]:
                spans += [{"resource": resource, **s} for s in scope["spans"]]
    return spans


def test_a_span_reaches_the_collector(t1, tmp_path, fresh, monkeypatch):
    config = tmp_path / "collector.yaml"
    config.write_text(CONFIG)
    config.chmod(0o644)
    with t1.container(
        IMAGE,
        ports={0: 4318},
        command=["--config=/etc/dsy/collector.yaml"],
        mounts={config: "/etc/dsy/collector.yaml"},
    ) as collector:
        collector.wait_ready(0)
        monkeypatch.setenv(traces.ENDPOINT, f"http://127.0.0.1:{collector.host_ports[0]}")
        monkeypatch.setattr(logs, "CLOSE_SECONDS", 0.1)
        setup("workflow")
        with stage_span(trace.get_tracer("dsy"), "stage", f"00-{TRACE_ID}-{SPAN_ID}-01"):
            pass
        trace.get_tracer_provider().shutdown()
        deadline = time.monotonic() + 30
        while not (spans := exported(collector.id)) and time.monotonic() < deadline:
            time.sleep(0.2)
    [span] = spans
    resource = {a["key"]: a["value"]["stringValue"] for a in span["resource"]}
    assert resource["service.name"] == "workflow"
    assert span["name"] == "stage"
    assert span["traceId"] == TRACE_ID and span["parentSpanId"] == SPAN_ID
