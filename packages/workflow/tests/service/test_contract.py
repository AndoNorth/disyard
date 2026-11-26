"""The process contract every service keeps: exit 2 on a missing variable, a ready file that
exists only while it runs, and exit 0 on SIGTERM well inside the pod's grace period."""

import os
import sys

import pytest
from dsy_artifactreaper import schedule
from dsy_servicekit.testing import (
    check_missing_env_exits_2,
    check_ready_file_lifecycle,
    check_sigterm_exits_0_within,
    free_port,
)
from dsy_storecheck.s3 import Creds, dump_credentials
from dsy_workflow.contract import naming
from dsy_workflow.service import ENV, OPTIONAL, REAPER_ENV

pytestmark = pytest.mark.seam

ARGV = [sys.executable, "-m", "dsy_workflow.service"]
# The pod's 90 s grace period less 5 s, which an idle service drains well inside.
SIGTERM_BUDGET_S = 85.0
REQUIRED = [name for key, name in ENV.items() if key not in OPTIONAL]


@pytest.fixture
def env(temporal, tmp_path) -> dict[str, str]:
    for role in ("workflow", "indexer"):
        dump_credentials(tmp_path / role, {role: Creds("key", "secret")})
    dump_credentials(tmp_path / "reaper", {"reaper": Creds("key", "secret")})
    base = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    return {
        **base,
        ENV["temporal_address"]: temporal,
        ENV["namespace"]: naming().namespace,
        ENV["build_id"]: "contract",
        ENV["metrics_address"]: f"127.0.0.1:{free_port()}",
        ENV["workflow_dsn"]: "postgresql://unused",
        ENV["artifacts_dsn"]: "postgresql://unused",
        ENV["store_endpoint"]: "http://127.0.0.1:1",
        ENV["workflow_credentials"]: str(tmp_path / "workflow"),
        ENV["indexer_credentials"]: str(tmp_path / "indexer"),
        schedule.ENV_DSN: "postgresql://unused",
        schedule.ENV_ENDPOINT: "http://127.0.0.1:1",
        schedule.ENV_CREDENTIALS: str(tmp_path / "reaper"),
        "DSY_READY_FILE": str(tmp_path / "ready"),
    }


def test_missing_env_exits_2():
    check_missing_env_exits_2(ARGV, [*REQUIRED, *REAPER_ENV])


@pytest.mark.integration
def test_ready_file_lifecycle(env, tmp_path):
    check_ready_file_lifecycle(ARGV, env, tmp_path / "ready")


@pytest.mark.integration
def test_sigterm_exits_0(env, tmp_path):
    check_sigterm_exits_0_within(ARGV, env, tmp_path / "ready", SIGTERM_BUDGET_S)
