"""A built wheel works from a clean virtual environment, away from the repository."""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# What admission reads from its dependencies' package data: the tenancy profile and its intake
# queues, the event schemas its intake decodes with, and the start contract of the workflow.
PROBE = """
import sys
from pathlib import Path

import wdf_admission
from wdf_broker.tenancy import load
from wdf_broker.topology import intake_queues
from wdf_compat import read_event
from wdf_workflow.contract import naming
from wdf_workflow.models import ResumeSignal
from wdf_workflow.taskpath import QUEUE_PREFIX_MEMO

profile = load("ci")
assert profile.priority_classes == ["standard"]
assert {queue.rsplit(".", 1)[1] for queue in intake_queues(profile)} == {"standard"}
assert naming().workflow.type == "JobWorkflow"
assert QUEUE_PREFIX_MEMO == "wdf_pool_queue_prefix"
assert read_event(Path(sys.argv[1]).read_bytes()).type == "dev.wdf.job.accepted.v1"
"""
ACCEPTED = ROOT / "contracts/events/vectors/accepted/valid/accepted.json"


def workspace_closure(package: str) -> list[str]:
    """The workspace packages that `package` needs, itself included, read from uv.lock."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    local = {name for name, p in packages.items() if "editable" in p.get("source", {})}
    seen, todo = set(), [package]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo += [d["name"] for d in packages[name].get("dependencies", []) if d["name"] in local]
    return sorted(seen)


def run(*cmd, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, f"{cmd}: {result.stdout}{result.stderr}"
    return result


def test_wheel_in_a_clean_venv_imports_admission(tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    wheels = tmp_path / "wheels"
    for package in workspace_closure("wdf-admission"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = tmp_path / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    work = tmp_path / "work"
    work.mkdir()
    probe = subprocess.run(
        [python, "-I", "-c", PROBE, ACCEPTED], capture_output=True, text=True, cwd=work
    )
    assert probe.returncode == 0, probe.stderr
