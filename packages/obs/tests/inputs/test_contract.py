"""The process contract of the exporter: exit 2 on a missing variable and exit 0 on SIGTERM well
inside the pod's grace period. The ready file is not part of it, since the exporter leaves it in
place on stop."""

import os
import sys
import urllib.request

import pytest
from dsy_obs.inputs.__main__ import ADDRESS_ENV, DSN_ENV, READY_ENV
from dsy_servicekit.testing import check_missing_env_exits_2, free_port, spawn, until

pytestmark = pytest.mark.seam

ARGV = [sys.executable, "-m", "dsy_obs.inputs"]
SIGTERM_BUDGET_S = 10.0
START_TIMEOUT_S = 30.0


def test_missing_env_exits_2():
    check_missing_env_exits_2(ARGV, [DSN_ENV])


def test_sigterm_exits_0(tmp_path):
    port = free_port()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "DSY_"))},
        # An unreachable database keeps the pass failing, which the process survives.
        DSN_ENV: "postgresql://nobody@127.0.0.1:1/none",
        ADDRESS_ENV: f"127.0.0.1:{port}",
        READY_ENV: str(tmp_path / "ready"),
    }
    service = spawn(ARGV, env)
    try:

        def serving() -> bool:
            assert service.proc.poll() is None, service._stderr()
            try:
                return (
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2).status
                    == 200
                )
            except OSError:
                return False

        until(serving, START_TIMEOUT_S, "the metrics endpoint")
        assert service.terminate(SIGTERM_BUDGET_S) == 0
    finally:
        service.kill()
