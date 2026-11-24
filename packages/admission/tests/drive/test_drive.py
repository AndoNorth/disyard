"""The driver's own checks, with no cluster: its command line, the counters it prints and the
verdict it draws from them, the metrics it reads across restarts of the admission pod, the
environment it puts on the deployment, the refused messages it publishes, and its refusal of a
cluster it cannot reach."""

import json
import os
import subprocess
import sys

import pytest
from dsy_admission.drive import (
    CLEARED,
    Counters,
    Tally,
    accepted,
    env_args,
    malformed,
    parse_metrics,
    split,
    unsupported,
)
from dsy_admission.drive.__main__ import main, parser
from dsy_compat import read_event
from dsy_events import EventError
from dsy_workflow.harness import source_bundle

METRICS = """\
# HELP dsy_admission_skipped_total dsy_admission_skipped_total
# TYPE dsy_admission_skipped_total counter
dsy_admission_skipped_total{reason="outstanding-action",service_name="temporal-core"} 3
dsy_admission_skipped_total{reason="started",service_name="temporal-core"} 1
dsy_admission_duplicates_total{service_name="temporal-core"} 0
dsy_admission_wait_seconds_bucket{priority_class="standard",le="0.1"} 4
temporal_request{operation="StartWorkflowExecution"} 9
"""


def key(name: str, **labels: str):
    return name, tuple(sorted({"service_name": "temporal-core", **labels}.items()))


def test_parses_the_journeys():
    args = parser().parse_args(
        ["--kube-context", "kind-dsy", "smoke", "--jobs", "500", "--global-cap", "20"]
    )
    assert (args.kube_context, args.journey, args.jobs, args.global_cap) == (
        "kind-dsy",
        "smoke",
        500,
        20,
    )
    assert parser().parse_args(["--kube-context", "k", "control"]).jobs == 20
    assert parser().parse_args(["--kube-context", "k", "restart", "--jobs", "200"]).jobs == 200
    deadletter = parser().parse_args(["--kube-context", "k", "deadletter"])
    assert (deadletter.journey, deadletter.global_cap) == ("deadletter", None)
    assert not hasattr(deadletter, "jobs")


@pytest.mark.parametrize(
    "argv",
    [
        ["smoke"],
        ["--kube-context", "k"],
        ["--kube-context", "k", "soak"],
        ["--kube-context", "k", "smoke", "--jobs", "0"],
        ["--kube-context", "k", "smoke", "--global-cap", "-1"],
        ["--kube-context", "k", "deadletter", "--jobs", "2"],
        ["--kube-context", "k", "control", "--jobs", "3"],
        ["--kube-context", "k", "restart", "--jobs", "1"],
    ],
)
def test_usage_error_exits_2(argv, capsys):
    assert main(argv) == 2
    assert capsys.readouterr().err


def test_refuses_a_cluster_it_cannot_reach(tmp_path):
    """Exit 2 before any rollout or forward, when the context names no cluster."""
    config = tmp_path / "kubeconfig"
    config.write_text("apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n")
    done = subprocess.run(
        [sys.executable, "-m", "dsy_admission.drive", "--kube-context", "no-such", "smoke"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env={**os.environ, "KUBECONFIG": str(config)},
    )
    print(done.stderr)
    assert done.returncode == 2
    assert "drive: refused: no deployment/admission in dsy-control on no-such" in done.stderr
    assert done.stdout == ""


def test_control_splits_a_quarter_per_move():
    assert split(20) == (5, 5, 5)
    assert split(7) == (1, 1, 1)
    assert not all(split(3))


def test_smoke_lines_and_verdict():
    counters = Counters("smoke", 500, 20, "h-0123abcd-", 500, 500, 0, 20, 0)
    assert counters.lines() == [
        "queue_prefix: h-0123abcd-",
        "published: 500",
        "finished: 500",
        "failed: 0",
        "max_live: 20",
        "duplicates: 0",
    ]
    assert counters.passed()
    for change in ({"max_live": 21}, {"duplicates": 1}, {"finished": 499, "failed": 1}):
        assert not Counters(**{**vars(counters), **change}).passed(), change
    assert Counters(**{**vars(counters), "global_cap": None, "max_live": 900}).passed()


def test_control_lines_and_verdict():
    counters = Counters("control", 20, None, "h-0123abcd-", 15, 20, 0, 7, 0)
    counters.skipped = 5
    counters.readmitted.update({"resumed-before-start": 5, "stuck-admitted": 5})
    counters.resumed = 5
    assert counters.lines()[-3:] == [
        "skipped: 5",
        "readmitted: resumed-before-start 5 stuck-admitted 5",
        "resumed: 5",
    ]
    assert counters.passed()
    counters.readmitted["stuck-admitted"] = 4
    assert not counters.passed()
    assert counters.lines()[-2] == "readmitted: resumed-before-start 5 stuck-admitted 4"


def test_control_counts_only_the_published():
    """The stuck rows have no message, so `published` counts the other three quarters."""
    counters = Counters("control", 20, None, "h-0123abcd-", 20, 20, 0, 7, 0, 5)
    counters.readmitted.update({"resumed-before-start": 5, "stuck-admitted": 5})
    counters.resumed = 5
    assert not counters.passed()


def test_deadletter_lines_and_verdict():
    counters = Counters("deadletter", 2, None, "h-0123abcd-", 2)
    counters.dead_lettered.update({"UNSUPPORTED_VERSION": 1, "MALFORMED": 1})
    counters.events = 2
    counters.rows_accepted = 2
    assert counters.lines()[-3:] == [
        "dead_lettered: MALFORMED 1 UNSUPPORTED_VERSION 1",
        "events: 2",
        "rows: accepted 2",
    ]
    assert counters.passed()
    counters.dead_lettered["ADMISSION_FAILED"] = 1
    assert not counters.passed()
    assert Counters("deadletter", 2).lines()[-3] == "dead_lettered: none"


def test_restart_lines_and_verdict():
    counters = Counters("restart", 200, None, "h-0123abcd-", 200, 200, 0, 31, 4)
    counters.restarts = 2
    assert counters.lines()[-2:] == ["restarts: 2", "oom_killed: 0"]
    assert counters.passed()
    counters.oom_killed = 1
    assert not counters.passed()


def test_parses_only_the_admission_samples():
    samples = parse_metrics(METRICS)
    assert samples == {
        key("dsy_admission_skipped_total", reason="outstanding-action"): 3,
        key("dsy_admission_skipped_total", reason="started"): 1,
        key("dsy_admission_duplicates_total"): 0,
        ("dsy_admission_wait_seconds_bucket", (("le", "0.1"), ("priority_class", "standard"))): 4,
    }


def test_tally_counts_from_the_first_sample_across_restarts():
    outstanding = key("dsy_admission_skipped_total", reason="outstanding-action")
    started = key("dsy_admission_skipped_total", reason="started")
    tally = Tally()
    tally.feed({outstanding: 3, started: 1})
    tally.feed({outstanding: 5, started: 1})
    assert tally.total("dsy_admission_skipped_total") == 2
    # A new pod starts its counters at zero, and a label it serves only later counts from zero.
    tally.feed({outstanding: 1})
    tally.feed({outstanding: 2, started: 4})
    assert tally.total("dsy_admission_skipped_total", reason="outstanding-action") == 4
    assert tally.total("dsy_admission_skipped_total", reason="started") == 3
    assert tally.total("dsy_admission_skipped_total") == 7
    assert tally.total("dsy_admission_duplicates_total") == 0


def deployment(env: list[dict]) -> dict:
    labels = {"app.kubernetes.io/name": "admission"}
    container = {"name": "admission", "image": "dsy-admission", "env": env}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "admission", "namespace": "dsy-control"},
        "spec": {
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}},
        },
    }


def set_env_locally(tmp_path, env: list[dict], args: list[str]) -> list[dict]:
    """The environment that `kubectl set env` gives the deployment, worked out with no cluster."""
    manifest = tmp_path / "deployment.json"
    manifest.write_text(json.dumps(deployment(env)))
    out = subprocess.run(
        ["kubectl", "set", "env", "-f", str(manifest), "--local", "-o", "json", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)["spec"]["template"]["spec"]["containers"][0].get("env", [])


def test_run_environment_replaces_what_an_earlier_run_left(tmp_path):
    left = [
        {"name": "DSY_POOL_QUEUE_PREFIX", "value": "h-dead0000-"},
        {"name": "DSY_ADMISSION_GLOBAL_CAP", "value": "3"},
        {"name": "DSY_TENANCY_PROFILE", "value": "ci"},
    ]
    assert set_env_locally(tmp_path, left, env_args("h-0123abcd-", None)) == [
        {"name": "DSY_POOL_QUEUE_PREFIX", "value": "h-0123abcd-"},
        {"name": "DSY_TENANCY_PROFILE", "value": "ci"},
    ]
    assert set_env_locally(tmp_path, left, env_args("h-0123abcd-", 20)) == [
        {"name": "DSY_POOL_QUEUE_PREFIX", "value": "h-0123abcd-"},
        {"name": "DSY_ADMISSION_GLOBAL_CAP", "value": "20"},
        {"name": "DSY_TENANCY_PROFILE", "value": "ci"},
    ]
    assert set_env_locally(tmp_path, left, CLEARED) == [
        {"name": "DSY_TENANCY_PROFILE", "value": "ci"}
    ]


def test_refused_messages_name_their_job():
    """Each refused body keeps the identity attributes, so admission writes the job's event."""
    job = accepted("acme", "standard", source_bundle("drive-test"))
    for body, reason in ((malformed, "MALFORMED"), (unsupported, "UNSUPPORTED_VERSION")):
        with pytest.raises(EventError) as raised:
            read_event(body(job.event))
        assert raised.value.dlq_reason == reason
        document = json.loads(body(job.event))
        assert (document["tenant"], document["jobid"]) == ("acme", job.job_id)
        assert (document["priorityclass"], document["jobclass"]) == ("standard", "short")
        assert document["traceparent"] == job.event.traceparent
