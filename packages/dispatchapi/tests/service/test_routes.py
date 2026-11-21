"""The route table, the listeners and the cap on requests in flight, over a service whose logins
reach no database, so every route answers without one."""

import http.client
import socket
import time
import uuid
from pathlib import Path

import pytest
from wft_dispatchapi.contract import routes
from wft_dispatchapi.limits import ConfigError
from wft_dispatchapi.service import ENV, Config, limits_profile
from wft_dispatchapi.service.routes import ROUTES
from wft_dispatchapi.testing import FakeGateway
from wft_jobdoc.profile_files import load_profile

JOB = str(uuid.uuid4())
VALUES = {
    "tenant": "acme",
    "kind": "index",
    "sha256": "0" * 64,
    "batch_key": "b",
    "job_id": JOB,
    "action": "cancel",
}
BODIES = {"PUT": b"x", "POST": b"{}", "GET": b""}


@pytest.fixture
def service(start, kit):
    nowhere = dict.fromkeys(("dispatch", "control", "artifacts", "reader"), kit.nowhere)
    return start(nowhere, FakeGateway())


def call(url: str, method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
    host, port = url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        connection.request(method, path, body=body)
        answer = connection.getresponse()
        return answer.status, answer.read()
    finally:
        connection.close()


def test_route_table_equals_routes_yaml():
    assert set(ROUTES) == {(route.method, route.path) for route in routes().routes}


def test_every_route_on_its_listener_only(service):
    for route in routes().routes:
        path = route.path.format(**VALUES)
        for listener in ("dispatch-api", "control-api"):
            status, body = call(service.url(listener), route.method, path, BODIES[route.method])
            where = f"{route.method} {route.path} on {listener}"
            if listener in route.listeners:
                assert body, f"{where} was not routed: {status}"
            else:
                assert (status, body) == (404, b""), f"{where} was routed"


def test_unknown_tenant_and_unknown_action(service):
    status, body = call(service.url("control-api"), "GET", f"/v1/tenants/globex/jobs/{JOB}")
    assert status == 404 and b"UNKNOWN_TENANT" in body
    status, body = call(service.url("control-api"), "POST", f"/v1/tenants/acme/jobs/{JOB}/stop")
    assert (status, body) == (404, b"")


def test_sixteen_in_flight_then_too_many_calls(service):
    control = service.url("control-api").removeprefix("http://").split(":")
    held = []
    for _ in range(service.limits.requests):
        sock = socket.create_connection((control[0], int(control[1])))
        # The body is cut short, so each request waits in its handler for the rest of it.
        sock.sendall(
            f"POST /v1/tenants/acme/jobs/{JOB}/cancel HTTP/1.1\r\nHost: test\r\n"
            "Content-Length: 2\r\n\r\n{".encode()
        )
        held.append(sock)
    deadline = time.monotonic() + 10
    while service.in_flight < service.limits.requests:
        assert time.monotonic() < deadline, f"{service.in_flight} requests in flight"
        time.sleep(0.05)

    status, body = call(service.url("dispatch-api"), "GET", "/v1/tenants/acme/depth")
    assert status == 429 and b"TOO_MANY_CALLS" in body
    assert call(service.url("control-api"), "GET", "/healthz")[0] == 200

    for sock in held:
        sock.sendall(b"}")
    for sock in held:
        answer = http.client.HTTPResponse(sock)
        answer.begin()
        # The logins reach no database, so the cancel itself answers that.
        assert answer.status == 503 and b"DATABASE_UNAVAILABLE" in answer.read()
        sock.close()
    status, body = call(service.url("dispatch-api"), "GET", "/v1/tenants/acme/depth")
    assert status == 503 and b"DATABASE_UNAVAILABLE" in body


def test_limits_profile_equals_the_ci_files(root: Path):
    assert limits_profile() == load_profile("ci", root)


def test_config_names_every_missing_variable():
    with pytest.raises(ConfigError) as refused:
        Config.from_env({"WFT_TENANCY_PROFILE": "ci"})
    assert all(name in str(refused.value) for name in ENV.values() if name != "WFT_TENANCY_PROFILE")
