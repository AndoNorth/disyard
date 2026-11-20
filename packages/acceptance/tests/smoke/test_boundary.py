"""The boundary rows against canned answers: a script for cluster-verify, a fake kubectl whose
port-forwards serve canned answers, in-test API listeners and stand-ins for the broker and Temporal
clients, so no cluster is touched."""

import time

import pytest
from wdf_acceptance import smoke
from wdf_acceptance.clients import broker
from wdf_clusterkit import Refused
from wdf_workflow.harness.cluster import CurrentBuild

VERIFIED = "ok preset fine\nok ports every host port listens on 127.0.0.1 only\n"
COUNTED = "cluster-verify ci: 10 ok, 0 failed\n"
POOLS = {"pools": {"compile": {}, "simulate": {}, "aggregate": {}}}
READY = {"status": "ready"}
JOB_SKIPS = [
    "skip job-submit job rows not written yet",
    "skip job-run job rows not written yet",
    "skip job-result job rows not written yet",
    "skip job-aggregate job rows not written yet",
    "skip dead-letters job rows not written yet",
]


class Service:
    async def describe_namespace(self, request):
        assert request.namespace == "wdf"


class Client:
    namespace = "wdf"
    workflow_service = Service()


@pytest.fixture
def healthy(verify, forwards, listener, monkeypatch):
    """Every layer answers as a healthy ci cluster does, the broker with no dead letter and
    Temporal with build exit-a current and deployed. Returns the fake kubectl's call log."""
    verify(0, VERIFIED + COUNTED)
    log = forwards({"svc/placement": [200, POOLS]})
    monkeypatch.setattr(smoke, "DISPATCH", listener(200, READY))
    monkeypatch.setattr(smoke, "CONTROL_API", listener(200, READY))
    monkeypatch.setattr(broker, "dead_letters", lambda address, timeout_s: 0)

    async def connect(address):
        return Client()

    async def current(client, context, timeout_s):
        assert (context, timeout_s) == ("kind-wdf", smoke.POLLERS_S)
        return CurrentBuild("exit-a", ("exit-a",), None)

    monkeypatch.setattr(smoke.temporal, "connect", connect)
    monkeypatch.setattr(smoke, "make_deployed_current", current)
    monkeypatch.delenv("WDF_PRESET", raising=False)
    return log


def run(capsys) -> tuple[int, list[str]]:
    code = smoke.main()
    return code, capsys.readouterr().out.splitlines()


def test_boundary_rows_all_ok(healthy, capsys):
    code, lines = run(capsys)
    assert lines[0].startswith("ok cluster-verify ci: 10 ok, 0 failed, ")
    assert lines[1:5] == [
        "ok api dispatch and control /readyz ready",
        "ok broker 0 dead letters before the job",
        "ok temporal namespace wdf, build exit-a current, deployed exit-a",
        "ok placement 3 pools",
    ]
    assert lines[5:10] == JOB_SKIPS
    assert lines[10].startswith("smoke ci: 5 ok, 0 failed, 5 skipped, ")
    # The job rows are not ok, so the gate does not pass yet.
    assert code == 1


def test_boundary_forwards_reach_the_layers(healthy, capsys):
    run(capsys)
    forwarded = [line.split()[1:] for line in healthy.read_text().splitlines()]
    assert [(ns, args[-2], args[-1].split(":")[1]) for ns, *args in forwarded] == [
        ("--namespace=wdf-infra", "svc/rabbitmq", "5672"),
        ("--namespace=wdf-infra", "svc/temporal-frontend", "7233"),
        ("--namespace=wdf-control", "svc/placement", "8080"),
    ]


@pytest.mark.parametrize("code", [2, 4])
def test_boundary_refusal_or_usage_ends_the_run(healthy, verify, capsys, code):
    verify(code, stderr="refused: no kind cluster named wdf, start one with: just up\n")
    assert smoke.main() == code
    out, err = capsys.readouterr()
    assert out == ""
    assert healthy.read_text() == ""


def test_boundary_cluster_verify_failure_names_its_fail_lines(healthy, verify, capsys):
    verify(1, "ok preset fine\nFAIL ready not ready: placement\nFAIL pressure x OOMKilled\n")
    code, lines = run(capsys)
    assert lines[0] == "FAIL cluster-verify ready not ready: placement; pressure x OOMKilled"
    # The other boundary rows still run, and the job rows wait on a boundary all ok.
    assert lines[1:5] == [
        "ok api dispatch and control /readyz ready",
        "ok broker 0 dead letters before the job",
        "ok temporal namespace wdf, build exit-a current, deployed exit-a",
        "ok placement 3 pools",
    ]
    assert lines[5] == "skip job-submit after cluster-verify failed"
    assert code == 1


def test_boundary_cluster_verify_past_its_deadline(healthy, verify, capsys, monkeypatch):
    monkeypatch.setattr(smoke, "VERIFY_S", 1.0)
    verify(0, VERIFIED, sleep_s=30)
    started = time.monotonic()
    code, lines = run(capsys)
    assert lines[0] == (
        "FAIL cluster-verify did not finish in 1 s, "
        "last: ok ports every host port listens on 127.0.0.1 only"
    )
    assert time.monotonic() - started < 10
    assert code == 1


def test_boundary_forward_that_never_listens(healthy, forwards, capsys, monkeypatch):
    monkeypatch.setattr(smoke, "PROBE_S", 2.0)
    forwards({"svc/placement": [200, POOLS], "svc/rabbitmq": "never"})
    printed = []
    add = smoke.Gate.add

    def timed(gate, row):
        printed.append(time.monotonic())
        add(gate, row)

    monkeypatch.setattr(smoke.Gate, "add", timed)
    code, lines = run(capsys)
    assert lines[2] == (
        "FAIL broker kubectl --context=kind-wdf --namespace=wdf-infra port-forward "
        "--address=127.0.0.1 svc/rabbitmq did not listen in 2 s"
    )
    assert printed[2] - printed[1] < smoke.PROBE_S + 1
    assert lines[3].startswith("ok temporal")
    assert lines[4] == "ok placement 3 pools"
    assert lines[5:10] == [
        line.replace("job rows not written yet", "after broker failed") for line in JOB_SKIPS
    ]
    assert code == 1


def test_boundary_dead_letters_before_the_job_pass(healthy, capsys, monkeypatch):
    monkeypatch.setattr(broker, "dead_letters", lambda address, timeout_s: 3)
    _, lines = run(capsys)
    assert lines[2] == "ok broker 3 dead letters before the job"


def test_boundary_api_not_ready(healthy, listener, capsys, monkeypatch):
    monkeypatch.setattr(smoke, "CONTROL_API", listener(503, {"status": "not ready"}))
    _, lines = run(capsys)
    assert lines[1] == 'FAIL api control /readyz answered 503 {"status": "not ready"}'


def test_boundary_api_not_listening(healthy, capsys, monkeypatch):
    monkeypatch.setattr(smoke, "DISPATCH", "http://127.0.0.1:1")
    _, lines = run(capsys)
    assert lines[1].startswith("FAIL api dispatch /readyz: <urlopen error ")


def test_boundary_temporal_makes_the_build_current(healthy, capsys, monkeypatch):
    async def made(client, context, timeout_s):
        return CurrentBuild("exit-a", ("exit-a",), "exit-a")

    monkeypatch.setattr(smoke, "make_deployed_current", made)
    _, lines = run(capsys)
    assert (
        lines[3]
        == "ok temporal namespace wdf, build exit-a current (made current), deployed exit-a"
    )


def test_boundary_temporal_refused(healthy, capsys, monkeypatch):
    async def refused(client, context, timeout_s):
        raise Refused("build old is current, but the deployed builds are ['new']")

    monkeypatch.setattr(smoke, "make_deployed_current", refused)
    _, lines = run(capsys)
    assert lines[3] == "FAIL temporal build old is current, but the deployed builds are ['new']"


def test_boundary_placement_missing_a_pool(healthy, forwards, capsys):
    forwards({"svc/placement": [200, {"pools": {"compile": {}, "simulate": {}}}]})
    _, lines = run(capsys)
    assert lines[4] == (
        "FAIL placement /pools answered 200 with pools ['compile', 'simulate'], "
        "want ['aggregate', 'compile', 'simulate']"
    )


def test_boundary_budget_names_the_row_it_ran_out_in(healthy, capsys, monkeypatch):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(smoke, "placement", interrupted)
    code, lines = run(capsys)
    assert lines[-2] == "FAIL budget 300 s ran out in placement"
    assert lines[-1].startswith("smoke ci: 4 ok, 1 failed, 0 skipped, ")
    assert code == 1
