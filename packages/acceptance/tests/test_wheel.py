"""A built wheel works from a clean virtual environment, away from the repository, with only its
declared dependencies installed."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# Every module of the package is imported, so a module that imports an undeclared dependency fails
# here and not only in the workspace environment, where the dev group installs every package.
PROBE = """
import importlib
import pkgutil

import temporalio
import wdf_acceptance
import wdf_broker
import wdf_clusterkit
import wdf_kernel
import wdf_workflow
import wdf_workloads
from wdf_acceptance.report import count_line, ok

for module in pkgutil.walk_packages(wdf_acceptance.__path__, "wdf_acceptance."):
    if not module.name.endswith(".__main__"):
        importlib.import_module(module.name)
assert count_line("ci", [ok("api", "ready")], 1) == "smoke ci: 1 ok, 0 failed, 0 skipped, 1 s"
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
    for package in workspace_closure("wdf-acceptance"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = work / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    return python


def test_wheel_declares_the_dependency_set():
    project = tomllib.loads((ROOT / "packages/acceptance/pyproject.toml").read_text())["project"]
    assert sorted(project["dependencies"]) == [
        "temporalio",
        "wdf-broker",
        "wdf-clusterkit",
        "wdf-kernel",
        "wdf-workflow",
        "wdf-workloads",
    ]


def test_wheel_in_a_clean_venv_imports_every_module(clean_python, tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    probe = subprocess.run(
        [clean_python, "-I", "-c", PROBE], capture_output=True, text=True, cwd=tmp_path
    )
    assert probe.returncode == 0, probe.stderr
