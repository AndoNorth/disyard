"""The plans and accepted events that the workflow tests start jobs from."""

import uuid
from datetime import UTC, datetime

from dsy_events import Event, build_event
from dsy_events.models import JobParams
from dsy_plan import Plan

HASH = "a" * 64
SOURCE = {
    "uri": f"s3://dsy-sources/sha256/aa/{HASH}",
    "sha256": HASH,
    "size": 2048,
    "name": "bundle.tar.gz",
    "kind": "source",
}
PARAMS = {
    "compile": {"source": SOURCE, "entry": "sim"},
    "simulate": {"workload": "pi", "args": {"n": 1000}, "seed": 7},
    "aggregate": {"aggregation": "digest_list"},
}
RESOURCES = {
    "compile": {"cpu_milli": 1000, "memory_mib": 1024},
    "simulate": {"cpu_milli": 250, "memory_mib": 384},
    "aggregate": {"cpu_milli": 1000, "memory_mib": 1024},
}
OUTPUT = {"compile": "component", "simulate": "result"}
JOB_PARAMS = JobParams(seed=7, parameters={})

# A compile, four simulate tasks over it and an aggregate over them.
FAN = {
    "build": ("compile", []),
    "sim-a": ("simulate", ["build"]),
    "sim-b": ("simulate", ["build"]),
    "sim-c": ("simulate", ["build"]),
    "sim-d": ("simulate", ["build"]),
    "agg": ("aggregate", ["sim-a", "sim-b", "sim-c", "sim-d"]),
}


def plan(
    tasks: dict[str, tuple[str, list[str]]],
    max_parallel: int = 8,
    *,
    retries: int = 1,
    checkpoint: frozenset[str] = frozenset(),
    allow_partial: frozenset[str] = frozenset(),
    **params,
) -> Plan:
    """A bound plan from `{id: (type, depends_on)}`, whose inputs are the declared output of each
    dependency, with `params` overriding the params of a task type, every task's `retries`, the
    `checkpoint` flag set on the simulate tasks named and `allow_partial` on the aggregate tasks
    named."""
    return Plan.model_validate(
        {
            "plan_version": 1,
            "profile": "ci",
            "max_parallel": max_parallel,
            "parameters": {},
            "tasks": [
                {
                    "id": tid,
                    "type": kind,
                    "pool": kind,
                    "depends_on": sorted(deps),
                    "inputs": [
                        {"task": dep, "output": OUTPUT[tasks[dep][0]]} for dep in sorted(deps)
                    ],
                    **RESOURCES[kind],
                    "run_timeout_s": 600,
                    "retries": retries,
                    "checkpoint": tid in checkpoint,
                    "allow_partial": tid in allow_partial,
                    "params": params.get(kind, PARAMS[kind]),
                }
                for tid, (kind, deps) in tasks.items()
            ],
        }
    )


def accepted_event(manifest_hash: str | None = None) -> Event:
    """The accepted event of a new job, whose manifest is named by its hash."""
    sha256 = manifest_hash or uuid.uuid4().hex * 2
    return build_event(
        "job.accepted",
        source="/dsy/dispatch",
        tenant="acme",
        jobid=str(uuid.uuid4()),
        priorityclass="standard",
        jobclass="simulate",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        time=datetime(2026, 11, 12, 12, 0, tzinfo=UTC),
        data={
            "manifest": {
                "uri": f"s3://dsy-sources/sha256/{sha256[:2]}/{sha256}",
                "sha256": sha256,
                "size": 2048,
                "name": "manifest.json",
                "kind": "manifest",
            },
            "batch_id": str(uuid.uuid4()),
            "submission_key": f"sub-{uuid.uuid4()}",
        },
    )
