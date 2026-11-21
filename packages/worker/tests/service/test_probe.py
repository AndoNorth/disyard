"""The egress probe prints one answer per endpoint and exits 0 only when each answer is the one
the pool's NetworkPolicy allows: the mirror from the compile pod alone, the app database never, and
the collector and Pyroscope exactly when the pod has the telemetry endpoint."""

import socket

import pytest
from wft_worker import probe
from wft_worker.config import MIRROR_ENV
from wft_worker.probe import OBS_ENV

OBS = {"collector", "pyroscope"}


@pytest.fixture(autouse=True)
def obs_off(monkeypatch):
    """The telemetry endpoint is unset unless a test sets it."""
    monkeypatch.delenv(OBS_ENV, raising=False)


@pytest.fixture
def endpoints(monkeypatch):
    """Points the probe at a local listener for the names given and a closed port for the rest."""
    listener = socket.create_server(("127.0.0.1", 0))
    closed = socket.create_server(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()

    def point(reachable: set[str]) -> None:
        open_port = listener.getsockname()[1]
        targets = {
            name: ("127.0.0.1", open_port if name in reachable else closed_port)
            for name in probe.TARGETS
        }
        monkeypatch.setattr(probe, "TARGETS", targets)

    yield point
    listener.close()


@pytest.mark.parametrize(
    ("mirror", "reachable", "code"),
    [
        ("http://mirror", {"temporal", "store", "mirror"}, 0),
        ("", {"temporal", "store"}, 0),
        ("", {"temporal", "store", "mirror"}, 1),
        ("http://mirror", {"temporal", "store"}, 1),
        ("", {"temporal", "store", "appdb"}, 1),
    ],
)
def test_probe_answers_against_policy(endpoints, monkeypatch, capsys, mirror, reachable, code):
    monkeypatch.setenv(MIRROR_ENV, mirror)
    endpoints(reachable)
    assert probe.main([]) == code
    printed = capsys.readouterr().out.splitlines()
    assert printed == [
        f"{name}: {'ok' if name in reachable else 'refused'}" for name in probe.TARGETS
    ]


@pytest.mark.parametrize(
    ("endpoint", "reachable", "code"),
    [
        ("http://collector.wft-obs:4318", OBS, 0),
        ("", set(), 0),
        ("", OBS, 1),
        ("http://collector.wft-obs:4318", set(), 1),
        ("http://collector.wft-obs:4318", {"collector"}, 1),
        ("http://collector.wft-obs:4318", {"pyroscope"}, 1),
    ],
)
def test_obs_targets_are_expected_exactly_when_the_endpoint_is_set(
    endpoints, monkeypatch, capsys, endpoint, reachable, code
):
    if endpoint:
        monkeypatch.setenv(OBS_ENV, endpoint)
    monkeypatch.setenv(MIRROR_ENV, "")
    endpoints({"temporal", "store"} | reachable)
    assert probe.main([]) == code
    printed = dict(line.split(": ") for line in capsys.readouterr().out.splitlines())
    for name in OBS:
        assert printed[name] == ("ok" if name in reachable else "refused")
