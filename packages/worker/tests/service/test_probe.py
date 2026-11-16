"""The egress probe prints one answer per endpoint and exits 0 only when each answer is the one
the pool's NetworkPolicy allows: the mirror from the compile pod alone, the app database never."""

import socket

import pytest
from wdf_worker import probe
from wdf_worker.config import MIRROR_ENV


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
