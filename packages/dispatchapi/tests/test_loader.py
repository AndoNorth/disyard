"""The loader accepts the pinned files and the valid vectors, and each invalid vector fails with
the reason of its directory."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from wft_dispatchapi import contract
from wft_dispatchapi.models import FILES, HEADER_LINE_MAX_BYTES

ROOT = Path(__file__).resolve().parents[3]
API = ROOT / "contracts/api"
PINNED = [
    API / "routes.yaml",
    API / "reasons.yaml",
    API / "control.yaml",
    API / "vectors/fingerprint.json",
]
VALID = sorted(API.glob("vectors/*/valid/*"))
INVALID = sorted(API.glob("vectors/*/invalid/*/*"))
CODES = ["schema-violation", "unknown-reason", "reason-scope"]
# The reasons that apply to each schema, in check order. The too-large vector of batch-header is
# above the 500 KiB that the large-file hook allows, so test_header_line_too_large builds it.
REASONS = {
    "routes": ["schema-violation"],
    "reasons": ["schema-violation"],
    "control": [
        *CODES,
        "transition-not-allowed",
        "event-not-of-stage",
        "control-cell-missing",
    ],
    "fingerprint": ["schema-violation", "fingerprint-mismatch"],
    "batch-header": ["schema-violation"],
    "batch-entry": ["schema-violation", "too-large"],
    "entry-answer": CODES,
    "error-line": CODES,
    "entry-error": CODES,
    "error": CODES,
    **{
        name: ["schema-violation"]
        for name in (
            "summary-line",
            "control-request",
            "control-answer",
            "job-status",
            "object-answer",
            "batch-summary",
            "job-page",
            "depth",
            "health",
        )
    },
}


def check(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "wft_dispatchapi.contract", "--check", *map(str, args)],
        capture_output=True,
        text=True,
    )


def vector_id(path: Path) -> str:
    return str(path.relative_to(API))


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


def test_loader_has_two_valid_vectors_for_every_schema():
    counts = {name: 0 for name in FILES}
    for path in VALID:
        counts[path.parent.parent.name] += 1
    assert {name for name, count in counts.items() if count < 2} == set()


def test_loader_check_sh_runs_the_loader():
    script = API / "check.sh"
    for pinned in PINNED:
        assert subprocess.run([script, pinned], capture_output=True).returncode == 0
    bad = sorted(API.glob("vectors/*/invalid/schema-violation/*"))[0]
    result = subprocess.run([script, bad], capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("reason: schema-violation:")
    assert subprocess.run([script], capture_output=True).returncode == 2


def test_loader_takes_the_name_from_as(tmp_path):
    copy = tmp_path / "file.yaml"
    copy.write_text(PINNED[0].read_text())
    assert check(copy, "--as", "routes").returncode == 0
    result = check(copy, "--as", "reasons")
    assert result.returncode == 1
    assert result.stderr.startswith("reason: schema-violation:")


def test_loader_usage_errors(tmp_path):
    unnamed = tmp_path / "unnamed.yaml"
    unnamed.write_text("{}\n")
    assert check(unnamed).returncode == 2
    assert check(tmp_path / "missing.yaml", "--as", "routes").returncode == 2
    assert check(unnamed, "--as", "no-such-schema").returncode == 2
    bare = subprocess.run(
        [sys.executable, "-m", "wft_dispatchapi.contract", str(PINNED[0])], capture_output=True
    )
    assert bare.returncode == 2


def test_loader_refuses_a_file_that_is_not_utf8(tmp_path):
    latin = tmp_path / "reasons.yaml"
    latin.write_bytes(b"reasons: {\xe9: 1}\n")
    result = check(latin)
    assert result.returncode == 1
    assert result.stderr.startswith("reason: schema-violation:")


def test_header_line_too_large(tmp_path):
    header = {"batch_key": "b", "template": {"pad": ""}}
    pad = HEADER_LINE_MAX_BYTES - len(json.dumps(header, separators=(",", ":")))
    for size, code in ((pad, 0), (pad + 1, 1)):
        header["template"]["pad"] = "x" * size
        line = tmp_path / "header.json"
        line.write_text(json.dumps(header, separators=(",", ":")) + "\n")
        result = check(line, "--as", "batch-header")
        assert result.returncode == code, result.stderr
    assert result.stderr.startswith("reason: too-large:")


def test_control_cells_are_status_moves():
    contract.check(API / "control.yaml", "control")
    vectors = sorted(API.glob("vectors/control/invalid/transition-not-allowed/*"))
    vectors += sorted(API.glob("vectors/control/invalid/event-not-of-stage/*"))
    assert len(vectors) >= 2
    for vector in vectors:
        with pytest.raises(contract.Refused) as refused:
            contract.check(vector, "control")
        assert refused.value.reason == vector.parent.name, vector


def test_pinned_files_load_through_package_data():
    assert {(r.method, r.path) for r in contract.routes().routes} >= {
        ("POST", "/v1/tenants/{tenant}/batches"),
        ("GET", "/readyz"),
    }
    assert "UNKNOWN_TENANT" in {r.code for r in contract.reasons().reasons}
    assert len(contract.control_table().overlay) > 0
