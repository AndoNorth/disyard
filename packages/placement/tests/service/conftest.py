"""The service in process over the fake Kubernetes API of the reader's tests, serving the `ci`
cluster of the policy tests, with its decision log captured."""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from dsy_placement import kube, service
from dsy_servicekit.testing import free_port

TESTS = Path(__file__).parents[1]


def ci_cluster() -> tuple[dict, dict]:
    items = json.loads((TESTS / "policy/ci-cluster.json").read_text())["items"]
    return (
        {"kind": "NodeList", "items": [i for i in items if i["kind"] == "Node"]},
        {"kind": "PodList", "items": [i for i in items if i["kind"] == "Pod"]},
    )


@pytest.fixture
def ci_api(api):
    """The fake API answering with the `ci` cluster."""
    api.nodes, api.pods = ci_cluster()
    return api


@pytest.fixture
def account(tmp_path, tls, token) -> Path:
    """A service account directory as a pod mounts it, trusting the fake API's certificate."""
    directory = tmp_path / "serviceaccount"
    directory.mkdir()
    (directory / "token").write_text(token.read_text())
    (directory / "ca.crt").write_bytes(tls[0].read_bytes())
    return directory


@pytest.fixture
def running(api, account):
    """Starts a service on a free port over the fake API with a snapshotter reading every
    `every_s`, and stops each one after the test."""

    def start(every_s: float = 60) -> service.Service:
        reader = kube.Reader(api.url, str(account / "token"), str(account / "ca.crt"))
        snapshotter = kube.Snapshotter(reader, every_s).start()
        made.append((service.Service(("127.0.0.1", 0), snapshotter), snapshotter))
        threading.Thread(target=made[-1][0].serve, daemon=True).start()
        return made[-1][0]

    made: list[tuple[service.Service, kube.Snapshotter]] = []
    yield start
    api.hold.set()
    for served, snapshotter in made:
        served.stop()
        snapshotter.stop()


@pytest.fixture
def decisions(caplog):
    """The decision log lines written so far, as the service writes them."""
    caplog.set_level(logging.INFO, logger=service.__name__)
    return lambda: [m for m in caplog.messages if m.startswith(("placed ", "unschedulable "))]


# The service's own entry point, with the service account directory given on the command line in
# place of the one a pod mounts.
START_ATTEMPTS = 5

ENTRY = "import sys; from pathlib import Path; from dsy_placement.service import main; " + (
    "sys.exit(main(account=Path(sys.argv[1])))"
)


@pytest.fixture
def spawn(api, account, tmp_path):
    """Starts the service in its own process over the fake API, waits for its ready file and returns
    its URL, its process and the file of its stdout; the process is killed after the test."""

    def launch(every_s: float, port: int) -> subprocess.Popen:
        api_host, _, api_port = api.url.removeprefix("https://").rpartition(":")
        env = os.environ | {
            "DSY_PLACEMENT_LISTEN": f"127.0.0.1:{port}",
            "DSY_SNAPSHOT_EVERY_S": str(every_s),
            "DSY_READY_FILE": str(ready),
            "DSY_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
            "KUBERNETES_SERVICE_HOST": api_host,
            "KUBERNETES_SERVICE_PORT": api_port,
        }
        with stdout.open("w") as out:
            made.append(
                subprocess.Popen(
                    [sys.executable, "-c", ENTRY, str(account)],
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            )
        return made[-1]

    def start(every_s: float = 30) -> tuple[str, subprocess.Popen, Path]:
        # A port free when chosen can be taken before the service binds it; a start that dies on
        # that is retried on fresh ports.
        for attempt in range(START_ATTEMPTS):
            port = free_port()
            process = launch(every_s, port)
            deadline = time.monotonic() + 30
            while not (ready.exists() and ready.stat().st_size == 0):
                if process.poll() is not None:
                    text = stdout.read_text()
                    assert "Address already in use" in text and attempt + 1 < START_ATTEMPTS, text
                    break
                assert time.monotonic() < deadline, "the service never became ready"
                time.sleep(0.05)
            else:
                return f"http://127.0.0.1:{port}", process, stdout

    ready, stdout = tmp_path / "ready", tmp_path / "stdout"
    made: list[subprocess.Popen] = []
    yield start
    api.hold.set()
    for process in made:
        process.kill()
        process.wait()


@pytest.fixture
def until():
    """Waits for a condition, failing the test after `timeout_s`."""

    def wait(condition, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not condition():
            assert time.monotonic() < deadline, "condition not met in time"
            time.sleep(0.01)

    return wait
