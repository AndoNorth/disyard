"""The snapshotter reads on its cadence, keeps the last good snapshot through failed reads with its
age and the error, and answers `current()` while a read is in flight."""

import time

import pytest
from wdf_placement import kube


class Snapshot:
    """Stands in for the pool snapshot: the node and pod names of one read."""

    def __init__(self, nodes: dict, pods: dict):
        self.nodes = [n["metadata"]["name"] for n in nodes["items"]]
        self.pods = [p["metadata"]["name"] for p in pods["items"]]


def until(condition, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


@pytest.fixture
def snapshotter(api, tls, token):
    def make(every_s: float) -> kube.Snapshotter:
        reader = kube.Reader(api.url, str(token), str(tls[0]))
        made.append(kube.Snapshotter(reader, every_s, build=Snapshot))
        return made[-1]

    made: list[kube.Snapshotter] = []
    yield make
    for snapshotter in made:
        if snapshotter._thread.is_alive():
            api.hold.set()
            snapshotter.stop()


def node_reads(api) -> int:
    return sum(path.startswith("/api/v1/nodes?") for path, _ in api.requests)


def test_snapshotter_keeps_last_good(api, snapshotter):
    snapshotter = snapshotter(every_s=60)
    snapshotter.read()
    first, age_s, error = snapshotter.current()
    assert first.nodes == ["wdf-worker"] and first.pods == ["compile-worker-0"]
    assert error is None

    api.fail = 3
    ages = [age_s]
    for _ in range(3):
        time.sleep(0.02)
        snapshotter.read()
        snapshot, age_s, error = snapshotter.current()
        assert snapshot is first
        assert "HTTP Error 500" in error
        ages.append(age_s)
    assert ages == sorted(ages) and len(set(ages)) == 4

    snapshotter.read()
    snapshot, age_s, error = snapshotter.current()
    assert snapshot is not first and snapshot.nodes == ["wdf-worker"]
    assert error is None
    assert age_s < ages[-1]


def test_snapshotter_reports_age_and_error(api, snapshotter):
    snapshotter = snapshotter(every_s=0.1)
    assert snapshotter.current() == (None, None, None)

    # Before the first good read there is no snapshot and no age, only the error.
    api.fail = 1_000
    snapshotter.start()
    until(lambda: snapshotter.current()[2] is not None)
    snapshot, age_s, error = snapshotter.current()
    assert snapshot is None and age_s is None
    assert error.startswith("HTTPError: HTTP Error 500") and "\n" not in error

    api.fail = 0
    until(lambda: snapshotter.current()[0] is not None)
    reads = node_reads(api)
    # Further reads keep coming on the cadence, so the age stays below it with some slack.
    until(lambda: node_reads(api) >= reads + 3)
    snapshot, age_s, error = snapshotter.current()
    assert snapshot is not None and error is None
    assert 0 <= age_s < 0.1 + 0.5


def test_snapshotter_never_blocks_current(api, snapshotter):
    snapshotter = snapshotter(every_s=0.05)
    snapshotter.read()
    first, _, _ = snapshotter.current()

    api.hold.clear()
    reads = node_reads(api)
    snapshotter.start()
    until(lambda: node_reads(api) == reads + 1)

    started = time.monotonic()
    for _ in range(100):
        snapshot, _, error = snapshotter.current()
        assert snapshot is first and error is None
    assert time.monotonic() - started < 0.5

    api.hold.set()
    until(lambda: snapshotter.current()[0] is not first)
