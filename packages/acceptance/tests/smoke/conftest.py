"""Fakes of what the gate reaches: `cluster-verify` as a script, `kubectl port-forward` as a
canned HTTP server on the forward's local port, the two API listeners as in-test servers, and the
API, store, broker and Temporal clients as stand-ins, so no cluster is touched. `healthy` makes
every one answer as a healthy ci cluster does."""

import hashlib
import io
import json
import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from wdf_kernel import writer
from wdf_kernel.expected import expected_blocks, expected_digest
from wdf_workflow.harness import echo_params
from wdf_workflow.harness.cluster import CurrentBuild

# Serves the canned answer of the forwarded resource from FAKE_FORWARDS, a JSON map of resource to
# [status, body], on the forward's local port, or never listens when the answer is "never".
KUBECTL = """
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
resource, ports = sys.argv[-2:]
answer = json.loads(os.environ["FAKE_FORWARDS"]).get(resource, [404, {}])
if answer == "never":
    time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(answer[1]).encode()
        self.send_response(answer[0])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("127.0.0.1", int(ports.split(":")[0])), Handler).serve_forever()
"""


@pytest.fixture
def forwards(tmp_path, monkeypatch) -> Callable[[dict], Path]:
    """Puts a fake kubectl on PATH that serves the given answers per resource, and returns the log
    of its calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = tmp_path / "kubectl.py"
    script.write_text(KUBECTL)
    fake = bin_dir / "kubectl"
    fake.write_text(f'#!{shutil.which("bash")}\nexec {sys.executable} {script} "$@"\n')
    fake.chmod(0o755)
    log = tmp_path / "kubectl.log"
    log.touch()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))

    served: dict = {}

    def answer(answers: dict) -> Path:
        """Adds the answers to those given before, a resource's latest answer winning."""
        served.update(answers)
        monkeypatch.setenv("FAKE_FORWARDS", json.dumps(served))
        return log

    return answer


@pytest.fixture
def verify(tmp_path, monkeypatch) -> Callable[..., None]:
    """Stands a script in for cluster-verify that prints `stdout` and `stderr`, then sleeps
    `sleep_s`, then exits with `code`."""
    from wdf_acceptance import smoke

    def stand_in(code: int, stdout: str = "", stderr: str = "", sleep_s: float = 0) -> None:
        script = tmp_path / "cluster-verify.sh"
        (tmp_path / "verify.out").write_text(stdout)
        (tmp_path / "verify.err").write_text(stderr)
        script.write_text(
            f"#!{shutil.which('bash')}\ncat {tmp_path}/verify.out\ncat {tmp_path}/verify.err >&2\n"
            f"sleep {sleep_s}\nexit {code}\n"
        )
        script.chmod(0o755)
        monkeypatch.setattr(smoke, "VERIFY", script)

    return stand_in


@pytest.fixture
def listener() -> Iterator[Callable[[int, dict], str]]:
    """Starts an in-test server that answers every GET with the canned status and body, and
    returns its base url."""
    started = []

    def start(status: int, body: dict) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


JOB_ID = "0b6e2f0a-5d1c-4f43-9a51-6b1f0d3c2e77"
VERIFIED = "ok preset fine\nok ports every host port listens on 127.0.0.1 only\n"
COUNTED = "cluster-verify ci: 10 ok, 0 failed\n"
POOLS = {"pools": {"compile": {}, "simulate": {}, "aggregate": {}}}
READY = {"status": "ready"}


def results_xml(seed: int) -> bytes:
    """A results.xml of the level 0 echo vector at `seed`, which holds its known answer only at the
    vector's own seed."""
    sink = io.BytesIO()
    writer.write(sink, seed, expected_blocks("echo", seed, echo_params()["args"]))
    return sink.getvalue()


def task(task_id: str, state: str, attempt: int) -> dict:
    return {"id": task_id, "state": state, "task_attempt": attempt}


def reference(task_id: str, kind: str, name: str, data: bytes) -> dict:
    return {
        "uri": f"s3://wdf-results/acme/b/{JOB_ID}/42/{task_id}/r1a1/{name}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "name": name,
        "kind": kind,
        "producer": {"job": JOB_ID, "task": task_id, "attempt": "r1a1"},
    }


RESULT = results_xml(echo_params()["seed"])
DIGEST = expected_digest("echo", echo_params()["seed"], echo_params()["args"])
SUMMARY = b'{"digests": []}'
ADMITTED = {"status": "admitted", "detail": {"tasks": [task("build", "running", 1)]}, "results": []}
RUNNING = {
    "status": "running",
    "detail": {"tasks": [task("build", "succeeded", 1), task("sim", "running", 1)]},
    "results": [],
}
SUCCEEDED = {
    "status": "succeeded",
    "detail": None,
    "results": [
        reference("sim", "result", "results.xml", RESULT),
        reference("agg", "summary", "summary.json", SUMMARY),
    ],
}


@dataclass
class Job:
    """What the API, the store and the broker answer for the job, and the API calls made."""

    # The status samples in order, the last one repeated; a callable sample is called instead.
    statuses: list
    # Each stored object by its uri.
    objects: dict[str, bytes]
    # The dead-letter counts in order, the last one repeated.
    dead_letters: list[int]
    calls: list[tuple] = field(default_factory=list)


@dataclass
class Healthy:
    # The fake kubectl's call log.
    log: Path
    job: Job


class Service:
    async def describe_namespace(self, request):
        assert request.namespace == "wdf"


class Client:
    namespace = "wdf"
    workflow_service = Service()


@pytest.fixture
def healthy(verify, forwards, listener, monkeypatch) -> Healthy:
    """Every layer answers as a healthy ci cluster does, the broker with no dead letter, Temporal
    with build exit-a current and deployed, and the job admitted, running with its compile done,
    then succeeded with results that hold the known answer."""
    from wdf_acceptance import smoke
    from wdf_acceptance.clients import broker, store

    verify(0, VERIFIED + COUNTED)
    log = forwards({"svc/placement": [200, POOLS]})
    monkeypatch.setattr(smoke, "DISPATCH", listener(200, READY))
    monkeypatch.setattr(smoke, "CONTROL_API", listener(200, READY))
    job = Job(
        statuses=[ADMITTED, RUNNING, SUCCEEDED],
        objects={
            r["uri"]: data for r, data in zip(SUCCEEDED["results"], (RESULT, SUMMARY), strict=True)
        },
        dead_letters=[0],
    )

    def next_of(answers: list):
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        return answer() if callable(answer) else answer

    class Api:
        def __init__(self, dispatch, control, tenant, timeout_s):
            assert tenant == "acme"

        def upload_source(self, data):
            job.calls.append(("upload", hashlib.sha256(data).hexdigest()))
            return {"uri": "s3://wdf-sources/sha256/ab/abc", "kind": "source"}

        def submit_one(self, document, batch_key):
            job.calls.append(("submit", batch_key))
            return JOB_ID

        def status(self, job_id):
            assert job_id == JOB_ID
            return next_of(job.statuses)

        def cancel_batch(self, batch_key):
            job.calls.append(("cancel", batch_key))
            return {"cancelled": 1}

    def fetch(uri, identity, endpoint):
        assert identity == "export"
        return job.objects[uri]

    monkeypatch.setattr(smoke.api, "Api", Api)
    monkeypatch.setattr(store, "fetch", fetch)
    monkeypatch.setattr(
        broker, "dead_letters", lambda address, timeout_s: next_of(job.dead_letters)
    )

    async def connect(address):
        return Client()

    async def current(client, context, timeout_s):
        assert (context, timeout_s) == ("kind-wdf", smoke.POLLERS_S)
        return CurrentBuild("exit-a", ("exit-a",), None)

    monkeypatch.setattr(smoke.temporal, "connect", connect)
    monkeypatch.setattr(smoke, "make_deployed_current", current)
    monkeypatch.delenv("WDF_PRESET", raising=False)
    return Healthy(log, job)


@pytest.fixture
def canned():
    """This module, for its canned answers and the helpers that build more, since test modules
    are imported by path and cannot import a conftest."""
    return sys.modules[__name__]
