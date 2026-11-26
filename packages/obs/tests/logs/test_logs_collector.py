"""One record pushed through the pinned collector image, which writes it by its file exporter."""

import json
import logging
import subprocess
import time

import pytest
from dsy_obs import logs

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
    logs:
      receivers: [otlp]
      exporters: [file]
"""


def exported(container_id: str) -> list[dict]:
    """The log records the file exporter has written so far, each with its resource attributes."""
    out = subprocess.run(
        ["docker", "logs", container_id], capture_output=True, text=True, check=True
    ).stdout
    records = []
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        for resource_logs in json.loads(line).get("resourceLogs", []):
            resource = resource_logs["resource"]["attributes"]
            for scope in resource_logs["scopeLogs"]:
                records += [{"resource": resource, **r} for r in scope["logRecords"]]
    return records


def attributes(pairs: list[dict]) -> dict[str, str]:
    return {a["key"]: a["value"]["stringValue"] for a in pairs}


def test_a_record_reaches_the_collector(t1, tmp_path, root_logger, monkeypatch):
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
        monkeypatch.setenv(logs.ENDPOINT, f"http://127.0.0.1:{collector.host_ports[0]}")
        logs.install("workflow")
        with logs.bind(job_id="j1", traceparent=f"00-{TRACE_ID}-{SPAN_ID}-01"):
            logging.getLogger("dsy.test").info("through the collector", extra={"pool": "compile"})
        next(h for h in root_logger.handlers if isinstance(h, logs.OtlpHandler)).close()
        deadline = time.monotonic() + 30
        while not (records := exported(collector.id)) and time.monotonic() < deadline:
            time.sleep(0.2)
    [record] = records
    assert attributes(record["resource"])["service.name"] == "workflow"
    assert record["body"]["stringValue"] == "through the collector"
    assert attributes(record["attributes"]) == {
        "job_id": "j1",
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "pool": "compile",
    }
