"""The cases that every pool worker is run against with `activity_contract`, over a store seeded
with what each case reads.

It builds the workload library from `wdf_workloads`, which a pool image does not carry, so it runs
in the repository's dev environment only.
"""

import json
import tempfile
import uuid
from pathlib import Path

from wdf_artifactclient.aggregate import put_index
from wdf_artifactclient.client import ArtifactClient
from wdf_artifactclient.models import IndexEntry
from wdf_compile.build import build
from wdf_events.models import ProducerRef
from wdf_workflow.activities.cache import bundle_key
from wdf_workflow.models import TaskInput
from wdf_workflow.testing import ContractCases
from wdf_workloads import bundle, main

ROOT = Path(__file__).resolve().parents[4]
VECTORS = ROOT / "contracts/activity/vectors/task-input/valid"
ECHO = ROOT / "contracts/workload/vectors/level0/echo/input.json"
ENTRY = "wdf_workloads.main"
# The class a pool worker fails each pool's failing case with, all deterministic: an entry
# module the bundle lacks, a workload the library lacks, and a result cut short.
FAILS = {
    "compile": "compile_failed",
    "simulate": "guest_failed",
    "aggregate": "aggregate_parse_failed",
}


def contract_cases(pool: str, store) -> ContractCases:
    """A succeeding and a failing task input of `pool`, built from the pool's task input vector,
    with a batch of their own, after the objects they read are put in `store`, a running store
    with an `endpoint` and the `credentials` of every store role whose buckets exist. Each input
    is put by the role that writes it on the platform. The failing input fails as FAILS says."""
    vector = json.loads((VECTORS / f"{pool}.json").read_text())
    vector["batch_id"] = str(uuid.uuid4())

    def role(name: str) -> ArtifactClient:
        return ArtifactClient(store.endpoint, store.credentials[name])

    def case(attempt: int, params: dict, inputs: list, cache_key: str | None = None) -> TaskInput:
        data = {**vector["request"]["data"], "inputs": inputs}
        return TaskInput.model_validate(
            {
                **vector,
                "request": {**vector["request"], "data": data},
                "task_attempt": attempt,
                "params": params,
                "cache_key": cache_key,
            }
        )

    with tempfile.TemporaryDirectory(prefix="wdf-contract-cases-") as work:
        work = Path(work)
        source = bundle.write(work)
        if pool == "compile":
            stored = role("submit").put_content(source, "source", bundle.NAME)
            params = {"source": stored.model_dump(mode="json"), "entry": ENTRY}
            missing = {**params, "entry": "wdf_workloads.missing"}
            return ContractCases(
                succeeds=case(1, params, [], bundle_key(source, ENTRY)),
                fails=case(2, missing, []),
            )
        if pool == "simulate":
            built = build(source, ENTRY, work / "built")
            component = role("compile").put_content(built.component, "component", "component.wasm")
            echo = json.loads(ECHO.read_text())
            inputs = [component.model_dump(mode="json")]
            return ContractCases(
                succeeds=case(1, echo, inputs),
                fails=case(2, {**echo, "workload": "missing"}, inputs),
            )
        return ContractCases(
            succeeds=case(1, *_index(vector, role, work / "whole", "sim-0")),
            fails=case(2, *_index(vector, role, work / "cut", "sim-1", cut=True)),
        )


def _index(vector: dict, role, out: Path, task: str, cut: bool = False) -> tuple[dict, list]:
    """The params and inputs of an aggregate over one level 0 echo result, whole or cut short,
    put as the result of simulate task `task` and indexed as the workflow indexes it."""
    out.mkdir()
    input_file = out / "input.json"
    input_file.write_bytes(ECHO.read_bytes())
    assert main.execute(input_file, out) == 0
    result = out / "results.xml"
    if cut:
        result.write_bytes(result.read_bytes()[:-20])
    request = vector["request"]
    producer = ProducerRef(job=request["jobid"], task=task, attempt="r0a1")
    reference = role("simulate").put_attempt(
        result, "result", "results.xml", producer, request["tenant"], vector["batch_id"], 0
    )
    header = {
        "job": request["jobid"],
        "tenant": request["tenant"],
        "task": request["taskid"],
        "partial": "refuse",
    }
    entry = IndexEntry(task=task, status="succeeded", reference=reference)
    index = put_index(role("indexer"), [entry], header)
    return {"aggregation": "digest_list"}, [index.model_dump(mode="json")]
