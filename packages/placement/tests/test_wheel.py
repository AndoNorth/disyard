"""A built wheel works from a clean virtual environment, away from the repository."""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# What placement reads from its dependency's package data: the pool table and the pool queues that
# the static placer answers from, and the conformance test that a placer passes.
PROBE = """
import dsy_placement
from dsy_workflow.contract import naming, pools
from dsy_workflow.placement import StaticPlacer
from dsy_workflow.testing import placer_contract

assert set(naming().pools) == {"compile", "simulate", "aggregate"}
assert pools().estimate is False
placer_contract(StaticPlacer())
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


def test_wheel_in_a_clean_venv_imports_placement(tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    wheels = tmp_path / "wheels"
    for package in workspace_closure("dsy-placement"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = tmp_path / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    work = tmp_path / "work"
    work.mkdir()
    probe = subprocess.run([python, "-I", "-c", PROBE], capture_output=True, text=True, cwd=work)
    assert probe.returncode == 0, probe.stderr
