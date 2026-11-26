"""The process contract every service keeps: exit 2 on a missing variable, a ready file that
exists only while it runs, and exit 0 on SIGTERM well inside the pod's grace period."""

import os
import sys

import pytest
from dsy_admission.service import ENV
from dsy_servicekit.testing import (
    check_missing_env_exits_2,
    check_ready_file_lifecycle,
    check_sigterm_exits_0_within,
    free_port,
)

from .test_service import FULL

pytestmark = pytest.mark.seam

ARGV = [sys.executable, "-m", "dsy_admission.service"]
# A drain of DRAIN_S and the release and close after it fit inside this.
SIGTERM_BUDGET_S = 25.0


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    return {
        **base,
        **FULL,
        "DSY_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
        "DSY_READY_FILE": str(tmp_path / "ready"),
    }


def test_missing_env_exits_2():
    check_missing_env_exits_2(ARGV, ENV.values())


def test_ready_file_lifecycle(env, tmp_path):
    check_ready_file_lifecycle(ARGV, env, tmp_path / "ready")


def test_sigterm_exits_0(env, tmp_path):
    check_sigterm_exits_0_within(ARGV, env, tmp_path / "ready", SIGTERM_BUDGET_S)
