"""The runner with the real wasmtime and the workload component: a warm-up that fills the module
cache, a warm guest, and a stop of a guest that would run far longer."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from wft_compile.build import build
from wft_runtime.command import GUEST_INPUT, INPUT_FILE, OUTPUT, command, warm_up
from wft_runtime.contract import canonical
from wft_worker.runner import Child, Exit
from wft_workloads import bundle

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[4]
ECHO = json.loads((ROOT / "contracts/workload/vectors/level0/echo/input.json").read_text())
MEMORY_MIB = 384
# Far more work than the test waits for, beating often enough that the first beat comes early.
BURN = {"workload": "cpu_burn", "seed": 7, "args": {"units": 10**9, "heartbeat_every": 1000}}
STOP_WITHIN_S = 6


@pytest.fixture(scope="module")
def component(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("workloads")
    return build(bundle.write(work), "wft_workloads.main", work / "built").component


@pytest.fixture(scope="module")
def cache(component, tmp_path_factory) -> Path:
    """A module cache filled by a warm-up run through the runner. The warm-up's exit is not
    checked: wasmtime fills the cache before the guest runs, and the noop input of the runtime
    library lacks the heartbeat_every the workload library requires, so the guest fails."""
    cache = tmp_path_factory.mktemp("cache")
    scratch = tmp_path_factory.mktemp("warm-up")

    async def body():
        child = await Child.start(warm_up(component, scratch, cache), scratch)
        return await child.wait()

    exit = asyncio.run(asyncio.wait_for(body(), 300))
    print("warm-up", exit)
    modules = [p for p in (cache / "wasmtime/modules").rglob("*") if p.is_file()]
    assert [p for p in modules if p.suffix != ".stats"], "the warm-up left no module cache entry"
    return cache


def scratch(path: Path, params: dict) -> Path:
    (path / GUEST_INPUT).mkdir(parents=True)
    (path / OUTPUT).mkdir()
    (path / GUEST_INPUT / INPUT_FILE).write_text(canonical(params))
    return path


def test_wasmtime_warm_guest_runs_echo(component, cache, tmp_path):
    work = scratch(tmp_path, ECHO)

    async def body():
        child = await Child.start(command(component, work, MEMORY_MIB, 30, cache), work)
        lines = [line async for line in child.lines()]
        return lines, await child.wait()

    lines, exit = asyncio.run(asyncio.wait_for(body(), 60))
    print(exit, lines)
    assert exit == Exit(0, None, False)
    assert (work / OUTPUT / "results.xml").is_file()


def test_wasmtime_stop_ends_cpu_burn(component, cache, tmp_path):
    """A cpu_burn guest beats, and is gone within 6 s of stop()."""
    work = scratch(tmp_path, BURN)

    async def body():
        child = await Child.start(command(component, work, MEMORY_MIB, None, cache), work)
        async for _ in child.lines():
            if child.beat is not None:
                break
        beat = child.beat
        started = time.monotonic()
        await child.stop()
        exit = await child.wait()
        return beat, exit, time.monotonic() - started

    beat, exit, seconds = asyncio.run(asyncio.wait_for(body(), 120))
    print(beat, exit, f"stopped in {seconds:.2f}s")
    assert beat["steps"] == BURN["args"]["units"]
    assert exit.code is None and exit.signal is not None
    assert seconds < STOP_WITHIN_S
