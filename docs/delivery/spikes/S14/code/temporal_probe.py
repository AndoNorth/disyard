"""Temporal series probe.

Drives one workflow that leaves a backlog on a queue nobody polls, times out one activity on its
heartbeat and completes a few quick activities on a single-slot pool-shaped queue, with the SDK's
Prometheus endpoint on, then prints every series line of the matching, history and SDK scrapes
that the result names.

    uv run --frozen python temporal_probe.py --frontend 127.0.0.1:PORT \
        --matching URL --history URL --sdk-port PORT
"""

import argparse
import asyncio
import re
import urllib.request
import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker

POOL_QUEUE = "s14-pool"
BACKLOG_QUEUE = "s14-backlog"
WORKFLOW_QUEUE = "s14-workflow"
BACKLOG = 20


@activity.defn
async def quick() -> str:
    return "ok"


@activity.defn
async def silent() -> str:
    # Runs past its heartbeat timeout without heartbeating, so the server times it out.
    await asyncio.sleep(15)
    return "late"


@workflow.defn
class Probe:
    @workflow.run
    async def run(self) -> str:
        for _ in range(BACKLOG):
            workflow.start_activity(
                "never",
                task_queue=BACKLOG_QUEUE,
                start_to_close_timeout=timedelta(hours=1),
            )
        for _ in range(5):
            await workflow.execute_activity(
                quick, task_queue=POOL_QUEUE, start_to_close_timeout=timedelta(seconds=30)
            )
        try:
            await workflow.execute_activity(
                silent,
                task_queue=POOL_QUEUE,
                start_to_close_timeout=timedelta(seconds=60),
                heartbeat_timeout=timedelta(seconds=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception as err:  # noqa: BLE001 - the timeout is the expected outcome
            workflow.logger.info("silent failed: %s", err)
        await workflow.sleep(timedelta(hours=1))
        return "done"


WANT = re.compile(
    r"^(approximate_backlog\w*|\w*heartbeat\w*|\w*timeout\w*|temporal_activity_schedule_to_start_latency\w*"
    r"|temporal_worker_task_slots\w*|temporal_num_pollers\w*)\{"
)


def scrape(name: str, url: str, needle: str) -> None:
    body = urllib.request.urlopen(url, timeout=10).read().decode()
    # The server's reporter writes a queue name with each "-" as "_", so both spellings are
    # searched.
    needles = {needle, needle.replace("-", "_")}
    lines = [
        line for line in body.splitlines() if WANT.match(line) and any(n in line for n in needles)
    ]
    print(f"== {name} {url}: {len(lines)} lines matching {needle!r}")
    for line in lines:
        print(line)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", required=True)
    parser.add_argument("--namespace", default="dsy")
    parser.add_argument("--matching", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--sdk-port", type=int, required=True)
    args = parser.parse_args()

    runtime = Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=f"127.0.0.1:{args.sdk_port}")
        )
    )
    client = await Client.connect(args.frontend, namespace=args.namespace, runtime=runtime)
    workflow_id = f"s14-probe-{uuid.uuid4().hex[:8]}"
    async with (
        Worker(client, task_queue=WORKFLOW_QUEUE, workflows=[Probe]),
        Worker(
            client,
            task_queue=POOL_QUEUE,
            activities=[quick, silent],
            max_concurrent_activities=1,
        ),
    ):
        handle = await client.start_workflow(Probe.run, id=workflow_id, task_queue=WORKFLOW_QUEUE)
        print(f"workflow {workflow_id}")
        # Two heartbeat timeouts of 3 s each, then two matching metric emission periods for the
        # backlog gauges.
        await asyncio.sleep(75)
        scrape("matching", args.matching, BACKLOG_QUEUE)
        scrape("matching", args.matching, POOL_QUEUE)
        scrape("history", args.history, "")
        scrape("sdk", f"http://127.0.0.1:{args.sdk_port}/metrics", POOL_QUEUE)
        await handle.terminate("probe finished")


if __name__ == "__main__":
    asyncio.run(main())
