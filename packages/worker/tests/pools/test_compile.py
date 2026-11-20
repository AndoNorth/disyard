"""The compile task path with SeaweedFS and the real uv, componentize-py and wasm-tools: the
workload library builds with its cache key, and each way a build ends maps to its failure class."""

import asyncio
import dataclasses
import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from wft_artifactclient.client import ArtifactClient
from wft_worker.pools import Context, compile
from wft_workflow.contract import check_task_result
from wft_workflow.models import REASON_MAX

pytestmark = pytest.mark.integration

# Children inherit it, so each process a task started is found by it in /proc.
MARK = "WFT_TEST_TASK"


@pytest.fixture
def given(cases):
    return cases("compile")


@pytest.fixture
def context(pool_store, tmp_path) -> Context:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    return Context(
        store=ArtifactClient(pool_store.endpoint, pool_store.credentials["compile"]),
        scratch=tmp_path / "scratch",
        slot_memory_mib=1024,
        mirror=mirror.as_uri(),
    )


def environment(start_to_close_s: float = 300) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        started_time=datetime.now(UTC),
        start_to_close_timeout=timedelta(seconds=start_to_close_s),
    )
    return env


def failure(task, context, env=None) -> ApplicationError:
    with pytest.raises(ApplicationError) as raised:
        asyncio.run((env or environment()).run(compile.run, task, context))
    error = raised.value
    [details] = error.details
    assert error.type == details.reason.split(":")[0]
    assert len(details.reason) <= REASON_MAX
    assert details.log_tail is not None
    return error


def marked(mark: str) -> dict[int, str]:
    """The live processes, other than this one, that carry the mark, with their command lines."""
    found = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if f"{MARK}={mark}".encode() not in (proc / "environ").read_bytes().split(b"\0"):
                continue
            state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if state not in ("Z", "X"):
            found[int(proc.name)] = command
    return found


def test_workload_bundle_builds_with_its_cache_key(given, context, pool_store):
    result = asyncio.run(environment().run(compile.run, given.succeeds, context))
    # The compile role only writes its outputs, so they are read back as the admin.
    admin = ArtifactClient(pool_store.endpoint, pool_store.credentials["admin"])
    check_task_result(result)
    component, build_info = result.outputs
    assert (component.kind, build_info.kind) == ("component", "build-info")
    document = json.loads(admin.get_bytes(build_info, build_info.size))
    assert document["cache_key"] == given.succeeds.cache_key
    assert document["component"]["sha256"] == component.sha256
    tail = admin.get_bytes(result.log_tail, result.log_tail.size).decode()
    assert f"cache_key: {given.succeeds.cache_key}" in tail
    assert result.log_tail.producer.attempt == "r0a1"
    assert not (context.scratch / compile.TASK).exists()


def test_missing_entry_compile_failed_not_retried(given, context):
    error = failure(given.fails, context)
    assert error.type == "compile_failed"
    assert error.non_retryable
    assert error.details[0].deterministic
    assert "wft_workloads.missing is not a module of the bundle" in error.details[0].reason


def test_library_error_is_worker_shutdown(given, context, monkeypatch):
    """A tool missing from the image is the platform's fault, so it is retried on the
    infrastructure budget and never blamed on the source."""
    tools = Path(shutil.which("wasm-tools")).parent
    path = [entry for entry in os.environ["PATH"].split(os.pathsep) if Path(entry) != tools]
    monkeypatch.setenv("PATH", os.pathsep.join(path))
    error = failure(given.succeeds, context)
    assert error.type == "worker_shutdown"
    assert not error.non_retryable
    assert not error.details[0].deterministic
    reason = error.details[0].reason
    assert reason.startswith("worker_shutdown: compile-library-error: ")
    assert "wasm-tools" in reason


def test_timeout_compile_timeout(given, context):
    """Two seconds are left once the upload reserve is kept back, which no build fits in."""
    env = environment(compile.UPLOAD_RESERVE_S + 2)
    error = failure(given.succeeds, context, env)
    assert error.type == "compile_timeout"
    assert not error.non_retryable
    assert "ran past the deadline" in error.details[0].reason


@pytest.mark.parametrize("running", ["uv", "componentize-py"])
def test_stop_ends_uv_and_componentize(given, context, monkeypatch, tmp_path, running):
    """A cancel while the build's uv or componentize-py runs leaves no process of the task. The
    install takes a moment only, so a uv that sleeps through `pip install` stands in for it."""
    mark = str(uuid.uuid4())
    monkeypatch.setenv(MARK, mark)
    if running == "uv":
        bin_ = tmp_path / "bin"
        bin_.mkdir()
        (bin_ / "uv").write_text(
            f'#!/bin/sh\n[ "$1" = pip ] && exec sleep 600\nexec {shutil.which("uv")} "$@"\n'
        )
        (bin_ / "uv").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
        sign = "sleep 600"
    else:
        sign = "componentize-py"
    env = environment()

    async def body():
        task = asyncio.create_task(env.run(compile.run, given.succeeds, context))
        until = time.monotonic() + 120
        while not any(sign in command for command in marked(mark).values()):
            assert not task.done(), task.result()
            assert time.monotonic() < until, f"no {sign} within 120 s"
            await asyncio.sleep(0.1)
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(body(), 180))
    until = time.monotonic() + 2
    while (left := marked(mark)) and time.monotonic() < until:
        time.sleep(0.05)
    assert left == {}
