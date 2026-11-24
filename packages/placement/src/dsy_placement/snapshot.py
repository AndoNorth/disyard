"""The capacity of each task pool, read from a Kubernetes node list and pod list.

Pure over the JSON the API answers, so a caller decides when and how the lists are read. A snapshot
decides no placement: it is recorded beside each answer and shown in the pool view.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from dsy_workflow.contract import naming
from pydantic import BaseModel, ConfigDict, NonNegativeInt

FINISHED = {"Succeeded", "Failed"}
WORKER_LABEL = "app.kubernetes.io/name"

_SUFFIXES = {
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}
MIB = Decimal(2**20)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PoolCapacity(_Model):
    """The nodes of a pool, their allocatable and requested CPU and memory, and its ready
    workers with their images."""

    nodes: list[str]
    allocatable_cpu_milli: NonNegativeInt
    allocatable_memory_mib: NonNegativeInt
    requested_cpu_milli: NonNegativeInt
    requested_memory_mib: NonNegativeInt
    ready_workers: NonNegativeInt
    images: list[str]

    @property
    def free_cpu_milli(self) -> int:
        return self.allocatable_cpu_milli - self.requested_cpu_milli

    @property
    def free_memory_mib(self) -> int:
        return self.allocatable_memory_mib - self.requested_memory_mib


class Snapshot(_Model):
    taken_at: datetime
    pools: dict[str, PoolCapacity]


def quantity(text: str) -> Decimal:
    """A Kubernetes quantity such as `1400m`, `1Gi` or `1212416Ki` as a plain number."""
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            return Decimal(text.removesuffix(suffix)) * _SUFFIXES[suffix]
    return Decimal(text)


def _resources(figures: Mapping[str, str]) -> tuple[Decimal, Decimal]:
    """The CPU in millicores and the memory in MiB of a resource list."""
    return (
        quantity(figures.get("cpu", "0")) * 1000,
        quantity(figures.get("memory", "0")) / MIB,
    )


def _whole(values: list[Decimal], rounding: str) -> int:
    return int(sum(values, Decimal(0)).to_integral_value(rounding=rounding))


def _ready(pod: Mapping[str, Any]) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def _pool(
    pool: str, nodes: list[dict], pods: list[dict], topology: Mapping[str, Any]
) -> PoolCapacity:
    label, value = topology["pool_label"], topology["pools"][pool]
    names = sorted(
        n["metadata"]["name"] for n in nodes if n["metadata"].get("labels", {}).get(label) == value
    )
    allocatable = [
        _resources(n["status"].get("allocatable", {}))
        for n in nodes
        if n["metadata"]["name"] in names
    ]
    # ponytail: regular containers only; init containers and pod overhead are left out because no
    # pool pod has either, so count max(init, sum) plus overhead once one does.
    requested = [
        _resources(c.get("resources", {}).get("requests", {}))
        for p in pods
        if p["spec"].get("nodeName") in names and p.get("status", {}).get("phase") not in FINISHED
        for c in p["spec"].get("containers", [])
    ]
    workers = [
        p
        for p in pods
        if p["metadata"].get("labels", {}).get(WORKER_LABEL) == f"{pool}-worker" and _ready(p)
    ]
    return PoolCapacity(
        nodes=names,
        allocatable_cpu_milli=_whole([cpu for cpu, _ in allocatable], ROUND_FLOOR),
        allocatable_memory_mib=_whole([mem for _, mem in allocatable], ROUND_FLOOR),
        requested_cpu_milli=_whole([cpu for cpu, _ in requested], ROUND_CEILING),
        requested_memory_mib=_whole([mem for _, mem in requested], ROUND_CEILING),
        ready_workers=len(workers),
        images=sorted({c["image"] for p in workers for c in p["spec"]["containers"]}),
    )


def read(
    nodes: Mapping[str, Any],
    pods: Mapping[str, Any],
    topology: Mapping[str, Any],
    taken_at: datetime | None = None,
) -> Snapshot:
    """The capacity of each task pool of naming.yaml, from a NodeList, a PodList across every
    namespace and the parsed topology contract, which names the pool label and its values.

    Allocatable figures round down and requests round up, so a pool never shows more free room
    than it has. The pools of the topology that run no task are left out.
    """
    return Snapshot(
        taken_at=taken_at or datetime.now(UTC),
        pools={
            pool: _pool(pool, nodes["items"], pods["items"], topology) for pool in naming().pools
        },
    )
