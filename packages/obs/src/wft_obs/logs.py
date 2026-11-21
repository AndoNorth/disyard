"""Structured logs: one JSON line per record on stdout, carrying the log fields of the
instrumentation contract and no other, and the same record pushed by OTLP HTTP when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set.

The push never blocks or fails the service: records wait in a queue capped at `QUEUE_SIZE`, a
background thread exports them in batches, and a record that finds the queue full is dropped and
counted by `dropped()`, which backs `wft_obs_log_dropped_total`.

    logs.install("dispatch")
    with logs.bind(job_id=job, traceparent=event.traceparent):
        log.info("accepted")
"""

import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TextIO

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.resources import Resource

CORE = ("ts", "level", "component", "msg")
FIELDS = (
    "tenant",
    "job_id",
    "task_id",
    "attempt_id",
    "trace_id",
    "span_id",
    "event_type",
    "cloudevent_id",
    "pool",
    "exit_class",
)
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
QUEUE_SIZE = 2048
BATCH_SIZE = 512
FLUSH_SECONDS = 1.0
CLOSE_SECONDS = 5.0
# Records of the export path itself go to stdout only, so a failing push never feeds its own queue.
NOT_PUSHED = ("opentelemetry", "urllib3", "requests")
SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}

TRACEPARENT = re.compile(r"[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
_bound: ContextVar[Mapping[str, object]] = ContextVar("wft_obs_log_fields")
_dropped = 0
_dropped_lock = threading.Lock()


def dropped() -> int:
    """Records dropped from a full export queue since the process started."""
    return _dropped


def _count_drop() -> None:
    global _dropped
    with _dropped_lock:
        _dropped += 1


def ids_of(traceparent: str) -> tuple[str, str] | None:
    """The trace id and parent span id a W3C `traceparent` holds, or None when it is malformed."""
    match = TRACEPARENT.fullmatch(traceparent)
    if match is None or set(match[1]) == {"0"} or set(match[2]) == {"0"}:
        return None
    return match[1], match[2]


def bind(*, traceparent: str | None = None, **fields: object):
    """A context manager that adds log fields to every record logged inside it.

    Keywords are the optional log fields of the contract; any other raises TypeError at the call.
    `traceparent` is the event's value as it is, and sets `trace_id` and `span_id`; a malformed
    one sets neither, so a bad event never fails the service that logs it.
    A None value leaves the field as it was.
    """
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise TypeError(f"bind() got fields outside the log contract: {', '.join(unknown)}")
    if traceparent is not None and (ids := ids_of(traceparent)) is not None:
        fields["trace_id"], fields["span_id"] = ids
    return _scope({k: v for k, v in fields.items() if v is not None})


@contextmanager
def _scope(fields: dict[str, object]) -> Iterator[None]:
    token = _bound.set({**_bound.get({}), **fields})
    try:
        yield
    finally:
        _bound.reset(token)


def fields_of(record: logging.LogRecord) -> dict[str, object]:
    """The optional log fields of a record: the bound ones, overridden by its `extra` keys."""
    own = {k: record.__dict__[k] for k in FIELDS if record.__dict__.get(k) is not None}
    return {**_bound.get({}), **own}


def message_of(record: logging.LogRecord) -> str:
    msg = record.getMessage()
    if record.exc_info:
        msg += "\n" + logging.Formatter().formatException(record.exc_info)
    return msg


class JsonFormatter(logging.Formatter):
    """Renders a record as one JSON object with the contract's fields only."""

    def __init__(self, component: str):
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        line = {
            "ts": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "component": self.component,
            "msg": message_of(record),
            **fields_of(record),
        }
        return json.dumps(line, default=str)


class OtlpHandler(logging.Handler):
    """Queues records for an OTLP log exporter that a background thread drains in batches."""

    def __init__(self, component: str, exporter=None):
        super().__init__()
        self.resource = Resource.create({"service.name": component})
        self.exporter = exporter or OTLPLogExporter()
        self.queue: deque = deque()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._drain, name="wft-obs-logs", daemon=True)
        self.thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(NOT_PUSHED):
            return
        try:
            if len(self.queue) >= QUEUE_SIZE:
                _count_drop()
                return
            self.queue.append(self._translate(record))
            if len(self.queue) >= BATCH_SIZE:
                self.wake.set()
        except Exception:
            self.handleError(record)

    def _translate(self, record: logging.LogRecord) -> ReadableLogRecord:
        severity = SEVERITY.get(record.levelno, SeverityNumber.UNSPECIFIED)
        attributes = {
            k: v if isinstance(v, int | float | bool) else str(v)
            for k, v in fields_of(record).items()
        }
        api_record = LogRecord(
            timestamp=int(record.created * 1e9),
            observed_timestamp=time.time_ns(),
            severity_text=record.levelname,
            severity_number=severity,
            body=message_of(record),
            attributes=attributes,
        )
        return ReadableLogRecord(log_record=api_record, resource=self.resource)

    def _drain(self) -> None:
        while not self.stopping.is_set():
            self.wake.wait(FLUSH_SECONDS)
            self.wake.clear()
            self._export_queued()
        self._export_queued()

    def _export_queued(self) -> None:
        while self.queue:
            batch = [self.queue.popleft() for _ in range(min(BATCH_SIZE, len(self.queue)))]
            try:
                self.exporter.export(batch)
            except Exception:  # the push is best effort, and a failed batch is gone
                logging.getLogger("opentelemetry.wft_obs").exception("log export failed")

    def close(self) -> None:
        """Exports what is queued within `CLOSE_SECONDS`, then shuts the exporter down."""
        self.stopping.set()
        self.wake.set()
        self.thread.join(CLOSE_SECONDS)
        self.exporter.shutdown()
        self.queue.clear()
        super().close()


def install(component: str, stream: TextIO | None = None, level: int = logging.INFO) -> None:
    """Makes the root logger write JSON lines to `stream` (stdout by default) and, when
    `OTEL_EXPORTER_OTLP_ENDPOINT` is set, push the same records; replaces any root handler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    out = logging.StreamHandler(stream or sys.stdout)
    out.setFormatter(JsonFormatter(component))
    root.addHandler(out)
    if os.environ.get(ENDPOINT):
        root.addHandler(OtlpHandler(component))
    root.setLevel(level)
