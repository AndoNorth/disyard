"""The fixtures that placement's test directories share: the repository root."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT
