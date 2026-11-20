"""The service of the tests: in this process over a store in a directory, FakeBroker and a
gateway the test picks, or in a process of its own through serve.py, as the pod runs it.

Every listener and the metrics endpoint bind a free port of the loopback.
"""

import hashlib
import http.client
import json
import os
import runpy
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from wft_broker.testing import FakeBroker
from wft_compile import bundle
from wft_dispatchapi.service import Config, Service

SERVE = Path(__file__).with_name("serve.py")
SERVE_TOOLS = runpy.run_path(str(SERVE))
DirStore = SERVE_TOOLS["DirStore"]
STORE_ENV = SERVE_TOOLS["STORE_ENV"]
READY_S = 60
# Any DSN of a login, for a test that never reaches the database.
NOWHERE = "postgresql://nobody:none@127.0.0.1:1/none?connect_timeout=1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def environment(logins: dict[str, str], temporal: str, **limits) -> dict[str, str]:
    """The environment of the service over `logins`, keyed as the `logins` fixture keys them,
    with every listener on a free port and each of `limits` as its WFT_DISPATCH_ variable."""
    env = {
        "WFT_TEMPORAL_ADDRESS": temporal,
        **{f"WFT_{key.upper()}_DSN": dsn for key, dsn in logins.items()},
        "WFT_STORE_ENDPOINT": "http://127.0.0.1:1",
        "WFT_SUBMIT_CREDENTIALS": "unused",
        "WFT_BROKER_ADDRESS": "127.0.0.1:1",
        "WFT_BROKER_PASSWORD": "unused",
        "WFT_TENANCY_PROFILE": "ci",
        "WFT_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
        "WFT_DISPATCH_API_LISTEN": f"127.0.0.1:{free_port()}",
        "WFT_CONTROL_API_LISTEN": f"127.0.0.1:{free_port()}",
    }
    env.update({f"WFT_DISPATCH_{name.upper()}": str(value) for name, value in limits.items()})
    return env


@pytest.fixture
def store(tmp_path):
    return DirStore(tmp_path / "store")


@pytest.fixture
def start(tenancy, store, tmp_path):
    """Starts a service in this process over `logins` and `gateway`, stopped after the test."""
    started = []

    def make(logins: dict[str, str], gateway, temporal: str = "127.0.0.1:1", **limits) -> Service:
        config = Config.from_env(environment(logins, temporal, **limits))
        service = Service(config, tenancy, store, gateway, FakeBroker("ci").connect, tmp_path)
        service.start()
        started.append(service)
        return service

    yield make
    for service in started:
        service.stop(drain_s=5)


@contextmanager
def process(env: dict[str, str], store_dir: Path, log: Path):
    """The service in a process of its own, logging to `log`, ready, with its URLs, and stopped by
    SIGTERM after."""
    full = {**os.environ, **env, STORE_ENV: str(store_dir)}
    with open(log, "ab") as out:
        proc = subprocess.Popen([sys.executable, str(SERVE)], env=full, stdout=out, stderr=out)
    urls = {
        "dispatch": f"http://{env['WFT_DISPATCH_API_LISTEN']}",
        "control": f"http://{env['WFT_CONTROL_API_LISTEN']}",
        "metrics": f"http://{env['WFT_METRICS_ADDRESS']}/metrics",
    }
    running = SimpleNamespace(proc=proc, **urls)
    try:
        deadline = time.monotonic() + READY_S
        while status(running.dispatch + "/readyz") != 200:
            assert proc.poll() is None, log.read_text()
            assert time.monotonic() < deadline, f"the service is not ready in {READY_S} s"
            time.sleep(0.2)
        yield running
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def spawn():
    return process


def status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return None


def request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, dict]:
    """The status and the JSON body of one call."""
    call = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(call, timeout=60) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def upload_bundle(url: str, tmp_path: Path) -> dict:
    """Uploads a fresh source bundle with the module `job` through the upload route, and returns
    its reference."""
    tree = tmp_path / secrets.token_hex(4)
    (tree / "job").mkdir(parents=True)
    (tree / "pyproject.toml").write_text('[project]\nname = "job"\nversion = "0"\n')
    (tree / "uv.lock").write_text(f"# {secrets.token_hex(8)}\n")
    (tree / "job" / "__init__.py").write_text("def run():\n    return 0\n")
    data = bundle.write(tree, tree.parent / f"{tree.name}-out").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    code, answer = request(f"{url}/v1/tenants/acme/objects/source/{sha}", "PUT", data)
    assert code == 201, answer
    return answer["reference"]


def template(source: dict) -> dict:
    """A compile of `source`, a simulate and an aggregate, with a seed and one integer parameter
    per job."""
    return {
        "schema": "urn:wft:schema:job:1.0",
        "parameters": {"seed": "seed", "n": "integer"},
        "tasks": {
            "build": {"type": "compile", "params": {"source": source, "entry": "job"}},
            "sim": {
                "type": "simulate",
                "inputs": [{"task": "build", "output": "component"}],
                "cpu": 0.25,
                "memory_mib": 384,
                "run_timeout_s": 600,
                "retries": 1,
                "params": {
                    "workload": "pi",
                    "args": {"n": {"param": "n"}},
                    "seed": {"param": "seed"},
                },
            },
            "agg": {
                "type": "aggregate",
                "inputs": [{"task": "sim", "output": "result"}],
                "cpu": 1,
                "memory_mib": 512,
                "run_timeout_s": 600,
                "params": {"aggregation": "summary_stats", "args": {"blocks": ["pi"]}},
            },
        },
    }


def batch(source: dict, batch_key: str, count: int) -> Iterator[bytes]:
    """The lines of a batch submit body of `count` jobs."""
    yield json.dumps({"batch_key": batch_key, "template": template(source)}).encode() + b"\n"
    for index in range(count):
        entry = {"index": index, "seed": index, "parameters": {"n": index % 97}}
        yield json.dumps(entry).encode() + b"\n"


class Call:
    """A batch submit call that sends its body from a thread while the test reads the answer
    lines, as the contract asks of a client. The body is sent in chunks of up to 500 lines, and
    after `pause_after` lines the sender waits until `go` is set."""

    def __init__(self, url: str, lines: Iterable[bytes], pause_after: int | None = None):
        host, port = url.removeprefix("http://").split(":")
        self.sock = socket.create_connection((host, int(port)))
        self.go = threading.Event()
        self.sender = threading.Thread(target=self._send, args=(lines, pause_after), daemon=True)
        self.sender.start()
        self.response = http.client.HTTPResponse(self.sock)
        self.response.begin()

    def _send(self, lines: Iterable[bytes], pause_after: int | None) -> None:
        head = (
            "POST /v1/tenants/acme/batches HTTP/1.1\r\nHost: test\r\n"
            "Content-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        try:
            self.sock.sendall(head.encode())
            pending = []
            for sent, data in enumerate(lines):
                if sent == pause_after:
                    self._chunk(pending)
                    pending = []
                    self.go.wait()
                pending.append(data)
                if len(pending) == 500:
                    self._chunk(pending)
                    pending = []
            self._chunk(pending)
            self.sock.sendall(b"0\r\n\r\n")
        except OSError:
            # The server ended the call and closed, so the rest of the body has nowhere to go.
            pass

    def _chunk(self, lines: list[bytes]) -> None:
        if payload := b"".join(lines):
            self.sock.sendall(b"%x\r\n%b\r\n" % (len(payload), payload))

    def answers(self) -> Iterator[dict]:
        try:
            while text := self.response.readline():
                yield json.loads(text)
        finally:
            self.go.set()
            self.sender.join(30)
            self.sock.close()


@pytest.fixture
def kit() -> SimpleNamespace:
    """The helpers above, for the tests of this directory, which the importlib import mode of the
    suite keeps from importing this module."""
    return SimpleNamespace(
        environment=environment,
        free_port=free_port,
        status=status,
        request=request,
        upload_bundle=upload_bundle,
        template=template,
        batch=batch,
        Call=Call,
        nowhere=NOWHERE,
    )
