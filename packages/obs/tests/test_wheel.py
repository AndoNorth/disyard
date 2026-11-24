"""A built wheel works from a clean virtual environment, away from the repository, with only its
declared dependencies installed."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# Every module of the package is imported, and the fake answers the client, so a module that imports
# an undeclared dependency fails here and not only in the workspace environment.
PROBE = """
import importlib
import pkgutil

import opentelemetry.exporter.otlp.proto.http
import opentelemetry.sdk
import pyroscope
import temporalio
import dsy_events
import dsy_obs
import yaml
from dsy_obs import query
from dsy_obs.targets import JOBS
from dsy_obs.testing import FakeObs

for module in pkgutil.walk_packages(dsy_obs.__path__, "dsy_obs."):
    if not module.name.endswith(".__main__"):
        importlib.import_module(module.name)
assert "rabbitmq-detailed" in JOBS
with FakeObs(vectors={"up": [{"metric": {"job": "kubelet"}, "value": [0, "1"]}]}) as fake:
    assert query.prom(fake.url, "up")[0]["value"] == [0, "1"]
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
    for package in workspace_closure("dsy-obs"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = work / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    return python


def test_wheel_declares_the_dependency_set():
    project = tomllib.loads((ROOT / "packages/obs/pyproject.toml").read_text())["project"]
    assert sorted(project["dependencies"]) == [
        "opentelemetry-exporter-otlp-proto-http",
        "opentelemetry-sdk",
        "pyroscope-io",
        "pyyaml",
        "temporalio",
        "dsy-events",
    ]


def test_wheel_in_a_clean_venv_imports_every_module(clean_python, tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    probe = subprocess.run(
        [clean_python, "-I", "-c", PROBE], capture_output=True, text=True, cwd=tmp_path
    )
    assert probe.returncode == 0, probe.stderr
