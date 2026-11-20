"""The shared helper that makes the deployed workflow worker build current, over a fake Temporal
client and a fake kubectl: each outcome, what it writes, and a build made current by another caller
during the poller wait."""

import asyncio
from types import SimpleNamespace

import pytest
from wdf_clusterkit import Refused
from wdf_workflow.contract import naming
from wdf_workflow.harness import cluster
from wdf_workflow.harness.cluster import CurrentBuild, make_deployed_current


class Service:
    """The workflow service calls of the helper, over one deployment's current build."""

    def __init__(self, current: str, raced: str | None = None):
        self.current = current
        # A build another caller makes current while the pollers are awaited.
        self.raced = raced
        self.writes: list[str] = []

    async def describe_worker_deployment(self, request):
        version = SimpleNamespace(build_id=self.current)
        routing = SimpleNamespace(current_deployment_version=version)
        return SimpleNamespace(
            worker_deployment_info=SimpleNamespace(routing_config=routing),
            conflict_token=b"token",
        )

    async def describe_worker_deployment_version(self, request):
        if self.raced is not None:
            self.current = self.raced
        queues = [SimpleNamespace(name=naming().workflow.queue)]
        return SimpleNamespace(
            worker_deployment_version_info=SimpleNamespace(task_queue_infos=queues)
        )

    async def set_worker_deployment_current_version(self, request):
        self.writes.append(request.build_id)
        self.current = request.build_id


def run(monkeypatch, current: str, deployed: list[str], raced: str | None = None):
    """The helper's result, or the Refused it raised, and the builds it wrote as current."""
    service = Service(current, raced)
    client = SimpleNamespace(namespace="wdf", identity="test", workflow_service=service)
    monkeypatch.setattr(cluster, "kubectl", lambda context, *args: " ".join(deployed))
    try:
        result = asyncio.run(make_deployed_current(client, "kind-wdf", timeout_s=1))
    except Refused as refused:
        result = refused
    return result, service.writes


def test_current_and_deployed_writes_nothing(monkeypatch):
    assert run(monkeypatch, "a", ["a"]) == (CurrentBuild("a", ("a",), None), [])


def test_current_beside_another_deployed_stays(monkeypatch):
    assert run(monkeypatch, "a", ["a", "b"]) == (CurrentBuild("a", ("a", "b"), None), [])


def test_current_not_deployed_refused(monkeypatch):
    refused, writes = run(monkeypatch, "a", ["b"])
    assert isinstance(refused, Refused)
    assert "a" in str(refused).split() and "['b']" in str(refused)
    assert writes == []


def test_none_current_one_deployed_made_current(monkeypatch):
    assert run(monkeypatch, "", ["b"]) == (CurrentBuild("b", ("b",), "b"), ["b"])


@pytest.mark.parametrize("deployed", [[], ["a", "b"]])
def test_none_current_not_one_deployed_refused(monkeypatch, deployed):
    refused, writes = run(monkeypatch, "", deployed)
    assert isinstance(refused, Refused)
    assert str(deployed) in str(refused)
    assert writes == []


def test_made_current_during_poller_wait_kept(monkeypatch):
    assert run(monkeypatch, "", ["b"], raced="b") == (CurrentBuild("b", ("b",), None), [])


def test_other_build_made_current_during_poller_wait_not_overwritten(monkeypatch):
    refused, writes = run(monkeypatch, "", ["b"], raced="c")
    assert isinstance(refused, Refused)
    assert "c" in str(refused).split() and "['b']" in str(refused)
    assert writes == []
