"""Measures the order in which one single-slot worker starts activities that wait on one fresh
pool-shaped queue of a Temporal dev server started through dsy_itest, and prints one figure line,
`figure <name> <value>`, per number of docs/delivery/spikes/S12/queue-order.md.

Per run, a measurement workflow on its own queue `s12-<hex>-control` schedules the activities,
numbered 0 to n-1, on `s12-<hex>-dsy-simulate`, with no worker on that queue. Once all are pending,
one worker with `max_concurrent_activities=1` starts on it and records the start order.

    python measure.py [--activities 200] [--gaps-ms 20,0] [--repeat 3] [--partitions N] [--out DIR]

A gap above 0 schedules each activity in its own workflow task, a timer of that gap apart; a gap of
0 schedules them all in one workflow task, as a job's sibling tasks are. `--partitions` sets the
read and write partition counts of every task queue through the dev server's dynamic config, as a
control against the server's default.
"""

import argparse
import asyncio
import os
import platform
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

from dsy_itest import Session
from temporalio import activity, workflow
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    GetClusterInfoRequest,
    ListTaskQueuePartitionsRequest,
)
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

NAMESPACE = "default"
# The pass line of the spike block: every activity starts within 10 minutes, and none starts after
# more than a quarter of the run's activities scheduled later than it.
DEADLINE_S = 600
MAX_OVERTAKING_SHARE = 0.25


def figure(out: Path, name: str, value) -> None:
    line = f"figure {name} {value}"
    print(line, flush=True)
    with (out / "figures.txt").open("a") as f:
        f.write(line + "\n")


def say(text: str) -> None:
    print(f"[{time.strftime('%T')}] {text}", file=sys.stderr, flush=True)


@workflow.defn(name="S12Measure")
class Measure:
    def __init__(self) -> None:
        self.scheduled = 0

    @workflow.query
    def count(self) -> int:
        return self.scheduled

    @workflow.run
    async def run(self, n: int, queue: str, gap_ms: int) -> None:
        pending = []
        for i in range(n):
            pending.append(
                workflow.start_activity(
                    "s12_step", i, task_queue=queue, start_to_close_timeout=timedelta(seconds=60)
                )
            )
            self.scheduled += 1
            if gap_ms:
                await workflow.sleep(timedelta(milliseconds=gap_ms))
        await asyncio.gather(*pending)


class Recorder:
    def __init__(self, n: int) -> None:
        self.n = n
        self.starts: list[tuple[int, float, int]] = []
        self.done = asyncio.Event()

    @activity.defn(name="s12_step")
    async def step(self, i: int) -> None:
        self.starts.append((i, time.monotonic(), activity.info().attempt))
        if len(self.starts) >= self.n:
            self.done.set()


def order_figures(order: list[int]) -> tuple[int, int, int]:
    """The inversion count, the largest overtaking and the activity it overtook most.

    The overtaking of activity i is the number of activities scheduled after i that started before
    it."""
    overtaking = [0] * len(order)
    inversions = 0
    for k, i in enumerate(order):
        later_first = sum(1 for j in order[:k] if j > i)
        overtaking[i] = later_first
        inversions += later_first
    worst = max(range(len(order)), key=overtaking.__getitem__)
    return inversions, overtaking[worst], worst


async def measure(address: str, out: Path, name: str, n: int, gap_ms: int) -> bool:
    client = await Client.connect(address, namespace=NAMESPACE)
    tag = secrets.token_hex(4)
    control, queue = f"s12-{tag}-control", f"s12-{tag}-dsy-simulate"
    figure(out, f"{name}.queue", queue)
    figure(out, f"{name}.gap_ms", gap_ms)
    async with Worker(
        client,
        task_queue=control,
        workflows=[Measure],
        workflow_runner=UnsandboxedWorkflowRunner(),
        disable_eager_activity_execution=True,
    ):
        handle = await client.start_workflow(
            Measure.run, args=[n, queue, gap_ms], id=f"s12-{tag}", task_queue=control
        )
        t0 = time.monotonic()
        while await handle.query(Measure.count) < n:
            await asyncio.sleep(0.2)
        # The last schedule command is sent; wait until the server holds every activity pending.
        while len((await handle.describe()).raw_description.pending_activities) < n:
            await asyncio.sleep(0.2)
        figure(out, f"{name}.schedule_s", round(time.monotonic() - t0, 2))
        partitions = await client.workflow_service.list_task_queue_partitions(
            ListTaskQueuePartitionsRequest(namespace=NAMESPACE, task_queue=TaskQueue(name=queue))
        )
        figure(out, f"{name}.activity_partitions", len(partitions.activity_task_queue_partitions))

        recorder = Recorder(n)
        say(f"{name}: {n} pending on {queue}, starting the single-slot worker")
        start = time.monotonic()
        async with Worker(
            client, task_queue=queue, activities=[recorder.step], max_concurrent_activities=1
        ):
            try:
                await asyncio.wait_for(recorder.done.wait(), DEADLINE_S)
            except TimeoutError:
                pass
            elapsed = time.monotonic() - start
        if len(recorder.starts) >= n:
            await handle.result()

    # A retried activity counts at its first start.
    order = list(dict.fromkeys(i for i, _, _ in recorder.starts))
    started = len(order)
    figure(out, f"{name}.started", started)
    figure(out, f"{name}.retried", sum(1 for _, _, attempt in recorder.starts if attempt > 1))
    figure(out, f"{name}.all_started_s", round(elapsed, 2))
    (out / f"{name}.order.txt").write_text(" ".join(map(str, order)) + "\n")
    passed = False
    if started == n:
        inversions, largest, worst = order_figures(order)
        figure(out, f"{name}.inversions", inversions)
        figure(out, f"{name}.inversions_max_possible", n * (n - 1) // 2)
        figure(out, f"{name}.largest_overtaking", largest)
        figure(out, f"{name}.largest_overtaking_activity", worst)
        figure(out, f"{name}.started_at_position", order.index(worst))
        passed = elapsed <= DEADLINE_S and largest <= MAX_OVERTAKING_SHARE * n
    figure(out, f"{name}.pass", passed)
    return passed


async def server_version(address: str) -> str:
    # The port accepts connections before the server answers, so the first connect is retried.
    for _ in range(60):
        try:
            client = await Client.connect(address, namespace=NAMESPACE)
            break
        except RuntimeError:
            await asyncio.sleep(1)
    else:
        client = await Client.connect(address, namespace=NAMESPACE)
    return (await client.workflow_service.get_cluster_info(GetClusterInfoRequest())).server_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activities", type=int, default=200)
    parser.add_argument("--gaps-ms", default="20,0")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--partitions", type=int)
    parser.add_argument("--out", type=Path, default=Path(f"/tmp/s12-{int(time.time())}"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    gaps = [int(g) for g in args.gaps_ms.split(",")]

    figure(args.out, "host.python", platform.python_version())
    figure(args.out, "host.kernel", platform.release())
    figure(args.out, "host.cpus", os.cpu_count())
    command = ["server", "start-dev", "--ip", "0.0.0.0", "--headless"]
    if args.partitions:
        for key in ("matching.numTaskqueueReadPartitions", "matching.numTaskqueueWritePartitions"):
            command += ["--dynamic-config-value", f"{key}={args.partitions}"]
    results = []
    with Session() as t1:
        with t1.container(
            "temporalio/temporal", ports={0: 7233}, command=tuple(command)
        ) as container:
            container.wait_ready(0)
            address = f"127.0.0.1:{container.host_ports[0]}"
            figure(args.out, "temporal.server_version", asyncio.run(server_version(address)))
            run = 0
            for _ in range(args.repeat):
                for gap in gaps:
                    run += 1
                    name = f"run{run}_gap{gap}"
                    results.append(
                        asyncio.run(measure(address, args.out, name, args.activities, gap))
                    )
    figure(args.out, "runs_passed", f"{sum(results)}/{len(results)}")
    say(f"figures and start orders in {args.out}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
