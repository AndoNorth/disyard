"""The smoke summary and exit codes of the recipe contract.

Each row prints one line in `cluster-verify`'s format, `ok|FAIL|skip <row> <detail>`, and a count
line closes the run, so both summaries read alike.
"""

from collections.abc import Iterable
from dataclasses import dataclass

OK = "ok"
FAILED = "FAIL"
SKIPPED = "skip"

EXIT_OK = 0
# A row failed, or the budget ran out.
EXIT_FAILED = 1
# An unsupported preset, a missing host tool, or cluster-verify's own usage exit.
EXIT_USAGE = 2
# No cluster dsy, one started for another preset, or the cluster lock was not taken in time.
EXIT_REFUSED = 4


@dataclass(frozen=True)
class Row:
    verdict: str
    name: str
    detail: str

    def __post_init__(self):
        if self.verdict not in (OK, FAILED, SKIPPED):
            raise ValueError(f"unknown verdict {self.verdict!r}")

    @property
    def line(self) -> str:
        return f"{self.verdict} {self.name} {self.detail}"


def ok(name: str, detail: str) -> Row:
    return Row(OK, name, detail)


def fail(name: str, detail: str) -> Row:
    return Row(FAILED, name, detail)


def skip(name: str, reason: str) -> Row:
    return Row(SKIPPED, name, reason)


def count_line(preset: str, rows: Iterable[Row], elapsed_s: float, gate: str = "smoke") -> str:
    """Every row counts in exactly one bucket, and `gate` names the level that ran."""
    verdicts = [row.verdict for row in rows]
    return (
        f"{gate} {preset}: {verdicts.count(OK)} ok, {verdicts.count(FAILED)} failed, "
        f"{verdicts.count(SKIPPED)} skipped, {elapsed_s:.0f} s"
    )


def exit_code(rows: Iterable[Row], passed_through: int | None = None) -> int:
    """0 when every row is ok and 1 otherwise, unless cluster-verify's usage or refusal exit ended
    the run, which is passed through as it is."""
    if passed_through is not None:
        if passed_through not in (EXIT_USAGE, EXIT_REFUSED):
            raise ValueError(f"only exit {EXIT_USAGE} or {EXIT_REFUSED} passes through")
        return passed_through
    return EXIT_OK if all(row.verdict == OK for row in rows) else EXIT_FAILED
