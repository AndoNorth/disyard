"""Fakes of what the boundary rows reach: `cluster-verify` as a script, `kubectl port-forward` as a
canned HTTP server on the forward's local port, and the two API listeners as in-test servers."""

import json
import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

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

    def answer(answers: dict) -> Path:
        monkeypatch.setenv("FAKE_FORWARDS", json.dumps(answers))
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
