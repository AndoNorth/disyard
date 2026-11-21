import logging

import pytest
import wft_obs.setup as obs_setup
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once


@pytest.fixture
def fresh(monkeypatch):
    """A process with no global tracer provider and no setup call yet; afterwards shuts down the
    provider the test installed and restores the root logger its `setup` replaced."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(obs_setup, "_installed", None)
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    installed = trace._TRACER_PROVIDER
    if isinstance(installed, TracerProvider):
        installed.shutdown()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(level)
