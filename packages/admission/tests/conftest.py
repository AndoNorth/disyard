"""The fixtures that admission's test directories share: the repository root, and the dev server
and client of `support.py`, which this file loads so the directories' conftests can import it."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location(
    "admission_tests_support", Path(__file__).with_name("support.py")
)
_support = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _support
_spec.loader.exec_module(_support)

# Fixtures are found by name in a conftest's namespace.
status, server, client = _support.status, _support.server, _support.client


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT
