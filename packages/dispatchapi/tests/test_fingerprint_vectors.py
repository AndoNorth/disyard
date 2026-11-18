"""The fingerprint formula equals the pinned vectors, and the vectors pin its key order."""

import json
from pathlib import Path

import pytest
from wdf_dispatchapi import contract

PINNED = Path(__file__).resolve().parents[3] / "contracts/api/vectors/fingerprint.json"
VECTORS = json.loads(PINNED.read_text())["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_fingerprint_vectors(vector):
    assert contract.canonical(vector["input"]) == vector["canonical"]
    assert contract.fingerprint(**vector["input"]) == vector["output"]


def test_fingerprint_vectors_are_distinct():
    assert len({v["output"] for v in VECTORS}) == len(VECTORS)


def test_fingerprint_vectors_refuse_a_formula_with_one_input_key_reordered(monkeypatch):
    def seed_first(fields: dict) -> str:
        reordered = {"seed": fields["seed"], **{k: v for k, v in fields.items() if k != "seed"}}
        return json.dumps(reordered, separators=(",", ":"), ensure_ascii=False)

    contract.check(PINNED, "fingerprint")
    monkeypatch.setattr(contract, "canonical", seed_first)
    with pytest.raises(contract.Refused) as refused:
        contract.check(PINNED, "fingerprint")
    assert refused.value.reason == "fingerprint-mismatch"
