"""The broker client against a canned `dlq list` run, so no broker is needed."""

import subprocess
import sys

import pytest
from wft_acceptance.clients import Failed, broker

# `dlq list` prints one line per dead letter, then one count line per reason.
LISTED = """\
1 MALFORMED json - acme - jobs.intake.standard 2026-11-20T09:00:00Z
2 UNSUPPORTED_VERSION unsupported-major 3a9617fa-df7c-5760-a644-42d559e0f433 acme standard \
jobs.intake.standard 2026-11-20T09:01:00Z
3 delivery_limit - 7d1f0a52-6c3b-5e8d-a1f4-29b7c3e5d061 acme standard jobs.intake.standard \
2026-11-20T09:02:00Z
4 delivery_limit - - - - jobs.intake.standard -
count MALFORMED 1
count UNSUPPORTED_VERSION 1
count delivery_limit 2
"""


@pytest.fixture
def ran(tmp_path, monkeypatch):
    """Answers the next `dlq list` with the canned (exit, stdout, stderr), and records its argv."""
    monkeypatch.setenv("WFT_STATE_DIR", str(tmp_path))
    calls, answer = [], {}

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("timeout")))
        return subprocess.CompletedProcess(argv, *answer["next"])

    monkeypatch.setattr(broker.subprocess, "run", run)

    def canned(returncode: int, stdout: str = "", stderr: str = "") -> list:
        answer["next"] = (returncode, stdout, stderr)
        return calls

    return canned


def test_dead_letters_count(ran, tmp_path):
    calls = ran(0, LISTED)
    assert broker.dead_letters("127.0.0.1:5672", timeout_s=15) == 4
    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "wft_broker.dlq",
                "list",
                "--address",
                "127.0.0.1:5672",
                "--credentials-dir",
                str(tmp_path / "rabbitmq"),
            ],
            15,
        )
    ]


def test_an_empty_queue_has_no_dead_letters(ran):
    ran(0)
    assert broker.dead_letters("127.0.0.1:5672") == 0


def test_a_failed_list_raises(ran):
    ran(3, stderr="error: the broker could not be reached\n")
    with pytest.raises(Failed, match="exited 3: error: the broker could not be reached"):
        broker.dead_letters("127.0.0.1:5672")
