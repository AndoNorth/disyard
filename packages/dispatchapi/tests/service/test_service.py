"""Readiness and metrics of the service in this process, over Postgres and the Temporal dev
server, with the store in a directory and FakeBroker."""

import re
import urllib.request

import pytest
from dsy_dispatchapi.gateway import TemporalGateway
from dsy_dispatchapi.testing import FakeGateway
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

pytestmark = [pytest.mark.integration, pytest.mark.pair]

D15 = {
    "dsy_dispatch_jobs_total",
    "dsy_dispatch_submit_seconds",
    "dsy_dispatch_unpublished_jobs",
    "dsy_dispatch_unpublished_oldest_seconds",
    "dsy_control_requests_total",
    "dsy_control_outbox_jobs",
    "dsy_control_outbox_oldest_seconds",
}
SAMPLE = re.compile(r"^(\w+?)(?:_bucket|_sum|_count)?(?:\{(.*)\})? \S+$")
LABEL = re.compile(r'(\w+)="([^"]*)"')
ID_LABELS = {"job_id", "jobid", "batch_id", "batch_key", "tenant", "submission_key", "trace_id"}


def test_ready_only_when_dependencies_answer(start, logins, temporal, store, db, kit):
    with TemporalGateway(temporal) as gateway:
        service = start(logins, gateway, temporal)
        ready = service.url("control-api") + "/readyz"
        assert kit.request(ready) == (200, {"status": "ready"})
        assert kit.request(service.url("dispatch-api") + "/readyz") == (200, {"status": "ready"})

        store.available = False
        assert kit.request(ready) == (503, {"status": "not-ready"})
        store.available = True
        assert kit.status(ready) == 200

        user = sql.Identifier(conninfo_to_dict(logins["reader"])["user"])
        db.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(user))
        assert kit.status(ready) == 503
        db.execute(sql.SQL("ALTER ROLE {} LOGIN").format(user))
        assert kit.status(ready) == 200

        with TemporalGateway(f"127.0.0.1:{kit.free_port()}", call_timeout_s=1) as down:
            service.gateway = down
            assert kit.status(ready) == 503
        service.gateway = gateway
        assert kit.status(ready) == 200
        # The health of the process never asks a dependency.
        store.available = False
        assert kit.request(service.url("control-api") + "/healthz") == (200, {"status": "ok"})
        service.stop(drain_s=5)


def test_metrics_names_and_no_id_labels(start, logins, tmp_path, kit):
    service = start(logins, FakeGateway())
    dispatch, control = service.url("dispatch-api"), service.url("control-api")
    source = kit.upload_bundle(dispatch, tmp_path)
    call = kit.Call(dispatch, kit.batch(source, "metrics", 3))
    answers = list(call.answers())
    assert [a.get("outcome") for a in answers[:3]] == ["accepted"] * 3, answers
    job_id = answers[0]["job_id"]
    code, answer = kit.request(f"{control}/v1/tenants/acme/jobs/{job_id}/cancel", "POST", b"{}")
    assert (code, answer["outcome"]) == (200, "applied")
    code, _ = kit.request(f"{control}/v1/tenants/acme/jobs/{job_id}/pause", "POST", b"{}")
    assert code == 409

    url = f"http://{service.config.metrics_address}/metrics"
    with urllib.request.urlopen(url, timeout=10) as scraped:
        text = scraped.read().decode()
    samples = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith("dsy_"):
            continue
        name, labels = SAMPLE.match(line).groups()
        found = dict(LABEL.findall(labels or ""))
        samples.setdefault(name, []).append((found, line.rsplit(" ", 1)[1]))
        assert not ID_LABELS & set(found), line
        assert not {job_id, answers[0]["trace_id"], "acme"} & set(found.values()), line

    assert set(samples) == D15
    jobs = {
        (labels["outcome"], labels["reason"]): v for labels, v in samples["dsy_dispatch_jobs_total"]
    }
    assert jobs["accepted", "none"] == "3"
    requests = {
        (labels["action"], labels["outcome"]): v
        for labels, v in samples["dsy_control_requests_total"]
    }
    assert (requests["cancel", "applied"], requests["pause", "refused"]) == ("1", "1")
