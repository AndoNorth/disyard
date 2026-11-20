"""The job rows against the canned answers of the healthy cluster in conftest, each test changing
the job's status samples, its stored objects or the dead-letter count."""

import os
import signal

from wdf_acceptance import smoke


def run(capsys) -> tuple[int, list[str], str]:
    code = smoke.main()
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def cancels(healthy) -> list[tuple]:
    return [call for call in healthy.job.calls if call[0] == "cancel"]


def test_job_rows_all_ok(healthy, canned, capsys):
    code, lines, err = run(capsys)
    batch = healthy.job.calls[1][1]
    assert lines[5:10] == [
        f"ok job-submit accepted {canned.JOB_ID}",
        "ok job-run succeeded, build ran (attempt 1)",
        f"ok job-result echo result matches, digest: {canned.DIGEST}",
        "ok job-aggregate summary present and matching",
        "ok dead-letters 0, no rise",
    ]
    assert batch.startswith("smoke-") and len(batch) == len("smoke-") + 12
    assert lines[10].startswith("smoke ci: 10 ok, 0 failed, 0 skipped, ")
    assert cancels(healthy) == []
    assert code == 0


def test_job_run_names_a_cache_hit(healthy, canned, capsys):
    cached = {**canned.RUNNING, "detail": {"tasks": [canned.task("build", "skipped", 0)]}}
    healthy.job.statuses[:] = [cached, canned.SUCCEEDED]
    _, lines, _ = run(capsys)
    assert lines[6] == "ok job-run succeeded, build hit the cache (attempt 0)"


def test_job_run_without_a_running_sample_of_the_build(healthy, canned, capsys):
    building = {**canned.RUNNING, "detail": {"tasks": [canned.task("build", "running", 1)]}}
    healthy.job.statuses[:] = [building, canned.SUCCEEDED]
    code, lines, err = run(capsys)
    assert lines[6] == "FAIL job-run ended succeeded, but no running sample showed build done"
    assert lines[7:10] == [
        "skip job-result after job-run failed",
        "skip job-aggregate after job-run failed",
        "ok dead-letters 0, no rise",
    ]
    assert cancels(healthy) == [("cancel", healthy.job.calls[1][1])]
    assert "cleanup: cancelled batch smoke-" in err
    assert lines[10].startswith("smoke ci: 7 ok, 1 failed, 2 skipped, ")
    assert code == 1


def test_job_run_that_ends_failed(healthy, canned, capsys):
    healthy.job.statuses[:] = [
        canned.RUNNING,
        {**canned.SUCCEEDED, "status": "failed", "results": []},
    ]
    code, lines, _ = run(capsys)
    assert lines[6] == "FAIL job-run ended failed"
    assert len(cancels(healthy)) == 1
    assert code == 1


def test_job_result_sha256_mismatch(healthy, canned, capsys):
    sim = canned.SUCCEEDED["results"][0]["uri"]
    healthy.job.objects[sim] += b" "
    code, lines, _ = run(capsys)
    assert lines[7].startswith(f"FAIL job-result {sim} has sha256 ")
    assert lines[8] == "skip job-aggregate after job-result failed"
    assert len(cancels(healthy)) == 1
    assert code == 1


def test_job_result_without_the_known_answer(healthy, canned, capsys):
    wrong = canned.results_xml(7)
    ended = {
        **canned.SUCCEEDED,
        "results": [canned.reference("sim", "result", "results.xml", wrong)],
    }
    healthy.job.statuses[:] = [canned.RUNNING, ended]
    healthy.job.objects[ended["results"][0]["uri"]] = wrong
    _, lines, _ = run(capsys)
    assert (
        lines[7] == "FAIL job-result wdf_kernel.check exited 1: differs: the seed is 7, expected 42"
    )


def test_job_aggregate_missing(healthy, canned, capsys):
    healthy.job.statuses[:] = [
        canned.RUNNING,
        {**canned.SUCCEEDED, "results": canned.SUCCEEDED["results"][:1]},
    ]
    _, lines, _ = run(capsys)
    assert lines[8] == "FAIL job-aggregate no result of agg in the status"
    assert len(cancels(healthy)) == 1


def test_job_dead_letters_above_the_baseline(healthy, canned, capsys):
    healthy.job.dead_letters[:] = [2, 3]
    code, lines, _ = run(capsys)
    assert lines[2] == "ok broker 2 dead letters before the job"
    assert lines[9] == "FAIL dead-letters 3, above 2 before the job"
    assert len(cancels(healthy)) == 1
    assert code == 1


def test_job_submit_refused_cancels_nothing(healthy, canned, capsys, monkeypatch):
    def refused(self, document, batch_key):
        raise smoke.api.Failed("the job was not accepted: {'outcome': 'rejected'}")

    monkeypatch.setattr(smoke.api.Api, "submit_one", refused)
    code, lines, _ = run(capsys)
    assert lines[5] == "FAIL job-submit the job was not accepted: {'outcome': 'rejected'}"
    assert lines[6:10] == [
        f"skip {name} after job-submit failed"
        for name in ("job-run", "job-result", "job-aggregate", "dead-letters")
    ]
    assert cancels(healthy) == []
    assert code == 1


def test_job_sigint_while_it_runs(healthy, canned, capsys):
    def interrupted() -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return canned.RUNNING

    healthy.job.statuses[:] = [canned.RUNNING, interrupted, canned.RUNNING]
    code, lines, err = run(capsys)
    assert lines[6] == "FAIL budget 300 s ran out in job-run"
    assert cancels(healthy) == [("cancel", healthy.job.calls[1][1])]
    assert "cleanup: cancelled batch smoke-" in err
    assert lines[7].startswith("smoke ci: 6 ok, 1 failed, 0 skipped, ")
    assert code == 1
