"""Runs the stand-in pool worker in a container capped like each `ci` slot and prints one
`figure <name> <value>` line per number of docs/delivery/spikes/S11/worker-in-slot.md.

The host holds a SeaweedFS container (one T1 lock file) and the capped worker container (the other),
so no other T1 work runs beside it, and the Temporal dev server runs as a host process from the
binary of the pinned CLI image. The capped container shares the host network to reach both on
127.0.0.1. A host workflow drives each step of a task path as one `s11_task` activity on the
container's queue, while a sampler reads the container's cgroup every 10 ms.
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from wdf_artifactclient.aggregate import put_index
from wdf_artifactclient.client import ArtifactClient, kind_caps
from wdf_artifactclient.models import IndexEntry
from wdf_compile.build import build
from wdf_events.models import ProducerRef
from wdf_itest.session import RUN_LABEL, Session, image_ref
from wdf_runtime.pins import GUEST_OVERHEAD_MIB
from wdf_storecheck.fixtures import running
from wdf_storecheck.s3 import dump_credentials
from wdf_temporal import event_data_converter
from wdf_workflow.contract import naming
from wdf_workloads import bundle

MIB = 1048576
ROOT = Path(__file__).resolve().parents[5]
CODE = Path(__file__).resolve().parent
JOB = "123e4567-e89b-42d3-a456-426614174000"
TENANT = "acme"
ENTRY = "wdf_workloads.main"
ECHO = json.loads((ROOT / "contracts/workload/vectors/level0/echo/input.json").read_text())
# The smallest valid row, so a result at the staging cap holds the most rows, as the aggregate
# library's own memory test builds it.
HEAD = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<results schema="wdf.sim.v1" seed="3">\n'
    b'<block id="b" kind="scalar">\n'
)
ROW = b'<row name="" value="0"/>\n'
TAIL = b"</block>\n</results>\n"
SLOTS = {"simulate": (384, 0.25), "compile": (1024, 1.0), "aggregate": (1024, 1.0)}


def figure(name: str, value) -> None:
    print(f"figure {name} {value}", flush=True)


def flat(path: Path) -> dict[str, int]:
    pairs = (line.partition(" ") for line in path.read_text().splitlines())
    return {key: int(value) for key, _, value in pairs}


def status_kib(pid: int, field: str) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1])
    raise KeyError(field)


def reserve_of(peak_mib: float) -> int:
    """The worker's peak plus a quarter in whole 64 MiB steps, the sizing rule of S8."""
    return math.ceil(peak_mib * 1.25 / 64) * 64


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sampler(threading.Thread):
    """Peak working set (memory.current less inactive_file) and peak anon, every 10 ms, since the
    last mark."""

    def __init__(self, cgroup: Path):
        super().__init__(daemon=True)
        self.cgroup = cgroup
        self.ws = self.anon = 0
        self._halt = threading.Event()
        self._lock = threading.Lock()

    def sample(self) -> None:
        try:
            stat = flat(self.cgroup / "memory.stat")
            current = int((self.cgroup / "memory.current").read_text())
        except (OSError, ValueError):
            return
        with self._lock:
            self.ws = max(self.ws, current - stat["inactive_file"])
            self.anon = max(self.anon, stat["anon"])

    def mark(self) -> tuple[float, float]:
        """The peaks since the last mark, in MiB, and a fresh start."""
        self.sample()
        with self._lock:
            peaks = (self.ws / MIB, self.anon / MIB)
            self.ws = self.anon = 0
        return peaks

    def run(self) -> None:
        while not self._halt.is_set():
            self.sample()
            self._halt.wait(0.01)

    def stop(self) -> None:
        self._halt.set()
        self.join()


@workflow.defn(name="S11Step")
class S11Step:
    @workflow.run
    async def run(self, queue: str, spec: dict) -> dict:
        return await workflow.execute_activity(
            "s11_task",
            spec,
            task_queue=queue,
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    run = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and run.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {run.stderr.strip()}")
    return run


def temporal_binary(out: Path, run_id: str) -> Path:
    """The temporal CLI of the pinned image, copied out of a container that is never started."""
    binary = out / "temporal"
    name = f"wdf-{run_id}-temporal-cli"
    docker(
        "create",
        "--name",
        name,
        "--label",
        f"{RUN_LABEL}={run_id}",
        image_ref("temporalio/temporal"),
    )
    try:
        docker("cp", f"{name}:/usr/local/bin/temporal", str(binary))
    finally:
        docker("rm", "-f", name, check=False)
    return binary


def start_dev_server(binary: Path, port: int, out: Path) -> subprocess.Popen:
    log = open(out / "temporal.log", "wb")
    server = subprocess.Popen(
        [str(binary), "server", "start-dev", "--ip", "127.0.0.1", "--port", str(port),
         "--headless", "--namespace", naming().namespace, "--log-level", "error"],
        stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip
    deadline = time.monotonic() + 60
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return server
        except OSError:
            if time.monotonic() > deadline or server.poll() is not None:
                raise RuntimeError("the dev server did not start") from None
            time.sleep(0.2)


def seed(store, out: Path) -> dict:
    """The workload library bundle, its component built by wdf_compile, and an index of one result
    at the staging cap."""
    role = lambda name: ArtifactClient(store.endpoint, store.credentials[name])  # noqa: E731
    store.store().create_buckets()
    tar = bundle.write(out / "seed")
    started = time.monotonic()
    built = build(tar, ENTRY, out / "seed" / "built")
    figure("seed.component_build_s", f"{time.monotonic() - started:.1f}")
    figure("seed.component_bytes", built.component.stat().st_size)
    figure("seed.component_mib", f"{built.component.stat().st_size / MIB:.1f}")
    figure("seed.cache_key", built.cache_key)
    source = role("indexer").put_content(tar, "source", "bundle.tar")
    component = role("compile").put_content(built.component, "component", "component.wasm")
    cap = kind_caps()["result"]
    result = out / "seed" / "results.xml"
    count = (cap - len(HEAD) - len(TAIL)) // len(ROW)
    with result.open("wb") as sink:
        sink.write(HEAD)
        sink.write(ROW * count)
        sink.write(TAIL)
    figure("seed.result_bytes", result.stat().st_size)
    figure("seed.result_cap_bytes", cap)
    figure("seed.result_rows", count)
    who = ProducerRef(job=JOB, task="simulate-0000", attempt="r0a1")
    reference = role("simulate").put_attempt(
        result, "result", "results.xml", who, TENANT, "seed", 3
    )
    entry = IndexEntry(task="simulate-0000", status="succeeded", reference=reference)
    header = {"job": JOB, "tenant": TENANT, "task": "aggregate-0", "partial": "refuse"}
    index = put_index(role("indexer"), [entry], header)
    dump = lambda ref: ref.model_dump(mode="json", by_alias=True, exclude_none=True)  # noqa: E731
    return {"source": dump(source), "component": dump(component), "index": dump(index)}


class Capped:
    """One worker container capped like a slot, removed on exit."""

    def __init__(
        self, args, pool: str, run: int, memory_mib: int, cpus: float, address: str, store
    ):
        self.name = f"wdf-{args.run_id}-{pool}-{memory_mib}-{run}"
        self.scratch = Path(tempfile.mkdtemp(prefix=f"s11-{pool}-", dir=args.out))
        creds = self.scratch.parent / f"{self.name}.creds.json"
        dump_credentials(
            creds,
            {
                pool: store.credentials[pool],
                **({"aggregate": store.credentials["aggregate"]} if pool == "aggregate" else {}),
            },
        )
        self.queue = f"s11-{pool}-{uuid.uuid4().hex[:8]}"
        env = {
            "HOME": "/var/wdf/home", "TMPDIR": "/var/wdf/tmp", "UV_CACHE_DIR": "/var/wdf/uv-cache",
            "XDG_CACHE_HOME": "/var/wdf/xdg-cache",
        }  # fmt: skip
        command = [
            "docker", "run", "--detach", "--name", self.name,
            "--label", f"{RUN_LABEL}={args.run_id}",
            "--memory", f"{memory_mib}m", "--memory-swap", f"{memory_mib}m", "--cpus", str(cpus),
            "--network", "host", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
            f"--volume={self.scratch}:/var/wdf", f"--volume={creds}:/etc/s11/creds.json:ro",
            f"--volume={CODE / 'worker.py'}:/opt/s11/worker.py:ro",
            *(f"--env={k}={v}" for k, v in env.items()), args.image,
            "python", "/opt/s11/worker.py", "--pool", pool, "--temporal", address,
            "--namespace", naming().namespace, "--queue", self.queue, "--store", store.endpoint,
            "--credentials", "/etc/s11/creds.json",
        ]  # fmt: skip
        self.id = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        self.pid = int(docker("inspect", "--format", "{{.State.Pid}}", self.id).stdout)
        self.cgroup = Path(f"/sys/fs/cgroup/system.slice/docker-{self.id}.scope")
        deadline = time.monotonic() + 120
        while not (self.scratch / "ready").exists():
            if time.monotonic() > deadline or not Path(f"/proc/{self.pid}").exists():
                logs = docker("logs", self.id, check=False).stderr[-2000:]
                raise RuntimeError(f"{self.name} never polled: {logs}")
            time.sleep(0.1)
        self.events = flat(self.cgroup / "memory.events")
        self.sampler = Sampler(self.cgroup)
        self.sampler.start()

    def worker_mib(self) -> tuple[float, float]:
        """The worker process's own resident memory now and its peak, in MiB."""
        return status_kib(self.pid, "VmRSS") / 1024, status_kib(self.pid, "VmHWM") / 1024

    def oom_kills(self) -> int:
        return flat(self.cgroup / "memory.events")["oom_kill"] - self.events["oom_kill"]

    def versions(self) -> None:
        """The tool versions of the image, read once its measurements are over."""
        for tool in ("python", "wasmtime", "wasm-tools", "uv", "componentize-py"):
            text = docker("exec", self.id, tool, "--version", check=False).stdout.split()
            figure(f"tool.{tool}", text[1] if len(text) > 1 else "missing")
        script = "import importlib.metadata as m; print(m.version('temporalio'))"
        figure("tool.temporalio", docker("exec", self.id, "python", "-c", script).stdout.strip())

    def close(self, keep_logs: Path) -> None:
        self.sampler.stop()
        logs = docker("logs", self.id, check=False)
        (keep_logs / f"{self.name}.log").write_text(logs.stdout + logs.stderr)
        docker("rm", "--force", "--volumes", self.id, check=False)
        shutil.rmtree(self.scratch, ignore_errors=True)


async def one_step(client: Client, capped: Capped, prefix: str, spec: dict) -> dict:
    capped.sampler.mark()
    started = time.monotonic()
    try:
        found = await client.execute_workflow(
            "S11Step", args=[capped.queue, spec], id=f"s11-{uuid.uuid4().hex}",
            task_queue="s11-driver",
        )  # fmt: skip
        error = None
    except Exception as failure:  # the step's verdict is a figure
        found, error = {}, f"{type(failure).__name__}: {getattr(failure, 'cause', None) or failure}"
    wall = time.monotonic() - started
    ws, anon = capped.sampler.mark()
    rss, hwm = capped.worker_mib()
    figure(f"{prefix}.wall_s", f"{wall:.2f}")
    figure(f"{prefix}.peak_ws_mib", f"{ws:.0f}")
    figure(f"{prefix}.peak_anon_mib", f"{anon:.0f}")
    figure(f"{prefix}.worker_rss_mib", f"{rss:.1f}")
    figure(f"{prefix}.worker_hwm_mib", f"{hwm:.1f}")
    figure(f"{prefix}.oom_kill", capped.oom_kills())
    if error:
        figure(f"{prefix}.error", json.dumps(error[:400]))
    for key in ("stage_s", "class", "worker_beats", "children_maxrss_mib", "results_sha256"):
        if key in found:
            figure(f"{prefix}.{key}", found[key])
    for key in ("fetch", "child"):
        if key in found:
            child = found[key]
            figure(f"{prefix}.{key}.exit", child["exit"])
            figure(f"{prefix}.{key}.wall_s", child["wall_s"])
            figure(f"{prefix}.{key}.beats", child["beats"])
            if child["exit"] != 0:
                figure(f"{prefix}.{key}.tail", json.dumps(child["tail"][-400:]))
    found["error"] = error
    return found


async def run_pool(args, client, address, store, seeded, pool: str, run: int) -> bool:
    memory_mib, cpus = SLOTS[pool]
    if pool == "simulate" and args.simulate_mib:
        memory_mib = args.simulate_mib
    prefix = f"{pool}.{memory_mib}.{run}"
    capped = Capped(args, pool, run, memory_mib, cpus, address, store)
    batch = f"{pool}-{run}"
    ok = True
    try:
        rss, hwm = capped.worker_mib()
        figure(f"{prefix}.idle.worker_rss_mib", f"{rss:.1f}")
        figure(f"{prefix}.idle.peak_ws_mib", f"{capped.sampler.mark()[0]:.0f}")
        stage_ref = {"simulate": "component", "compile": "source", "aggregate": "index"}[pool]
        found = await one_step(
            client,
            capped,
            f"{prefix}.stage",
            {"step": "stage", "reference": seeded[stage_ref], "batch": batch},
        )
        ok &= found["error"] is None
        if pool == "simulate":
            # The worker's own peak covers collecting a result at the staging cap, its largest
            # store transfer on this pool, which runs after the guest has exited.
            found = await one_step(client, capped, f"{prefix}.upload", {
                "step": "upload", "bytes": kind_caps()["result"], "task": "simulate-upload",
                "batch": batch,
            })  # fmt: skip
            ok &= found["error"] is None
        _, worker_peak = capped.worker_mib()
        reserve = reserve_of(worker_peak)
        figure(f"{prefix}.worker_reserve_mib", reserve)
        if pool == "simulate":
            found = await one_step(
                client,
                capped,
                f"{prefix}.warm_up",
                {"step": "warm_up", "reference": seeded["component"]},
            )
            ok &= found["error"] is None and found["child"]["exit"] == 0
            task_mib = memory_mib - reserve
            figure(f"{prefix}.echo.memory_mib", task_mib)
            figure(f"{prefix}.echo.guest_cap_mib", task_mib - GUEST_OVERHEAD_MIB)
            found = await one_step(client, capped, f"{prefix}.echo", {
                "step": "simulate", "reference": seeded["component"], "params": ECHO,
                "memory_mib": task_mib, "timeout_s": 120, "task": "simulate-echo", "batch": batch,
            })  # fmt: skip
            ok &= found["error"] is None and found.get("class") == "ok"
            # Probe beyond the pass line: a warm guest that fills most of its cap.
            hold = task_mib - GUEST_OVERHEAD_MIB - args.hold_margin_mib
            figure(f"{prefix}.hold.mib", hold)
            await one_step(client, capped, f"{prefix}.hold", {
                "step": "simulate", "reference": seeded["component"],
                "params": {"workload": "mem_hold", "seed": 7,
                           "args": {"mib": hold, "units": 4, "heartbeat_every": 1}},
                "memory_mib": task_mib, "timeout_s": 120, "task": "simulate-hold", "batch": batch,
            })  # fmt: skip
        elif pool == "compile":
            found = await one_step(client, capped, f"{prefix}.build", {
                "step": "compile", "reference": seeded["source"], "entry": ENTRY, "batch": batch,
            })  # fmt: skip
            ok &= found["error"] is None and found["child"]["exit"] == 0
        else:
            for aggregation in ("digest_list", "summary_stats"):
                found = await one_step(client, capped, f"{prefix}.{aggregation}", {
                    "step": "aggregate", "reference": seeded["index"], "aggregation": aggregation,
                    "task": f"aggregate-{aggregation.replace('_', '-')}", "batch": batch,
                })  # fmt: skip
                ok &= found["error"] is None and found["child"]["exit"] == 0
        oom = capped.oom_kills()
        ok &= oom == 0
        figure(f"{prefix}.oom_kill", oom)
        figure(
            f"{prefix}.memory_peak_mib",
            f"{int((capped.cgroup / 'memory.peak').read_text()) / MIB:.0f}",
        )
        figure(f"{prefix}.worker_hwm_mib", f"{capped.worker_mib()[1]:.1f}")
        figure(f"{prefix}.completed", "yes" if ok else "no")
        if not args.versions_read:
            capped.versions()
            args.versions_read = True
    finally:
        capped.close(Path(args.out))
    return ok


async def drive(args, store) -> None:
    binary = temporal_binary(Path(args.out), args.run_id)
    port = free_port()
    server = start_dev_server(binary, port, Path(args.out))
    try:
        address = f"127.0.0.1:{port}"
        client = await Client.connect(
            address, namespace=naming().namespace, data_converter=event_data_converter
        )
        seeded = seed(store, Path(args.out))
        async with Worker(
            client, task_queue="s11-driver", workflows=[S11Step],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):  # fmt: skip
            for pool in args.pools:
                passed = 0
                for run in range(1, args.runs + 1):
                    passed += await run_pool(args, client, address, store, seeded, pool, run)
                figure(f"{pool}.runs_passed", f"{passed}/{args.runs}")
    finally:
        server.terminate()
        server.wait(10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--pools", nargs="+", default=["simulate", "compile", "aggregate"])
    parser.add_argument("--simulate-mib", type=int, help="a simulate slot other than the preset's")
    parser.add_argument("--hold-margin-mib", type=int, default=32)
    args = parser.parse_args()
    args.versions_read = False
    with Session(containers=2) as t1:
        args.run_id = t1.run_id
        with running(t1, "seaweedfs", Path(args.out)) as store:
            asyncio.run(drive(args, store))


if __name__ == "__main__":
    main()
