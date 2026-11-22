"""The state inputs exporter: its names held to the catalogue, and against a migrated database as
the `wft_reader` group, a pass served on the Temporal runtime's Prometheus endpoint."""

import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import yaml
from wft_obs import metrics
from wft_obs.inputs import Exporter, metric_name
from wft_obs.inputs.__main__ import serve
from wft_stateops.inputs import load, parse

ROOT = Path(__file__).resolve().parents[4]
CATALOGUE = yaml.safe_load((ROOT / "contracts/instrumentation/metrics.yaml").read_text())["metrics"]
EXPORTED = [i for i in load().inputs if i.source.kind in ("sql", "sql_labelled", "invariants")]
READER = "t_state_reader"
UNREACHABLE = "postgresql://nobody@127.0.0.1:1/none"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def scrape(url: str) -> str:
    return urllib.request.urlopen(url, timeout=5).read().decode()


def series(text: str, name: str) -> dict[str, float]:
    """The samples of `name` keyed by their labels without the runtime's `service_name`, empty
    for an unlabelled one."""
    found = re.findall(rf"^{name}(\{{[^}}]*\}})? (\S+)$", text, re.MULTILINE)
    return {
        re.sub(r'[{,]?service_name="[^"]*",?', "", labels).strip("{},"): float(value)
        for labels, value in found
    }


@pytest.fixture(scope="module")
def served():
    """One runtime for the module, since its endpoint stays bound for the life of the process."""
    port = free_port()
    runtime = metrics.runtime(f"127.0.0.1:{port}", {})
    yield runtime.metric_meter, f"http://127.0.0.1:{port}/metrics"


@pytest.fixture
def reader(appdb):
    """The DSN of a login that is a member of `wft_reader` only, as the module's login is."""
    with psycopg.connect(appdb, autocommit=True) as admin:
        admin.execute(f"drop role if exists {READER}")
        admin.execute(f"create role {READER} login password 'x' in role wft_reader")
    parts = urlsplit(appdb)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{READER}:x@{host}"))


def test_the_names_are_the_catalogued_state_names():
    catalogued = {n for n in CATALOGUE if n.startswith("wft_state_")}
    assert {metric_name(i) for i in EXPORTED} == catalogued


def test_the_store_inputs_are_not_exported():
    exporter = Exporter(metrics.runtime(f"127.0.0.1:{free_port()}", {}).metric_meter, "", load())
    assert {i.name for i in exporter.inputs} == {i.name for i in EXPORTED}
    assert not {"store_bytes", "store_capacity_share"} & {i.name for i in exporter.inputs}


def test_an_unreachable_database_counts_an_error_for_every_input(served):
    meter, _ = served
    exporter = Exporter(meter, UNREACHABLE, load())

    assert exporter.run_once() == 0
    assert exporter.errors == {i.name: 1 for i in EXPORTED}


def test_serve_marks_ready_after_a_pass_that_reached_the_database(tmp_path):
    ready, stop = tmp_path / "ready", threading.Event()

    class Pass:
        def __init__(self, reads: list[int]):
            self.reads = reads

        def run_once(self) -> int:
            reads = self.reads.pop(0)
            if not self.reads:
                stop.set()
            return reads

    serve(Pass([0]), stop, ready, 0)
    assert not ready.exists()

    stop.clear()
    serve(Pass([0, 3]), stop, ready, 0)
    assert ready.exists()


@pytest.mark.integration
def test_every_input_is_served_as_its_gauge(reader, served):
    meter, url = served
    exporter = Exporter(meter, reader, load())

    assert exporter.run_once() == len(EXPORTED)
    assert not exporter.errors

    text = scrape(url)
    for item in EXPORTED:
        assert series(text, metric_name(item)), item.name
    assert series(text, "wft_state_connections")[""] > 0
    assert series(text, "wft_state_database_size_bytes")[""] > 0
    codes = CATALOGUE["wft_state_artifact_failures"]["labels"]["code"]["values"]
    labels = "".join(series(text, "wft_state_artifact_failures"))
    assert sorted(re.findall(r'code="([^"]+)"', labels)) == sorted(codes)


@pytest.mark.integration
def test_a_failing_query_keeps_the_last_value_and_counts_the_error(appdb, served):
    meter, url = served
    with psycopg.connect(appdb, autocommit=True) as admin:
        admin.execute("create table flip (n int)")
        admin.execute("insert into flip values (1)")
    inputs = parse(
        """
inputs:
  - name: flaky
    unit: rows
    source: {kind: sql, ref: select 10 / n from flip}
  - name: steady
    unit: rows
    source: {kind: sql, ref: select count(*) from flip}
"""
    )
    exporter = Exporter(meter, appdb, inputs)

    assert exporter.run_once() == 2
    assert series(scrape(url), "wft_state_flaky") == {"": 10.0}

    with psycopg.connect(appdb, autocommit=True) as admin:
        admin.execute("update flip set n = 0")
        admin.execute("insert into flip values (1)")
    assert exporter.run_once() == 1

    text = scrape(url)
    assert series(text, "wft_state_flaky") == {"": 10.0}
    assert series(text, "wft_state_steady") == {"": 2.0}
    assert exporter.errors == {"flaky": 1}


@pytest.mark.integration
def test_the_process_serves_the_inputs_and_stops_on_sigterm(reader, tmp_path):
    port, ready = free_port(), tmp_path / "ready"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "WFT_"))},
        "WFT_STATE_INPUTS_DSN": reader,
        "WFT_METRICS_ADDRESS": f"127.0.0.1:{port}",
        "WFT_READY_FILE": str(ready),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "wft_obs.inputs"], env=env, stdout=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert process.poll() is None, process.stdout.read()
            assert time.monotonic() < deadline, "the exporter never became ready"
            time.sleep(0.2)
        assert series(scrape(f"http://127.0.0.1:{port}/metrics"), "wft_state_connections")[""] > 0
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
    finally:
        process.kill()
        process.stdout.close()


def test_the_process_without_a_dsn_exits_2(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "WFT_"))}
    command = [sys.executable, "-m", "wft_obs.inputs"]
    run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 2
    assert "WFT_STATE_INPUTS_DSN" in run.stderr
