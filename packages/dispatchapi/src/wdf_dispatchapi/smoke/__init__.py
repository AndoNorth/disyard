"""The smoke driver: scenarios of the deployed dispatch and control API, driven from the host
through its two NodePorts, and through port-forwards bound to 127.0.0.1 where a scenario counts
what the API does not show, with the credentials of the state directory.

`upload` uploads a fresh source bundle twice. `submit` submits a batch, sends it again and calls a
tenant outside the profile. `cancel-before-start` holds admission at no replica while it submits
and cancels a job. `run` runs jobs to their end. `pause-resume` pauses a job while its simulate
task runs and resumes it, and `cancel-running` cancels one there. `restart` deletes the dispatch
pod while a batch is being submitted, sends the batch again, counts what reached the rows, the
event history, Temporal and the dead-letter queue, and then cancels the batch.

Every scenario but `upload` first makes the deployed workflow worker build current when no build
is, as the workflow harness does.

Every job runs on the deployed pool workers over the workload library bundle: the level 0 `echo`
params, or the level 1 `cpu_burn` workload, long enough to still run when a pause or a cancel
arrives, whose simulate task sets `checkpoint`, so a pause withdraws it.
"""

import asyncio
import hashlib
import http.client
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import wdf_keys
from temporalio.client import Client
from wdf_broker.tenancy import load
from wdf_jobstate import status_vocabulary
from wdf_workflow import versioning
from wdf_workflow.contract import naming
from wdf_workflow.harness import REAL_ENTRY, Refused, echo_params, source_bundle
from wdf_workflow.harness.cluster import WORKER_LABEL, Forward, kubectl, state_dir

from ..gateway import TemporalGateway

SCENARIOS = (
    "upload",
    "submit",
    "cancel-before-start",
    "run",
    "pause-resume",
    "cancel-running",
    "restart",
)
PROFILE = "ci"
INFRA = "wdf-infra"
CONTROL = "wdf-control"
DEPLOYMENT = "dispatch"
ADMISSION = "admission"
TERMINAL = status_vocabulary().terminal
# A level 1 workload of about 35 s in the 250m simulate slot, at about 170 units a second (an
# estimate from the calibrated host rate), so a pause or a cancel lands while it runs.
BURN = {"workload": "cpu_burn", "args": {"units": 6000, "heartbeat_every": 100}, "seed": 7}
PAUSE_RESUME = ("pausing", "paused", "resume-pending", "running", "succeeded")
CANCEL_RUNNING = ("cancelling", "cancelled")
EVENTS = {
    "accepted": "dev.wdf.job.accepted.v1",
    "published": "dev.wdf.job.published.v1",
    "admitted": "dev.wdf.job.admitted.v1",
}
# How long a job on the real pools has to reach a state, a compile of the workload library first.
JOB_S = 1800.0
# How long a pod, a rollout or a forward has to come up.
UP_S = 300.0
# The bound on how long the rows of a resent batch have to carry their receipts. A full intake
# queue holds back the publishes beyond its max length until admission drains it, so the time
# depends on the admission rate, and the bound only catches a backlog that never clears.
UNPUBLISHED_S = 600.0
# How often the backlog is counted while it clears, and how often a count is printed.
UNPUBLISHED_POLL_S = 1.0
UNPUBLISHED_PRINT_S = 10.0
POLL_S = 0.5
# How long the restart waits before it deletes the pod, and again before the rest of the call, so
# the pod's stop has begun when the entries after the first half arrive (an estimate).
RESTART_AFTER_S = 3.0
CALL_TIMEOUT_S = 120
# The workflow ids that one visibility query names.
LIST_CHUNK = 100


class Failed(Exception):
    """A call the scenario relies on answered something else."""


def request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, dict]:
    """The status and JSON body of one call."""
    call = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(call, timeout=CALL_TIMEOUT_S) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def stream(url: str, lines: Iterable[bytes]) -> list[dict]:
    """The answer lines of an NDJSON call, read while a thread sends its body, as the contract asks
    of a client. A call whose connection the server drops, as a deleted pod does, returns the lines
    it answered, and a call refused before its answer raises Failed."""
    parts = urlsplit(url)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=CALL_TIMEOUT_S)

    def send() -> None:
        try:
            sock.sendall(
                f"POST {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                "Content-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
            )
            for line in lines:
                sock.sendall(b"%x\r\n%b\r\n" % (len(line), line))
            sock.sendall(b"0\r\n\r\n")
        except OSError:
            # The server ended the call and closed, so the rest of the body has nowhere to go.
            pass

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    answers = []
    try:
        response = http.client.HTTPResponse(sock)
        response.begin()
        if response.status != 200:
            raise Failed(f"{url} answered {response.status}: {response.read().decode()}")
        while text := response.readline():
            answers.append(json.loads(text))
    except (OSError, http.client.HTTPException):
        # The connection went with the pod.
        pass
    finally:
        sender.join(CALL_TIMEOUT_S)
        sock.close()
    return answers


def template(source: dict, simulate: dict, checkpoint: bool = False) -> dict:
    """A compile of the workload library bundle, one simulate task running `simulate`, and an
    aggregate over its result."""
    return {
        "schema": "urn:wdf:schema:job:1.0",
        "parameters": {},
        "tasks": {
            "build": {"type": "compile", "params": {"source": source, "entry": REAL_ENTRY}},
            "sim": {
                "type": "simulate",
                "inputs": [{"task": "build", "output": "component"}],
                "cpu": 0.25,
                "memory_mib": 384,
                "run_timeout_s": 600,
                "retries": 1,
                "checkpoint": checkpoint,
                "params": simulate,
            },
            "agg": {
                "type": "aggregate",
                "inputs": [{"task": "sim", "output": "result"}],
                "cpu": 1,
                "memory_mib": 512,
                "run_timeout_s": 600,
                "params": {"aggregation": "digest_list"},
            },
        },
    }


def batch(batch_key: str, document: dict, count: int) -> list[bytes]:
    """The lines of a batch submit body of `count` jobs of one template."""
    header = {"batch_key": batch_key, "template": document}
    entries = ({"index": n, "seed": n, "parameters": {}} for n in range(count))
    return [json.dumps(line).encode() + b"\n" for line in (header, *entries)]


def state(answer: dict) -> str:
    """What a status read or a control answer shows of a job: its view, else its status."""
    return answer["view"] or answer["status"]


@dataclass
class Seen:
    """The states a job showed, each once in a run of the same state."""

    states: list[str] = field(default_factory=list)

    def add(self, answer: dict) -> str:
        shown = state(answer)
        if not self.states or self.states[-1] != shown:
            self.states.append(shown)
        return shown


class Smoke:
    """One scenario against the API at `dispatch` and `control`, and the cluster of `context`."""

    def __init__(self, dispatch: str, control: str, context: str, jobs: int):
        self.dispatch, self.control, self.context, self.jobs = dispatch, control, context, jobs
        self.tenant = next(iter(load(PROFILE).tenants))
        self.passed = True

    def expect(self, name: str, value, wanted) -> None:
        """Prints `name: value` and fails the scenario unless `wanted` holds of the value, or
        equals it."""
        print(f"{name}: {value}", flush=True)
        if not (wanted(value) if callable(wanted) else value == wanted):
            self.passed = False

    def tenant_url(self, base: str, path: str, tenant: str | None = None) -> str:
        return f"{base}/v1/tenants/{tenant or self.tenant}/{path}"

    # The API.

    def upload(self, kind: str, data: bytes) -> tuple[int, dict]:
        sha256 = hashlib.sha256(data).hexdigest()
        return request(self.tenant_url(self.dispatch, f"objects/{kind}/{sha256}"), "PUT", data)

    def workloads(self) -> dict:
        """The reference of the workload library bundle, uploaded unless it is already stored."""
        # The workload library is a package of the workspace's dev group, not of the service image.
        from wdf_workloads import bundle as workloads

        with tempfile.TemporaryDirectory(prefix="wdf-smoke-") as work:
            data = workloads.write(Path(work)).read_bytes()
        code, answer = self.upload("source", data)
        if code not in (200, 201):
            raise Failed(f"the upload of the workload library bundle answered {code}: {answer}")
        return answer["reference"]

    def submit(self, lines: Iterable[bytes]) -> list[dict]:
        return stream(self.tenant_url(self.dispatch, "batches"), lines)

    def submit_one(self, document: dict) -> str:
        """Submits one job of the template under a fresh batch key, and returns its id."""
        answers = self.submit(batch(f"smoke-{uuid.uuid4().hex[:12]}", document, 1))
        if answers[0].get("outcome") != "accepted":
            raise Failed(f"the job was not accepted: {answers[0]}")
        return answers[0]["job_id"]

    def status(self, job_id: str) -> dict:
        code, answer = request(self.tenant_url(self.control, f"jobs/{job_id}"))
        if code != 200:
            raise Failed(f"the status read of {job_id} answered {code}: {answer}")
        return answer

    def act(self, job_id: str, action: str) -> dict:
        url = self.tenant_url(self.control, f"jobs/{job_id}/{action}")
        code, answer = request(url, "POST", json.dumps({"reason": "smoke"}).encode())
        if code != 200:
            raise Failed(f"the {action} of {job_id} answered {code}: {answer}")
        return answer

    def until(self, job_id: str, done: Callable[[dict], bool], what: str, seen: Seen | None = None):
        """Polls the job's status until `done` holds of it, recording each state it shows."""
        deadline = time.monotonic() + JOB_S
        while True:
            answer = self.status(job_id)
            if seen is not None:
                seen.add(answer)
            if done(answer):
                return answer
            if time.monotonic() > deadline:
                raise Failed(f"no {what} of {job_id} in {JOB_S:.0f} s, last {state(answer)}")
            time.sleep(POLL_S)

    def ended(self, job_id: str, seen: Seen | None = None) -> dict:
        return self.until(job_id, lambda a: a["status"] in TERMINAL, "end", seen)

    def simulating(self, job_id: str) -> None:
        """Waits until the job's simulate task runs."""

        def running(answer: dict) -> bool:
            if answer["status"] in TERMINAL:
                raise Failed(f"{job_id} ended {answer['status']} before its simulate task ran")
            tasks = (answer["detail"] or {}).get("tasks", [])
            return answer["status"] == "running" and any(
                t["id"] == "sim" and t["state"] == "running" for t in tasks
            )

        self.until(job_id, running, "running simulate task")

    # The cluster.

    def kubectl(self, *args: str) -> str:
        return kubectl(self.context, f"--namespace={CONTROL}", *args)

    def pods(self, name: str) -> list[dict]:
        listed = self.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={name}", "-o", "json")
        return json.loads(listed)["items"]

    @contextmanager
    def forward(self, namespace: str, resource: str, port: int) -> Iterator[str]:
        """The address of a port-forward to the resource, open for the block."""
        forward = Forward(self.context, namespace, resource, port)
        forward.open()
        try:
            yield f"127.0.0.1:{forward.port}"
        finally:
            forward.close()

    @contextmanager
    def admission_held(self) -> Iterator[None]:
        """Admission at no replica for the block, so no job starts, and back at its replicas
        after, also when the block fails."""
        deployment = f"deployment/{ADMISSION}"
        replicas = self.kubectl("get", deployment, "-o", "jsonpath={.spec.replicas}") or "1"
        self.kubectl("scale", deployment, "--replicas=0")
        try:
            deadline = time.monotonic() + UP_S
            while self.pods(ADMISSION):
                if time.monotonic() > deadline:
                    raise Failed(f"admission still has a pod {UP_S:.0f} s after its scale to 0")
                time.sleep(1)
            yield
        finally:
            self.kubectl("scale", deployment, f"--replicas={replicas}")
            self.kubectl("rollout", "status", deployment, f"--timeout={UP_S:.0f}s")

    def restart_pod(self) -> str:
        """Deletes the dispatch pod with its grace period, without waiting, and returns its name."""
        [pod] = [p for p in self.pods(DEPLOYMENT) if not p["metadata"].get("deletionTimestamp")]
        name = pod["metadata"]["name"]
        self.kubectl("delete", "pod", name, "--wait=false")
        return name

    def ready_again(self, gone: str) -> None:
        """Waits for a ready dispatch pod other than `gone` and for both listeners to answer."""
        deadline = time.monotonic() + UP_S
        while True:
            fresh = [
                p
                for p in self.pods(DEPLOYMENT)
                if p["metadata"]["name"] != gone
                and any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in p.get("status", {}).get("conditions", [])
                )
            ]
            if fresh and all(_ready(base) for base in (self.dispatch, self.control)):
                return
            if time.monotonic() > deadline:
                raise Failed(f"no ready dispatch pod within {UP_S:.0f} s after {gone}")
            time.sleep(1)

    # The scenarios.

    def run_upload(self) -> None:
        data = source_bundle(f"smoke-{uuid.uuid4().hex}")
        answers = [self.upload("source", data) for _ in range(2)]
        codes = Counter(code for code, _ in answers)
        self.expect("created", codes[201], 1)
        self.expect("already_present", codes[200], 1)

    def run_submit(self) -> None:
        document = template(self.workloads(), echo_params())
        lines = batch(f"smoke-{uuid.uuid4().hex[:12]}", document, self.jobs)
        first = Counter(a.get("outcome") for a in self.submit(lines))
        again = Counter(a.get("outcome") for a in self.submit(lines))
        self.expect("accepted", first["accepted"], self.jobs)
        self.expect("duplicate_on_resend", again["duplicate"], self.jobs)
        code, _ = request(self.tenant_url(self.dispatch, "batches", "nobody"), "POST", lines[0])
        self.expect("unknown_tenant", code, 404)

    def run_cancel_before_start(self) -> None:
        document = template(self.workloads(), echo_params())
        with self.admission_held():
            job_id = self.submit_one(document)
            answer = self.act(job_id, "cancel")
            self.expect("row_only", int(answer["outcome"] == "applied"), 1)
            self.expect("status", self.status(job_id)["status"], "cancelled")
            with self.forward(INFRA, "svc/temporal-frontend", 7233) as temporal:
                with TemporalGateway(temporal) as gateway:
                    workflow = "started" if gateway.exists(job_id) else "none"
            self.expect("workflow", workflow, "none")

    def run_run(self) -> None:
        document = template(self.workloads(), echo_params())
        answers = self.submit(batch(f"smoke-{uuid.uuid4().hex[:12]}", document, self.jobs))
        ended = [self.ended(a["job_id"]) for a in answers if a.get("outcome") == "accepted"]
        statuses = sorted({a["status"] for a in ended}) or ["none"]
        self.expect("status", ",".join(statuses), "succeeded")
        fewest = min((len(a["results"]) for a in ended), default=0)
        self.expect("results", fewest, lambda n: n >= 1)

    def run_pause_resume(self) -> None:
        job_id = self.submit_one(template(self.workloads(), BURN, checkpoint=True))
        self.simulating(job_id)
        seen = Seen()
        seen.add(self.act(job_id, "pause"))
        self.until(job_id, lambda a: a["status"] == "paused", "pause", seen)
        seen.add(self.act(job_id, "resume"))
        self.ended(job_id, seen)
        self.expect("seen", ",".join(seen.states), ",".join(PAUSE_RESUME))

    def run_cancel_running(self) -> None:
        job_id = self.submit_one(template(self.workloads(), BURN, checkpoint=True))
        self.simulating(job_id)
        seen = Seen()
        seen.add(self.act(job_id, "cancel"))
        self.ended(job_id, seen)
        self.expect("seen", ",".join(seen.states), ",".join(CANCEL_RUNNING))

    def run_restart(self) -> None:
        batch_key = f"smoke-restart-{uuid.uuid4().hex[:12]}"
        lines = batch(batch_key, template(self.workloads(), echo_params()), self.jobs)
        # The header and the first half of the entries, then the rest once the pod is stopping, so
        # the stop lands in the middle of the call.
        half = 1 + self.jobs // 2
        stopping = threading.Event()

        def body() -> Iterator[bytes]:
            yield from lines[:half]
            stopping.wait()
            yield from lines[half:]

        gone: list[str] = []

        def restart() -> None:
            try:
                time.sleep(RESTART_AFTER_S)
                gone.append(self.restart_pod())
                time.sleep(RESTART_AFTER_S)
            finally:
                stopping.set()

        deleter = threading.Thread(target=restart, daemon=True)
        deleter.start()
        first = self.submit(body())
        deleter.join()
        if not gone:
            raise Failed("the dispatch pod was not deleted")
        answered = sum("outcome" in a for a in first)
        print(f"answered_before_restart: {answered}", flush=True)
        print(f"ended_with: {first[-1] if first else 'nothing'}", flush=True)
        self.ready_again(gone[0])
        # The last line is the summary, with no outcome.
        again = Counter(a["outcome"] for a in self.submit(lines) if "outcome" in a)
        print(f"resend: {' '.join(f'{k} {n}' for k, n in sorted(again.items()))}", flush=True)
        job_ids = [wdf_keys.job_id(self.tenant, f"{batch_key}/{n}") for n in range(self.jobs)]
        try:
            self.count(batch_key, job_ids)
        finally:
            self.cancel_batch(batch_key)

    def count(self, batch_key: str, job_ids: list[str]) -> None:
        """What the restart left in the rows, the event history, Temporal and the dead-letter
        queue."""
        password = (state_dir() / "appdb.credential").read_text().strip()
        batch_id = wdf_keys.batch_id(self.tenant, batch_key)
        with (
            self.forward(INFRA, "pod/appdb-0", 5432) as appdb,
            psycopg.connect(
                f"postgresql://postgres@{appdb}/postgres", password=password, autocommit=True
            ) as db,
        ):
            unpublished, rises = self.clear_backlog(db, batch_id)
            rows = set(
                r
                for (r,) in db.execute(
                    "SELECT job_id::text FROM accepted_job WHERE batch_id = %s", (batch_id,)
                )
            )
            events = Counter(
                {
                    (job, kind): n
                    for job, kind, n in db.execute(
                        "SELECT job_id::text, type, count(*) FROM event"
                        " WHERE job_id = ANY(%s::uuid[]) AND type = ANY(%s) GROUP BY 1, 2",
                        (job_ids, list(EVENTS.values())),
                    )
                }
            )
        with self.forward(INFRA, "svc/temporal-frontend", 7233) as temporal:
            executions = asyncio.run(_executions(temporal, job_ids))
        accepted = EVENTS["accepted"]
        lost = [j for j in job_ids if j not in rows or not events[j, accepted]]
        doubled = [
            j
            for j in job_ids
            if executions[j] > 1 or any(events[j, kind] > 1 for kind in EVENTS.values())
        ]
        self.expect("rows", len(rows), self.jobs)
        self.expect("accepted_events", sum(events[j, accepted] for j in job_ids), self.jobs)
        self.expect("lost", len(lost), 0)
        self.expect("doubled", len(doubled), 0)
        self.expect("dlq", self.dead_letters(), 0)
        self.expect("unpublished_rises", rises, 0)
        self.expect("unpublished", unpublished, 0)

    def clear_backlog(self, db: psycopg.Connection, batch_id: str) -> tuple[int, int]:
        """Waits for every row of the batch to carry its receipt, printing the count as it falls,
        and returns the last count and how many times it rose between two counts."""
        start = time.monotonic()
        unpublished, rises, printed = _unpublished(db, batch_id), 0, None
        while True:
            elapsed = time.monotonic() - start
            if printed is None or elapsed - printed >= UNPUBLISHED_PRINT_S or not unpublished:
                print(f"unpublished_at: {elapsed:.0f}s {unpublished}", flush=True)
                printed = elapsed
            if not unpublished or elapsed >= UNPUBLISHED_S:
                return unpublished, rises
            time.sleep(UNPUBLISHED_POLL_S)
            last, unpublished = unpublished, _unpublished(db, batch_id)
            rises += unpublished > last

    def dead_letters(self) -> int:
        """The messages in the dead-letter queue, as the operator's tool lists them."""
        with self.forward(INFRA, "svc/rabbitmq", 5672) as broker:
            listed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "wdf_broker.dlq",
                    "list",
                    "--address",
                    broker,
                    "--credentials-dir",
                    str(state_dir() / "rabbitmq"),
                ],
                capture_output=True,
                text=True,
            )
        if listed.returncode != 0:
            raise Failed(f"wdf_broker.dlq list exited {listed.returncode}: {listed.stderr.strip()}")
        counts = [line.split() for line in listed.stdout.splitlines() if line.startswith("count ")]
        return sum(int(n) for _, _, n in counts)

    def cancel_batch(self, batch_key: str) -> None:
        """Cancels every job of the batch, so its jobs leave the pools once the counts are taken."""
        url = self.tenant_url(self.control, f"batches/{batch_key}/cancel")
        lines = [json.dumps({"reason": "smoke"}).encode()]
        answers = stream(url, lines)
        summary = answers[-1].get("summary") if answers else None
        print(f"cleanup: cancelled batch {batch_key}: {summary}", file=sys.stderr, flush=True)

    def first_current(self) -> None:
        """Makes the deployed workflow worker build current when no build is, as the harness does,
        since a deployment with no current build gives a new job to no worker."""
        with self.forward(INFRA, "svc/temporal-frontend", 7233) as temporal:
            build = asyncio.run(_first_current(temporal, self.context))
        if build is not None:
            print(f"smoke: made build {build} current", file=sys.stderr, flush=True)

    def run(self, scenario: str) -> bool:
        if scenario != "upload":
            self.first_current()
        getattr(self, "run_" + scenario.replace("-", "_"))()
        return self.passed


def _ready(base: str) -> bool:
    try:
        return request(f"{base}/readyz") == (200, {"status": "ready"})
    except (OSError, ValueError):
        return False


def _unpublished(db: psycopg.Connection, batch_id: str) -> int:
    return db.execute(
        "SELECT count(*) FROM accepted_job"
        " WHERE batch_id = %s AND status = 'accepted' AND published_at IS NULL",
        (batch_id,),
    ).fetchone()[0]


async def _executions(address: str, job_ids: list[str]) -> Counter:
    """The workflow executions of each job, open or closed."""
    client = await Client.connect(address, namespace=naming().namespace)
    ids = {wdf_keys.workflow_id(j): j for j in job_ids}
    executions: Counter = Counter()
    names = list(ids)
    for start in range(0, len(names), LIST_CHUNK):
        chunk = ", ".join(f"'{name}'" for name in names[start : start + LIST_CHUNK])
        async for execution in client.list_workflows(f"WorkflowId IN ({chunk})"):
            executions[ids[execution.id]] += 1
    return executions


async def _first_current(address: str, context: str) -> str | None:
    """The build made current, or None when one already was."""
    client = await Client.connect(address, namespace=naming().namespace)
    if await versioning.current(client) is not None:
        return None
    builds = kubectl(
        context,
        f"--namespace={CONTROL}",
        "get",
        "deployments",
        "-l",
        WORKER_LABEL,
        "-o",
        "jsonpath={.items[*].metadata.labels.wdf\\.dev/build-id}",
    ).split()
    if len(builds) != 1:
        raise Failed(f"no current build, and the deployed builds are {builds}")
    await versioning.set_current(client, builds[0])
    return builds[0]


def reachable(context: str) -> None:
    """Refuses a context whose cluster does not answer or has no dispatch deployment."""
    try:
        kubectl(
            context,
            "--request-timeout=10s",
            f"--namespace={CONTROL}",
            "get",
            f"deploy/{DEPLOYMENT}",
        )
    except FileNotFoundError:
        raise Refused("kubectl is not on PATH") from None
    except subprocess.CalledProcessError as error:
        raise Refused(
            f"no deployment/{DEPLOYMENT} in {CONTROL} on {context}: {error.stderr.strip()}"
        ) from None
