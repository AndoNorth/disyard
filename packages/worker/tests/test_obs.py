"""The worker's metrics and outcome logs: one count and one JSON line per task under the exit class
that ended it, for every class of every pool, with no line per heartbeat."""

import asyncio
import dataclasses
import io
import json
import logging
import re
import socket
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from dsy_artifactclient.errors import StoreUnavailable
from dsy_obs.logs import JsonFormatter
from dsy_worker import metrics
from dsy_worker.activity import run_task, serving
from dsy_worker.metrics import ADDRESS_ENV, BUCKETS, Metrics, classes_of, runtime_of
from dsy_worker.pools import Context
from dsy_workflow.models import TaskInput
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

ROOT = Path(__file__).resolve().parents[3]
VECTORS = ROOT / "contracts/activity/vectors/task-input/valid"
CATALOGUE = yaml.safe_load((ROOT / "contracts/instrumentation/metrics.yaml").read_text())["metrics"]
POOLS = ("compile", "simulate", "aggregate")
CASES = [(pool, exit_class) for pool in POOLS for exit_class in classes_of(pool)]
SAMPLE = re.compile(r"^(?P<name>\w+)(?:\{(?P<labels>[^}]*)\})? (?P<value>\S+)$")


def free_address() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


def scrape(address: str) -> dict[tuple[str, frozenset], float]:
    """Every sample of the metrics endpoint, by name and label set."""
    with urllib.request.urlopen(f"http://{address}/metrics", timeout=10) as reply:
        text = reply.read().decode()
    found = {}
    for line in text.splitlines():
        if line.startswith("#") or not (match := SAMPLE.match(line)):
            continue
        pairs = re.findall(r'(\w+)="([^"]*)"', match["labels"] or "")
        labels = [(k, v) for k, v in pairs if k != "service_name"]
        found[match["name"], frozenset(labels)] = float(match["value"])
    return found


def pool_labels(pool: str, exit_class: str) -> frozenset:
    return frozenset({("pool", pool), ("exit_class", exit_class)})


@pytest.fixture
def served(tmp_path):
    """Serves tasks of a pool into its metrics on a port of its own and returns the address they
    are scraped from."""
    address = free_address()
    runtime = runtime_of({ADDRESS_ENV: address})
    scopes = []

    def serve(pool: str) -> str:
        context = Context(store=None, scratch=tmp_path / "task", slot_memory_mib=384)
        scope = serving(context, tmp_path / "tmp", Metrics(runtime.metric_meter, pool))
        scope.__enter__()
        scopes.append(scope)
        return address

    yield serve
    for scope in scopes:
        scope.__exit__(None, None, None)


@pytest.fixture
def lines():
    """The JSON lines the root logger writes while the test runs."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test-worker"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)
    root.setLevel(level)


def task_of(pool: str) -> TaskInput:
    return TaskInput.model_validate_json((VECTORS / f"{pool}.json").read_text())


def script(monkeypatch, pool: str, outcome) -> None:
    """Sets the task path of `pool` to one that ends as `outcome`: an error it raises, or a result
    it returns."""
    import importlib

    async def run(task, context):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(importlib.import_module(f"dsy_worker.pools.{pool}"), "run", run)


def run(task: TaskInput, environment: ActivityEnvironment | None = None):
    return asyncio.run((environment or ActivityEnvironment()).run(run_task, task))


def end_as(monkeypatch, pool: str, exit_class: str) -> None:
    if exit_class == metrics.OK:
        script(monkeypatch, pool, object())
    else:
        script(monkeypatch, pool, ApplicationError("scripted", type=exit_class, non_retryable=True))


@pytest.mark.parametrize(("pool", "exit_class"), CASES)
def test_each_exit_class_is_counted_under_its_pool(monkeypatch, served, pool, exit_class):
    address = served(pool)
    end_as(monkeypatch, pool, exit_class)
    try:
        run(task_of(pool))
    except ApplicationError:
        pass
    samples = scrape(address)
    assert samples["dsy_worker_tasks_total", pool_labels(pool, exit_class)] == 1
    others = {
        labels
        for (name, labels), value in samples.items()
        if name == "dsy_worker_tasks_total" and value and labels != pool_labels(pool, exit_class)
    }
    assert not others
    assert samples["dsy_worker_task_seconds_count", pool_labels(pool, exit_class)] == 1


@pytest.mark.parametrize("pool", POOLS)
def test_every_series_of_a_pool_is_served_before_its_first_task(served, pool):
    samples = scrape(served(pool))
    for exit_class in classes_of(pool):
        assert samples["dsy_worker_tasks_total", pool_labels(pool, exit_class)] == 0
    assert samples["dsy_worker_aggregate_inputs_total", frozenset()] == 0
    assert samples["dsy_worker_aggregate_rows_total", frozenset()] == 0


@pytest.mark.parametrize(
    ("pool", "exit_class"),
    [("simulate", "oom_killed"), ("compile", "compile_oom"), ("aggregate", "aggregate_oom")],
)
def test_oom_classes_are_counted_classes(pool, exit_class):
    assert (pool, exit_class) in CASES


def test_an_error_outside_the_pool_classes_is_logged_and_not_counted(monkeypatch, served, lines):
    address = served("simulate")
    script(monkeypatch, "simulate", StoreUnavailable("down"))
    with pytest.raises(ApplicationError) as raised:
        run(task_of("simulate"))
    samples = scrape(address)
    assert not [v for (n, _), v in samples.items() if n == "dsy_worker_tasks_total" and v]
    [line] = [x for x in lines() if x["msg"].startswith("task ended")]
    assert line["exit_class"] == raised.value.type


def test_one_outcome_line_per_task_carrying_its_ids(monkeypatch, served, lines):
    served("simulate")
    script(monkeypatch, "simulate", object())
    task = task_of("simulate")
    run(task)
    script(monkeypatch, "simulate", ApplicationError("x", type="timeout"))
    with pytest.raises(ApplicationError):
        run(task)
    ended = [x for x in lines() if x["msg"].startswith("task ended")]
    assert [x["exit_class"] for x in ended] == ["ok", "timeout"]
    for line in ended:
        assert line["job_id"] == task.request.jobid
        assert line["task_id"] == task.request.taskid
        assert line["attempt_id"]
        assert line["pool"] == "simulate"
        assert line["tenant"] == task.request.tenant
        assert line["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert [x["level"] for x in ended] == ["INFO", "WARNING"]


def test_heartbeats_are_recorded_and_never_logged(monkeypatch, served, lines):
    address = served("simulate")

    async def slow(task, context):
        await asyncio.sleep(1.2)
        return object()

    import dsy_worker.pools.simulate

    monkeypatch.setattr(dsy_worker.pools.simulate, "run", slow)
    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(
        environment.info, heartbeat_timeout=timedelta(seconds=0.9)
    )
    run(task_of("simulate"), environment)
    lag = scrape(address)[
        "dsy_worker_heartbeat_lag_seconds_count", frozenset({("pool", "simulate")})
    ]
    assert lag >= 2
    assert len([x for x in lines() if x["msg"].startswith("task ended")]) == 1
    assert not [x for x in lines() if "heartbeat" in x["msg"].lower()]


@pytest.mark.parametrize("name", sorted(BUCKETS))
def test_buckets_are_the_catalogue_buckets(name):
    assert BUCKETS[name] == CATALOGUE[name]["buckets"]


def test_the_default_runtime_serves_nothing_without_an_address():
    assert runtime_of({}) is not None
    assert runtime_of({ADDRESS_ENV: ""}) is not None
