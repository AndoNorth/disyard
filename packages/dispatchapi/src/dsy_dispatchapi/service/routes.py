"""The route table of the service and its HTTP handler: each route of routes.yaml mapped to the
handler that answers it, served on the listeners that routes.yaml names for it.

A path outside the table, or on a listener that does not serve it, answers 404 with no body. A
path under `/v1/tenants/{tenant}` answers 404 `UNKNOWN_TENANT` before any body is read when the
tenant is not in the tenancy profile, and every route but the probes answers 429 `TOO_MANY_CALLS`
while the process has its limit of requests in flight. Every connection closes after its answer,
because an answer refused before the body was read leaves the body unread.
"""

import logging
import re
import socket
import time
from collections.abc import Callable
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, get_args
from urllib.parse import parse_qsl, unquote

import psycopg
from dsy_artifactclient.errors import ArtifactError
from dsy_obs import logs
from dsy_temporal import new_traceparent, trace_id_of
from dsy_temporal.trace import stage_span
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ValidationError

from .. import control, reads
from ..contract import reasons, routes
from ..control import ControlRefused
from ..models import (
    HEADER_LINE_MAX_BYTES,
    Action,
    ControlAnswer,
    ControlRequest,
    EntryAnswer,
    ErrorBody,
    ErrorLine,
    Health,
    SummaryLine,
)
from ..ndjson import Reader, Stop, Writer, request_body
from ..objects import Refused
from ..ports import TemporalUnavailable
from ..reads import ReadRefused

if TYPE_CHECKING:
    from . import Service

log = logging.getLogger(__name__)

PROBES = frozenset({"/healthz", "/readyz"})
# A control body is a reason and, on a resume, a template of at most the header line's size.
BODY_MAX = HEADER_LINE_MAX_BYTES
# The code of a control body that is not a ControlRequest. The contract has no code of its own for
# it, and this one is a 400 that names a request the client must fix.
BAD_BODY = "INVALID_QUERY"
# A submit or batch control call cut short by the service's stop: every line before it stands,
# and the client sends the call again once the service is back.
STOPPING = ("TOO_MANY_CALLS", "the service is stopping, so the call ends here and a resend is safe")
# How long a closing connection reads what the client still sends.
LINGER_S = 5.0
HTTP = {row.code: row.http for row in reasons().reasons}
PARAM = re.compile(r"^\{(\w+)\}$")
_PROPAGATOR = TraceContextTextMapPropagator()
tracer = trace.get_tracer("dsy")


class _NoRoute(Exception):
    """A path whose parameter takes a value outside the route's, which no route serves."""


class Handler(BaseHTTPRequestHandler):
    """One request on one listener of `service`, which the server class sets."""

    protocol_version = "HTTP/1.1"
    service: "Service"
    listener: str

    def do_GET(self):
        self._route("GET")

    def do_PUT(self):
        self._route("PUT")

    def do_POST(self):
        self._route("POST")

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)

    def finish(self):
        """Ends the answer, then reads and drops what the client still sends for a while: a close
        with request bytes unread resets the connection, which can drop the answer before the
        client reads it, and an answer refused or a call stopped before its body ends leaves
        bytes unread."""
        super().finish()
        try:
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(LINGER_S)
            deadline = time.monotonic() + LINGER_S
            while time.monotonic() < deadline and self.connection.recv(65_536):
                pass
        except OSError:
            pass

    def _route(self, method: str) -> None:
        self.close_connection = True
        self.trace_id = trace_id_of(new_traceparent())
        path, _, query = self.path.partition("?")
        found = match(self.listener, method, path)
        if found is None:
            return self._send(404, b"", None)
        route, params = found
        if route[1] in PROBES:
            return ROUTES[route](self.service, self, params, query)
        with self.service.request() as admitted:
            if not admitted:
                limit = self.service.limits.requests
                return self.refuse("TOO_MANY_CALLS", f"the process has {limit} requests in flight")
            tenant = params.get("tenant")
            if tenant is not None and tenant not in self.service.tenancy.tenants:
                message = f"{tenant} is not a tenant of the configuration"
                return self.refuse("UNKNOWN_TENANT", message)
            try:
                ROUTES[route](self.service, self, params, query)
            except _NoRoute:
                self._send(404, b"", None)
            except (Refused, ControlRefused, ReadRefused) as refused:
                self.refuse(refused.code, refused.message)
            except psycopg.OperationalError as error:
                self.refuse("DATABASE_UNAVAILABLE", str(error))
            except ArtifactError as error:
                self.refuse("STORE_UNAVAILABLE", str(error))
            except TemporalUnavailable as error:
                self.refuse("TEMPORAL_UNAVAILABLE", str(error))
            except OSError as error:
                # The client is gone or idle, so no answer can reach it.
                log.info("%s %s: the connection ended: %s", method, path, error)

    def _send(self, status: int, body: bytes, content_type: str | None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def answer(self, status: int, model: BaseModel) -> None:
        self._send(status, model.model_dump_json().encode(), "application/json")

    def refuse(self, code: str, message: str) -> None:
        self.answer(HTTP[code], ErrorBody(code=code, message=message, trace_id=self.trace_id))

    def ndjson(self) -> Writer:
        """Begins an NDJSON answer and returns its writer."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        return Writer(self.wfile, self.service.limits, self.connection)

    def body(self):
        self.connection.settimeout(self.service.limits.idle_timeout_s)
        return request_body(self.rfile, self.headers)

    def control_request(self) -> ControlRequest:
        data = self.body().read(BODY_MAX + 1)
        if len(data) > BODY_MAX:
            raise Refused(BAD_BODY, f"the body is above {BODY_MAX} bytes")
        try:
            return ControlRequest.model_validate_json(data or b"{}")
        except ValidationError as error:
            raise Refused(BAD_BODY, f"the body is not a control request: {error}") from None


def upload(service: "Service", request: Handler, params: dict, query: str) -> None:
    status, answer = service.uploads.put(params["kind"], params["sha256"], request.body())
    request.answer(status, answer)


class _Draining(Reader):
    """A submit body that ends at the next entry once the service stops, after which the call
    answers the entries it read and then an ErrorLine."""

    def __init__(self, body, limits, connection, stopping):
        super().__init__(body, limits, connection)
        self._stopping = stopping

    def entries(self):
        items = super().entries()
        while not self._stopping.is_set():
            if (item := next(items, None)) is None:
                return
            yield item
            if isinstance(item, Stop):
                return
        yield Stop(*STOPPING)


class _Answers:
    """The writer of a submit answer, which counts each entry's outcome, links the call's span to
    the jobs of its first chunk, and logs and spans each accepted job in the trace of the job."""

    def __init__(
        self,
        writer: Writer,
        service: "Service",
        span,
        dispatch: psycopg.Connection,
        tenant: str,
    ):
        self._writer, self._service, self._span, self._dispatch = writer, service, span, dispatch
        self._tenant = tenant
        self._linked = False

    def write(self, *lines) -> None:
        self._writer.write(*lines)
        answers = [line for line in lines if isinstance(line, EntryAnswer)]
        for answer in answers:
            self._service.metrics.answered(answer)
        if answers:
            self._observe(answers)

    def end(self) -> None:
        self._writer.end()

    def _observe(self, answers: list[EntryAnswer]) -> None:
        first, self._linked = not self._linked, True
        # A span that records nothing needs no link, so the first chunk's duplicates are read only
        # for a tracer.
        linking = first and self._span.is_recording()
        accepted = [a.job_id for a in answers if a.outcome == "accepted"]
        wanted = [a.job_id for a in answers if a.outcome != "rejected"] if linking else accepted
        if not wanted:
            return
        try:
            rows = self._dispatch.execute(
                "SELECT job_id::text, traceparent FROM accepted_job WHERE job_id = ANY(%s::uuid[])",
                (wanted,),
            ).fetchall()
        except psycopg.Error as error:
            # The answer is written and its rows are durable, so only the telemetry is lost.
            log.warning("the traceparents of a chunk were not read: %s", error)
            return
        traceparents = dict(rows)
        if linking:
            for traceparent in traceparents.values():
                context = _PROPAGATOR.extract({"traceparent": traceparent})
                self._span.add_link(trace.get_current_span(context).get_span_context())
        for job_id in accepted:
            if (traceparent := traceparents.get(job_id)) is not None:
                self._accepted(job_id, traceparent)

    def _accepted(self, job_id: str, traceparent: str) -> None:
        with (
            logs.bind(tenant=self._tenant, job_id=job_id, traceparent=traceparent),
            stage_span(tracer, "dispatch.accept", traceparent) as span,
        ):
            span.set_attribute("tenant", self._tenant)
            span.set_attribute("jobid", job_id)
            log.info("accepted")


def submit(service: "Service", request: Handler, params: dict, query: str) -> None:
    tenant, limits = params["tenant"], service.limits
    reader = _Draining(request.body(), limits, request.connection, service.stopping)
    with (
        tracer.start_as_current_span("dispatch.submit", kind=SpanKind.SERVER) as span,
        service.connect("dispatch") as dispatch,
        service.connect("artifacts") as artifacts,
    ):
        context = span.get_span_context()
        if context.is_valid:
            request.trace_id = format(context.trace_id, "032x")
        began = time.monotonic()
        service.submit.call(
            tenant,
            reader,
            dispatch,
            artifacts,
            lambda: _Answers(request.ndjson(), service, span, dispatch, tenant),
            request.trace_id,
        )
        service.metrics.submitted(time.monotonic() - began)


def _action(params: dict) -> str:
    if params["action"] not in get_args(Action):
        raise _NoRoute()
    return params["action"]


def control_job(service: "Service", request: Handler, params: dict, query: str) -> None:
    action = _action(params)
    body = request.control_request()
    tenant, job_id = params["tenant"], params["job_id"]
    try:
        with service.connect("control") as conn:
            if action == "resume":
                patched = body.template is not None
                with service.connect("artifacts") if patched else nullcontext() as artifacts:
                    answer = service.resume.call(
                        conn, artifacts, service.gateway, tenant, job_id, body
                    )
            else:
                answer = control.control_job(conn, service.gateway, tenant, job_id, action, body)
    except ControlRefused:
        service.metrics.control(action, "refused")
        raise
    service.metrics.control(action, answer.outcome)
    request.answer(200, answer)


def control_batch(service: "Service", request: Handler, params: dict, query: str) -> None:
    action = _action(params)
    body = request.control_request()
    with service.connect("control") as conn:
        lines = control.control_batch(
            conn, service.gateway, params["tenant"], params["batch_key"], action, body
        )
        writer = request.ndjson()
        try:
            for line in lines:
                writer.write(line)
                if isinstance(line, SummaryLine):
                    break
                outcome = line.outcome if isinstance(line, ControlAnswer) else "refused"
                service.metrics.control(action, outcome)
                if service.stopping.is_set():
                    writer.write(_error_line(request, *STOPPING))
                    break
        except psycopg.OperationalError as error:
            writer.write(_error_line(request, "DATABASE_UNAVAILABLE", str(error)))
        writer.end()


def _error_line(request: Handler, code: str, message: str) -> ErrorLine:
    return ErrorLine(error=ErrorBody(code=code, message=message, trace_id=request.trace_id))


def job_status(service: "Service", request: Handler, params: dict, query: str) -> None:
    with service.connect("reader") as conn:
        answer = reads.job_status(conn, service.gateway, params["tenant"], params["job_id"])
    request.answer(200, answer)


def batch_summary(service: "Service", request: Handler, params: dict, query: str) -> None:
    with service.connect("reader") as conn:
        answer = reads.batch_summary(conn, params["tenant"], params["batch_key"])
    request.answer(200, answer)


def job_page(service: "Service", request: Handler, params: dict, query: str) -> None:
    values = dict(parse_qsl(query, keep_blank_values=True))
    with service.connect("reader") as conn:
        answer = reads.job_page(conn, params["tenant"], params["batch_key"], values)
    request.answer(200, answer)


def depth(service: "Service", request: Handler, params: dict, query: str) -> None:
    with service.connect("reader") as conn:
        answer = reads.depth(conn, params["tenant"])
    request.answer(200, answer)


def healthz(service: "Service", request: Handler, params: dict, query: str) -> None:
    request.answer(200, Health(status="ok"))


def readyz(service: "Service", request: Handler, params: dict, query: str) -> None:
    if service.ready():
        request.answer(200, Health(status="ready"))
    else:
        request.answer(503, Health(status="not-ready"))


Route = tuple[str, str]
ROUTES: dict[Route, Callable[["Service", Handler, dict, str], None]] = {
    ("PUT", "/v1/tenants/{tenant}/objects/{kind}/{sha256}"): upload,
    ("POST", "/v1/tenants/{tenant}/batches"): submit,
    ("GET", "/v1/tenants/{tenant}/batches/{batch_key}"): batch_summary,
    ("GET", "/v1/tenants/{tenant}/batches/{batch_key}/jobs"): job_page,
    ("GET", "/v1/tenants/{tenant}/depth"): depth,
    ("GET", "/v1/tenants/{tenant}/jobs/{job_id}"): job_status,
    ("POST", "/v1/tenants/{tenant}/jobs/{job_id}/{action}"): control_job,
    ("POST", "/v1/tenants/{tenant}/batches/{batch_key}/{action}"): control_batch,
    ("GET", "/healthz"): healthz,
    ("GET", "/readyz"): readyz,
}
# The listeners of each route, from the contract.
LISTENERS: dict[Route, frozenset[str]] = {
    (route.method, route.path): frozenset(route.listeners) for route in routes().routes
}


def match(listener: str, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
    """The route of `listener` that serves `method` and `path`, with the path's parameters, each
    decoded on its own so that a batch key may hold an encoded slash."""
    segments = path.split("/")
    for route in ROUTES:
        if route[0] != method or listener not in LISTENERS.get(route, ()):
            continue
        template = route[1].split("/")
        if len(template) != len(segments):
            continue
        params = {}
        for part, segment in zip(template, segments, strict=True):
            if name := PARAM.match(part):
                if not segment:
                    break
                params[name.group(1)] = unquote(segment)
            elif part != segment:
                break
        else:
            return route, params
    return None
