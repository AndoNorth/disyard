"""The smoke gate as an ordered driver: one boundary row per layer, then the level 0 job rows.

The boundary rows always all run, each under its own deadline, so a dead layer fails its own row and
the rest still print; a refusal or usage exit of `cluster-verify` ends the run with that exit and
no other row. The job rows run only when every boundary row is ok.

Run by `scripts/smoke.sh` from the repository root, which holds the cluster lock and the budget
timer; the timer's SIGINT ends the run with a `FAIL budget` row and the count line. When a job row
fails, or the budget runs out, after the job was submitted, the run cancels its batch before the
count line, so a late job does not hold the pools into the next run.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from wft_clusterkit import CONTROL, INFRA, Forward, until
from wft_workflow.contract import naming
from wft_workflow.harness import ECHO, echo_params
from wft_workflow.harness.cluster import TERMINAL, make_deployed_current
from wft_workflow.ready import DONE
from wft_workloads import bundle

from .. import report
from ..clients import api, broker, store, temporal
from . import results, telemetry

CONTEXT = "kind-wft"
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
# Kept back from the job's wait for the result rows, the dead-letter row and the cancel, by the
# observability switch: with it on, the telemetry row's 60 s deadline comes out of the job's wait.
KEEP_S = {"off": 30.0, "on": 90.0}
# Short enough that a cancel after the budget ran out ends before the timer's kill, 10 s later.
CANCEL_S = 5.0
# The first tenant of the ci preset.
TENANT = "acme"


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
        self.started = time.monotonic()
        self.wall_started = time.time()
        # When each row ended, which the results push turns into the seconds of each row.
        self.row_ends: list[float] = []
        # The observability switch of the running cluster, and whether the job's compile ran on a
        # compile worker and not from the cache.
        self.obs = telemetry.switch()
        self.compiled = True
        # The batch key and job id once the job was accepted, and its last status once it ended.
        self.batch: str | None = None
        self.job_id: str | None = None
        self.ended: dict = {}

    def add(self, row: report.Row) -> None:
        self.rows.append(row)
        self.row_ends.append(time.monotonic())
        print(row.line, flush=True)

    def probe(self, name: str, check: Callable[[], str]) -> None:
        """Runs one row: `check` returns its pass detail or raises with what it saw."""
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
        failed = next((row.name for row in self.rows if row.verdict != report.OK), None)
        client = api.Api(DISPATCH, CONTROL_API, TENANT, PROBE_S)
        rows = (
            ("job-submit", lambda: self.submit(client)),
            ("job-run", lambda: self.run_job(client)),
            ("job-result", self.result),
            ("job-aggregate", self.aggregate),
        )
        for name, check in rows:
            if failed:
                self.add(report.skip(name, f"after {failed} failed"))
                continue
            self.probe(name, check)
            if self.rows[-1].verdict != report.OK:
                failed = name
        # Whatever the job did, a job that was submitted is checked for dead letters.
        if self.batch is None:
            self.add(report.skip("dead-letters", f"after {failed} failed"))
        else:
            self.probe("dead-letters", self.no_rise)
        self.telemetry_row()

    def cancel(self) -> None:
        """Cancels the batch when a row failed after the job was submitted; a cancel that fails is
        reported and does not change the verdict, which the failed row already holds."""
        if self.batch is None or all(row.verdict == report.OK for row in self.rows):
            return
        try:
            summary = api.Api(DISPATCH, CONTROL_API, TENANT, CANCEL_S).cancel_batch(self.batch)
            print(f"cleanup: cancelled batch {self.batch}: {summary}", file=sys.stderr, flush=True)
        except Exception as error:
            print(f"cleanup: the cancel of batch {self.batch} failed: {error}", file=sys.stderr)

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

    def submit(self, client: api.Api) -> str:
        """Row 6: uploads the workload library bundle and submits one echo job in a fresh batch."""
        with tempfile.TemporaryDirectory(prefix="wft-smoke-") as work:
            source = client.upload_source(bundle.write(Path(work)).read_bytes())
        key = api.batch_key()
        self.job_id = client.submit_one(api.template(source, echo_params()), key)
        self.batch = key
        return f"accepted {self.job_id}"

    def run_job(self, client: api.Api) -> str:
        """Row 7: the job runs and ends succeeded, and a sample taken while it ran shows its compile
        done. The detail is null once the job ends, so the compile is read from the running
        samples."""
        timeout_s = BUDGET_S - KEEP_S[self.obs] - (time.monotonic() - self.started)
        builds: list[dict] = []
        last = {"status": "unread"}

        def ended() -> dict | None:
            last.update(client.status(self.job_id))
            if last["status"] == "running":
                tasks = (last["detail"] or {}).get("tasks", [])
                builds.extend(t for t in tasks if t["id"] == "build" and t["state"] in DONE)
            return dict(last) if last["status"] in TERMINAL else None

        try:
            self.ended = asyncio.run(until(ended, timeout_s, "end"))
        except TimeoutError:
            raise TimeoutError(f"no end in {timeout_s:.0f} s, last {last['status']}") from None
        if self.ended["status"] != "succeeded":
            raise RuntimeError(f"ended {self.ended['status']}")
        if not builds:
            raise RuntimeError("ended succeeded, but no running sample showed build done")
        build = builds[-1]
        self.compiled = build["state"] != "skipped"
        how = "ran" if self.compiled else "hit the cache"
        return f"succeeded, build {how} (attempt {build['task_attempt']})"

    def result(self) -> str:
        """Row 8: the simulate result holds the known answer of the level 0 echo vector."""
        data = fetched(self.ended, "sim")
        with tempfile.NamedTemporaryFile(prefix="wft-smoke-", suffix=".xml") as file:
            file.write(data)
            file.flush()
            checked = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "wft_kernel.check",
                    "results",
                    "--input",
                    str(ECHO / "input.json"),
                    "--results",
                    file.name,
                ],
                capture_output=True,
                text=True,
                timeout=PROBE_S,
            )
        if checked.returncode != 0:
            raise RuntimeError(
                f"wft_kernel.check exited {checked.returncode}: {checked.stderr.strip()}"
            )
        return f"echo result matches, {checked.stdout.strip()}"

    def aggregate(self) -> str:
        """Row 9: the aggregate's summary is stored as its reference says, by digest only."""
        fetched(self.ended, "agg")
        return "summary present and matching"

    def telemetry_row(self) -> None:
        """Row 11: with the switch on, the stack holds the run's telemetry; with it off, nothing is
        checked. It runs only after rows 1 and 6 were ok."""
        if self.obs == "off":
            self.add(report.ok(telemetry.ROW, "observability off, not checked"))
            return
        failed = next(
            (
                r.name
                for r in self.rows
                if r.name in ("cluster-verify", "job-submit") and r.verdict != report.OK
            ),
            None,
        )
        if failed or self.job_id is None:
            self.add(report.skip(telemetry.ROW, f"after {failed or 'job-submit'} failed"))
            return
        self.probe(
            telemetry.ROW,
            lambda: telemetry.check(forwarded, self.job_id, self.compiled, self.wall_started),
        )

    def no_rise(self) -> str:
        """Row 10: the job left no dead letter beyond the count before it."""
        if self.dead_letters is None:
            raise RuntimeError("no count before the job")
        before = self.dead_letters
        self.count_dead_letters()
        if self.dead_letters > before:
            raise RuntimeError(f"{self.dead_letters}, above {before} before the job")
        return f"{self.dead_letters}, no rise"


@contextlib.contextmanager
def forwarded(
    namespace: str, resource: str, port: int, timeout_s: float | None = None
) -> Iterator[str]:
    """The address of a forward to the resource, opened within the deadline (the probe deadline
    unless given), for the block."""
    forward = Forward(CONTEXT, namespace, resource, port)
    forward.open(timeout_s=PROBE_S if timeout_s is None else timeout_s)
    try:
        yield f"127.0.0.1:{forward.port}"
    finally:
        forward.close()


def fetched(status: dict, task: str) -> bytes:
    """The bytes of the task's last result in the job's status, read as the export identity, whose
    sha256 must match its reference."""
    references = [r for r in status.get("results", []) if r["producer"]["task"] == task]
    if not references:
        raise RuntimeError(f"no result of {task} in the status")
    reference = references[-1]
    with forwarded(INFRA, "svc/objectstore", 8333) as address:
        data = store.fetch(reference["uri"], "export", f"http://{address}")
    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 != reference["sha256"]:
        raise RuntimeError(
            f"{reference['uri']} has sha256 {sha256}, its reference {reference['sha256']}"
        )
    return data


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
    gate = Gate(os.environ.get("WFT_PRESET", "ci"))
    try:
        gate.run()
    except KeyboardInterrupt:
        gate.add(report.fail("budget", f"{BUDGET_S} s ran out in {gate.at}"))
    if gate.passed_through is not None:
        return report.exit_code(gate.rows, gate.passed_through)
    gate.cancel()
    print(report.count_line(gate.preset, gate.rows, time.monotonic() - gate.started), flush=True)
    results.push(gate.obs, forwarded, gate.rows, gate.row_ends, gate.started, gate.batch)
    return report.exit_code(gate.rows)
