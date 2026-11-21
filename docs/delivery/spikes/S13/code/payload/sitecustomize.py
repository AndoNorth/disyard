# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Starts trace export, log export and profiling in a process, from its environment.

OTEL_EXPORTER_OTLP_ENDPOINT turns on a TracerProvider and a log handler that push to the collector, and
WFT_PYROSCOPE_ADDRESS turns on Pyroscope push mode, with the queue sizes of the epic's D14 and D21.
The component name comes from S13_COMPONENT. Any failure is swallowed, because the wiring must never stop a service.
"""

import logging
import os

component = os.environ.get("S13_COMPONENT", "unknown")
endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
pyroscope_address = os.environ.get("WFT_PYROSCOPE_ADDRESS")

if endpoint:
    try:
        from opentelemetry import trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        resource = Resource.create({"service.name": component})
        provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(1.0)))
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint + "/v1/traces"), max_queue_size=2048
            )
        )
        trace.set_tracer_provider(provider)

        logs = LoggerProvider(resource=resource)
        logs.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=endpoint + "/v1/logs"), max_queue_size=2048
            )
        )
        set_logger_provider(logs)
        logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logs))
        logging.getLogger().setLevel(min(logging.getLogger().level or logging.INFO, logging.INFO))
    except Exception as error:  # noqa: BLE001
        print(f"s13 wiring: otlp not started: {error!r}", flush=True)

if pyroscope_address:
    try:
        import pyroscope

        pyroscope.configure(
            application_name=f"wft.{component}",
            server_address=pyroscope_address,
            tags={"pool": os.environ.get("S13_POOL", "")} if os.environ.get("S13_POOL") else {},
        )
    except Exception as error:  # noqa: BLE001
        print(f"s13 wiring: pyroscope not started: {error!r}", flush=True)
