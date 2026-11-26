"""The process contract every service keeps: exit 2 on a missing variable and exit 0 on SIGTERM
well inside the pod's grace period. Readiness is the probe of /readyz, so no ready file."""

import signal
import sys
import time

import pytest
from dsy_dispatchapi.service import ENV
from dsy_servicekit.testing import check_missing_env_exits_2

pytestmark = pytest.mark.seam

ARGV = [sys.executable, "-m", "dsy_dispatchapi.service"]
# The drain of DRAIN_S and the close after it fit inside this.
SIGTERM_BUDGET_S = 25.0


def test_missing_env_exits_2():
    check_missing_env_exits_2(ARGV, ENV.values())


@pytest.mark.integration
@pytest.mark.pair
def test_sigterm_exits_0(spawn, logins, temporal, tmp_path, kit):
    log = tmp_path / "service.log"
    with spawn(kit.environment(logins, temporal), tmp_path / "store", log) as service:
        started = time.monotonic()
        service.proc.send_signal(signal.SIGTERM)
        code = service.proc.wait(SIGTERM_BUDGET_S)
        assert code == 0, log.read_text()
        assert time.monotonic() - started < SIGTERM_BUDGET_S
