"""naming() and pools() read their file once, however often they are called."""

import pytest
from dsy_workflow import contract


@pytest.fixture
def reads(monkeypatch):
    """The files read through contract._read, with both caches cleared around the test."""
    contract.naming.cache_clear()
    contract.pools.cache_clear()
    names: list[str] = []
    real = contract._read
    monkeypatch.setattr(contract, "_read", lambda name: names.append(name) or real(name))
    yield names
    contract.naming.cache_clear()
    contract.pools.cache_clear()


@pytest.mark.parametrize(("loader", "file"), [("naming", "naming.yaml"), ("pools", "pools.yaml")])
def test_loader_reads_its_file_once(reads, loader, file):
    load = getattr(contract, loader)
    assert load() is load()
    assert reads == [file]
