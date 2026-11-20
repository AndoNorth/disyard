"""A fake Kubernetes API over TLS with a self-signed certificate, which serves a node list and a pod
list, records each request, and can fail or hold the reads a test asks it to."""

import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOLD_S = 10


def node(name: str, pool: str) -> dict:
    return {
        "metadata": {"name": name, "labels": {"wft.dev/pool": pool}},
        "status": {"allocatable": {"cpu": "1", "memory": "2Gi"}},
    }


def pod(name: str, node_name: str, phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "namespace": "wft-workers"},
        "spec": {
            "nodeName": node_name,
            "containers": [{"resources": {"requests": {"cpu": "250m"}}}],
        },
        "status": {"phase": phase},
    }


class FakeApi:
    def __init__(self):
        self.nodes = {"kind": "NodeList", "items": [node("wft-worker", "compile")]}
        self.pods = {"kind": "PodList", "items": [pod("compile-worker-0", "wft-worker")]}
        self.requests: list[tuple[str, str | None]] = []
        # The next `fail` node reads answer 500, and while `hold` is clear a node read waits on it.
        self.fail = 0
        self.hold = threading.Event()
        self.hold.set()
        self.lock = threading.Lock()
        self.url = ""

    def answer(self, path: str) -> tuple[int, dict]:
        if path.startswith("/api/v1/nodes?"):
            self.hold.wait(HOLD_S)
            with self.lock:
                if self.fail:
                    self.fail -= 1
                    return 500, {"kind": "Status", "code": 500}
            return 200, self.nodes
        if path.startswith("/api/v1/pods?"):
            return 200, self.pods
        return 404, {"kind": "Status", "code": 404}


def certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and its key."""
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1"]
        + ["-nodes", "-days", "1", "-subj", "/CN=127.0.0.1"]
        + ["-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture(scope="session")
def tls(tmp_path_factory) -> tuple[Path, Path]:
    return certificate(tmp_path_factory.mktemp("tls"))


@pytest.fixture(scope="session")
def other_ca(tmp_path_factory) -> Path:
    """A certificate that did not sign the fake API's."""
    return certificate(tmp_path_factory.mktemp("other"))[0]


@pytest.fixture
def api(tls):
    fake = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            fake.requests.append((self.path, self.headers.get("Authorization")))
            status, body = fake.answer(self.path)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(*tls)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    fake.url = f"https://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield fake
    fake.hold.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def token(tmp_path) -> Path:
    path = tmp_path / "token"
    path.write_text("token-1\n")
    return path
