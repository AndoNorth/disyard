"""Trace export: a global `TracerProvider` that pushes every span by OTLP HTTP, installed only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set; with it unset no provider is installed and spans stay
no-ops, so `current_traceparent()` inside a stage span returns the parent's `traceparent`.

The push never blocks or fails the service: ended spans wait in a queue capped at `QUEUE_SIZE`,
a background thread exports them in batches, and a span that finds the queue full is dropped and
counted by `dropped()`, which backs `wft_obs_span_dropped_total`.

    traces.install("workflow")
"""

import os
import threading

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
QUEUE_SIZE = 2048
BATCH_SIZE = 512
SAMPLE_RATIO = 1.0
CLOSE_SECONDS = 5.0
_dropped = 0
_dropped_lock = threading.Lock()


def dropped() -> int:
    """Spans dropped from a full export queue since the process started."""
    return _dropped


def _count_drop() -> None:
    global _dropped
    with _dropped_lock:
        _dropped += 1


class CountingBatchSpanProcessor(BatchSpanProcessor):
    """A batch processor that drops the newest span when its queue is full, and counts it.

    The SDK's own processor drops the oldest span and logs a warning for each one, which under a
    refusing collector would flood the service's own log with a line per span.
    """

    def __init__(self, exporter: SpanExporter):
        super().__init__(exporter, max_queue_size=QUEUE_SIZE, max_export_batch_size=BATCH_SIZE)

    def on_end(self, span: ReadableSpan) -> None:
        # shortcut: reads the SDK's private queue, pinned by the drop test; replace if the SDK
        # exposes queue fullness.
        if len(self._batch_processor._queue) >= QUEUE_SIZE:
            _count_drop()
            return
        super().on_end(span)

    def shutdown(self) -> None:
        """Exports what is queued within `CLOSE_SECONDS`, so a refusing collector never holds the
        process's exit for the SDK's 30 s."""
        self._batch_processor.shutdown(timeout_millis=int(CLOSE_SECONDS * 1000))


def provider(component: str) -> TracerProvider:
    """A provider for `component`, sampling every trace whose parent is sampled, exporting by
    OTLP HTTP to the endpoint of `OTEL_EXPORTER_OTLP_ENDPOINT`."""
    made = TracerProvider(
        resource=Resource.create({"service.name": component}),
        sampler=ParentBased(TraceIdRatioBased(SAMPLE_RATIO)),
    )
    made.add_span_processor(CountingBatchSpanProcessor(OTLPSpanExporter()))
    return made


def install(component: str) -> TracerProvider | None:
    """Sets the global provider when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and returns it, or
    returns None and leaves spans as no-ops."""
    if not os.environ.get(ENDPOINT):
        return None
    made = provider(component)
    trace.set_tracer_provider(made)
    return made
