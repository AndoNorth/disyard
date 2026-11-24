"""The results push, over `FakeObs` standing behind the forward to Prometheus and at Grafana's
address, each test changing the record or what the fake refuses."""

import base64
import json
from pathlib import Path

from dsy_acceptance import smoke
from dsy_acceptance.smoke import results

ROWS = [*map(str, range(1, 12)), "run"]
SMOKE_DASHBOARD = Path(__file__).resolve().parents[4] / "deploy/obs/grafana/dashboards/smoke.json"


def run(capsys) -> tuple[int, list[str], str]:
    code = smoke.main()
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def gauges(fake) -> dict[str, list[dict]]:
    """The data points of each gauge, with their attributes read into a dict."""
    assert len(fake.pushed) == 1
    metrics = json.loads(fake.pushed[0])["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    return {
        m["name"]: [
            {
                **{a["key"]: a["value"]["stringValue"] for a in p["attributes"]},
                "value": p["asDouble"],
            }
            for p in m["gauge"]["dataPoints"]
        ]
        for m in metrics
    }


def test_results_push_every_row_and_the_run_and_post_one_annotation(fake_obs, capsys):
    code, lines, err = run(capsys)
    pushed = gauges(fake_obs)
    verdicts = pushed["dsy_smoke_row_verdict"]
    assert [p["row"] for p in verdicts] == ROWS
    assert all(p["verdict"] == "ok" and p["value"] == 1.0 for p in verdicts)
    seconds = pushed["dsy_smoke_row_seconds"]
    assert [p["row"] for p in seconds] == ROWS
    assert all(p["value"] >= 0 for p in seconds)
    assert seconds[-1]["value"] >= max(p["value"] for p in seconds[:-1])
    [note] = fake_obs.annotations
    assert note["tags"] == ["smoke"]
    assert note["text"].startswith("smoke ok, batch smoke-")
    assert (code, err) == (0, "")
    assert lines[-1].startswith("smoke ci: 11 ok, 0 failed, 0 skipped, ")


def test_results_post_to_the_otlp_receiver_and_to_grafana_with_the_credential(fake_obs, capsys):
    run(capsys)
    sent = [(r.method, r.path) for r in fake_obs.received if r.path.startswith("/api/")]
    pushes = [call for call in sent if call[0] == "POST"]
    assert pushes == [
        ("POST", "/api/v1/otlp/v1/metrics"),
        ("POST", "/api/annotations"),
    ]


def test_results_are_pushed_after_the_count_line(fake_obs, capsys, monkeypatch):
    seen = []
    push = results.push

    def after(*args):
        seen.append(capsys.readouterr().out.splitlines()[-1])
        push(*args)

    monkeypatch.setattr(results, "push", after)
    run(capsys)
    assert seen[0].startswith("smoke ci: 11 ok")


def test_results_of_a_failed_row_6_still_push_with_row_11_skipped_and_the_run_failed(
    fake_obs, capsys, monkeypatch
):
    def refused(self, document, batch_key):
        raise smoke.api.Failed("not accepted")

    monkeypatch.setattr(smoke.api.Api, "submit_one", refused)
    code, lines, _ = run(capsys)
    verdicts = {p["row"]: p["verdict"] for p in gauges(fake_obs)["dsy_smoke_row_verdict"]}
    assert [*verdicts] == ROWS
    assert verdicts["6"] == "FAIL"
    assert verdicts["11"] == "skip"
    assert verdicts["run"] == "FAIL"
    assert fake_obs.annotations[0]["text"].startswith("smoke FAIL, batch none")
    assert code == 1


def test_results_with_the_record_off_push_nothing(fake_obs, record, capsys):
    record("off")
    code, _, err = run(capsys)
    assert (code, err) == (0, "")
    assert fake_obs.received == []
    assert fake_obs.pushed == [] and fake_obs.annotations == []


def test_results_when_the_backends_refuse_change_nothing_but_one_stderr_line(
    fake_obs, healthy, canned, capsys, monkeypatch
):
    expected_code, expected_lines, _ = run(capsys)
    healthy.job.statuses[:] = [canned.ADMITTED, canned.RUNNING, canned.SUCCEEDED]
    shown = smoke.forwarded

    def refusing(namespace, resource, port, timeout_s=None):
        # Only the results push names its own bound, so row 11 still reads the stack.
        if timeout_s is not None:
            raise TimeoutError("svc/prometheus did not listen in 5 s")
        return shown(namespace, resource, port)

    monkeypatch.setattr(smoke, "forwarded", refusing)
    monkeypatch.setattr(results, "GRAFANA", "http://127.0.0.1:1")
    code, lines, err = run(capsys)
    failed = [line for line in err.splitlines() if line.startswith("smoke results: ")]
    assert len(failed) == 1
    assert "gauges: svc/prometheus did not listen in 5 s" in failed[0]
    assert "annotation: " in failed[0]
    assert code == expected_code
    assert lines[:11] == expected_lines[:11]
    assert lines[11].split(", ")[:3] == expected_lines[11].split(", ")[:3]


def test_results_annotation_is_posted_with_basic_auth(fake_obs, capsys, monkeypatch):
    sent = []
    post = results._post

    def noted(url, body, timeout_s, headers):
        sent.append((url, headers))
        post(url, body, timeout_s, headers)

    monkeypatch.setattr(results, "_post", noted)
    run(capsys)
    [(_, headers)] = [s for s in sent if s[0].endswith("/api/annotations")]
    assert base64.b64decode(headers["Authorization"].removeprefix("Basic ")) == b"admin:secret"


def test_the_dashboard_names_each_row_series_by_the_row_it_numbers():
    """A row is pushed under its number, so the per-row panels name each series by override."""
    dashboard = json.loads(SMOKE_DASHBOARD.read_text())
    named = {
        panel["title"]: {
            override["matcher"]["options"]: override["properties"][0]["value"]
            for override in panel["fieldConfig"]["overrides"]
        }
        for panel in dashboard["panels"]
        if panel.get("fieldConfig", {}).get("overrides")
    }
    expected = {f"row {i}": name.rstrip(":") for i, name in enumerate(results.ROWS, 1)}
    assert named == {
        "Seconds per row over runs": expected,
        "Failures per row over runs": expected,
    }
