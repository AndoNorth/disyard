"""Stand-in pool worker, run inside the capped container: one Temporal activity worker with one
slot, the store client of its pool's identity, and the imports of the pool worker's core and pool.

The activity `s11_task` runs one step of a task path: it stages through the store client in a
thread, runs every child in its own process group with its output read in chunks into a tail,
heartbeats from its own loop, and uploads what the child wrote. Each step returns what the worker
saw of its children.
"""

import argparse
import asyncio
import hashlib
import json
import os
import resource
import shutil
import signal
import time
from datetime import timedelta
from pathlib import Path

# The pool worker's core, imported whatever the pool, so their memory is counted.
import dsy_events  # noqa: F401
import dsy_keys  # noqa: F401
import dsy_stages  # noqa: F401
from dsy_artifactclient.client import ArtifactClient
from dsy_events.models import ProducerRef
from dsy_runtime.heartbeat import beats
from dsy_storecheck.s3 import load_credentials
from dsy_temporal import event_data_converter, tracing_interceptor
from dsy_workflow.models import (  # noqa: F401
    FailureDetails,
    HeartbeatDetails,
    TaskInput,
    TaskResult,
)
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

TAIL_BYTES = 65536
HEARTBEAT_S = 10
JOB = "123e4567-e89b-42d3-a456-426614174000"
TENANT = "acme"
CFG: argparse.Namespace


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("DSY_")}


async def run_child(argv: list[str], cwd: Path | None = None) -> dict:
    """One child in its own process group, its stdout and stderr merged and read in chunks."""
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env=child_env(),
    )
    tail, pending, count = bytearray(), b"", 0
    while chunk := await process.stdout.read(TAIL_BYTES):
        tail += chunk
        del tail[:-TAIL_BYTES]
        *lines, pending = (pending + chunk).split(b"\n")
        pending = pending[-TAIL_BYTES:]
        count += sum(1 for _ in beats(line.decode(errors="replace") for line in lines))
    code = await process.wait()
    return {
        "exit": code,
        "wall_s": round(time.monotonic() - started, 2),
        "beats": count,
        "tail": bytes(tail[-1500:]).decode(errors="replace"),
    }


def task_dir() -> Path:
    """Task scratch and TMPDIR emptied when a task starts."""
    task = Path(CFG.scratch) / "task"
    for path in (task, Path(os.environ["TMPDIR"])):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
    return task


def producer(task: str) -> ProducerRef:
    return ProducerRef(job=JOB, task=task, attempt="r0a1")


async def upload_tail(task: str, batch: str, tail: str) -> None:
    await asyncio.to_thread(
        CFG.client.put_attempt, tail.encode(), "log-tail", "tail.log", producer(task), TENANT,
        batch, 0,
    )  # fmt: skip


async def staged(reference: dict, dest: Path) -> float:
    started = time.monotonic()
    await asyncio.to_thread(CFG.client.get, reference, dest)
    return round(time.monotonic() - started, 2)


async def step_stage(spec: dict) -> dict:
    """Staging only: fetch the task's input object and upload a log tail, with no child."""
    task = task_dir()
    seconds = await staged(spec["reference"], task / "staged")
    await upload_tail(f"{CFG.pool}-stage", spec["batch"], "staged\n")
    return {"stage_s": seconds}


async def step_warm_up(spec: dict) -> dict:
    from dsy_runtime.command import warm_up

    task = task_dir()
    component = task / "component.wasm"
    seconds = await staged(spec["reference"], component)
    argv = warm_up(component, task / "warm", Path(CFG.scratch) / "cache")
    # The workload library's noop needs heartbeat_every, which the runtime's warm-up input lacks,
    # so the guest would raise after the compile: give it a valid noop input instead.
    noop = {"args": {"heartbeat_every": 1}, "seed": 0, "workload": "noop"}
    (task / "warm" / "guest-input" / "input.json").write_text(json.dumps(noop))
    ran = await run_child(argv)
    return {"stage_s": seconds, "child": ran}


async def step_simulate(spec: dict) -> dict:
    from dsy_runtime.classify import Facts, classify, read_error_file
    from dsy_runtime.command import OUTPUT, command
    from dsy_runtime.run import copy_input, input_digest

    task = task_dir()
    component = task / "component.wasm"
    seconds = await staged(spec["reference"], component)
    input_file = task / "input.json"
    input_file.write_text(json.dumps(spec["params"]))
    argv = command(
        component, task, spec["memory_mib"], spec["timeout_s"], Path(CFG.scratch) / "cache"
    )
    digest = copy_input(input_file, task)
    (task / OUTPUT).mkdir(parents=True, exist_ok=True)
    ran = await run_child(argv)
    results = task / OUTPUT / "results.xml"
    facts = Facts(
        input_modified=input_digest(task) != digest,
        results_bytes=results.stat().st_size if results.is_file() else None,
    )
    classified = classify(ran["exit"], ran["tail"], read_error_file(task), facts)
    found = {"stage_s": seconds, "child": ran, "class": classified.cls}
    if results.is_file():
        found["results_sha256"] = hashlib.sha256(results.read_bytes()).hexdigest()
        await asyncio.to_thread(
            CFG.client.put_attempt, results, "result", "results.xml",
            producer(spec["task"]), TENANT, spec["batch"], spec["params"]["seed"],
        )  # fmt: skip
    await upload_tail(spec["task"], spec["batch"], ran["tail"])
    return found


async def step_upload(spec: dict) -> dict:
    """The collection of a result at the staging cap, as a guest could leave one, with no child."""
    task = task_dir()
    results = task / "results.xml"
    with results.open("wb") as sink:
        for _ in range(spec["bytes"] // TAIL_BYTES):
            sink.write(b"0" * TAIL_BYTES)
    started = time.monotonic()
    await asyncio.to_thread(
        CFG.client.put_attempt, results, "result", "results.xml",
        producer(spec["task"]), TENANT, spec["batch"], 0,
    )  # fmt: skip
    return {"stage_s": round(time.monotonic() - started, 2)}


async def step_compile(spec: dict) -> dict:
    task = task_dir()
    bundle = task / "bundle.tar"
    seconds = await staged(spec["reference"], bundle)
    wheels, out = task / "wheels", task / "out"
    fetch = await run_child(
        ["python", "-m", "dsy_compile.limits", "fetch", "--bundle", str(bundle),
         "--find-links", "file:///var/dsy/mirror", "--out", str(wheels), "--timeout", "600"],
    )  # fmt: skip
    build = await run_child(
        ["python", "-m", "dsy_compile", "build", "--bundle", str(bundle), "--entry", spec["entry"],
         "--out", str(out), "--find-links", str(wheels), "--timeout", "600"],
    )  # fmt: skip
    found = {"stage_s": seconds, "fetch": fetch, "child": build}
    if build["exit"] == 0:
        component = await asyncio.to_thread(
            CFG.client.put_content, out / "component.wasm", "component", "component.wasm"
        )
        await asyncio.to_thread(
            CFG.client.put_content, out / "build-info.json", "build-info", "build-info.json"
        )
        found["component_sha256"] = component.sha256
    await upload_tail(f"{CFG.pool}-build", spec["batch"], build["tail"])
    return found


async def step_aggregate(spec: dict) -> dict:
    task = task_dir()
    index, out = task / "index.json", task / "out"
    index.write_text(json.dumps(spec["reference"]))
    ran = await run_child(
        ["python", "-m", "dsy_aggregate", "run", "--index", str(index), "--tenant", TENANT,
         "--aggregation", spec["aggregation"], "--args", "{}", "--out", str(out),
         "--endpoint", CFG.store, "--credentials", CFG.credentials, "--role", "aggregate"],
    )  # fmt: skip
    found = {"child": ran}
    if ran["exit"] == 0:
        await asyncio.to_thread(
            CFG.client.put_attempt, out / "summary.json", "summary", "summary.json",
            producer(spec["task"]), TENANT, spec["batch"],
        )  # fmt: skip
    await upload_tail(spec["task"], spec["batch"], ran["tail"])
    return found


STEPS = {
    "stage": step_stage,
    "warm_up": step_warm_up,
    "simulate": step_simulate,
    "upload": step_upload,
    "compile": step_compile,
    "aggregate": step_aggregate,
}


@activity.defn(name="s11_task")
async def s11_task(spec: dict) -> dict:
    """Heartbeats from its own loop every HEARTBEAT_S, whatever the step prints."""
    step = asyncio.ensure_future(STEPS[spec["step"]](spec))
    beats_sent = 0
    while True:
        done, _ = await asyncio.wait({step}, timeout=HEARTBEAT_S)
        if done:
            break
        activity.heartbeat({"step": spec["step"], "started_at": None})
        beats_sent += 1
    found = step.result()
    found["worker_beats"] = beats_sent
    found["self_maxrss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    found["children_maxrss_mib"] = round(
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1
    )
    return found


async def main() -> None:
    global CFG
    parser = argparse.ArgumentParser()
    for flag in ("--pool", "--temporal", "--namespace", "--queue", "--store", "--credentials"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--scratch", default="/var/dsy")
    CFG = parser.parse_args()
    for name in ("HOME", "TMPDIR", "UV_CACHE_DIR", "XDG_CACHE_HOME"):
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)
    if CFG.pool == "simulate":
        import dsy_runtime.classify  # noqa: F401
        import dsy_runtime.command  # noqa: F401
    elif CFG.pool == "compile":
        import dsy_compile.limits  # noqa: F401
    CFG.client = ArtifactClient(CFG.store, load_credentials(CFG.credentials)[CFG.pool])
    client = await Client.connect(
        CFG.temporal,
        namespace=CFG.namespace,
        data_converter=event_data_converter,
        interceptors=[tracing_interceptor()],
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    worker = Worker(
        client,
        task_queue=CFG.queue,
        activities=[s11_task],
        max_concurrent_activities=1,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    async with worker:
        (Path(CFG.scratch) / "ready").touch()
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
