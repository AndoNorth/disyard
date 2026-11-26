"""The process contract every service keeps: exit 2 on a missing variable, a ready file that
exists only while it runs, and exit 0 on SIGTERM. Placement has no drain, so the budget is the
few seconds of stopping the HTTP server."""

import os
import sys

import pytest
from dsy_placement.service import ENV
from dsy_servicekit.testing import (
    check_missing_env_exits_2,
    check_ready_file_lifecycle,
    check_sigterm_exits_0_within,
    free_port,
)

pytestmark = pytest.mark.seam

# The service's own entry point with the service account directory given on the command line in
# place of the one a pod mounts.
ENTRY = "import sys; from pathlib import Path; from dsy_placement.service import main; " + (
    "sys.exit(main(account=Path(sys.argv[1])))"
)
SIGTERM_BUDGET_S = 5.0


@pytest.fixture
def argv(account) -> list[str]:
    return [sys.executable, "-c", ENTRY, str(account)]


@pytest.fixture
def env(api, tmp_path) -> dict[str, str]:
    api_host, _, api_port = api.url.removeprefix("https://").rpartition(":")
    base = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    return {
        **base,
        ENV["listen"]: f"127.0.0.1:{free_port()}",
        ENV["metrics_address"]: f"127.0.0.1:{free_port()}",
        ENV["ready_file"]: str(tmp_path / "ready"),
        ENV["api_host"]: api_host,
        ENV["api_port"]: api_port,
    }


def test_missing_env_exits_2(argv):
    check_missing_env_exits_2(argv, [ENV["api_host"]])


def test_ready_file_lifecycle(argv, env, tmp_path):
    check_ready_file_lifecycle(argv, env, tmp_path / "ready")


def test_sigterm_exits_0(argv, env, tmp_path):
    check_sigterm_exits_0_within(argv, env, tmp_path / "ready", SIGTERM_BUDGET_S)
