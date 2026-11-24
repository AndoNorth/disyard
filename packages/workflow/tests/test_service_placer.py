"""ServicePlacer passes the placer contract over a placement server in this process, raises on a
refusal or a timeout so the `place` local activity retries, and the service picks it only when
DSY_PLACEMENT_URL is set."""

import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from dsy_workflow.placement import (
    PlaceCall,
    PlacementRequest,
    PlaceResult,
    ServicePlacer,
    StaticPlacer,
)
from dsy_workflow.service import ENV, REAPER_ENV, Config
from dsy_workflow.testing import placer_contract

SIMULATE = PlacementRequest(task_type="simulate", cpu_milli=250, memory_mib=512, run_timeout_s=900)


@contextmanager
def server(answer):
    """The URL of a server whose `POST /place` hands the body to `answer` and sends its
    `(status, body)`."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            status, reply = answer(self.path, body)
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
            except BrokenPipeError:
                # A client that timed out has closed the connection.
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def static(path: str, body: bytes) -> tuple[int, bytes]:
    if path != "/place":
        return 404, b"not found"
    call = PlaceCall.model_validate_json(body)
    answer = StaticPlacer().place(call.request, call.escalate)
    return 200, PlaceResult(answer).model_dump_json().encode()


def test_service_placer_passes_placer_contract():
    with server(static) as url:
        placer_contract(ServicePlacer(url))
        assert ServicePlacer(url + "/").place(SIMULATE) == StaticPlacer().place(SIMULATE)


@pytest.mark.parametrize("status", [204, 400, 404, 503])
def test_service_placer_raises_on_refusal(status):
    with server(lambda path, body: (status, b"" if status == 204 else b"refused")) as url:
        with pytest.raises((OSError, RuntimeError)):
            ServicePlacer(url).place(SIMULATE)


def test_service_placer_raises_on_timeout():
    def slow(path, body):
        time.sleep(1)
        return static(path, body)

    with server(slow) as url:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            ServicePlacer(url, timeout_s=0.2).place(SIMULATE)
        assert time.monotonic() - started < 1


def environ(**changes) -> dict[str, str]:
    return {name: "x" for name in [*ENV.values(), *REAPER_ENV]} | changes


def test_service_uses_static_without_url():
    absent = environ()
    del absent["DSY_PLACEMENT_URL"]
    for unset in (absent, environ(DSY_PLACEMENT_URL="")):
        config = Config.from_env(unset)
        assert config.placement_url is None
        assert isinstance(config.placer(), StaticPlacer)


def test_service_uses_service_placer_with_url():
    url = "http://placement.dsy-control.svc:8080"
    placer = Config.from_env(environ(DSY_PLACEMENT_URL=url)).placer()
    assert isinstance(placer, ServicePlacer)
    assert placer.url == f"{url}/place"
