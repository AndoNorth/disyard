"""The happy path scenarios of level 4: the acceptance job (H1) and the compile cache hit (N6).

Every function takes the platform as a `Platform` of callables, so the verdicts run against fakes
and the live suite wires the real clients in. A check raises `Failed` naming what it saw.
"""

import hashlib
import io
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from dsy_events.artifact_layout import layout
from dsy_events.models import DATA_MODELS
from dsy_kernel.expected import expected_blocks
from dsy_kernel.reader import read, same_blocks
from dsy_workflow.contract import naming
from dsy_workflow.harness.cluster import TERMINAL

from ..clients import Failed, api
from ..fixtures import bundles, jobs
from . import batch_key, wait

# The identities of contracts/store/roles.yaml the scenarios read the store as.
WORKFLOW_ROLE = "workflow"
EXPORT_ROLE = "export"
# The one level 1 workload of the jobs, small enough that a run is dominated by its compiles.
WORKLOAD = "cpu_burn"
UNITS = 40
HEARTBEAT_EVERY = 10
# The seeds of chains a and b of H1, and of N6's job, all distinct.
SEEDS = (11, 12)
HIT_SEED = 13
H1_TIMEOUT_S = 360.0
N6_TIMEOUT_S = 120.0
# A pod of the aggregate pool runs the aggregate natively, so no WebAssembly runtime may run in it.
WASM_RUNTIME = "wasmtime"
AGGREGATE_POOL = "aggregate"
LIFECYCLE = ("job.accepted", "job.admitted", "job.started", "job.succeeded")


@dataclass
class Platform:
    """What the scenarios reach, one callable per surface."""

    upload: Callable[[bytes], dict]
    # Submits one job of the template under a batch key and returns its id and trace id.
    submit: Callable[[dict, str], tuple[str, str]]
    status: Callable[[str], dict]
    cancel: Callable[[str], object]
    # The accepted-job row, or None.
    row: Callable[[str], object]
    # The event rows of a job in time order.
    events: Callable[[str], list]
    # How many workflow executions carry the job's workflow id.
    executions: Callable[[str], int]
    # The bytes at a uri, read as the named store identity.
    fetch: Callable[[str, str], bytes]
    # The uri of the build-info the compile cache entry of a wasm names, or None.
    build_info_uri: Callable[[str], str | None]
    # The process names of each live pod of the aggregate pool.
    aggregate_processes: Callable[[], list[set[str]]]


@dataclass
class Run:
    """A submitted job and what the scenario knows of it."""

    batch: str
    job_id: str
    trace_id: str
    seeds: tuple[int, ...]
    # Each chain's name, in the order of its seed.
    chains: tuple[str, ...]
    sources: tuple[dict, ...]
    samples: list[str] = field(default_factory=list)
    ended: dict = field(default_factory=dict)


def simulate_params(seed: int) -> dict:
    return {
        "workload": WORKLOAD,
        "args": {"units": UNITS, "heartbeat_every": HEARTBEAT_EVERY},
        "seed": seed,
    }


def type_of(name: str) -> str:
    """The CloudEvent type of an event name of the events contract, which fails on an unknown one so
    a rename shows here and not as an empty match."""
    major = 1
    if (name, f"{major}.0") not in DATA_MODELS:
        raise Failed(f"{name} is not an event type of the events contract")
    return f"dev.dsy.{name}.v{major}"


@dataclass
class Fact:
    """One stored event with its data parsed."""

    name: str
    task: str | None
    attempt: int | None
    time: datetime
    data: dict


def facts(events: list, name: str) -> list[Fact]:
    wanted = type_of(name)
    return [
        Fact(name, e.task_id, e.attempt, e.event_time, json.loads(e.body).get("data") or {})
        for e in events
        if e.type == wanted
    ]


def one(events: list, name: str, task: str | None = None) -> Fact:
    found = [f for f in facts(events, name) if task is None or f.task == task]
    if len(found) != 1:
        raise Failed(f"{len(found)} {name} events for task {task}, want 1")
    return found[0]


def first_time(events: list, name: str, task: str) -> datetime:
    found = [f.time for f in facts(events, name) if f.task == task]
    if not found:
        raise Failed(f"no {name} event for task {task}")
    return min(found)


def accepted(client: api.Api, document: dict, key: str) -> tuple[str, str]:
    """Submits one job of the template and returns its job id and the trace id of the submit."""
    lines = api.batch(key, document, 1)
    answers = api.stream(client.url(client.dispatch, "batches"), lines, client.timeout_s)
    first = answers[0] if answers else {}
    if first.get("outcome") != "accepted":
        raise Failed(f"the job was not accepted: {first or 'no answer'}")
    return first["job_id"], first["trace_id"]


def guarded(platform: Platform, batch: str, scenario: Callable[[], object]):
    """Runs the scenario and cancels its batch when it fails, so a late job does not hold the pools
    into the next scenario."""
    try:
        return scenario()
    except BaseException:
        try:
            platform.cancel(batch)
        except Exception as error:
            print(f"cleanup: the cancel of batch {batch} failed: {error}")
        raise


def ended(platform: Platform, run: Run, timeout_s: float) -> dict:
    """Polls the job's status until it is terminal, keeping every distinct status seen."""

    def check() -> dict | None:
        status = platform.status(run.job_id)
        if not run.samples or run.samples[-1] != status["status"]:
            run.samples.append(status["status"])
        return status if status["status"] in TERMINAL else None

    try:
        run.ended = wait(check, timeout_s, f"end of job {run.job_id}")
    except TimeoutError as error:
        raise Failed(f"{error}, statuses seen {run.samples}") from None
    return run.ended


def submit_chains(platform: Platform, sources: tuple[dict, ...], seeds: tuple[int, ...]) -> Run:
    """Submits one job of two compile then simulate chains and one aggregate over both."""
    key = batch_key()
    document = jobs.two_chains(sources, tuple(simulate_params(s) for s in seeds))
    job_id, trace_id = guarded(platform, key, lambda: platform.submit(document, key))
    return Run(key, job_id, trace_id, seeds, ("a", "b"), sources)


def h1(platform: Platform) -> Run:
    """The acceptance job over two fresh bundles, run to its end and checked on every surface."""
    sources = tuple(
        platform.upload(bundles.variant(f"h1-{name}-{uuid.uuid4().hex[:8]}")) for name in "ab"
    )
    run = submit_chains(platform, sources, SEEDS)

    def verify():
        ended(platform, run, H1_TIMEOUT_S)
        check_accepted(platform, run)
        check_status(platform, run)
        events = platform.events(run.job_id)
        wasm = check_wasm(platform, run, events)
        check_chains(run, events, wasm)
        check_results(platform, run)
        check_aggregate(platform, run, events)

    guarded(platform, run.batch, verify)
    return run


def check_accepted(platform: Platform, run: Run) -> None:
    """The row exists, carries the trace of the submit, and was accepted before its publish."""
    row = platform.row(run.job_id)
    if row is None:
        raise Failed(f"no accepted row for job {run.job_id}")
    if run.trace_id not in row.traceparent:
        raise Failed(f"row traceparent {row.traceparent} lacks the trace id {run.trace_id}")
    if row.published_at is None:
        raise Failed(f"job {run.job_id} was never confirmed published")
    accepted = one(platform.events(run.job_id), "job.accepted")
    if row.created_at > row.published_at or accepted.time > row.published_at:
        raise Failed(
            f"the row ({row.created_at}) and its accepted event ({accepted.time}) "
            f"must come before the publish confirmation ({row.published_at})"
        )


def subsequence(wanted: tuple[str, ...], seen: list[str]) -> bool:
    rest = iter(seen)
    return all(item in rest for item in wanted)


def check_status(platform: Platform, run: Run) -> None:
    """The job ended succeeded through the lifecycle, and one workflow carries its workflow id."""
    if run.ended["status"] != "succeeded":
        raise Failed(f"ended {run.ended['status']}, want succeeded")
    stored = [f.name for f in sorted_facts(platform.events(run.job_id), LIFECYCLE)]
    if tuple(stored) != LIFECYCLE:
        raise Failed(f"lifecycle events {stored}, want {list(LIFECYCLE)}")
    if not subsequence(("accepted", "admitted", "running", "succeeded"), run.samples):
        raise Failed(f"statuses seen {run.samples} left out part of the lifecycle")
    found = platform.executions(run.job_id)
    if found != 1:
        raise Failed(f"{found} workflow executions carry the workflow id of {run.job_id}, want 1")


def sorted_facts(events: list, names: tuple[str, ...]) -> list[Fact]:
    found = [f for name in names for f in facts(events, name)]
    return sorted(found, key=lambda f: f.time)


def component_of(events: list, task: str) -> dict:
    """The component reference the simulate task's request names, which is by reference and hash."""
    inputs = one(events, "task.requested", task).data["inputs"]
    components = [i for i in inputs if i["kind"] == "component"]
    if len(components) != 1:
        raise Failed(f"the request of {task} names {len(components)} components, want 1")
    return components[0]


def check_wasm(platform: Platform, run: Run, events: list) -> dict[str, dict]:
    """Each chain has its own wasm under its sha256, a cache miss, equal to its build-info."""
    wanted = layout()
    wasm = {}
    for chain in run.chains:
        build, sim = f"{chain}-build", f"{chain}-sim"
        if facts([e for e in events if e.task_id == build], "task.skipped"):
            raise Failed(f"{build} was a cache hit, want a miss")
        if not [f for f in facts(events, "task.started") if f.task == build]:
            raise Failed(f"{build} never started a compile attempt")
        ref = component_of(events, sim)
        bucket = wanted["kinds"]["component"]["bucket"]
        if not ref["uri"].startswith(f"s3://{bucket}/") or not ref["uri"].endswith(ref["sha256"]):
            raise Failed(f"{sim} reads {ref['uri']}, not a content address in {bucket}")
        data = platform.fetch(ref["uri"], WORKFLOW_ROLE)
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise Failed(f"the wasm at {ref['uri']} does not hash to {ref['sha256']}")
        info_uri = platform.build_info_uri(ref["uri"])
        if info_uri is None:
            raise Failed(f"no compile cache entry names {ref['uri']}")
        info = json.loads(platform.fetch(info_uri, WORKFLOW_ROLE))
        if info["component"]["sha256"] != ref["sha256"]:
            raise Failed(
                f"build-info of {chain} says {info['component']['sha256']}, wasm is {ref['sha256']}"
            )
        wasm[chain] = ref
    if wasm["a"]["sha256"] == wasm["b"]["sha256"]:
        raise Failed("chains a and b built the same wasm, want two distinct bundles")
    return wasm


def check_chains(run: Run, events: list, wasm: dict[str, dict]) -> None:
    """Each simulate started after its own compile ended, and read that chain's wasm."""
    for chain in run.chains:
        built = min(
            f.time
            for name in ("task.succeeded", "task.skipped")
            for f in facts(events, name)
            if f.task == f"{chain}-build"
        )
        started = first_time(events, "task.started", f"{chain}-sim")
        if started < built:
            raise Failed(f"{chain}-sim started at {started}, before {chain}-build ended at {built}")
        ref = component_of(events, f"{chain}-sim")
        if ref != wasm[chain]:
            raise Failed(f"{chain}-sim reads {ref['uri']}, not its own wasm {wasm[chain]['uri']}")


def result_key(row, task: str, attempt: str, name: str, kind: str) -> str:
    """The object uri of an attempt-scoped result of the layout contract, rendered from its
    template."""
    spec = layout()["kinds"][kind]
    values = {
        "tenant": row.tenant,
        "batch": row.batch_id,
        "job": row.job_id,
        "seed": str(row.seed),
        "task": task,
        "attempt": attempt,
        "name": name,
    }
    return f"s3://{spec['bucket']}/" + re.sub(r"<(\w+)>", lambda m: values[m[1]], spec["key"])


def result_of(status: dict, task: str, kind: str) -> dict:
    found = [r for r in status["results"] if r["producer"]["task"] == task and r["kind"] == kind]
    if len(found) != 1:
        raise Failed(f"{len(found)} {kind} results of {task} in the status, want 1")
    return found[0]


def fetched(platform: Platform, reference: dict) -> bytes:
    data = platform.fetch(reference["uri"], EXPORT_ROLE)
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise Failed(f"{reference['uri']} does not hash to {reference['sha256']}")
    return data


def check_blocks(platform: Platform, run: Run, status: dict, chain: str, seed: int) -> dict:
    """The chain's one results.xml sits under its attempt prefix and holds the blocks the host
    recomputes for the seed."""
    task = f"{chain}-sim"
    reference = result_of(status, task, "result")
    row = platform.row(run.job_id)
    if reference["name"] != "results.xml":
        raise Failed(f"{task} wrote {reference['name']}, want results.xml")
    want = result_key(row, task, reference["producer"]["attempt"], reference["name"], "result")
    if reference["uri"] != want:
        raise Failed(f"{task} result is at {reference['uri']}, not its attempt prefix {want}")
    parsed = read(io.BytesIO(fetched(platform, reference)))
    params = simulate_params(seed)
    different = same_blocks(expected_blocks(WORKLOAD, seed, params["args"]), parsed["blocks"])
    if parsed["seed"] != seed or different:
        raise Failed(f"{task} blocks differ from those of seed {seed}: {different}")
    if not parsed["blocks"] or len(parsed["blocks"][0]["rows"]) != UNITS:
        raise Failed(f"{task} holds {parsed['blocks']!r:.80}, want {UNITS} rows")
    return reference


def check_results(platform: Platform, run: Run) -> None:
    status = run.ended
    for chain, seed in zip(run.chains, run.seeds, strict=True):
        check_blocks(platform, run, status, chain, seed)
    summary = json.loads(fetched(platform, result_of(status, "agg", "summary")))
    want = jobs.two_seed_summary(WORKLOAD, simulate_params(0)["args"], run.seeds)
    if summary != want:
        raise Failed(f"summary.json {summary!r:.120} differs from the host's {want!r:.120}")


def check_aggregate(platform: Platform, run: Run, events: list) -> None:
    """The aggregate ran on the aggregate pool after both simulates, over their results by
    reference, in a pod with no WebAssembly runtime."""
    queue = one(events, "task.placed", "agg").data["queue"]
    pool = naming().pools[AGGREGATE_POOL]
    if queue != pool:
        raise Failed(f"agg was placed on {queue}, want the aggregate pool queue {pool}")
    started = first_time(events, "task.started", "agg")
    for chain in run.chains:
        done = first_time(events, "task.succeeded", f"{chain}-sim")
        if started < done:
            raise Failed(f"agg started at {started}, before {chain}-sim ended at {done}")
    inputs = one(events, "task.requested", "agg").data["inputs"]
    wanted = {result_of(run.ended, f"{c}-sim", "result")["uri"] for c in run.chains}
    if {i["uri"] for i in inputs} != wanted:
        raise Failed(f"agg reads {sorted(i['uri'] for i in inputs)}, want {sorted(wanted)}")
    pods = platform.aggregate_processes()
    if not pods:
        raise Failed("no pod of the aggregate pool to probe")
    for names in pods:
        if WASM_RUNTIME in names:
            raise Failed(f"a {WASM_RUNTIME} process runs in an aggregate pod: {sorted(names)}")


def n6(platform: Platform, first: Run) -> Run:
    """H1's bundle of chain a again as a new job with another seed: no compile attempt, the same
    wasm, and the blocks of the new seed."""
    key = batch_key()
    document = api.template(first.sources[0], simulate_params(HIT_SEED))
    job_id, trace_id = guarded(platform, key, lambda: platform.submit(document, key))
    run = Run(key, job_id, trace_id, (HIT_SEED,), ("a",), first.sources[:1])

    def verify():
        ended(platform, run, N6_TIMEOUT_S)
        if run.ended["status"] != "succeeded":
            raise Failed(f"ended {run.ended['status']}, want succeeded")
        events = platform.events(run.job_id)
        check_hit(events, "build", "sim", component_of(platform.events(first.job_id), "a-sim"))
        status = run.ended
        reference = result_of(status, "sim", "result")
        parsed = read(io.BytesIO(fetched(platform, reference)))
        args = simulate_params(HIT_SEED)["args"]
        different = same_blocks(expected_blocks(WORKLOAD, HIT_SEED, args), parsed["blocks"])
        if parsed["seed"] != HIT_SEED or different:
            raise Failed(f"sim blocks differ from those of seed {HIT_SEED}: {different}")

    guarded(platform, key, verify)
    return run


def check_hit(events: list, build: str, sim: str, wasm: dict) -> None:
    """The build was skipped on a cache hit and never ran, and the simulate read the wasm of the
    first job."""
    one(events, "task.skipped", build)
    ran = [f for name in ("task.started", "task.attempt.failed") for f in facts(events, name)]
    if [f for f in ran if f.task == build]:
        raise Failed(f"{build} ran a compile attempt on a cache hit")
    ref = component_of(events, sim)
    if ref["sha256"] != wasm["sha256"]:
        raise Failed(f"{sim} reads wasm {ref['sha256']}, want the first job's {wasm['sha256']}")
