"""The peak resident memory of the admission service through a burst of 2,000 admissions, with the
real broker client against RabbitMQ and the starts on a recording fake over a Temporal client that
connects lazily, so the SDK's native core is loaded with no server. RabbitMQ and Postgres are the
two containers of the session, so the measurement runs in a module of its own."""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from wdf_broker.client import Credentials, connect
from wdf_broker.models import Outcome
from wdf_broker.tenancy import load
from wdf_broker.testing.container import broker_container
from wdf_broker.topology import intake_queues
from wdf_workflow.contract import naming

pytestmark = [pytest.mark.integration, pytest.mark.pair]

JOBS = 2000
# A margin under the 128 MiB memory limit of the service's pod.
PEAK_LIMIT_MIB = 112
LIMIT_S = 600
HELPER = Path(__file__).with_name("recording_service.py")
[QUEUE] = intake_queues(load("ci"))


@pytest.fixture(scope="module")
def rabbitmq(t1):
    with broker_container(t1, "ci") as broker:
        yield broker


def test_service_peak_memory(rabbitmq, appdb, admission_dsn, submit, counts, free_port, tmp_path):
    base = {name: value for name, value in os.environ.items() if not name.startswith("WDF_")}
    env = {
        **base,
        "WDF_TEMPORAL_ADDRESS": f"127.0.0.1:{free_port()}",
        "WDF_TEMPORAL_NAMESPACE": naming().namespace,
        "WDF_ADMISSION_DSN": admission_dsn,
        "WDF_BROKER_ADDRESS": rabbitmq.address,
        "WDF_BROKER_PASSWORD": rabbitmq.passwords["admission"],
        "WDF_TENANCY_PROFILE": "ci",
        "WDF_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
        "WDF_READY_FILE": str(tmp_path / "ready"),
        "WDF_TEST_SUPERUSER_DSN": appdb,
    }
    log = tmp_path / "service.log"
    with log.open("w") as stderr:
        process = subprocess.Popen(
            [sys.executable, str(HELPER)], env=env, stdout=subprocess.PIPE, stderr=stderr, text=True
        )
    try:
        publisher = connect(
            rabbitmq.address, Credentials("dispatch", rabbitmq.passwords["dispatch"])
        )

        def publish(body: bytes) -> None:
            # The intake queue holds at most 1,000 messages, so the burst waits while it is full.
            while (outcome := publisher.publish(QUEUE, body)) is Outcome.REJECTED:
                time.sleep(0.05)
            assert outcome is Outcome.ACCEPTED

        began = time.monotonic()
        try:
            submit(publish, JOBS)
        finally:
            publisher.close()
        while counts("succeeded") < JOBS:
            assert process.poll() is None, log.read_text()[-4000:]
            assert time.monotonic() - began < LIMIT_S, f"{counts('succeeded')} of {JOBS} admitted"
            time.sleep(0.5)
        elapsed = time.monotonic() - began
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        out, _ = process.communicate(timeout=60)
    assert process.returncode == 0, log.read_text()[-4000:]
    peak = float(re.search(r"peak_rss_mib: ([\d.]+)", out)[1])
    print(f"\njobs: {JOBS} admitted in {elapsed:.1f} s")
    print(out.strip())
    print(f"limit_mib: {PEAK_LIMIT_MIB}")
    assert peak <= PEAK_LIMIT_MIB
