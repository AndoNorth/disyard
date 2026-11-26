"""The verdicts of the happy path scenarios (H1 and N6) against a fake platform that behaves as a
healthy cluster does: each assertion has a passing case, and a failing case made by breaking one
fact of the fake."""

import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dsy_acceptance import e2e
from dsy_acceptance.clients import Failed
from dsy_acceptance.e2e import happy
from dsy_acceptance.fixtures import jobs
from dsy_events.artifact_layout import layout
from dsy_kernel import writer
from dsy_kernel.expected import expected_blocks
from dsy_workflow.contract import naming

TENANT, BATCH_ID, TRACE = (
    "e2e",
    "b6d2f5d0-6b1e-4d0a-9a1c-0f4f6a7c1e11",
    "4bf92f3577b34da6a3ce929d0e0e4736",
)
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
ARGS = happy.simulate_params(0)["args"]
LAYOUT = layout()
COMPONENTS = LAYOUT["kinds"]["component"]["bucket"]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_uri(data: bytes) -> str:
    return f"s3://{COMPONENTS}/sha256/{digest(data)[:2]}/{digest(data)}"


def results_xml(seed: int) -> bytes:
    sink = io.BytesIO()
    writer.write(sink, seed, expected_blocks(happy.WORKLOAD, seed, ARGS))
    return sink.getvalue()


@dataclass
class World:
    """What the fake cluster holds for one H1 job and, after it, one N6 job."""

    job: str = "0b6e2f0a-5d1c-4f43-9a51-6b1f0d3c2e77"
    objects: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    rows: dict = field(default_factory=dict)
    statuses: dict = field(default_factory=dict)
    infos: dict = field(default_factory=dict)
    cancelled: list = field(default_factory=list)
    executions: int = 1
    pods: list = field(default_factory=lambda: [{"python", "sh"}])
    clock: int = 0
    seeds: tuple = happy.SEEDS
    sources: list = field(default_factory=list)
    uploads: int = 0
    submitted: list = field(default_factory=list)

    def at(self) -> datetime:
        self.clock += 1
        return T0 + timedelta(seconds=self.clock)

    def event(self, job: str, name: str, task: str | None = None, attempt=None, **data):
        body = json.dumps({"data": data})
        row = SimpleNamespace(
            type=happy.type_of(name),
            task_id=task,
            attempt=attempt,
            event_time=self.at(),
            body=body,
        )
        self.events.setdefault(job, []).append(row)

    def store(self, data: bytes) -> dict:
        ref = {"uri": content_uri(data), "sha256": digest(data), "kind": "component"}
        self.objects[ref["uri"]] = data
        return ref

    def result(self, job: str, task: str, seed: int, data: bytes, name: str, kind: str) -> dict:
        row = self.rows[job]
        row_like = SimpleNamespace(**vars(row))
        uri = happy.result_key(row_like, task, "r1a1", name, kind)
        ref = {
            "uri": uri,
            "sha256": digest(data),
            "name": name,
            "kind": kind,
            "producer": {"job": job, "task": task, "attempt": "r1a1"},
        }
        self.objects[uri] = data
        return ref

    def chain(self, job: str, chain: str, wasm: dict, hit: bool = False):
        build, sim = ("build", "sim") if chain == "" else (f"{chain}-build", f"{chain}-sim")
        if hit:
            self.event(job, "task.skipped", build, reason="compile-cache-hit", cache_key="0" * 64)
        else:
            self.event(job, "task.started", build, 1)
            self.event(job, "task.succeeded", build, 1)
        self.event(job, "task.requested", sim, inputs=[wasm])
        self.event(job, "task.placed", sim, queue=naming().pools["simulate"])
        self.event(job, "task.started", sim, 1)
        self.event(job, "task.succeeded", sim, 1)

    def add_job(self, job: str, seeds: tuple, wasms: list, chains: tuple, hit: bool = False):
        published = T0 + timedelta(hours=1)
        self.rows[job] = SimpleNamespace(
            job_id=job,
            tenant=TENANT,
            batch_id=BATCH_ID,
            seed=0,
            traceparent=f"00-{TRACE}-00f067aa0ba902b7-01",
            created_at=T0,
            published_at=published,
        )
        self.event(job, "job.accepted")
        for name in ("job.admitted", "job.started"):
            self.event(job, name)
        results = []
        for chain, seed, wasm in zip(chains, seeds, wasms, strict=True):
            self.chain(job, chain, wasm, hit)
            task = f"{chain}-sim" if chain else "sim"
            results.append(self.result(job, task, seed, results_xml(seed), "results.xml", "result"))
        if len(chains) == 2:
            self.event(job, "task.requested", "agg", inputs=[{"uri": r["uri"]} for r in results])
            self.event(job, "task.placed", "agg", queue=naming().pools["aggregate"])
            self.event(job, "task.started", "agg", 1)
            self.event(job, "task.succeeded", "agg", 1)
            summary = json.dumps(jobs.two_seed_summary(happy.WORKLOAD, ARGS, seeds)).encode()
            results.append(self.result(job, "agg", 0, summary, "summary.json", "summary"))
        self.event(job, "job.succeeded")
        self.statuses[job] = ["accepted", "admitted", "running", "succeeded"], results

    def platform(self) -> happy.Platform:
        samples: dict = {}

        def submit(document, key):
            self.submitted.append((document, key))
            if len(self.submitted) == 1:
                wasms = [self.store(b"wasm-a"), self.store(b"wasm-b")]
                for wasm in wasms:
                    info = json.dumps({"component": {"sha256": wasm["sha256"]}}).encode()
                    uri = f"s3://{COMPONENTS}/sha256/ff/{digest(info)}"
                    self.objects[uri] = info
                    self.infos[wasm["uri"]] = uri
                self.add_job(self.job, self.seeds, wasms, ("a", "b"))
                return self.job, TRACE
            wasm = self.store(b"wasm-a")
            second = "1c9f2d3e-0b6e-4f0a-8d1c-5d6b1f0a2e77"
            self.add_job(second, (happy.HIT_SEED,), [wasm], ("",), hit=True)
            self.job2 = second
            return second, TRACE

        def status(job_id):
            seen, results = self.statuses[job_id]
            index = samples.get(job_id, 0)
            samples[job_id] = index + 1
            now = seen[min(index, len(seen) - 1)]
            return {"status": now, "results": results if now == "succeeded" else []}

        def upload(data):
            self.uploads += 1
            return {"uri": f"s3://sources/{self.uploads}", "kind": "source"}

        return happy.Platform(
            upload=upload,
            submit=submit,
            status=status,
            cancel=self.cancelled.append,
            row=lambda job_id: self.rows.get(job_id),
            events=lambda job_id: list(self.events[job_id]),
            executions=lambda job_id: self.executions,
            fetch=lambda uri, identity: self.objects[uri],
            build_info_uri=self.infos.get,
            aggregate_processes=lambda: self.pods,
        )


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(
        happy, "wait", lambda check, t, what: e2e.wait(check, t, what, every_s=0.001)
    )
    monkeypatch.setattr(happy.bundles, "variant", lambda name: name.encode())


@pytest.fixture
def world() -> World:
    return World()


def test_h1_passes_on_a_healthy_cluster(world):
    run = happy.h1(world.platform())
    assert run.job_id == world.job and run.trace_id == TRACE
    assert run.samples == ["accepted", "admitted", "running", "succeeded"]
    assert world.cancelled == []
    assert world.uploads == 2


def test_each_h1_submit_uses_a_fresh_e2e_batch_key(world):
    happy.h1(world.platform())
    key = world.submitted[0][1]
    assert key.startswith("e2e-") and len(key) == len("e2e-") + 12


def test_h1_runs_two_chains_over_distinct_bundles_and_seeds(world):
    happy.h1(world.platform())
    tasks = world.submitted[0][0]["tasks"]
    assert set(tasks) == {"a-build", "a-sim", "b-build", "b-sim", "agg"}
    seeds = [tasks[f"{c}-sim"]["params"]["seed"] for c in "ab"]
    assert len(set(seeds)) == 2
    assert tasks["a-build"]["params"]["source"] != tasks["b-build"]["params"]["source"]


def failing(world, match, mutate):
    """Runs H1 after `mutate` broke one fact of the fake, and expects Failed naming it."""
    mutate(world)
    with pytest.raises(Failed, match=match):
        happy.h1(world.platform())
    assert world.cancelled == [world.submitted[0][1]]


def after_submit(world, change):
    """Applies `change` to the fake once the job exists, before its checks read it."""
    platform = world.platform()
    submit = platform.submit

    def changed(document, key):
        answer = submit(document, key)
        change(world)
        return answer

    platform.submit = changed
    return platform


def broken(world, match, change):
    with pytest.raises(Failed, match=match):
        happy.h1(after_submit(world, change))
    assert world.cancelled == [world.submitted[0][1]]


def test_a_wasm_that_does_not_hash_to_its_address_fails(world):
    def swap(w):
        uri = content_uri(b"wasm-a")
        w.objects[uri] = b"tampered"

    broken(world, "does not hash", swap)


def test_a_build_info_of_another_wasm_fails(world):
    def swap(w):
        uri = w.infos[content_uri(b"wasm-b")]
        w.objects[uri] = json.dumps({"component": {"sha256": "0" * 64}}).encode()

    broken(world, "build-info of b", swap)


def test_a_wasm_without_a_cache_entry_fails(world):
    broken(world, "no compile cache entry", lambda w: w.infos.pop(content_uri(b"wasm-a")))


def test_blocks_of_another_seed_fail(world):
    def other_seed(w):
        job = w.job
        _, results = w.statuses[job]
        sim_b = next(r for r in results if r["producer"]["task"] == "b-sim")
        w.objects[sim_b["uri"]] = results_xml(99)
        sim_b["sha256"] = digest(w.objects[sim_b["uri"]])

    broken(world, "b-sim blocks differ", other_seed)


def test_a_result_that_does_not_hash_to_its_reference_fails(world):
    def corrupt(w):
        _, results = w.statuses[w.job]
        w.objects[results[0]["uri"]] += b" "

    broken(world, "does not hash", corrupt)


def test_a_summary_of_other_seeds_fails(world):
    def other(w):
        _, results = w.statuses[w.job]
        agg = next(r for r in results if r["producer"]["task"] == "agg")
        w.objects[agg["uri"]] = json.dumps(
            jobs.two_seed_summary(happy.WORKLOAD, ARGS, (1, 2))
        ).encode()
        agg["sha256"] = digest(w.objects[agg["uri"]])

    broken(world, "summary.json", other)


def test_a_wasmtime_process_in_the_aggregate_pod_fails(world):
    broken(world, "wasmtime process", lambda w: w.pods.append({"wasmtime", "sh"}))


def test_no_aggregate_pod_to_probe_fails(world):
    broken(world, "no pod of the aggregate pool", lambda w: w.pods.clear())


def test_an_aggregate_on_another_pool_fails(world):
    def moved(w):
        placed = next(e for e in w.events[w.job] if e.task_id == "agg" and "placed" in e.type)
        placed.body = json.dumps({"data": {"queue": naming().pools["simulate"]}})

    broken(world, "want the aggregate pool queue", moved)


def test_an_aggregate_that_started_before_a_simulate_ended_fails(world):
    def early(w):
        end = next(
            e
            for e in w.events[w.job]
            if e.task_id == "b-sim" and e.type == happy.type_of("task.succeeded")
        )
        end.event_time = T0 + timedelta(days=1)

    broken(world, "agg started at", early)


def test_a_simulate_that_started_before_its_compile_ended_fails(world):
    def early(w):
        end = next(
            e
            for e in w.events[w.job]
            if e.task_id == "a-build" and e.type == happy.type_of("task.succeeded")
        )
        end.event_time = T0 + timedelta(days=1)

    broken(world, "before a-build ended", early)


def test_a_cache_hit_in_h1_fails(world):
    def hit(w):
        w.event(w.job, "task.skipped", "a-build", reason="compile-cache-hit", cache_key="0" * 64)

    broken(world, "a-build was a cache hit", hit)


def test_two_chains_on_one_wasm_fail(world):
    def same(w):
        for e in w.events[w.job]:
            if e.task_id == "b-sim" and e.type == happy.type_of("task.requested"):
                e.body = next(
                    x.body
                    for x in w.events[w.job]
                    if x.task_id == "a-sim" and x.type == happy.type_of("task.requested")
                )

    broken(world, "built the same wasm|reads", same)


def test_a_job_that_did_not_succeed_fails(world):
    def failed(w):
        w.statuses[w.job] = (["accepted", "failed"], [])

    broken(world, "ended failed", failed)


def test_a_status_out_of_lifecycle_order_fails(world):
    def reordered(w):
        seen, results = w.statuses[w.job]
        w.statuses[w.job] = (["running", "accepted", "succeeded"], results)

    broken(world, "not in lifecycle order", reordered)


def test_a_status_poll_that_missed_a_short_state_passes(world):
    def coarse(w):
        seen, results = w.statuses[w.job]
        w.statuses[w.job] = (["accepted", "running", "succeeded"], results)

    happy.h1(after_submit(world, coarse))


def test_two_workflows_for_one_job_fail(world):
    broken(world, "2 workflow executions", lambda w: setattr(w, "executions", 2))


def test_a_row_published_before_it_was_accepted_fails(world):
    def late(w):
        w.rows[w.job].created_at = T0 + timedelta(days=2)

    broken(world, "must come before the publish", late)


def test_an_unpublished_row_fails(world):
    broken(
        world, "never confirmed published", lambda w: setattr(w.rows[w.job], "published_at", None)
    )


def test_a_row_missing_fails(world):
    broken(world, "no accepted row", lambda w: w.rows.pop(w.job))


def test_a_row_of_another_trace_fails(world):
    def other(w):
        w.rows[w.job].traceparent = "00-" + "1" * 32 + "-00f067aa0ba902b7-01"

    broken(world, "lacks the trace id", other)


def test_a_status_that_never_ends_times_out_and_cancels(world, monkeypatch):
    monkeypatch.setattr(happy, "H1_TIMEOUT_S", 0.05)

    def hang(w):
        w.statuses[w.job] = (["running"], [])

    with pytest.raises(Failed, match="no end of job .* statuses seen"):
        happy.h1(after_submit(world, hang))
    assert world.cancelled == [world.submitted[0][1]]


def test_a_rejected_submit_fails_before_any_cancel_is_needed(world):
    platform = world.platform()

    def refused(document, key):
        raise Failed("the job was not accepted: {'outcome': 'rejected'}")

    platform.submit = refused
    with pytest.raises(Failed, match="not accepted"):
        happy.h1(platform)


def test_n6_passes_after_h1(world):
    platform = world.platform()
    first = happy.h1(platform)
    run = happy.n6(platform, first)
    assert run.seeds == (happy.HIT_SEED,) and happy.HIT_SEED not in first.seeds
    document, key = world.submitted[1]
    assert document["tasks"]["build"]["params"]["source"] == first.sources[0]
    assert key.startswith("e2e-") and key != first.batch
    assert world.cancelled == []


def n6_broken(world, match, change):
    platform = world.platform()
    first = happy.h1(platform)
    submit = platform.submit

    def changed(document, key):
        answer = submit(document, key)
        change(world)
        return answer

    platform.submit = changed
    with pytest.raises(Failed, match=match):
        happy.n6(platform, first)
    assert world.cancelled == [world.submitted[1][1]]


def test_a_compile_attempt_on_the_cache_hit_fails(world):
    n6_broken(world, "ran a compile attempt", lambda w: w.event(w.job2, "task.started", "build", 1))


def test_a_cache_hit_that_was_not_skipped_fails(world):
    def unskip(w):
        w.events[w.job2] = [e for e in w.events[w.job2] if e.type != happy.type_of("task.skipped")]

    n6_broken(world, "0 task.skipped events", unskip)


def test_a_hit_that_reads_another_wasm_fails(world):
    def other(w):
        for e in w.events[w.job2]:
            if e.task_id == "sim" and e.type == happy.type_of("task.requested"):
                wasm = w.store(b"another wasm")
                e.body = json.dumps({"data": {"inputs": [wasm]}})

    n6_broken(world, "reads wasm", other)


def test_blocks_of_another_seed_on_the_hit_fail(world):
    def other(w):
        _, results = w.statuses[w.job2]
        w.objects[results[0]["uri"]] = results_xml(happy.SEEDS[0])
        results[0]["sha256"] = digest(w.objects[results[0]["uri"]])

    n6_broken(world, "sim blocks differ", other)


def test_an_unknown_event_name_fails_loudly():
    with pytest.raises(Failed, match="not an event type"):
        happy.type_of("job.invented")
