"""A real `python -m wdf_worker --pool <pool>` process per test, on a queue prefix of its own, over
the module's dev server and the session's SeaweedFS."""

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from wdf_storecheck.s3 import dump_credentials
from wdf_worker.config import ENV, MIRROR_ENV, PREFIX_ENV
from wdf_workflow.contract import naming

# The slot of each pool: simulate in the 512 MiB that spike S11 passed, since the 384 MiB of the ci
# preset leaves the guest no cap after the worker's reserve.
SLOTS = {"compile": 1024, "simulate": 512, "aggregate": 1024}
READY_S = 60


@dataclass
class Running:
    process: subprocess.Popen
    queue: str
    scratch: Path
    ready_file: Path
    log: Path

    def stop(self) -> None:
        self.process.send_signal(signal.SIGTERM)

    def output(self) -> str:
        return self.log.read_text(errors="replace")


def environment(pool: str, temporal: str, store, root: Path, prefix: str) -> dict[str, str]:
    """The environment of the pool's process, holding its own store identity only, on the host's
    environment with no other WDF_ variable."""
    credentials = root / "credentials.json"
    dump_credentials(credentials, {pool: store.credentials[pool]})
    given = {
        ENV["temporal_address"]: temporal,
        ENV["namespace"]: naming().namespace,
        ENV["store_endpoint"]: store.endpoint,
        ENV["store_credentials"]: str(credentials),
        ENV["slot_memory_mib"]: str(SLOTS[pool]),
        ENV["scratch"]: str(root / "scratch"),
        ENV["ready_file"]: str(root / "ready"),
        PREFIX_ENV: prefix,
    }
    if pool == "compile":
        # The workload library needs no package, so an empty wheelhouse stands in for the mirror.
        (root / "mirror").mkdir()
        given[MIRROR_ENV] = (root / "mirror").as_uri()
    host = {k: v for k, v in os.environ.items() if not k.startswith("WDF_")}
    return host | given


@pytest.fixture
def worker(temporal, pool_store, tmp_path):
    """Starts the pool's worker process and waits for its ready file. Each one still running at
    the end of the test is stopped, and its output printed."""
    started: list[Running] = []

    def start(pool: str) -> Running:
        root = tmp_path / pool
        root.mkdir()
        prefix = f"t-{uuid.uuid4().hex[:8]}-"
        log = root / "worker.log"
        process = subprocess.Popen(
            [sys.executable, "-m", "wdf_worker", "--pool", pool],
            env=environment(pool, temporal, pool_store, root, prefix),
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
        )
        running = Running(
            process, prefix + naming().pools[pool], root / "scratch", root / "ready", log
        )
        started.append(running)
        deadline = time.monotonic() + READY_S
        while not running.ready_file.exists():
            assert process.poll() is None, running.output()
            assert time.monotonic() < deadline, running.output()
            time.sleep(0.1)
        return running

    yield start
    for running in started:
        with contextlib.suppress(ProcessLookupError):
            running.process.kill()
        running.process.wait()
        print(running.output())
