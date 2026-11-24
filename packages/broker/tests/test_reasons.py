"""The reason table: its rows, its agreement with the job.dead.lettered event and with the
dead-letter counter."""

import subprocess
import sys
from pathlib import Path

import yaml
from dsy_broker.contract import dead_letter_codes, reasons

ROOT = Path(__file__).resolve().parents[3]
MISMATCH = ROOT / "contracts/broker/vectors/reasons/invalid/event-code-mismatch"
METRICS = ROOT / "contracts/instrumentation/metrics.yaml"

# Every row of the pinned table: source, on an intake queue, event and recovery.
ROWS = {
    "MALFORMED": ("application", "expected", "job.dead.lettered", "never"),
    "UNSUPPORTED_VERSION": ("application", "expected", "job.dead.lettered", "after-fix"),
    "ADMISSION_FAILED": ("application", "expected", None, "replay"),
    "delivery_limit": ("broker", "expected", None, "replay"),
    "rejected": ("broker", "not-expected", None, "after-fix"),
    "maxlen": ("broker", "not-expected", None, "after-fix"),
    "expired": ("broker", "not-expected", None, "after-fix"),
}


def check(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "dsy_broker.contract", "--check", str(path)],
        capture_output=True,
        text=True,
    )


def test_reasons_match_event_enum():
    emitted = {row.code for row in reasons().reasons if row.event == "job.dead.lettered"}
    assert emitted == dead_letter_codes() == {"MALFORMED", "UNSUPPORTED_VERSION"}
    vectors = sorted(MISMATCH.iterdir())
    assert "adds-invalid-dag.yaml" in [v.name for v in vectors]
    for vector in vectors:
        result = check(vector)
        assert result.returncode == 1, vector
        assert result.stderr.startswith("reason: event-code-mismatch:"), result.stderr


def test_reason_rows_are_those_of_the_spec():
    rows = {
        row.code: (row.source, row.on_intake, row.event, row.recovery) for row in reasons().reasons
    }
    assert rows == ROWS


def test_dead_letter_counter_counts_the_application_codes():
    metrics = yaml.safe_load(METRICS.read_text())["metrics"]
    application = [row.code for row in reasons().reasons if row.source == "application"]
    counted = metrics["dsy_broker_dead_letter_total"]["labels"]["reason"]["values"]
    assert counted == application
