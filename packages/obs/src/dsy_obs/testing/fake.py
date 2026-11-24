"""One HTTP server on 127.0.0.1 that answers the read calls of `dsy_obs.query` for Prometheus, Loki,
Tempo and Pyroscope from canned data, and records the smoke gate's pushes to Prometheus's OTLP
receiver and Grafana's annotations.

The same address stands for every backend, so a caller passes `url` wherever it would pass the
address of one of them.
"""

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

TRACES = "/api/traces/"


@dataclass
class Received:
    method: str
    path: str
    query: dict[str, str]
    body: bytes


@dataclass
class FakeObs:
    # PromQL expression to its instant vector; an expression not listed answers an empty vector.
    vectors: dict[str, list[dict]] = field(default_factory=dict)
    active_targets: list[dict] = field(default_factory=list)
    # LogQL query to its streams; a query not listed answers no streams.
    streams: dict[str, list[dict]] = field(default_factory=dict)
    # Hex trace id to its OTLP JSON; an id not listed answers 404, as Tempo does.
    traces: dict[str, dict] = field(default_factory=dict)
    # The `service_name` values Pyroscope lists, whatever the window.
    services: list[str] = field(default_factory=list)
    # The bodies posted to Prometheus's OTLP receiver and the annotations posted to Grafana.
    pushed: list[bytes] = field(default_factory=list)
    annotations: list[dict] = field(default_factory=list)
    received: list[Received] = field(default_factory=list)
    url: str = ""
    _httpd: ThreadingHTTPServer | None = field(default=None, repr=False)

    def start(self) -> "FakeObs":
        handler = type("Bound", (_Handler,), {"fake": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> "FakeObs":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

    def answer(self, method: str, path: str, query: dict[str, str], body: bytes):
        """The status and JSON answer of one call."""
        match method, path:
            case "GET", "/api/v1/query":
                return 200, _success(
                    {"resultType": "vector", "result": self.vectors.get(query.get("query", ""), [])}
                )
            case "GET", "/api/v1/targets":
                return 200, _success({"activeTargets": self.active_targets, "droppedTargets": []})
            case "GET", "/loki/api/v1/query_range":
                result = self.streams.get(query.get("query", ""), [])
                return 200, _success({"resultType": "streams", "result": result})
            case "GET", _ if path.startswith(TRACES):
                found = self.traces.get(unquote(path.removeprefix(TRACES)))
                return (200, found) if found is not None else (404, {"error": "trace not found"})
            case "POST", "/querier.v1.QuerierService/LabelValues":
                asked = json.loads(body or b"{}").get("name")
                return 200, {"names": list(self.services) if asked == "service_name" else []}
            case "POST", "/api/v1/otlp/v1/metrics":
                self.pushed.append(body)
                return 200, {}
            case "POST", "/api/annotations":
                self.annotations.append(json.loads(body))
                return 200, {"id": len(self.annotations), "message": "Annotation added"}
        return 404, {"error": f"no route {method} {path}"}


def _success(data: dict) -> dict:
    return {"status": "success", "data": data}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: FakeObs

    def _handle(self) -> None:
        parts = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.fake.received.append(Received(self.command, parts.path, query, body))
        status, answer = self.fake.answer(self.command, parts.path, query, body)
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle

    def log_message(self, *args) -> None:
        pass
