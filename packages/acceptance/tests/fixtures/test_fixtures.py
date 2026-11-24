"""The e2e fixtures: variant bundles differ in hash, the failing bundle fails its real compile, the
templates match the job schema, and the host values equal what the workloads write."""

import hashlib
import json
from pathlib import Path

import jsonschema
from dsy_acceptance.fixtures import bundles, jobs
from dsy_compile.__main__ import main
from dsy_kernel import expected_digest
from dsy_workflow.harness import REAL_ENTRY, echo_params

ROOT = Path(__file__).resolve().parents[4]
JOB_SCHEMA = json.loads((ROOT / "contracts/job/schemas/job/1.0.json").read_text())
SOURCES = tuple(
    json.loads(
        (ROOT / "contracts/api/vectors/object-answer/valid/created-source.json").read_text()
    )["reference"]
    | {"uri": f"s3://dsy-sources/sha256/{h * 2}/{h * 64}"}
    for h in "ab"
)
CPU_BURN = {"workload": "cpu_burn", "args": {"units": 3, "heartbeat_every": 1}}


def test_variants_differ_in_source_hash():
    a, b = bundles.variant("a"), bundles.variant("b")
    assert bundles.variant("a") == a
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(b).hexdigest()


def test_missing_entry_bundle_fails_its_compile(tmp_path, capsys):
    path = tmp_path / "bundle.tar"
    path.write_bytes(bundles.missing_entry())
    code = main(
        [
            "build",
            "--bundle",
            str(path),
            "--entry",
            bundles.MISSING_ENTRY,
            "--out",
            str(tmp_path / "out"),
        ]
    )
    reason = capsys.readouterr().err.strip().splitlines()[-1]
    print(reason)
    assert code == 3
    assert reason.startswith("reason: compile_failed: ")
    assert "has no attribute 'Run'" in reason


def test_templates_match_the_job_schema():
    simulates = (echo_params(), echo_params())
    for document in (
        jobs.two_chains(SOURCES, simulates),
        jobs.failing_second_chain(SOURCES, simulates, allow_partial=True),
    ):
        jsonschema.validate(document, JOB_SCHEMA)


def test_two_chains_wiring():
    document = jobs.failing_second_chain(SOURCES, (echo_params(), echo_params()), True)
    tasks = document["tasks"]
    assert set(tasks) == {"a-build", "a-sim", "b-build", "b-sim", "agg"}
    assert tasks["a-build"]["params"]["entry"] == REAL_ENTRY
    assert tasks["b-build"]["params"]["entry"] == bundles.MISSING_ENTRY
    assert tasks["b-sim"]["inputs"] == [{"task": "b-build", "output": "component"}]
    assert [i["task"] for i in tasks["agg"]["inputs"]] == ["a-sim", "b-sim"]
    assert tasks["agg"]["allow_partial"] is True


def test_two_seed_summary_holds_the_digest_of_each_seed():
    summary = jobs.two_seed_summary("cpu_burn", CPU_BURN["args"], (3, 4))
    assert summary["inputs"] == 2 and summary["missing"] == []
    assert [d["digest"] for d in summary["digests"]] == [
        expected_digest("cpu_burn", seed, CPU_BURN["args"]) for seed in (3, 4)
    ]
