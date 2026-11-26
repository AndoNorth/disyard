"""The worker takes its Temporal namespace from DSY_TEMPORAL_NAMESPACE, as admission and the
workflow service do, and the old variable no longer stands in for it."""

import os
import subprocess
import sys

from dsy_worker.config import ENV

OLD = "DSY_" + "NAMESPACE"
ARGV = [sys.executable, "-m", "dsy_worker", "--pool", "simulate"]


def test_only_the_old_variable_exits_2_naming_the_missing_one():
    environ = {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}
    environ |= {name: "x" for name in ENV.values()} | {ENV["slot_memory_mib"]: "384", OLD: "dsy"}
    environ.pop("DSY_TEMPORAL_NAMESPACE", None)
    run = subprocess.run(ARGV, env=environ, capture_output=True, text=True, timeout=60)
    assert run.returncode == 2
    assert "DSY_TEMPORAL_NAMESPACE" in run.stderr
