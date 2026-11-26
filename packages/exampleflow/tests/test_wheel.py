"""The wheel closure of the example installs into a clean virtual environment, away from the
repository, and the module is found there by its entry point."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.seam

ROOT = Path(__file__).resolve().parents[3]

PROBE = """
from importlib.metadata import entry_points

import dsy_exampleflow
from dsy_workflow import modules

[point] = [p for p in entry_points(group=modules.GROUP) if p.name == "exampleflow"]
assert point.load() is dsy_exampleflow.workers
assert "exampleflow" in [name for name, _ in modules.load()]
"""


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


@pytest.fixture(scope="module")
def clean_python(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("wheel")
    wheels = work / "wheels"
    for package in workspace_closure("dsy-exampleflow"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = work / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    return python


def test_wheel_closure_follows_the_lock():
    closure = workspace_closure("dsy-exampleflow")
    assert {"dsy-exampleflow", "dsy-workflow", "dsy-temporal"} <= set(closure)


def test_wheel_in_a_clean_venv_finds_the_entry_point(clean_python, tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    probe = subprocess.run(
        [clean_python, "-I", "-c", PROBE], capture_output=True, text=True, cwd=tmp_path
    )
    assert probe.returncode == 0, probe.stderr
