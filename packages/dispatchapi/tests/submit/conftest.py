"""The submit route over Postgres and SeaweedFS: a store client and a resolver of the `submit`
identity, the dispatch and artifacts logins, a template that names an uploaded source bundle, and
two ways to send a call: in process, and over HTTP as a client that reads while it sends."""

import http.client
import io
import json
import re
import secrets
import socket
import threading
from collections.abc import Iterable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from dsy_artifactclient.client import ArtifactClient, RetryPolicy
from dsy_compile import bundle
from dsy_dispatchapi.limits import Limits
from dsy_dispatchapi.ndjson import Reader, Writer, request_body
from dsy_dispatchapi.objects import Refused
from dsy_dispatchapi.resolver import StoreSourceResolver
from dsy_dispatchapi.submit import Submit
from dsy_jobdoc.profile_files import load_profile

NO_WAIT = RetryPolicy(sleep=lambda seconds: None)
ROUTE = re.compile("^/v1/tenants/([^/]+)/batches$")


@pytest.fixture(scope="session")
def profile(root):
    return load_profile("ci", root)


@pytest.fixture
def store(shelf) -> ArtifactClient:
    return ArtifactClient(shelf.endpoint, shelf.credentials["submit"], NO_WAIT)


@pytest.fixture
def make_submit(store, profile, tenancy, tmp_path):
    def make(tenancy=tenancy, **limits) -> Submit:
        resolver = StoreSourceResolver(store, tmp_path)
        return Submit(store, resolver, profile, tenancy, Limits(**limits))

    return make


@pytest.fixture
def dispatch(logins):
    with psycopg.connect(logins["dispatch"], autocommit=True) as connection:
        yield connection


@pytest.fixture
def artifacts(logins):
    with psycopg.connect(logins["artifacts"], autocommit=True) as connection:
        yield connection


@pytest.fixture
def source(store, tmp_path) -> dict:
    """The reference of a fresh source bundle with the module `job`, uploaded as the upload route
    stores it."""
    tree = tmp_path / secrets.token_hex(4)
    (tree / "job").mkdir(parents=True)
    (tree / "pyproject.toml").write_text('[project]\nname = "job"\nversion = "0"\n')
    (tree / "uv.lock").write_text(f"# {secrets.token_hex(8)}\n")
    (tree / "job" / "__init__.py").write_text("def run():\n    return 0\n")
    path = bundle.write(tree, tree.parent / f"{tree.name}-out")
    return store.put_content(path, "source", bundle.NAME).model_dump(mode="json")


@pytest.fixture
def template(source) -> dict:
    """Three tasks, a compile of the uploaded bundle, a simulate and an aggregate, with a seed and
    one integer parameter per job."""
    return {
        "schema": "urn:dsy:schema:job:1.0",
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


def line(data: dict) -> bytes:
    return json.dumps(data).encode() + b"\n"


@pytest.fixture
def entries():
    """The lines of `count` valid entries from `start`, made one at a time."""

    def make(count: int, start: int = 0) -> Iterator[bytes]:
        for index in range(start, start + count):
            yield line({"index": index, "seed": index, "parameters": {"n": index % 97}})

    return make


def unchunk(raw: bytes) -> list[dict]:
    """The lines of an answer body written with chunked transfer encoding."""
    body, rest = b"", raw
    while True:
        size, rest = rest.split(b"\r\n", 1)
        if not (length := int(size, 16)):
            break
        body, rest = body + rest[:length], rest[length + 2 :]
    return [json.loads(text) for text in body.splitlines()]


@pytest.fixture
def call(dispatch, artifacts):
    """Sends one call in process and returns its answer lines, or raises Refused."""

    def send(submit: Submit, body, tenant: str = "acme") -> list[dict]:
        if isinstance(body, bytes | list):
            body = io.BytesIO(body if isinstance(body, bytes) else b"".join(body))
        answer = io.BytesIO()
        reader = Reader(body, submit.limits)
        submit.call(tenant, reader, dispatch, artifacts, lambda: Writer(answer, submit.limits))
        return unchunk(answer.getvalue())

    return send


@pytest.fixture
def route(make_submit, logins):
    """The address of an HTTP server that serves the submit route alone, each call on connections
    of its own, as the service does."""
    submit = make_submit()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            tenant = ROUTE.fullmatch(self.path).group(1)
            reader = Reader(request_body(self.rfile, self.headers), submit.limits, self.connection)
            self.close_connection = True

            def begin() -> Writer:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                return Writer(self.wfile, submit.limits, self.connection)

            with (
                psycopg.connect(logins["dispatch"], autocommit=True) as dispatch,
                psycopg.connect(logins["artifacts"], autocommit=True) as artifacts,
            ):
                try:
                    submit.call(tenant, reader, dispatch, artifacts, begin)
                except Refused as refused:
                    body = json.dumps({"code": refused.code, "message": refused.message}).encode()
                    self.send_response(refused.http)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


@pytest.fixture
def post():
    return _post


def _post(address, tenant: str, lines: Iterable[bytes]) -> tuple[int, Iterator[dict]]:
    """Posts a chunked body from one thread while the caller reads the answer lines, as the
    contract asks of a client, because the server answers each chunk before it reads the next."""
    sock = socket.create_connection(address)
    head = (
        f"POST /v1/tenants/{tenant}/batches HTTP/1.1\r\nHost: test\r\n"
        "Content-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n"
    )

    def send():
        sock.sendall(head.encode())
        batch = []
        for data in lines:
            batch.append(data)
            if len(batch) == 500:
                payload = b"".join(batch)
                sock.sendall(b"%x\r\n%b\r\n" % (len(payload), payload))
                batch = []
        payload = b"".join(batch)
        if payload:
            sock.sendall(b"%x\r\n%b\r\n" % (len(payload), payload))
        sock.sendall(b"0\r\n\r\n")

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    response = http.client.HTTPResponse(sock)
    response.begin()

    def answers() -> Iterator[dict]:
        try:
            while text := response.readline():
                yield json.loads(text)
        finally:
            sender.join()
            sock.close()

    return response.status, answers()
