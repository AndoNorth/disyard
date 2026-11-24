"""The status, batch and depth reads on the reader login, over rows and events seeded in Postgres,
with Temporal faked."""

import uuid

import pytest
from dsy_dispatchapi.reads import ReadRefused, batch_summary, depth, job_page, job_status
from dsy_dispatchapi.testing import running_status
from dsy_jobstate import mark_delivered, mark_published, request_action, transition
from dsy_keys import batch_id
from dsy_temporal import trace_id_of

pytestmark = pytest.mark.integration

WORKFLOW = "/dsy/workflow"


def refused(code: str):
    return pytest.raises(ReadRefused, match=f"^{code}: ")


def result_of(row, task: str, attempt: int) -> dict:
    return {
        "result": {
            "uri": f"s3://dsy-results/acme/b/j/7/{task}/r0a{attempt}/result.bin",
            "sha256": "0" * 64,
            "size": attempt,
            "name": "result.bin",
            "kind": "result",
            "producer": {"job": row.job_id, "task": task, "attempt": f"r0a{attempt}"},
        }
    }


def test_status_of_a_row_that_never_started(reader, db, seed, gateway):
    (row,) = seed()
    request_action(db, row.job_id, "cancel")
    gateway.available = False
    answer = job_status(reader, gateway, "acme", row.job_id)
    assert answer.model_dump(include={"job_id", "tenant", "batch_id", "submission_key"}) == {
        "job_id": row.job_id,
        "tenant": "acme",
        "batch_id": row.batch_id,
        "submission_key": "batch/0",
    }
    assert (answer.status, answer.view, answer.requested_action) == (
        "accepted",
        "cancelling",
        "cancel",
    )
    assert (answer.control_seq, answer.control_delivered, answer.published) == (1, False, False)
    assert answer.trace_id == trace_id_of(row.traceparent)
    assert answer.terminal_at is None and answer.created_at <= answer.updated_at
    # A row at accepted has no workflow, so Temporal is not asked and its outage shows nowhere.
    assert (answer.detail, answer.detail_error, answer.results, answer.dead_letter) == (
        None,
        None,
        [],
        None,
    )


def test_status_shows_the_row_and_the_workflow(reader, db, seed, gateway):
    (row,) = seed()
    mark_published(db, row.job_id)
    transition(db, row.job_id, "accepted", "admitted")
    transition(db, row.job_id, "admitted", "running")
    seq = request_action(db, row.job_id, "pause").control_seq
    mark_delivered(db, row.job_id, seq)
    gateway.start(row.job_id)
    answer = job_status(reader, gateway, "acme", row.job_id)
    assert (answer.status, answer.view, answer.control_delivered, answer.published) == (
        "running",
        "pausing",
        True,
        True,
    )
    assert answer.detail == running_status() and answer.detail_error is None


def test_detail_unavailable_when_query_fails(reader, db, seed, gateway):
    rows = seed(count=3)
    for row, to in zip(rows, ("admitted", "paused", "cancelled"), strict=True):
        transition(db, row.job_id, "accepted", to, row_only=to != "admitted")
    admitted, paused, cancelled = (row.job_id for row in rows)
    gateway.start(admitted)

    gateway.available = False
    for job in (admitted, paused):
        answer = job_status(reader, gateway, "acme", job)
        assert (answer.detail, answer.detail_error) == (None, "unavailable")
    ended = job_status(reader, gateway, "acme", cancelled)
    assert (ended.status, ended.detail, ended.detail_error) == ("cancelled", None, None)
    assert ended.terminal_at is not None

    gateway.available = True
    assert job_status(reader, gateway, "acme", admitted).detail == running_status()
    never_started = job_status(reader, gateway, "acme", paused)
    assert (never_started.detail, never_started.detail_error) == (None, "no-workflow")


def test_results_in_task_order(reader, seed, record, gateway):
    (row,) = seed()
    # Stored out of task order, with task "a" producing a second result after a retry.
    for task, attempt, minutes in (("c", 1, 1), ("a", 1, 2), ("b", 1, 3), ("a", 2, 4)):
        record(
            row,
            "task.result.ready",
            result_of(row, task, attempt),
            minutes=minutes,
            source=WORKFLOW,
            taskid=task,
            attempt=attempt,
        )
    record(
        row,
        "job.accepted",
        {
            "manifest": row.manifest_ref,
            "batch_id": row.batch_id,
            "submission_key": row.submission_key,
        },
        source="/dsy/dispatch",
    )
    results = job_status(reader, gateway, "acme", row.job_id).results
    assert [(r.producer.task, r.producer.attempt) for r in results] == [
        ("a", "r0a1"),
        ("a", "r0a2"),
        ("b", "r0a1"),
        ("c", "r0a1"),
    ]
    assert results[0].model_dump(mode="json") == result_of(row, "a", 1)["result"]


def test_dead_letter_field(reader, seed, record, gateway):
    clean, lettered = seed(count=2)
    for minutes, reason in ((1, "MALFORMED"), (5, "UNSUPPORTED_VERSION")):
        data = {"dlq_reason": reason, "detail": f"refused at minute {minutes}"}
        record(lettered, "job.dead.lettered", data, minutes=minutes, source="/dsy/admission")
    assert job_status(reader, gateway, "acme", clean.job_id).dead_letter is None
    letter = job_status(reader, gateway, "acme", lettered.job_id).dead_letter
    assert (letter.dlq_reason, letter.detail) == ("UNSUPPORTED_VERSION", "refused at minute 5")
    assert letter.time.isoformat() == "2026-11-18T12:05:00+00:00"
    # The row stays at accepted, so the field is what tells the job from a queued one.
    assert job_status(reader, gateway, "acme", lettered.job_id).status == "accepted"


def test_batch_summary_counts_by_status(reader, db, seed):
    rows = seed(count=4)
    transition(db, rows[0].job_id, "accepted", "admitted")
    transition(db, rows[1].job_id, "accepted", "cancelled", row_only=True)
    summary = batch_summary(reader, "acme", "batch")
    assert summary.batch_id == batch_id("acme", "batch")
    assert (summary.total, summary.counts) == (4, {"accepted": 2, "admitted": 1, "cancelled": 1})
    with refused("BATCH_NOT_FOUND"):
        batch_summary(reader, "acme", "absent")


def test_page_is_ordered_and_bounded(reader, db, seed):
    rows = seed(count=25)
    ids = sorted(row.job_id for row in rows)
    pages, after = [], None
    while True:
        query = {"limit": "10"} | ({"after": after} if after else {})
        page = job_page(reader, "acme", "batch", query)
        pages.append(len(page.jobs))
        assert [job.job_id for job in page.jobs] == ids[sum(pages[:-1]) : sum(pages)]
        if (after := page.next) is None:
            break
    assert pages == [10, 10, 5]
    # A page that ends exactly at the last job has no next page.
    assert job_page(reader, "acme", "batch", {"limit": "25"}).next is None
    assert len(job_page(reader, "acme", "batch", {}).jobs) == 25

    request_action(db, ids[3], "pause")
    transition(db, ids[3], "accepted", "paused", row_only=True)
    request_action(db, ids[7], "cancel")
    paused = job_page(reader, "acme", "batch", {"status": "paused"}).jobs
    assert [(job.job_id, job.status, job.view) for job in paused] == [(ids[3], "paused", None)]
    cancelling = job_page(reader, "acme", "batch", {"after": ids[6], "limit": "1"}).jobs
    assert [(job.job_id, job.view) for job in cancelling] == [(ids[7], "cancelling")]
    # A filter or a cursor past the end gives an empty page of a batch that exists.
    assert job_page(reader, "acme", "batch", {"status": "failed"}).jobs == []
    assert job_page(reader, "acme", "batch", {"after": ids[-1]}).jobs == []

    for query in (
        {"limit": "0"},
        {"limit": "1001"},
        {"limit": "ten"},
        {"limit": "-1"},
        {"status": "pausing"},
        {"after": "not-a-job"},
        {"after": ids[0].upper()},
        {"order": "desc"},
    ):
        with refused("INVALID_QUERY"):
            job_page(reader, "acme", "batch", query)
    with refused("BATCH_NOT_FOUND"):
        job_page(reader, "acme", "absent", {})


def test_depth_counts_accepted_by_class(reader, db, seed):
    assert depth(reader, "acme").depth == {}
    seed("short-jobs", count=3)
    admitted = seed("long-jobs", count=2, job_class="long")
    transition(db, admitted[0].job_id, "accepted", "admitted")
    seed("long-jobs", count=1, job_class="long", start=2)
    seed("elsewhere", count=4, tenant="other")
    assert depth(reader, "acme").depth == {"standard": {"short": 3, "long": 2}}


def test_reads_are_tenant_scoped(reader, seed, gateway):
    (theirs,) = seed("shared", tenant="other")
    seed("shared", count=2)
    with refused("JOB_NOT_FOUND"):
        job_status(reader, gateway, "acme", theirs.job_id)
    for absent in (str(uuid.uuid4()), "not-a-job"):
        with refused("JOB_NOT_FOUND"):
            job_status(reader, gateway, "acme", absent)
    assert job_status(reader, gateway, "other", theirs.job_id).tenant == "other"
    # The same batch key names a different batch in each tenant.
    assert batch_summary(reader, "acme", "shared").total == 2
    assert batch_summary(reader, "other", "shared").total == 1
    assert theirs.job_id not in {j.job_id for j in job_page(reader, "acme", "shared", {}).jobs}
    assert len(job_page(reader, "other", "shared", {}).jobs) == 1
    with refused("BATCH_NOT_FOUND"):
        job_page(reader, "nobody", "shared", {})
    assert depth(reader, "other").depth == {"standard": {"short": 1}}
