"""Test doubles of the observability stack."""

from .fake import FakeObs
from .otlp import OtlpReceiver

__all__ = ["FakeObs", "OtlpReceiver"]
