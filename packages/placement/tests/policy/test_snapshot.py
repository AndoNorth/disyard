"""The pool snapshot over the node and pod lists of the `ci` cluster.

On `ci` a pool node has the preset size less 100m and less 256 MiB plus a sixteenth of its memory,
and carries kindnet (100m, 50 MiB), kube-proxy (no requests) and its one pool worker.
"""

import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from dsy_placement.snapshot import PoolCapacity, quantity, read
from dsy_workflow.contract import naming

IMAGES = {
    "compile": "dsy-compile-worker:tilt-6c1f2b9e0a4d7e31",
    "simulate": "dsy-simulate-worker:tilt-0d5a8e3c7b2f1946",
    "aggregate": "dsy-aggregate-worker:tilt-9e4b1c7f2a6d0853",
}
CI = {
    "compile": PoolCapacity(
        nodes=["dsy-worker3"],
        allocatable_cpu_milli=1400,
        allocatable_memory_mib=1184,
        requested_cpu_milli=1100,
        requested_memory_mib=1074,
        ready_workers=1,
        images=[IMAGES["compile"]],
    ),
    "simulate": PoolCapacity(
        nodes=["dsy-worker4"],
        allocatable_cpu_milli=400,
        allocatable_memory_mib=584,
        requested_cpu_milli=350,
        requested_memory_mib=562,
        ready_workers=1,
        images=[IMAGES["simulate"]],
    ),
    "aggregate": PoolCapacity(
        nodes=["dsy-worker5"],
        allocatable_cpu_milli=1400,
        allocatable_memory_mib=1184,
        requested_cpu_milli=1100,
        requested_memory_mib=1074,
        ready_workers=1,
        images=[IMAGES["aggregate"]],
    ),
}


def pod(name: str, node: str | None, cpu: str, memory: str, phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "nodeName": node,
            "containers": [
                {
                    "name": name,
                    "image": "busybox",
                    "resources": {"requests": {"cpu": cpu, "memory": memory}},
                }
            ],
        },
        "status": {"phase": phase},
    }


def worker(pods: dict, pool: str) -> dict:
    return next(p for p in pods["items"] if p["metadata"]["name"].startswith(f"{pool}-worker-"))


def test_snapshot_sums_pool_nodes(cluster, topology):
    nodes, pods = cluster
    taken_at = datetime(2026, 11, 18, tzinfo=UTC)
    snapshot = read(nodes, pods, topology, taken_at=taken_at)
    assert snapshot.taken_at == taken_at
    assert snapshot.pools == CI
    simulate = snapshot.pools["simulate"]
    assert (simulate.free_cpu_milli, simulate.free_memory_mib) == (50, 22)

    second = copy.deepcopy(
        next(n for n in nodes["items"] if n["metadata"]["name"] == "dsy-worker4")
    )
    second["metadata"]["name"] = "dsy-worker6"
    nodes["items"].append(second)
    pods["items"].append(pod("kindnet-x2j8f", "dsy-worker6", "100m", "50Mi"))
    simulate = read(nodes, pods, topology).pools["simulate"]
    assert simulate.nodes == ["dsy-worker4", "dsy-worker6"]
    assert (simulate.allocatable_cpu_milli, simulate.allocatable_memory_mib) == (800, 1168)
    assert (simulate.requested_cpu_milli, simulate.requested_memory_mib) == (450, 612)


def test_snapshot_counts_daemon_requests(cluster, topology):
    """kube-system pods on a pool node count, because the scheduler counts them against it."""
    nodes, pods = cluster
    pods["items"].remove(worker(pods, "simulate"))
    simulate = read(nodes, pods, topology).pools["simulate"]
    assert (simulate.requested_cpu_milli, simulate.requested_memory_mib) == (100, 50)
    assert simulate.ready_workers == 0


def test_snapshot_skips_finished_pods(cluster, topology):
    nodes, pods = cluster
    pods["items"] += [
        pod("done", "dsy-worker4", "4", "4Gi", phase="Succeeded"),
        pod("crashed", "dsy-worker4", "4", "4Gi", phase="Failed"),
        pod("unbound", None, "4", "4Gi", phase="Pending"),
    ]
    assert read(nodes, pods, topology).pools == CI

    pods["items"].append(pod("starting", "dsy-worker4", "10m", "1Mi", phase="Pending"))
    simulate = read(nodes, pods, topology).pools["simulate"]
    assert (simulate.requested_cpu_milli, simulate.requested_memory_mib) == (360, 563)


def test_snapshot_ready_workers_and_images(cluster, topology):
    nodes, pods = cluster
    assert {pool: c.images for pool, c in read(nodes, pods, topology).pools.items()} == {
        pool: [image] for pool, image in IMAGES.items()
    }

    rolled = copy.deepcopy(worker(pods, "simulate"))
    rolled["metadata"]["name"] = "simulate-worker-5c9f7d6b4a-k3n8p"
    rolled["spec"]["containers"][0]["image"] = "dsy-simulate-worker:tilt-1f2e3d4c5b6a7980"
    pods["items"].append(rolled)
    simulate = read(nodes, pods, topology).pools["simulate"]
    assert simulate.ready_workers == 2
    assert simulate.images == [
        "dsy-simulate-worker:tilt-0d5a8e3c7b2f1946",
        rolled["spec"]["containers"][0]["image"],
    ]

    for condition in worker(pods, "simulate")["status"]["conditions"]:
        condition["status"] = "False"
    simulate = read(nodes, pods, topology).pools["simulate"]
    assert simulate.ready_workers == 1
    assert simulate.images == [rolled["spec"]["containers"][0]["image"]]


def test_snapshot_ignores_unlabelled_nodes(cluster, topology):
    """The control plane, the infra and obs nodes and their pods are in no task pool."""
    nodes, pods = cluster
    roles = {n["metadata"]["labels"].get(topology["pool_label"]) for n in nodes["items"]}
    assert {None, "infra", "obs"} <= roles
    stray = copy.deepcopy(next(n for n in nodes["items"] if n["metadata"]["name"] == "dsy-worker"))
    stray["metadata"].update(name="dsy-stray", labels={})
    nodes["items"].append(stray)
    pods["items"].append(pod("busy", "dsy-stray", "1", "1Gi"))

    snapshot = read(nodes, pods, topology)
    assert set(snapshot.pools) == set(naming().pools) == {"compile", "simulate", "aggregate"}
    assert snapshot.pools == CI


def test_snapshot_pool_without_nodes_is_empty(cluster, topology):
    """A cluster with no simulate node shows the pool with nothing in it."""
    nodes, pods = cluster
    nodes["items"] = [n for n in nodes["items"] if n["metadata"]["name"] != "dsy-worker4"]
    pods["items"].remove(worker(pods, "simulate"))
    assert read(nodes, pods, topology).pools["simulate"] == PoolCapacity(
        nodes=[],
        allocatable_cpu_milli=0,
        allocatable_memory_mib=0,
        requested_cpu_milli=0,
        requested_memory_mib=0,
        ready_workers=0,
        images=[],
    )


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1", 1),
        ("1400m", Decimal("1.4")),
        ("1212416Ki", 1184 * 2**20),
        ("1Gi", 2**30),
        ("512Mi", 512 * 2**20),
        ("1k", 1000),
        ("1M", 10**6),
        ("2E", 2 * 10**18),
        ("1e3", 1000),
        ("0.5", Decimal("0.5")),
    ],
)
def test_snapshot_reads_quantities(text, value):
    assert quantity(text) == value
