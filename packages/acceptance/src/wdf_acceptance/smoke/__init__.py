"""The smoke gate as an ordered driver: one boundary row per layer, then the level 0 job rows.

The boundary rows always all run, each under its own deadline, so a dead layer fails its own row and
the rest still print; a refusal or usage exit of `cluster-verify` ends the run with that exit and
no other row. The job rows run only when every boundary row is ok.

Run by `scripts/smoke.sh` from the repository root, which holds the cluster lock and the budget
timer; the timer's SIGINT ends the run with a `FAIL budget` row and the count line.
"""

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from wdf_clusterkit import CONTROL, INFRA, Forward
from wdf_workflow.contract import naming
from wdf_workflow.harness.cluster import make_deployed_current

from .. import report
from ..clients import api, broker, temporal

CONTEXT = "kind-wdf"
VERIFY = Path("scripts/cluster-verify.sh")
# The host ports of deploy/host/ports.yaml.
DISPATCH = "http://127.0.0.1:18081"
CONTROL_API = "http://127.0.0.1:18080"
BUDGET_S = 300
VERIFY_S = 90.0
# Every boundary probe but cluster-verify, its forward included.
PROBE_S = 15.0
# How long the shared helper waits for the deployed build's pollers before it makes it current.
POLLERS_S = 30.0
JOB_ROWS = ("job-submit", "job-run", "job-result", "job-aggregate", "dead-letters")


class Gate:
    def __init__(self, preset: str):
        self.preset = preset
        self.rows: list[report.Row] = []
        # The exit of cluster-verify that ends the run, when it refused or was misused.
        self.passed_through: int | None = None
        # The dead letters counted before the job, the baseline of the last job row.
        self.dead_letters: int | None = None
        # The row running now, which the budget names when it runs out.
        self.at = "cluster-verify"

    def add(self, row: report.Row) -> None:
        self.rows.append(row)
        print(row.line, flush=True)

    def probe(self, name: str, check: Callable[[], str]) -> None:
        """Runs one boundary row: `check` returns its pass detail or raises with what it saw."""
        self.at = name
        try:
            self.add(report.ok(name, check()))
        except Exception as error:
            self.add(report.fail(name, str(error) or type(error).__name__))

    def run(self) -> None:
        self.cluster_verify()
        if self.passed_through is not None:
            return
        self.probe("api", ready_apis)
        self.probe("broker", self.count_dead_letters)
        self.probe("temporal", lambda: asyncio.run(temporal_row()))
        self.probe("placement", placement)
        self.at = "job rows"
        failed = next((row.name for row in self.rows if row.verdict != report.OK), None)
        reason = f"after {failed} failed" if failed else "job rows not written yet"
        for name in JOB_ROWS:
            self.add(report.skip(name, reason))

    def cluster_verify(self) -> None:
        """Row 1: runs cluster-verify under the held lock and copies its count line, or its FAIL
        lines, or the last check line it printed before its deadline."""
        started = time.monotonic()
        lines: list[str] = []
        # Its own session, so the deadline ends the kubectl calls it started along with it.
        process = subprocess.Popen(
            [str(VERIFY)], stdout=subprocess.PIPE, text=True, start_new_session=True
        )
        reader = threading.Thread(target=lambda: lines.extend(process.stdout), daemon=True)
        reader.start()
        try:
            code = process.wait(VERIFY_S)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        reader.join()
        if code is None:
            last = lines[-1].strip() if lines else "no check line"
            detail = f"did not finish in {VERIFY_S:.0f} s, last: {last}"
            self.add(report.fail("cluster-verify", detail))
            return
        if code in (report.EXIT_USAGE, report.EXIT_REFUSED):
            self.passed_through = code
            return
        elapsed = f"{time.monotonic() - started:.0f} s"
        counted = [line.strip() for line in lines if line.startswith("cluster-verify ")]
        if code == 0 and counted:
            detail = f"{counted[-1].removeprefix('cluster-verify ')}, {elapsed}"
            self.add(report.ok("cluster-verify", detail))
            return
        failures = [
            line.strip().removeprefix("FAIL ") for line in lines if line.startswith("FAIL ")
        ]
        self.add(report.fail("cluster-verify", "; ".join(failures) or f"exited {code}"))

    def count_dead_letters(self) -> str:
        """Row 3: the dead letters before the job, recorded as the baseline; any count passes."""
        deadline = time.monotonic() + PROBE_S
        with forwarded(INFRA, "svc/rabbitmq", 5672) as address:
            self.dead_letters = broker.dead_letters(address, timeout_s=deadline - time.monotonic())
        return f"{self.dead_letters} dead letters before the job"


@contextlib.contextmanager
def forwarded(namespace: str, resource: str, port: int) -> Iterator[str]:
    """The address of a forward to the resource, opened within the probe deadline, for the block."""
    forward = Forward(CONTEXT, namespace, resource, port)
    forward.open(timeout_s=PROBE_S)
    try:
        yield f"127.0.0.1:{forward.port}"
    finally:
        forward.close()


def ready_apis() -> str:
    """Row 2: both listeners' readiness, which covers the logins, a store head and Temporal."""
    deadline = time.monotonic() + PROBE_S
    for listener, base in (("dispatch", DISPATCH), ("control", CONTROL_API)):
        try:
            left_s = max(deadline - time.monotonic(), 0.1)
            code, body = api.request(f"{base}/readyz", timeout_s=left_s)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{listener} /readyz: {error}") from None
        if (code, body) != (200, {"status": "ready"}):
            raise RuntimeError(f"{listener} /readyz answered {code} {json.dumps(body)}")
    return "dispatch and control /readyz ready"


async def temporal_row() -> str:
    """Row 4: the namespace answers and a deployed build is current, made current when none was."""
    with forwarded(INFRA, "svc/temporal-frontend", 7233) as address:
        try:
            async with asyncio.timeout(PROBE_S + POLLERS_S):
                client = await temporal.connect(address)
                await client.workflow_service.describe_namespace(
                    DescribeNamespaceRequest(namespace=client.namespace)
                )
                build = await make_deployed_current(client, CONTEXT, POLLERS_S)
        except TimeoutError:
            raise TimeoutError(f"no answer in {PROBE_S + POLLERS_S:.0f} s") from None
    made = " (made current)" if build.made else ""
    deployed = ", ".join(build.deployed)
    return f"namespace {client.namespace}, build {build.current} current{made}, deployed {deployed}"


def placement() -> str:
    """Row 5: placement answers with every pool of the naming contract."""
    with forwarded(CONTROL, "svc/placement", 8080) as address:
        code, body = api.request(f"http://{address}/pools", timeout_s=PROBE_S)
    want = set(naming().pools)
    got = set(body.get("pools", {})) if code == 200 else set()
    if code != 200 or got != want:
        raise RuntimeError(f"/pools answered {code} with pools {sorted(got)}, want {sorted(want)}")
    return f"{len(got)} pools"


def main() -> int:
    started = time.monotonic()
    gate = Gate(os.environ.get("WDF_PRESET", "ci"))
    try:
        gate.run()
    except KeyboardInterrupt:
        gate.add(report.fail("budget", f"{BUDGET_S} s ran out in {gate.at}"))
    if gate.passed_through is not None:
        return report.exit_code(gate.rows, gate.passed_through)
    print(report.count_line(gate.preset, gate.rows, time.monotonic() - started), flush=True)
    return report.exit_code(gate.rows)
