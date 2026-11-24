import pytest
from dsy_dispatchapi import smoke


@pytest.fixture
def counts(monkeypatch):
    """Feeds `clear_backlog` a scripted sequence of unpublished counts, with no waiting."""
    script: list[int] = []
    monkeypatch.setattr(smoke, "_unpublished", lambda db, batch_id: script.pop(0))
    monkeypatch.setattr(smoke, "UNPUBLISHED_POLL_S", 0.0)
    return script


def test_falls_to_zero_with_no_rise(counts):
    counts.extend([809, 584, 584, 272, 0])
    assert smoke.Smoke.clear_backlog(None, None, "b") == (0, 0)
    assert counts == []


def test_counts_each_rise(counts):
    counts.extend([5, 7, 3, 4, 0])
    assert smoke.Smoke.clear_backlog(None, None, "b") == (0, 2)


def test_stops_at_the_bound(counts, monkeypatch):
    monkeypatch.setattr(smoke, "UNPUBLISHED_S", 0.0)
    counts.extend([106, 0])
    assert smoke.Smoke.clear_backlog(None, None, "b") == (106, 0)
