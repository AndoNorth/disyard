"""T2: the harness against the cluster of a kube context, through its own port-forwards bound to
127.0.0.1, with the credentials of the state directory.

The deployed workflow worker runs the real control activities, so the harness seeds what they read
as the dispatch API would: each job's row, claimed from `accepted` to `admitted`, and its manifest
and source in the store under the `submit` identity, indexed and linked to the job. The FakePool
of each pool holds the store identity of that pool, as the pool workers do. A run on the real
pools reads the results the jobs committed through the index and the `workflow` identity.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from temporalio.client import Client, WorkflowHandle, WorkflowQueryFailedError
from temporalio.service import RPCError
from wft_artifactclient.client import ArtifactClient
from wft_clusterkit import (
    CONTROL,
    INFRA,
    UP_S,
    WORKERS,
    Forward,
    appdb_password,
    kubectl,
    store_credentials,
    until,
)
from wft_jobstate import (
    JobRow,
    Moved,
    insert_accepted,
    request_action,
    status_vocabulary,
    transition,
)

from .. import versioning
from ..contract import naming
from ..models import ControlSignal, ResumeSignal, StatusQuery, TaskInput
from . import (
    DEADLINE_S,
    PARAMS,
    POOL_WORKERS,
    SIMULATE_TASKS,
    WORKFLOW_WORKER,
    Job,
    Pool,
    Refused,
    Scripts,
    matches_echo,
    search_attributes,
)

# Each forward: the namespace, the resource and the port behind it.
FORWARDS = {
    "temporal": (INFRA, "svc/temporal-frontend", 7233),
    "appdb": (INFRA, "pod/appdb-0", 5432),
    "objectstore": (INFRA, "svc/objectstore", 8333),
}
TERMINAL = status_vocabulary().terminal
WORKER_LABEL = f"app.kubernetes.io/name={WORKFLOW_WORKER}"
REGISTRY = "localhost:5001"
IMAGE = f"wft-{WORKFLOW_WORKER}"
# Run from the root of the tree whose image a rollout builds.
BUILD = Path("deploy/workflow/image.sh")


@dataclass(frozen=True)
class CurrentBuild:
    # The build current when the helper returned.
    current: str
    # The builds deployed as workflow worker Deployments, as listed.
    deployed: tuple[str, ...]
    # The build the helper made current, or None when it wrote nothing.
    made: str | None


async def make_deployed_current(
    client: Client, context: str, timeout_s: float = versioning.POLLERS_S
) -> CurrentBuild:
    """Makes the deployed build current when no build is, as a first deploy needs, since a
    deployment with no current build gives a new job to no worker. Refuses a current build that is
    not deployed, and, when no build is current, any number of deployed builds but one."""
    listed = await asyncio.to_thread(
        kubectl,
        context,
        f"--namespace={CONTROL}",
        "get",
        "deployments",
        "-l",
        WORKER_LABEL,
        "-o",
        "jsonpath={.items[*].metadata.labels.wft\\.dev/build-id}",
    )
    deployed = tuple(listed.split())
    current = await versioning.current(client)
    made = None
    if current is None:
        if len(deployed) != 1:
            raise Refused(f"no current build, and the deployed builds are {list(deployed)}")
        # A build made current by another caller during the poller wait is kept, not overwritten.
        current = await versioning.set_current(client, deployed[0], timeout_s, only_if_none=True)
        if current is None:
            current = made = deployed[0]
    if current not in deployed:
        raise Refused(f"build {current} is current, but the deployed builds are {list(deployed)}")
    return CurrentBuild(current, deployed, made)


class Cluster:
    """T2: the cluster of a kube context with the workflow worker deployed."""

    tier = "T2"

    def __init__(self, context: str):
        self.context = context
        self.forwards = {name: Forward(context, *spec) for name, spec in FORWARDS.items()}
        self.client: Client | None = None
        self.db: psycopg.Connection | None = None
        self._watch: asyncio.Task | None = None

    def refuse(
        self, command: str, scenario: str | None, targets: list[str], pools: str = "fake"
    ) -> None:
        if command == "rollout" and not BUILD.is_file():
            raise Refused(f"rollout builds the image with {BUILD}; run it from the tree's root")

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.forwards['objectstore'].port}"

    async def __aenter__(self) -> "Cluster":
        from ..testing import connect

        try:
            for forward in self.forwards.values():
                await asyncio.to_thread(forward.open)
            self._watch = asyncio.create_task(self._reopen())
            self.credentials = store_credentials()
            self.password = appdb_password()
            self.db = await asyncio.to_thread(self._connect)
            self.submit = ArtifactClient(self.endpoint, self.credentials["submit"])
            self.client = await connect(f"127.0.0.1:{self.forwards['temporal'].port}")
            made = (await make_deployed_current(self.client, self.context)).made
            if made is not None:
                print(f"harness: made build {made} current", file=sys.stderr, flush=True)
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)
        if self.db is not None:
            self.db.close()
        for forward in self.forwards.values():
            await asyncio.to_thread(forward.close)

    def _connect(self) -> psycopg.Connection:
        port = self.forwards["appdb"].port
        return psycopg.connect(
            f"postgresql://postgres@127.0.0.1:{port}/postgres",
            password=self.password,
            autocommit=True,
        )

    async def _reopen(self) -> None:
        """Opens again each forward whose kubectl exited, as it does when its pod goes away."""
        while True:
            for name, forward in self.forwards.items():
                if not forward.alive:
                    try:
                        await asyncio.to_thread(forward.open)
                    except TimeoutError as error:
                        print(f"harness: forward to {name}: {error}", file=sys.stderr, flush=True)
            await asyncio.sleep(1)

    def _seed(self, jobs: list[Job]) -> None:
        rows = [
            JobRow(
                job_id=job.job_id,
                tenant=job.event.tenant,
                submission_key=job.event.data["submission_key"],
                batch_id=job.event.data["batch_id"],
                priority_class=job.event.priorityclass,
                job_class=job.event.jobclass,
                manifest_ref=job.event.data["manifest"],
                schema_version="urn:wft:schema:job.accepted:1.0",
                traceparent=job.event.traceparent,
                seed=PARAMS.seed,
                parameters=PARAMS.parameters,
                fingerprint=job.event.data["manifest"]["sha256"],
            )
            for job in jobs
        ]
        insert_accepted(self.db, rows)
        for job in jobs:
            self.submit.put_content(
                job.manifest, "manifest", "manifest.json", conn=self.db, job_id=job.job_id
            )
            self.submit.put_content(
                job.source, "source", "bundle.tar", conn=self.db, job_id=job.job_id
            )
            moved = transition(self.db, job.job_id, "accepted", "admitted")
            if moved.status != "admitted":
                raise RuntimeError(f"the row of {job.job_id} could not be claimed: {moved}")

    async def seed(self, jobs: list[Job]) -> None:
        await asyncio.to_thread(self._seed, jobs)

    def start_options(self, job: Job) -> dict:
        return {"search_attributes": search_attributes(job.event)}

    @asynccontextmanager
    async def running(
        self, scripts: Scripts, hold: dict[str, float], prefix: str = ""
    ) -> AsyncIterator[dict]:
        async with AsyncExitStack() as stack:
            fakes = {}
            for pool, script in scripts.items():
                store = ArtifactClient(self.endpoint, self.credentials[pool])
                fakes[pool] = await stack.enter_async_context(
                    Pool(
                        self.client,
                        pool,
                        script,
                        store=store,
                        queue=prefix + naming().pools[pool],
                        hold_s=hold.get(pool, 0.0),
                    )
                )
            yield fakes

    def _verified(self, jobs: list[Job]) -> int:
        """The jobs whose every simulate result, as the index holds it, has the expected values of
        the level 0 `echo` vector."""
        reader = ArtifactClient(self.endpoint, self.credentials["workflow"])
        rows = self.db.execute(
            "SELECT producer_job::text, producer_task, producer_attempt, uri, sha256, size, name"
            " FROM artifact WHERE kind = 'result' AND deleted_at IS NULL"
            " AND producer_job::text = ANY(%s)",
            ([job.job_id for job in jobs],),
        ).fetchall()
        matched: dict[str, int] = {}
        for job, task, attempt, uri, sha256, size, name in rows:
            reference = {
                "uri": uri,
                "sha256": sha256,
                "size": size,
                "name": name,
                "kind": "result",
                "producer": {"job": job, "task": task, "attempt": attempt},
            }
            if matches_echo(reader.get_bytes(reference, size)):
                matched[job] = matched.get(job, 0) + 1
        return sum(matched.get(job.job_id) == SIMULATE_TASKS for job in jobs)

    async def verified(self, jobs: list[Job]) -> int:
        return await asyncio.to_thread(self._verified, jobs)

    def _pods(self, target: str) -> tuple[str, list[str]]:
        """The namespace of a target and the names of its pods: the workflow worker's by label,
        a pool worker's or a Temporal service's by the name of its Deployment."""
        if target == WORKFLOW_WORKER:
            names = kubectl(
                self.context,
                f"--namespace={CONTROL}",
                "get",
                "pods",
                "-l",
                WORKER_LABEL,
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ).split()
            return CONTROL, names
        namespace = WORKERS if target in POOL_WORKERS else INFRA
        pods = json.loads(
            kubectl(self.context, f"--namespace={namespace}", "get", "pods", "-o", "json")
        )
        names = [
            pod["metadata"]["name"]
            for pod in pods["items"]
            if pod["metadata"]["name"].startswith(f"{target}-")
            and pod["status"].get("phase") != "Succeeded"
        ]
        return namespace, names

    def _ready_again(self, target: str, gone: set[str]) -> bool:
        namespace, names = self._pods(target)
        for name in set(names) - gone:
            pod = json.loads(
                kubectl(self.context, f"--namespace={namespace}", "get", "pod", name, "-o", "json")
            )
            conditions = pod["status"].get("conditions", [])
            if any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
                return True
        return False

    def _restart(self, targets: list[str]) -> None:
        deleted = {}
        for target in targets:
            namespace, names = self._pods(target)
            if not names:
                raise Refused(f"no pod of {target} in the cluster of {self.context}")
            deleted[target] = (namespace, set(names))
        # Every target goes down at once, while the tasks of the jobs are in flight. A pool worker
        # goes with no grace, so its drain cannot finish the task it runs.
        for target, (namespace, names) in deleted.items():
            force = ["--grace-period=0", "--force"] if target in POOL_WORKERS else []
            kubectl(
                self.context,
                f"--namespace={namespace}",
                "delete",
                "pod",
                "--wait=false",
                *force,
                *sorted(names),
            )
        deadline = time.monotonic() + UP_S
        for target, (_, names) in deleted.items():
            while not self._ready_again(target, names):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{target} was not ready again within {UP_S:.0f} s")
                time.sleep(1)
        # The forward to the frontend holds a stream to a pod that is gone, and the others
        # forward to pods that never restart here.
        if any(target.startswith("temporal-") for target in targets):
            self.forwards["temporal"].open()

    async def restart(self, targets: list[str]) -> None:
        await asyncio.to_thread(self._restart, targets)

    async def zombie_commits(self, zombies: list[TaskInput]) -> int:
        """The index rows of the outputs of the attempts that zombies completed after timing out."""
        from ..testing.pool import producer_of

        attempts = [producer_of(task) for task in zombies]

        def count() -> int:
            return sum(
                self.db.execute(
                    "SELECT count(*) FROM artifact WHERE producer_job = %s"
                    " AND producer_task = %s AND producer_attempt = %s",
                    (p.job, p.task, p.attempt),
                ).fetchone()[0]
                for p in attempts
            )

        return await asyncio.to_thread(count)

    async def _status(self, handle: WorkflowHandle) -> StatusQuery | None:
        try:
            return await handle.query(naming().queries[0], result_type=StatusQuery)
        except (RPCError, WorkflowQueryFailedError):
            # A job whose plan is not loaded yet answers no status.
            return None

    async def pause_resume(self, handle: WorkflowHandle, job: Job) -> bool:
        """Pauses the job while its simulate tasks run and resumes it once it is paused, as the
        control API and admission would, and says whether it was paused and then resumed."""

        async def running() -> StatusQuery | None:
            status = await self._status(handle)
            simulating = status is not None and any(
                t.state == "running" and t.id.startswith("sim-") for t in status.tasks
            )
            ended = status is not None and status.job_status in TERMINAL
            return status if simulating or ended else None

        status = await until(running, DEADLINE_S, f"simulate task of {job.job_id} running")
        if status.job_status in TERMINAL:
            # The job ended before the pause could land while it ran.
            return False
        pause = await asyncio.to_thread(request_action, self.db, job.job_id, "pause")
        await handle.signal("pause", ControlSignal(control_seq=pause.control_seq, reason="harness"))

        async def paused() -> bool:
            status = await self._status(handle)
            return status is not None and status.job_status == "paused"

        await until(paused, DEADLINE_S, f"pause of {job.job_id}")

        def request_resume() -> int:
            resume = request_action(self.db, job.job_id, "resume")
            moved = transition(self.db, job.job_id, "paused", "resume-pending")
            if not isinstance(moved, Moved):
                raise RuntimeError(f"the row of {job.job_id} did not move to resume-pending")
            return resume.control_seq

        control_seq = await asyncio.to_thread(request_resume)
        await handle.signal("resume", ResumeSignal(control_seq=control_seq, manifest=None))
        return True

    def _deploy(self, old: str, new: str) -> None:
        """Builds the image of this tree as build `new`, pushes it, and runs it beside build `old`
        as its own Deployment, a copy of the old one with the build id and the image changed."""
        image = f"{REGISTRY}/{IMAGE}:{new}"
        env = {**os.environ, naming().workflow.build_id_env: new}
        subprocess.run([str(BUILD), image], check=True, env=env)
        subprocess.run(["docker", "push", "--quiet", image], check=True)
        deployment = json.loads(
            kubectl(
                self.context,
                f"--namespace={CONTROL}",
                "get",
                "deployment",
                f"{WORKFLOW_WORKER}-{old}",
                "-o",
                "json",
            )
        )
        meta = deployment["metadata"]
        deployment["metadata"] = {
            "name": f"{WORKFLOW_WORKER}-{new}",
            "namespace": meta["namespace"],
            "labels": {**meta["labels"], "wft.dev/build-id": new},
        }
        deployment.pop("status", None)
        spec = deployment["spec"]
        for labels in (spec["selector"]["matchLabels"], spec["template"]["metadata"]["labels"]):
            labels["wft.dev/build-id"] = new
        [container] = spec["template"]["spec"]["containers"]
        container["image"] = image
        subprocess.run(
            ["kubectl", f"--context={self.context}", "apply", "-f", "-"],
            input=json.dumps(deployment),
            text=True,
            check=True,
            capture_output=True,
        )
        kubectl(
            self.context,
            f"--namespace={CONTROL}",
            "rollout",
            "status",
            f"deployment/{WORKFLOW_WORKER}-{new}",
            f"--timeout={UP_S:.0f}s",
        )

    async def deploy(self, old: str, new: str) -> None:
        await asyncio.to_thread(self._deploy, old, new)

    def deleter(self):
        return versioning.kubectl_delete(self.context)
