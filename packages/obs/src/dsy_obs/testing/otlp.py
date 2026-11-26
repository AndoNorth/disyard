"""An OTLP HTTP receiver on a free loopback port, for a test that points an exporter at it.

It inflates a gzip body, keeps the body of every push by path, and decodes the spans of a trace
push.
Every push is answered with an empty success.
"""

import gzip
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

TRACES = "/v1/traces"


class OtlpReceiver:
    """Started on construction; `stop` it, or use it as a context manager."""

    def __init__(self):
        # Each push's inflated body, by request path.
        self.bodies: dict[str, list[bytes]] = defaultdict(list)
        # Every span pushed to /v1/traces, with the string attributes of its resource.
        self.spans: list[tuple[dict, object]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                outer._keep(self.path, data)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def _keep(self, path: str, data: bytes) -> None:
        with self._lock:
            self.bodies[path].append(data)
            if path == TRACES:
                request = ExportTraceServiceRequest.FromString(data)
                for resource_spans in request.resource_spans:
                    resource = {
                        a.key: a.value.string_value for a in resource_spans.resource.attributes
                    }
                    for scope in resource_spans.scope_spans:
                        self.spans += [(resource, span) for span in scope.spans]

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "OtlpReceiver":
        return self

    def __exit__(self, *_) -> None:
        self.stop()
