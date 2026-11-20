"""The admission driver: journeys of jobs through the deployed admission, driven from the host
through its own port-forwards bound to 127.0.0.1, with the credentials of the state directory.

The driver stands in for the dispatch and control API: it seeds each job's row and accepted event
in one transaction, puts its manifest and source in the store, publishes its intake message as the
broker user `dispatch` and marks its receipt, and makes the control moves through the job rows and
the Temporal client. The jobs run on FakePools on the host, behind a fresh queue prefix that the
driver sets on the deployment for the run, so no task reaches the deployed pool workers.

`smoke` publishes the jobs and lets them finish. `control` pauses a quarter of them before they
start and resumes them once their messages were skipped, leaves a quarter in `admitted` with no
workflow and no message, and pauses a quarter while they run and asks for their resume, which
admission grants. `deadletter` publishes a malformed message and one of an unsupported major,
each naming a seeded job. `restart` deletes the admission pod twice while jobs are admitted, once
with its grace period and once with none.

The driver counts what admission did from the job rows, the dead-letter queue, read as the broker
user `operator`, and the admission metrics, scraped through a forward to the deployment.
"""

import asyncio
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio.client import WorkflowQueryFailedError
from temporalio.service import RPCError
from wft_broker.client import Credentials, connect
from wft_broker.contract import topology
from wft_broker.dlq import DLQ, PASS_LIMIT, PASS_TIMEOUT_S, Letter
from wft_broker.models import Outcome
from wft_broker.tenancy import load
from wft_clusterkit import CONTROL, INFRA, UP_S, Forward, Refused, kubectl, oom_killed, ready, until
from wft_events import Event, build_event, to_bytes
from wft_eventstore import insert_events
from wft_jobstate import (
    JobRow,
    Moved,
    insert_accepted,
    mark_published,
    request_action,
    status_vocabulary,
    transition,
)
from wft_keys import workflow_id
from wft_plan import canonical_bytes, manifest_reference
from wft_temporal import new_traceparent
from wft_workflow.contract import naming
from wft_workflow.harness import (
    DEADLINE_S,
    HOLD_S,
    PARAMS,
    Job,
    content,
    smoke_plan,
    source_bundle,
)
from wft_workflow.models import ControlSignal, StatusQuery
from wft_workflow.testing import Succeed

from ..service import GLOBAL_CAP_ENV, PREFIX_ENV
from ..service.metrics import READMITTED

JOURNEYS = ("smoke", "control", "deadletter", "restart")
PROFILE = "ci"
DEPLOYMENT = "admission"
POD_LABEL = f"app.kubernetes.io/name={DEPLOYMENT}"
METRICS_PORT = 9464
BROKER_PORT = 5672
# The broker module's password helper, run from the root of the tree.
HELPER = Path("deploy/rabbitmq/config.py")
DISPATCH = "dispatch"
OPERATOR = "operator"
# How long the two dead letters have to reach the dead-letter queue, which the first delivery of
# each message sends there.
DEAD_LETTER_S = 120.0
# How often the live count and the metrics are sampled.
SAMPLE_S = 0.5
DEADLETTER_JOBS = 2
DEAD_LETTERED = "dev.wft.job.dead.lettered.v1"
TERMINAL = status_vocabulary().terminal
SAMPLE = re.compile(r"^(wft_admission_\w+)(?:\{([^}]*)\})? (\S+)$")
LABEL = re.compile(r'(\w+)="([^"]*)"')


def password(user: str) -> str:
    """The password of a broker user, from the broker module's helper, which generates it into the
    state directory on first use."""
    if not HELPER.is_file():
        raise Refused(f"the broker passwords come from {HELPER}; run it from the tree's root")
    done = subprocess.run(
        [sys.executable, str(HELPER), "password", user], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def env_args(prefix: str, global_cap: int | None) -> list[str]:
    """The `kubectl set env` arguments that put the run's prefix and cap on the deployment, and
    remove a cap that an earlier run left when this one sets none."""
    cap = f"{GLOBAL_CAP_ENV}={global_cap}" if global_cap else f"{GLOBAL_CAP_ENV}-"
    return [f"{PREFIX_ENV}={prefix}", cap]


CLEARED = [f"{PREFIX_ENV}-", f"{GLOBAL_CAP_ENV}-"]


def set_env(context: str, args: list[str]) -> None:
    """Sets the deployment's environment and waits for its rollout, which `Recreate` makes a full
    stop of the old pod before the new one starts."""
    deployment = f"deployment/{DEPLOYMENT}"
    kubectl(context, f"--namespace={CONTROL}", "set", "env", deployment, *args)
    kubectl(
        context, f"--namespace={CONTROL}", "rollout", "status", deployment, f"--timeout={UP_S:.0f}s"
    )


def split(jobs: int) -> tuple[int, int, int]:
    """The jobs of `control` paused before they start, left stuck at `admitted`, and paused while
    they run, a quarter each, the rest running plain."""
    quarter = jobs // 4
    return quarter, quarter, quarter


def parse_metrics(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Each admission sample of a Prometheus text exposition, keyed by its name and labels."""
    samples = {}
    for line in text.splitlines():
        if match := SAMPLE.match(line):
            labels = tuple(sorted(LABEL.findall(match[2] or "")))
            samples[match[1], labels] = float(match[3])
    return samples


class Tally:
    """What each counter gained since the first sample, over restarts of the process that serves
    it: a value below the last one means the counter started again from zero.

    ponytail: what a pod counted after its last scrape and before it was deleted is lost, which only
    `restart` meets; scrape the pod's endpoint before each delete if that count must be exact.
    """

    def __init__(self):
        self.gained: Counter = Counter()
        self.last: dict | None = None

    def feed(self, samples: dict) -> None:
        if self.last is None:
            self.last = dict(samples)
            return
        for key, value in samples.items():
            before = self.last.get(key)
            self.gained[key] += value if before is None or value < before else value - before
            self.last[key] = value

    def total(self, name: str, **labels: str) -> int:
        """The gain of a counter, summed over every label value that `labels` does not fix."""
        wanted = set(labels.items())
        return int(sum(v for (n, ls), v in self.gained.items() if n == name and wanted <= set(ls)))


@dataclass
class Counters:
    journey: str
    jobs: int
    global_cap: int | None = None
    queue_prefix: str = ""
    published: int = 0
    finished: int = 0
    failed: int = 0
    max_live: int = 0
    duplicates: int = 0
    skipped: int = 0
    readmitted: Counter = field(default_factory=Counter)
    resumed: int = 0
    dead_lettered: Counter = field(default_factory=Counter)
    events: int = 0
    rows_accepted: int = 0
    restarts: int = 0
    oom_killed: int = 0

    def lines(self) -> list[str]:
        lines = [
            f"queue_prefix: {self.queue_prefix}",
            f"published: {self.published}",
            f"finished: {self.finished}",
            f"failed: {self.failed}",
            f"max_live: {self.max_live}",
            f"duplicates: {self.duplicates}",
        ]
        if self.journey == "control":
            readmitted = " ".join(f"{r} {self.readmitted[r]}" for r in READMITTED)
            lines += [
                f"skipped: {self.skipped}",
                f"readmitted: {readmitted}",
                f"resumed: {self.resumed}",
            ]
        elif self.journey == "deadletter":
            reasons = " ".join(f"{r} {n}" for r, n in sorted(self.dead_lettered.items()))
            lines += [
                f"dead_lettered: {reasons or 'none'}",
                f"events: {self.events}",
                f"rows: accepted {self.rows_accepted}",
            ]
        elif self.journey == "restart":
            lines += [f"restarts: {self.restarts}", f"oom_killed: {self.oom_killed}"]
        return lines

    def passed(self) -> bool:
        if self.journey == "deadletter":
            return (
                self.published == DEADLETTER_JOBS
                and self.dead_lettered == Counter(MALFORMED=1, UNSUPPORTED_VERSION=1)
                and self.events == DEADLETTER_JOBS
                and self.rows_accepted == DEADLETTER_JOBS
            )
        paused, stuck, running = split(self.jobs)
        # The stuck rows of `control` have no message.
        published = self.jobs - stuck if self.journey == "control" else self.jobs
        ok = self.published == published and self.finished == self.jobs and self.failed == 0
        if self.journey == "smoke":
            capped = self.global_cap is None or self.max_live <= self.global_cap
            return ok and capped and self.duplicates == 0
        if self.journey == "control":
            return (
                ok
                and self.skipped == paused
                and self.readmitted == Counter({READMITTED[0]: paused, READMITTED[1]: stuck})
                and self.resumed == running
            )
        return ok and self.restarts == 2 and self.oom_killed == 0


def accepted(tenant: str, priority_class: str, source: bytes) -> Job:
    """A smoke job of the tenant and priority class, accepted as the dispatch API would."""
    plan = smoke_plan(content(source, "source", "bundle.tar"))
    job_id = str(uuid.uuid4())
    event = build_event(
        "job.accepted",
        source="/wft/dispatch",
        tenant=tenant,
        jobid=job_id,
        priorityclass=priority_class,
        jobclass="short",
        traceparent=new_traceparent(),
        time=datetime.now(UTC),
        data={
            "manifest": manifest_reference(plan),
            "batch_id": str(uuid.uuid4()),
            "submission_key": f"drive-{job_id}",
        },
    )
    return Job(event, canonical_bytes(plan), source)


def row_of(event: Event) -> JobRow:
    return JobRow(
        job_id=event.jobid,
        tenant=event.tenant,
        submission_key=event.data["submission_key"],
        batch_id=event.data["batch_id"],
        priority_class=event.priorityclass,
        job_class=event.jobclass,
        manifest_ref=event.data["manifest"],
        schema_version=event.dataschema,
        traceparent=event.traceparent,
        seed=PARAMS.seed,
        parameters=PARAMS.parameters,
        fingerprint=event.data["manifest"]["sha256"],
    )


def malformed(event: Event) -> bytes:
    """The accepted event with no manifest, which names its job and fails its schema."""
    document = json.loads(to_bytes(event))
    document["data"].pop("manifest")
    return json.dumps(document).encode()


def unsupported(event: Event) -> bytes:
    """The accepted event under a major that no reader knows, which names its job."""
    document = json.loads(to_bytes(event))
    document["type"] = "dev.wft.job.accepted.v2"
    document["dataschema"] = "urn:wft:schema:job.accepted:2.0"
    return json.dumps(document).encode()


class Driver:
    """One journey on the cluster of the harness's `Cluster`, whose forwards, database connection,
    store client and Temporal client it uses, plus a forward to the broker and one to the admission
    metrics."""

    def __init__(self, cluster, counters: Counters, passwords: dict[str, str]):
        self.cluster = cluster
        self.counters = counters
        self.passwords = passwords
        self.context = cluster.context
        profile = load(PROFILE)
        self.tenant = next(iter(profile.tenants))
        self.priority_class = profile.priority_classes[0]
        template = topology().intake_queue.template
        self.queue = template.replace("<tenant>", self.tenant).replace(
            "<class>", self.priority_class
        )
        # The harness's connection is shared by the threads of the journey, one call at a time.
        self.lock = threading.Lock()
        self.tally = Tally()
        self.source = source_bundle("drive")

    async def db(self, call: Callable[..., Any], *args) -> Any:
        """Runs `call(connection, *args)` in a thread, alone on the harness's connection."""

        def locked():
            with self.lock:
                return call(self.cluster.db, *args)

        return await asyncio.to_thread(locked)

    def _broker(self, user: str):
        port = self.cluster.forwards["rabbitmq"].port
        return connect(f"127.0.0.1:{port}", Credentials(user, self.passwords[user]))

    # The job rows, the store and the intake queue, as the dispatch API would use them.

    async def seed(self, count: int) -> list[Job]:
        jobs = [accepted(self.tenant, self.priority_class, self.source) for _ in range(count)]
        store = self.cluster.submit

        def seed(db, job: Job) -> None:
            with db.transaction():
                insert_accepted(db, [row_of(job.event)])
                insert_events(db, [job.event])
            store.put_content(job.manifest, "manifest", "manifest.json", conn=db, job_id=job.job_id)
            store.put_content(job.source, "source", "bundle.tar", conn=db, job_id=job.job_id)

        for job in jobs:
            await self.db(seed, job)
        return jobs

    async def publish(self, jobs: list[Job], body: Callable[[Event], bytes] = to_bytes) -> None:
        broker = await asyncio.to_thread(self._broker, DISPATCH)
        try:
            for job in jobs:
                outcome = await asyncio.to_thread(broker.publish, self.queue, body(job.event))
                if outcome is not Outcome.ACCEPTED:
                    raise RuntimeError(f"the publish of {job.job_id} was {outcome.value}")
                await self.db(mark_published, job.job_id)
                self.counters.published += 1
        finally:
            await asyncio.to_thread(broker.close)

    async def statuses(self, jobs: Iterable[Job]) -> dict[str, str]:
        def read(db, ids: list[str]) -> dict[str, str]:
            return dict(
                db.execute(
                    "SELECT job_id::text, status FROM accepted_job WHERE job_id = ANY(%s::uuid[])",
                    (ids,),
                ).fetchall()
            )

        return await self.db(read, [job.job_id for job in jobs])

    async def left_accepted(self, jobs: list[Job], every: bool = True) -> bool:
        """Whether every one of the jobs, or any when not `every`, has left `accepted`."""
        moved = [s != "accepted" for s in (await self.statuses(jobs)).values()]
        return all(moved) if every else any(moved)

    async def ended(self, jobs: list[Job]) -> None:
        """Waits until every job ended or DEADLINE_S passed, and counts how they ended."""

        async def done() -> bool:
            return all(s in TERMINAL for s in (await self.statuses(jobs)).values())

        try:
            await until(done, DEADLINE_S, "end of every job", 1.0)
        except TimeoutError:
            pass
        ended = await self.statuses(jobs)
        self.counters.finished = sum(s == "succeeded" for s in ended.values())
        self.counters.failed = len(jobs) - self.counters.finished

    # The live count and the admission metrics, sampled for the whole run.

    async def scraped(self) -> bool:
        """Feeds the tally one scrape of the admission metrics, and says whether one came."""
        url = f"http://127.0.0.1:{self.cluster.forwards['admission'].port}/metrics"

        def scrape() -> str:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.read().decode()

        try:
            self.tally.feed(parse_metrics(await asyncio.to_thread(scrape)))
        except OSError:
            # The pod or its forward is restarting.
            return False
        return True

    async def sample(self) -> None:
        def live(db) -> int:
            return db.execute(
                "SELECT count(*) FROM accepted_job WHERE status IN ('admitted', 'running')"
            ).fetchone()[0]

        while True:
            self.counters.max_live = max(self.counters.max_live, await self.db(live))
            await self.scraped()
            await asyncio.sleep(SAMPLE_S)

    async def run(self) -> None:
        # The first scrape is the baseline, taken before anything is published.
        await until(self.scraped, UP_S, "scrape of the admission metrics", 1.0)
        sampler = asyncio.create_task(self.sample())
        try:
            await getattr(self, self.counters.journey)()
        finally:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        await self.scraped()
        self.counters.duplicates = self.tally.total("wft_admission_duplicates_total")

    def pools(self, hold: dict[str, float] | None = None):
        scripts = {pool: lambda task: Succeed() for pool in naming().pools}
        return self.cluster.running(scripts, hold or {}, self.counters.queue_prefix)

    # The journeys.

    async def smoke(self) -> None:
        jobs = await self.seed(self.counters.jobs)
        async with self.pools():
            await self.publish(jobs)
            await self.ended(jobs)

    async def control(self) -> None:
        jobs = await self.seed(self.counters.jobs)
        paused, stuck, running = split(len(jobs))
        before = jobs[:paused]
        stuck_jobs = jobs[paused : paused + stuck]
        while_running = jobs[paused + stuck : paused + stuck + running]
        after = jobs[paused + stuck :]
        for job in before:
            await self.db(pause_before_start, job.job_id)
        for job in stuck_jobs:
            await self.db(leave_admitted, job.job_id)
        async with self.pools({"simulate": HOLD_S}):
            await self.publish([*before, *after])
            pauses = [asyncio.create_task(self.pause_while_running(j)) for j in while_running]
            # One queue holds every message in publish order, so once each job published after the
            # paused ones has left `accepted`, admission has pulled and skipped the paused ones.
            await until(lambda: self.left_accepted(after), DEADLINE_S, "pull of every message", 1.0)
            for job in before:
                await self.db(resume_before_start, job.job_id)
            await asyncio.gather(*pauses)
            await self.ended(jobs)
        self.counters.skipped = self.tally.total("wft_admission_skipped_total")
        for reason in READMITTED:
            self.counters.readmitted[reason] = self.tally.total(
                "wft_admission_readmitted_total", reason=reason
            )
        self.counters.resumed = self.tally.total("wft_admission_resumed_total")

    async def status(self, job: Job) -> StatusQuery | None:
        handle = self.cluster.client.get_workflow_handle(workflow_id(job.job_id))
        try:
            return await handle.query(naming().queries[0], result_type=StatusQuery)
        except (RPCError, WorkflowQueryFailedError):
            # A job that is not started yet, or whose plan is not loaded, answers no status.
            return None

    async def pause_while_running(self, job: Job) -> None:
        """Pauses the job while a simulate task runs, as the control API would, then asks for its
        resume and leaves the signal to admission."""

        async def simulating() -> StatusQuery | None:
            status = await self.status(job)
            if status is None:
                return None
            ended = status.job_status in TERMINAL
            busy = any(t.state == "running" and t.id.startswith("sim-") for t in status.tasks)
            return status if busy or ended else None

        status = await until(simulating, DEADLINE_S, f"simulate task of {job.job_id}", 0.5)
        if status.job_status in TERMINAL:
            return
        pause = await self.db(request_action, job.job_id, "pause")
        handle = self.cluster.client.get_workflow_handle(workflow_id(job.job_id))
        await handle.signal("pause", ControlSignal(control_seq=pause.control_seq, reason="drive"))

        async def paused() -> bool:
            return (await self.statuses([job]))[job.job_id] == "paused"

        await until(paused, DEADLINE_S, f"pause of {job.job_id}", 0.5)
        await self.db(request_resume, job.job_id)

    async def deadletter(self) -> None:
        bad, old = await self.seed(DEADLETTER_JOBS)
        await self.publish([bad], malformed)
        await self.publish([old], unsupported)
        ids = {bad.job_id, old.job_id}

        async def dead_lettered() -> bool:
            self.counters.dead_lettered = await asyncio.to_thread(self.dead_letters, ids)
            return sum(self.counters.dead_lettered.values()) >= len(ids)

        await until(dead_lettered, DEAD_LETTER_S, "dead letters", 1.0)

        def events(db) -> int:
            return db.execute(
                "SELECT count(*) FROM event WHERE job_id = ANY(%s::uuid[]) AND type = %s",
                (list(ids), DEAD_LETTERED),
            ).fetchone()[0]

        self.counters.events = await self.db(events)
        statuses = (await self.statuses([bad, old])).values()
        self.counters.rows_accepted = sum(s == "accepted" for s in statuses)
        self.counters.finished = sum(s == "succeeded" for s in statuses)
        self.counters.failed = sum(s in TERMINAL and s != "succeeded" for s in statuses)

    def dead_letters(self, ids: set[str]) -> Counter:
        """The reasons of the dead letters of the jobs, read in one pass that puts every message
        back in its order, as the operator's tool does."""
        broker = self._broker(OPERATOR)
        try:
            held = broker.browser(DLQ).drain(PASS_LIMIT, PASS_TIMEOUT_S)
            reasons = Counter()
            for position, delivery in enumerate(held, 1):
                letter = Letter(position, delivery)
                if letter.jobid in ids:
                    reasons[letter.reason] += 1
            for delivery in held:
                delivery.released()
            return reasons
        finally:
            broker.close()

    async def restart(self) -> None:
        jobs = await self.seed(self.counters.jobs)
        first, second = jobs[: len(jobs) // 2], jobs[len(jobs) // 2 :]
        async with self.pools({"simulate": HOLD_S}):
            await self.publish(first)
            await until(
                lambda: self.left_accepted(first, every=False), UP_S, "first admission", 1.0
            )
            await asyncio.to_thread(self.delete_pod, False)
            await self.publish(second)
            await until(
                lambda: self.left_accepted(second, every=False), UP_S, "admission again", 1.0
            )
            await asyncio.to_thread(self.delete_pod, True)
            await self.ended(jobs)
        [last] = await asyncio.to_thread(self.pods)
        self.counters.oom_killed += oom_killed(last)

    def pods(self) -> list[dict]:
        """The ready admission pods."""
        listed = kubectl(
            self.context, f"--namespace={CONTROL}", "get", "pods", "-l", POD_LABEL, "-o", "json"
        )
        return [pod for pod in json.loads(listed)["items"] if ready(pod)]

    def delete_pod(self, force: bool) -> None:
        """Deletes the ready admission pod, with no grace when `force`, once its last termination
        is read, and waits for its replacement to be ready."""
        [pod] = self.pods()
        name = pod["metadata"]["name"]
        self.counters.oom_killed += oom_killed(pod)
        grace = ["--grace-period=0", "--force"] if force else []
        kubectl(
            self.context, f"--namespace={CONTROL}", "delete", "pod", name, "--wait=false", *grace
        )
        deadline = time.monotonic() + UP_S
        while not [p for p in self.pods() if p["metadata"]["name"] != name]:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no admission pod ready within {UP_S:.0f} s after {name}")
            time.sleep(1)
        self.counters.restarts += 1


def moved(result, job_id: str) -> None:
    if not isinstance(result, Moved):
        raise RuntimeError(f"the row of {job_id} did not move: {result}")


def pause_before_start(db, job_id: str) -> None:
    """The pause of a job that never started: the request and its row-only move, in one
    transaction."""
    with db.transaction():
        request_action(db, job_id, "pause")
        moved(transition(db, job_id, "accepted", "paused", row_only=True), job_id)


def resume_before_start(db, job_id: str) -> None:
    """The resume of a job that never started: the request and its moves back to `accepted`, in one
    transaction, after which no message is left to admit it."""
    with db.transaction():
        request_action(db, job_id, "resume")
        moved(transition(db, job_id, "paused", "resume-pending"), job_id)
        moved(transition(db, job_id, "resume-pending", "accepted", row_only=True), job_id)


def request_resume(db, job_id: str) -> None:
    """The resume of a paused job that started, whose signal is admission's to send."""
    with db.transaction():
        request_action(db, job_id, "resume")
        moved(transition(db, job_id, "paused", "resume-pending"), job_id)


def leave_admitted(db, job_id: str) -> None:
    """A row claimed with no workflow and no message, with its receipt, as a crash between the
    claim and the start leaves it."""
    mark_published(db, job_id)
    moved(transition(db, job_id, "accepted", "admitted"), job_id)


async def drive(context: str, counters: Counters, passwords: dict[str, str]) -> Counters:
    from wft_workflow.harness.cluster import Cluster

    cluster = Cluster(context)
    cluster.forwards["rabbitmq"] = Forward(context, INFRA, "svc/rabbitmq", BROKER_PORT)
    cluster.forwards["admission"] = Forward(context, CONTROL, f"deploy/{DEPLOYMENT}", METRICS_PORT)
    async with cluster:
        await Driver(cluster, counters, passwords).run()
    return counters
