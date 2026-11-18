"""A built wheel works from a clean virtual environment, away from the repository."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# The API contract and the contracts it is checked against come from package data, the tenancy
# profile from the broker's, and the stub of Temporal runs with no repository around it.
PROBE = """
from wdf_broker.tenancy import load
from wdf_dispatchapi.contract import control_table, reasons, routes
from wdf_dispatchapi.models import CONTRACT
from wdf_dispatchapi.testing import FakeGateway, gateway_contract

for name in ("routes.yaml", "reasons.yaml", "control.yaml", "status.yaml", "stages.yaml"):
    assert (CONTRACT / name).read_text(), name
assert "UNKNOWN_TENANT" in {r.code for r in reasons().reasons}
assert routes() and control_table()
profile = load("ci")
assert list(profile.tenants) == ["acme"]
assert profile.priority_classes == ["standard"]
gateway = FakeGateway()
gateway.start("job")
gateway_contract(gateway, "job")
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
    for package in workspace_closure("wdf-dispatchapi"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = work / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    return python


def test_wheel_in_a_clean_venv_loads_the_contracts_and_runs_the_stub(clean_python, tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    probe = subprocess.run(
        [clean_python, "-I", "-c", PROBE], capture_output=True, text=True, cwd=tmp_path
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("name", ["routes.yaml", "reasons.yaml", "control.yaml"])
def test_wheel_loader_checks_a_file_away_from_the_repository(clean_python, tmp_path, name):
    copy = tmp_path / name
    copy.write_text((ROOT / "contracts/api" / name).read_text())
    result = subprocess.run(
        [clean_python, "-I", "-m", "wdf_dispatchapi.contract", "--check", str(copy)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
