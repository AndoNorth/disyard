"""The OTLP receiver keeps what an exporter sends it, plain or gzipped."""

import gzip
import urllib.request

from dsy_obs.testing import OtlpReceiver
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


def post(url: str, body: bytes, gzipped: bool) -> int:
    headers = {"Content-Encoding": "gzip"} if gzipped else {}
    data = gzip.compress(body) if gzipped else body
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=5) as answer:
        return answer.status


def one_span(name: str, service: str) -> bytes:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    attribute = resource_spans.resource.attributes.add()
    attribute.key, attribute.value.string_value = "service.name", service
    resource_spans.scope_spans.add().spans.add().name = name
    return request.SerializeToString()


def test_gzip_and_plain_trace_pushes_decode_to_spans():
    with OtlpReceiver() as receiver:
        for gzipped, name in ((True, "zipped"), (False, "plain")):
            assert post(f"{receiver.url}/v1/traces", one_span(name, "svc"), gzipped) == 200
    assert [(resource, span.name) for resource, span in receiver.spans] == [
        ({"service.name": "svc"}, "zipped"),
        ({"service.name": "svc"}, "plain"),
    ]


def test_another_signal_is_kept_by_path_and_not_decoded_as_spans():
    with OtlpReceiver() as receiver:
        assert post(f"{receiver.url}/v1/metrics", b"\x01\x02", gzipped=True) == 200
    assert receiver.bodies["/v1/metrics"] == [b"\x01\x02"]
    assert receiver.spans == []
