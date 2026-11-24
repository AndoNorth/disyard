"""The identity of an entry: its key never generated, its classes defaulted from the header, and
its fingerprint equal to the pinned vectors."""

import json
from pathlib import Path

import dsy_keys
import pytest
from dsy_dispatchapi.keys import batch_id, identify, job_key
from dsy_dispatchapi.models import BatchEntry, BatchHeader

PINNED = Path(__file__).resolve().parents[3] / "contracts/api/vectors/fingerprint.json"
VECTORS = json.loads(PINNED.read_text())["vectors"]
MANIFEST = "05b3abf2579a5eb66403cd78be557fd860633a1fe2103c7642030defe32c657f"


def header(**fields) -> BatchHeader:
    return BatchHeader(batch_key="nightly", template={}, **fields)


def entry(**fields) -> BatchEntry:
    return BatchEntry(**{"index": 7, "seed": 0, "parameters": {}, **fields})


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_fingerprint_matches_vectors(vector):
    given = vector["input"]
    classes = {"priority_class": given["priority_class"], "job_class": given["job_class"]}
    line = entry(seed=given["seed"], parameters=given["parameters"])

    from_header = identify("acme", header(**classes), line, given["manifest_sha256"])
    from_entry = identify(
        "acme", header(), line.model_copy(update=classes), given["manifest_sha256"]
    )

    assert from_header.fingerprint == from_entry.fingerprint == vector["output"]


def test_job_key_defaults_to_batch_key_and_index():
    assert job_key("nightly", entry(index=7)) == "nightly/7"
    assert job_key("nightly", entry(index=7, submission_key="run-42")) == "run-42"


def test_ids_derive_from_the_tenant_and_the_keys():
    identity = identify("acme", header(), entry(), MANIFEST)

    assert identity.key == "nightly/7"
    assert identity.job_id == dsy_keys.job_id("acme", "nightly/7")
    assert identity.job_id != identify("other", header(), entry(), MANIFEST).job_id
    assert batch_id("acme", header()) == dsy_keys.batch_id("acme", "nightly")


def test_entry_classes_override_the_header():
    defaults = identify("acme", header(), entry(), MANIFEST)
    overridden = identify("acme", header(), entry(job_class="long", priority_class="p"), MANIFEST)

    assert (defaults.priority_class, defaults.job_class) == ("standard", "short")
    assert (overridden.priority_class, overridden.job_class) == ("p", "long")
    assert overridden.fingerprint != defaults.fingerprint


def test_every_field_of_the_job_changes_the_fingerprint():
    base = identify("acme", header(), entry(), MANIFEST).fingerprint
    changed = [
        identify("acme", header(), entry(seed=1), MANIFEST),
        identify("acme", header(), entry(parameters={"n": 1}), MANIFEST),
        identify("acme", header(), entry(), "0" * 64),
    ]

    assert base not in {identity.fingerprint for identity in changed}


def test_the_key_and_index_are_not_in_the_fingerprint():
    first = identify("acme", header(), entry(index=1), MANIFEST)
    second = identify("acme", header(), entry(index=2, submission_key="k"), MANIFEST)

    assert first.fingerprint == second.fingerprint
