"""A built wheel works from a clean virtual environment, away from the repository, and each pool's
extra adds exactly that pool's library and binary."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

PROBE = """
import wdf_worker.__main__, wdf_worker.probe, wdf_worker.runner, wdf_worker.service
from wdf_worker.activity import run_task
from wdf_worker.config import ENV, Config
from wdf_worker.pools import aggregate, compile, simulate

environ = {name: "/var/wdf/x" for name in ENV.values()} | {"WDF_SLOT_MEMORY_MIB": "384"}
assert Config.from_env("simulate", environ).queue == "wdf-simulate"
assert run_task.__temporal_activity_definition.name == "run_task"
"""

EXTRAS = {
    "compile": {"uv", "wdf-compile"},
    "simulate": {"wdf-runtime"},
    "aggregate": {"wdf-aggregate"},
}


def lock() -> dict[str, dict]:
    return {p["name"]: p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}


def workspace_closure(package: str) -> list[str]:
    """The workspace packages that `package` needs with every extra, itself included."""
    packages = lock()
    local = {name for name, p in packages.items() if "editable" in p.get("source", {})}
    seen, todo = set(), [package]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        found = packages[name]
        needs = [*found.get("dependencies", [])]
        for extra in found.get("optional-dependencies", {}).values():
            needs += extra
        todo += [d["name"] for d in needs if d["name"] in local]
    return sorted(seen)


def run(*cmd, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, f"{cmd}: {result.stdout}{result.stderr}"
    return result


def test_wheel_extras_add_each_pool_its_own_library():
    worker = lock()["wdf-worker"]
    extras = worker["optional-dependencies"]
    assert {pool: {d["name"] for d in extras[pool]} for pool in extras} == EXTRAS
    core = {d["name"] for d in worker["dependencies"]}
    assert "wdf-workflow" in core and not core & {"uv", "wdf-aggregate"}


def test_wheel_in_a_clean_venv_imports_every_module(tmp_path):
    """Runs isolated from a directory with no repository above it, so a path into the tree fails."""
    wheels = tmp_path / "wheels"
    for package in workspace_closure("wdf-worker"):
        run("uv", "build", "--wheel", "--package", package, "-o", str(wheels), cwd=ROOT)
    venv = tmp_path / "venv"
    run("uv", "venv", "--python", sys.executable, str(venv))
    python = venv / "bin/python"
    run("uv", "pip", "install", "--python", str(python), *map(str, wheels.glob("*.whl")))
    work = tmp_path / "work"
    work.mkdir()
    probe = subprocess.run([python, "-I", "-c", PROBE], capture_output=True, text=True, cwd=work)
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("pool", EXTRAS)
def test_wheel_export_of_each_pool_lists_its_extra_only(pool):
    exported = run(
        "uv",
        "export",
        "--frozen",
        "--no-hashes",
        "--package",
        "wdf-worker",
        "--extra",
        pool,
        cwd=ROOT,
    ).stdout
    lines = {line.split("==")[0] for line in exported.splitlines() if line[:1].isalpha()}
    lines |= {line.removeprefix("-e ./packages/") for line in exported.splitlines()}
    assert ("uv" in lines) == (pool == "compile")
    assert ("aggregate" in lines) == (pool == "aggregate")
    assert {"workflow", "compile", "runtime", "itest", "componentize-py", "psycopg"} <= lines
