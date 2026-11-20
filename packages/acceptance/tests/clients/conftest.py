"""An in-test HTTP server on 127.0.0.1 that answers each call with the next canned answer of its
method and path, query left out, and records every call it got."""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest


@dataclass
class Call:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Server:
    url: str
    # (method, path) to the (status, body) answers still to give, in order.
    answers: dict[tuple[str, str], list[tuple[int, bytes]]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def answer(self, method: str, path: str, *answers: tuple[int, bytes]) -> None:
        self.answers.setdefault((method, path), []).extend(answers)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: Server

    def body(self) -> bytes:
        if self.headers.get("Transfer-Encoding") == "chunked":
            data = b""
            while size := int(self.rfile.readline(), 16):
                data += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
            return data
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def handle_call(self) -> None:
        body, path = self.body(), urlsplit(self.path).path
        self.state.calls.append(Call(self.command, path, dict(self.headers), body))
        queued = self.state.answers.get((self.command, path))
        status, answer = queued.pop(0) if queued else (404, b'{"error": "no canned answer"}')
        self.send_response(status)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    do_GET = do_PUT = do_POST = handle_call

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def serve() -> Iterator:
    """Starts servers on demand, each on a free port, and stops them all after the test."""
    started = []

    def start() -> Server:
        state = Server("")
        handler = type("Bound", (Handler,), {"state": state})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return state

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()
