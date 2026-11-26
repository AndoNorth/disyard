"""The process contract every service keeps: exit 2 on a missing variable, a ready file that
exists only while it runs, and exit 0 on SIGTERM well inside the pod's grace period."""

import sys

import pytest
from dsy_servicekit.testing import (
    check_missing_env_exits_2,
    check_ready_file_lifecycle,
    check_sigterm_exits_0_within,
)
from dsy_worker.config import ENV

from .conftest import environment

pytestmark = pytest.mark.seam

ARGV = [sys.executable, "-m", "dsy_worker", "--pool", "simulate"]
# The pod's grace period of 75 s less the 5 s the kubelet keeps for itself.
SIGTERM_BUDGET_S = 70.0


@pytest.fixture
def env(temporal, pool_store, tmp_path) -> dict[str, str]:
    return environment("simulate", temporal, pool_store, tmp_path, "t-contract-")


def test_missing_env_exits_2():
    check_missing_env_exits_2(ARGV, ENV.values())


@pytest.mark.integration
@pytest.mark.pair
def test_ready_file_lifecycle(env, tmp_path):
    check_ready_file_lifecycle(ARGV, env, tmp_path / "ready")


@pytest.mark.integration
@pytest.mark.pair
def test_sigterm_exits_0(env, tmp_path):
    check_sigterm_exits_0_within(ARGV, env, tmp_path / "ready", SIGTERM_BUDGET_S)
