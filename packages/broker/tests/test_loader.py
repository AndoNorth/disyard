"""The loader accepts the pinned files and the valid vectors, and each invalid vector fails with
the reason of its directory."""

import subprocess
import sys
from pathlib import Path

import pytest
from wdf_broker.contract import topology
from wdf_broker.models import FILES

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = {name: ROOT / "contracts" / name for name in ("broker", "tenancy")}
PINNED = [
    CONTRACTS["broker"] / "topology.yaml",
    CONTRACTS["broker"] / "reasons.yaml",
    CONTRACTS["tenancy"] / "tenancy.yaml",
]
VALID = sorted(p for c in CONTRACTS.values() for p in c.glob("vectors/*/valid/*"))
INVALID = sorted(p for c in CONTRACTS.values() for p in c.glob("vectors/*/invalid/*/*"))
# The reasons that apply to each schema, in check order.
REASONS = {
    "topology": ["schema-violation", "bad-name", "dlq-ttl-set", "bad-user"],
    "reasons": ["schema-violation", "duplicate-code", "event-code-mismatch", "unexpected-recovery"],
    "tenancy": [
        "schema-violation",
        "bad-tenant",
        "bad-class",
        "cap-above-global",
        "unknown-profile",
    ],
}


def check(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "wdf_broker.contract", "--check", *map(str, args)],
        capture_output=True,
        text=True,
    )


def vector_id(path: Path) -> str:
    return str(path.relative_to(ROOT / "contracts"))


@pytest.mark.parametrize("path", PINNED + VALID, ids=vector_id)
def test_loader_accepts_a_valid_file(path):
    result = check(path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", INVALID, ids=vector_id)
def test_loader_refuses_an_invalid_vector_with_the_reason_of_its_directory(path):
    result = check(path)
    assert result.returncode == 1
    assert result.stderr.startswith(f"reason: {path.parent.name}:"), result.stderr


def test_loader_has_a_vector_for_every_reason_of_every_schema():
    found: dict[str, set[str]] = {}
    for path in INVALID:
        found.setdefault(path.parent.parent.parent.name, set()).add(path.parent.name)
    assert {name: sorted(reasons) for name, reasons in found.items()} == {
        name: sorted(reasons) for name, reasons in REASONS.items()
    }
    assert set(REASONS) == set(FILES)


def test_loader_has_a_valid_vector_for_every_schema():
    assert {p.parent.parent.name for p in VALID} == set(FILES)


@pytest.mark.parametrize("contract", CONTRACTS.values(), ids=lambda c: c.name)
def test_loader_check_sh_runs_the_loader(contract):
    script = contract / "check.sh"
    for pinned in [p for p in PINNED if p.parent == contract]:
        assert subprocess.run([script, pinned], capture_output=True).returncode == 0
    bad = sorted(contract.glob("vectors/*/invalid/schema-violation/*"))[0]
    result = subprocess.run([script, bad], capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("reason: schema-violation:")
    assert subprocess.run([script], capture_output=True).returncode == 2


def test_loader_takes_the_name_from_as(tmp_path):
    copy = tmp_path / "file.yaml"
    copy.write_text(PINNED[0].read_text())
    assert check(copy, "--as", "topology").returncode == 0
    result = check(copy, "--as", "tenancy")
    assert result.returncode == 1
    assert result.stderr.startswith("reason: schema-violation:")


def test_loader_usage_errors(tmp_path):
    unnamed = tmp_path / "unnamed.yaml"
    unnamed.write_text("{}\n")
    assert check(unnamed).returncode == 2
    assert check(tmp_path / "missing.yaml", "--as", "topology").returncode == 2
    assert check(unnamed, "--as", "no-such-schema").returncode == 2
    bare = subprocess.run(
        [sys.executable, "-m", "wdf_broker.contract", str(PINNED[0])], capture_output=True
    )
    assert bare.returncode == 2


def test_loader_refuses_a_file_that_is_not_utf8(tmp_path):
    latin = tmp_path / "tenancy.yaml"
    latin.write_bytes(b"profiles: {\xe9: 1}\n")
    result = check(latin)
    assert result.returncode == 1
    assert result.stderr.startswith("reason: schema-violation:")


def test_dlq_policy_has_no_ttl():
    policy = topology().dlq_policy
    assert policy.definition.message_ttl is None
    assert policy.definition.delivery_limit == -1
    for vector in sorted((CONTRACTS["broker"] / "vectors/topology/invalid/dlq-ttl-set").iterdir()):
        result = check(vector)
        assert result.returncode == 1
        assert result.stderr.startswith("reason: dlq-ttl-set:"), result.stderr
