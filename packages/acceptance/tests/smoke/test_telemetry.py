"""Row 11, the telemetry row, over `FakeObs` standing behind every forward to the stack, each test
changing what the fake stack shows."""

import time

import pytest
from dsy_acceptance import smoke
from dsy_acceptance.smoke import telemetry
from dsy_obs.targets import JOBS

PASS = (
    f"ok telemetry: {len(JOBS)} scrape jobs up, logs of 6 services, trace of 3 services, 8 profiles"
)


def run(capsys) -> tuple[int, list[str]]:
    code = smoke.main()
    return code, capsys.readouterr().out.splitlines()


def asked(fake) -> list[str]:
    """The LogQL queries the row sent, in order."""
    return [r.query["query"] for r in fake.received if r.path == "/loki/api/v1/query_range"]


def test_telemetry_passes_when_the_stack_shows_everything(fake_obs, capsys):
    code, lines = run(capsys)
    assert lines[10] == PASS
    assert code == 0


def test_telemetry_queries_are_bound_by_stream_and_time(fake_obs, capsys, canned):
    run(capsys)
    queries = asked(fake_obs)
    assert len(queries) == 6
    assert all(q.startswith('{service_name="') for q in queries)
    assert all(f'| job_id="{canned.JOB_ID}"' in q for q in queries)
    bounded = [r for r in fake_obs.received if r.path == "/loki/api/v1/query_range"]
    # Every window opens at the start of the run, so none scans back through old logs.
    assert all(time.time() - int(r.query["start"]) / 1e9 < 60 for r in bounded)


def test_telemetry_reads_the_trace_of_the_accepted_line(fake_obs, capsys, canned):
    run(capsys)
    traced = [r.path for r in fake_obs.received if r.path.startswith("/api/traces/")]
    assert traced and set(traced) == {f"/api/traces/{canned.TRACE_ID}"}


def test_telemetry_with_a_cached_compile_asks_for_no_compile_worker(
    fake_obs, healthy, capsys, canned
):
    canned.seed_obs(fake_obs, compiled=False)
    cached = {**canned.RUNNING, "detail": {"tasks": [canned.task("build", "skipped", 0)]}}
    healthy.job.statuses[:] = [cached, canned.SUCCEEDED]
    code, lines = run(capsys)
    assert lines[6] == "ok job-run succeeded, build hit the cache (attempt 0)"
    assert lines[10] == (
        f"ok telemetry: {len(JOBS)} scrape jobs up, logs of 5 services, "
        "trace of 3 services, 7 profiles"
    )
    assert not any("compile-worker" in q for q in asked(fake_obs))
    assert code == 0


def test_telemetry_a_target_down_names_its_job(fake_obs, capsys):
    fake_obs.vectors["up"][0]["value"][1] = "0"
    down = fake_obs.vectors["up"][0]["metric"]["job"]
    code, lines = run(capsys)
    assert lines[10].startswith(f"FAIL telemetry: after 1 s: target {down} is down")
    assert code == 1


def test_telemetry_a_missing_and_an_unknown_scrape_job(fake_obs, capsys):
    gone = fake_obs.vectors["up"].pop()["metric"]["job"]
    fake_obs.vectors["up"].append({"metric": {"job": "stray"}, "value": [0, "1"]})
    _, lines = run(capsys)
    assert f"no target of job {gone}" in lines[10]
    assert "target job stray is not a scrape job of the stack" in lines[10]


def test_telemetry_a_component_without_a_log_line_names_it(fake_obs, capsys, canned):
    del fake_obs.streams[telemetry.log_query("aggregate-worker", canned.JOB_ID)]
    code, lines = run(capsys)
    assert lines[10] == "FAIL telemetry: after 1 s: no log line of aggregate-worker"
    assert code == 1


def test_telemetry_without_the_accepted_line_names_dispatch_and_the_spans(fake_obs, capsys, canned):
    del fake_obs.streams[telemetry.accepted_query(canned.JOB_ID)]
    _, lines = run(capsys)
    assert lines[10] == (
        "FAIL telemetry: after 1 s: no log line of dispatch; "
        "no span of dispatch, there is no trace id; "
        "no span of admission, there is no trace id; "
        "no span of workflow, there is no trace id"
    )


def test_telemetry_a_trace_without_a_service_names_it_and_the_trace(fake_obs, capsys, canned):
    fake_obs.traces[canned.TRACE_ID] = canned.obs_trace("dispatch", "admission")
    _, lines = run(capsys)
    assert lines[10] == (
        f"FAIL telemetry: after 1 s: no span of workflow in trace {canned.TRACE_ID}"
    )


def test_telemetry_a_trace_tempo_does_not_know_names_each_service(fake_obs, capsys, canned):
    del fake_obs.traces[canned.TRACE_ID]
    _, lines = run(capsys)
    assert lines[10].count("in trace") == 3
    assert "no span of dispatch in trace" in lines[10]
    assert "HTTP 404" in lines[10]


def test_telemetry_a_component_without_a_profile_names_it(fake_obs, capsys):
    fake_obs.services.remove("dsy.placement")
    _, lines = run(capsys)
    assert lines[10] == "FAIL telemetry: after 1 s: no profile of placement"


def test_telemetry_waits_for_the_signals_within_the_deadline(fake_obs, capsys, monkeypatch):
    monkeypatch.setattr(telemetry, "DEADLINE_S", 30.0)
    profiles = list(fake_obs.services)
    fake_obs.services = []
    polls = []
    profiled = telemetry.query.profiled

    def late(address, start, end):
        polls.append(1)
        if len(polls) == 3:
            fake_obs.services = profiles
        return profiled(address, start, end)

    monkeypatch.setattr(telemetry.query, "profiled", late)
    code, lines = run(capsys)
    assert lines[10] == PASS
    assert len(polls) == 3
    assert code == 0


def test_telemetry_off_checks_nothing(healthy, record, capsys):
    record("off")
    code, lines = run(capsys)
    assert lines[10] == "ok telemetry: observability off, not checked"
    assert code == 0


def test_telemetry_with_no_record_reads_off(healthy, capsys):
    _, lines = run(capsys)
    assert lines[10] == "ok telemetry: observability off, not checked"


@pytest.mark.parametrize(
    ("failing", "name"), [("cluster-verify", "cluster-verify"), ("job-submit", "job-submit")]
)
def test_telemetry_skips_after_row_1_or_6_failed(
    fake_obs, verify, capsys, monkeypatch, failing, name
):
    if failing == "cluster-verify":
        verify(1, "FAIL ready not ready: placement\n")
    else:

        def refused(self, document, batch_key):
            raise smoke.api.Failed("not accepted")

        monkeypatch.setattr(smoke.api.Api, "submit_one", refused)
    code, lines = run(capsys)
    assert f"skip telemetry: after {name} failed" in lines
    assert not [r for r in fake_obs.received if r.path.startswith("/api/v1/query")]
    assert code == 1


def test_telemetry_reserve_is_90_s_with_the_record_on_and_30_s_off(
    fake_obs, healthy, canned, record, capsys, monkeypatch
):
    waited = []
    until = smoke.until

    async def noted(check, timeout_s, what):
        waited.append(timeout_s)
        return await until(check, timeout_s, what)

    monkeypatch.setattr(smoke, "until", noted)
    run(capsys)
    healthy.job.statuses[:] = [canned.ADMITTED, canned.RUNNING, canned.SUCCEEDED]
    record("off")
    run(capsys)
    on, off = waited
    assert smoke.BUDGET_S - 90.0 - 5 < on <= smoke.BUDGET_S - 90.0
    assert smoke.BUDGET_S - 30.0 - 5 < off <= smoke.BUDGET_S - 30.0


def test_telemetry_budget_that_runs_out_in_the_row_names_it(fake_obs, capsys, monkeypatch):
    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(telemetry, "check", interrupted)
    code, lines = run(capsys)
    assert lines[10] == "FAIL budget 300 s ran out in telemetry:"
    assert code == 1
