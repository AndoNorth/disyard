"""The verdicts and the runners of N1 and N2, against canned statuses and events, so a wrong
outcome is shown to fail without a cluster."""

import json
from dataclasses import dataclass

import pytest
from dsy_acceptance.e2e import negatives

JOB = "0b6e2f0a-5d1c-4f43-9a51-6b1f0d3c2e77"


@dataclass
class Row:
    type: str
    task_id: str | None
    attempt: int | None
    body: str


def ev(type_: str, task: str | None = None, attempt: int | None = 1, **data) -> Row:
    return Row(f"dev.dsy.{type_}.v1", task, attempt, json.dumps({"data": data}))


def ref(task: str) -> dict:
    return {"producer": {"task": task}}


COMPILE = "compile_failed: entry module app has no attribute 'Run'"
CHAIN_A = [ev("task.succeeded", t) for t in ("a-build", "a-sim")]
B_FAILED = [
    ev("task.attempt.failed", "b-build", reason=COMPILE),
    ev("task.failed", "b-build", reason=COMPILE),
]
FAILED_STATUS = {"status": "failed", "results": [ref("a-sim")]}
FAILED = [*CHAIN_A, *B_FAILED, ev("job.failed", None, None, reason="retries-exhausted: b-build")]
PARTIAL_STATUS = {"status": "partial", "results": [ref("a-sim"), ref("agg")]}
PARTIAL = [
    *CHAIN_A,
    *B_FAILED,
    ev("task.succeeded", "agg"),
    ev("job.partial", None, None, skipped_tasks=["b-build", "b-sim"]),
]
PARSE = "aggregate_parse_failed: sim: results.xml: scalar row misses column value"
BAD_STATUS = {"status": "failed", "results": [ref("sim")]}
BAD = [
    ev("task.succeeded", "sim"),
    ev("task.attempt.failed", "agg", reason=PARSE),
    ev("task.failed", "agg", reason=PARSE),
    ev("job.failed", None, None, reason="retries-exhausted: agg"),
]


def test_canonical_outcomes_pass():
    negatives.check_failing_compile(FAILED_STATUS, FAILED)
    negatives.check_partial(PARTIAL_STATUS, PARTIAL)
    negatives.check_malformed_block(BAD_STATUS, BAD)


def drop(events: list, type_: str, task: str | None = None) -> list:
    return [e for e in events if not (negatives.name_of(e.type) == type_ and e.task_id == task)]


@pytest.mark.parametrize(
    ("status", "events", "match"),
    [
        (
            FAILED_STATUS,
            [*FAILED, ev("task.attempt.failed", "b-build", 2, reason=COMPILE)],
            "retried",
        ),
        (FAILED_STATUS, [*FAILED, ev("task.succeeded", "agg")], "aggregate ran"),
        ({**FAILED_STATUS, "results": [ref("a-sim"), ref("agg")]}, FAILED, "aggregate ran"),
        ({**FAILED_STATUS, "status": "succeeded"}, FAILED, "not failed"),
        ({**FAILED_STATUS, "status": "partial"}, FAILED, "not failed"),
        (FAILED_STATUS, drop(FAILED, "job.failed"), "0 job.failed"),
        (
            FAILED_STATUS,
            [*drop(FAILED, "job.failed"), ev("job.failed", None, None, reason="x")],
            "typed",
        ),
        (FAILED_STATUS, drop(FAILED, "task.succeeded", "a-sim"), "chain a did not finish"),
        ({**FAILED_STATUS, "results": []}, FAILED, "not kept"),
    ],
)
def test_failing_compile_verdicts(status, events, match):
    with pytest.raises(AssertionError, match=match):
        negatives.check_failing_compile(status, events)


@pytest.mark.parametrize(
    ("status", "events", "match"),
    [
        ({**PARTIAL_STATUS, "status": "failed"}, PARTIAL, "not partial"),
        (PARTIAL_STATUS, drop(PARTIAL, "task.succeeded", "agg"), "did not run"),
        ({**PARTIAL_STATUS, "results": [ref("a-sim")]}, PARTIAL, "did not run"),
        (PARTIAL_STATUS, [*PARTIAL, ev("job.failed", None, None, reason="x: y")], "also wrote"),
        (
            PARTIAL_STATUS,
            drop(PARTIAL, "job.partial") + [ev("job.partial", skipped_tasks=[])],
            "skipped",
        ),
    ],
)
def test_partial_verdicts(status, events, match):
    with pytest.raises(AssertionError, match=match):
        negatives.check_partial(status, events)


@pytest.mark.parametrize(
    ("status", "events", "match"),
    [
        (BAD_STATUS, [*BAD, ev("task.attempt.failed", "agg", 2, reason=PARSE)], "retried"),
        (
            BAD_STATUS,
            [
                *drop(BAD, "task.failed", "agg"),
                ev("task.failed", "agg", reason="aggregate_failed: x"),
            ],
            "not aggregate_parse_failed",
        ),
        (
            BAD_STATUS,
            [
                *drop(BAD, "task.failed", "agg"),
                ev("task.failed", "agg", reason="aggregate_parse_failed: results.xml bad"),
            ],
            "does not name its input",
        ),
        (
            BAD_STATUS,
            [*drop(BAD, "task.failed", "agg"), ev("task.failed", "agg", reason="")],
            "no typed reason",
        ),
        ({**BAD_STATUS, "status": "partial"}, BAD, "not failed"),
        (BAD_STATUS, drop(BAD, "task.succeeded", "sim"), "simulate did not finish"),
    ],
)
def test_malformed_block_verdicts(status, events, match):
    with pytest.raises(AssertionError, match=match):
        negatives.check_malformed_block(status, events)


class FakeApi:
    """Answers statuses per job, and records what it was asked."""

    def __init__(self, statuses: list[dict]):
        self.uploads: list[bytes] = []
        self.submitted: list[tuple[dict, str]] = []
        self.statuses = statuses
        self.polls = 0

    def upload_source(self, data: bytes) -> dict:
        self.uploads.append(data)
        return {"uri": f"s3://dsy-sources/sha256/{len(self.uploads):064x}"}

    def submit_one(self, document: dict, batch: str) -> str:
        self.submitted.append((document, batch))
        return JOB

    def status(self, job_id: str) -> dict:
        self.polls += 1
        return {"job_id": job_id, **self.statuses[min(self.polls, len(self.statuses)) - 1]}


def keys():
    n = iter(range(10))
    return lambda: f"e2e-{next(n):012x}"


def test_n1_runs_two_jobs_with_fresh_batches_and_the_partial_variant_second():
    client = FakeApi([])
    answers = iter([FAILED, PARTIAL])
    statuses = iter([FAILED_STATUS, PARTIAL_STATUS])
    client.status = lambda job_id: {"job_id": job_id, **next(statuses)}
    negatives.failing_compile(client, lambda job_id: next(answers), keys(), timeout_s=5)
    assert [b for _, b in client.submitted] == ["e2e-000000000000", "e2e-000000000001"]
    partial = [d["tasks"]["agg"]["allow_partial"] for d, _ in client.submitted]
    assert partial == [False, True]
    assert len(client.uploads) == 2


def test_n1_checks_the_partial_variant():
    client = FakeApi([FAILED_STATUS])
    with pytest.raises(AssertionError, match="not partial"):
        negatives.failing_compile(
            client, lambda job_id: FAILED if client.submitted else [], keys(), 5
        )
    assert len(client.submitted) == 2


def test_n2_submits_the_bad_block_fault_and_checks_it():
    client = FakeApi([BAD_STATUS])
    negatives.malformed_block(client, lambda job_id: BAD, keys(), timeout_s=5)
    document, batch = client.submitted[0]
    assert document["tasks"]["sim"]["params"]["args"] == {"mode": "bad_block"}
    assert batch.startswith("e2e-")


def test_a_job_that_never_ends_times_out_naming_its_last_status():
    client = FakeApi([{"status": "running"}])
    with pytest.raises(TimeoutError, match="did not end in 0 s, last running"):
        negatives.run_to_end(client, {}, "e2e-000000000000", 0)
