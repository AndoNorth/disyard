"""The per-scenario summary of a run, in the row format of the smoke gate: one
`ok|FAIL|skip <scenario> <detail>` line per scenario, then a count line."""

from dataclasses import dataclass, field

from .. import report
from . import ORDER

GATE = "e2e"


@dataclass
class Scenario:
    checks: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)


class Summary:
    """Collects the outcome of every phase of every test, grouped by the scenario id."""

    def __init__(self) -> None:
        self.scenarios: dict[str, Scenario] = {}

    def add(self, scenario: str, when: str, outcome: str, seconds: float, detail: str) -> None:
        """One phase of one test: a `call` that passed counts as a check, a failure in any phase
        fails the scenario, and a skip in any phase skips it."""
        found = self.scenarios.setdefault(scenario, Scenario())
        found.seconds += seconds
        if outcome == "failed":
            found.failures.append(detail)
        elif outcome == "skipped":
            found.skips.append(detail)
        elif when == "call":
            found.checks += 1

    def rows(self) -> list[report.Row]:
        """Scenarios in run order, any not in ORDER after them."""
        known = [name for name in ORDER if name in self.scenarios]
        rows = []
        for name in (*known, *sorted(set(self.scenarios) - set(ORDER))):
            found = self.scenarios[name]
            if found.failures:
                rows.append(report.fail(name, "; ".join(found.failures)))
            elif found.skips and not found.checks:
                rows.append(report.skip(name, "; ".join(found.skips)))
            else:
                noun = "check" if found.checks == 1 else "checks"
                rows.append(report.ok(name, f"{found.checks} {noun}, {found.seconds:.0f} s"))
        return rows

    def lines(self, preset: str, elapsed_s: float) -> list[str]:
        rows = self.rows()
        return [row.line for row in rows] + [report.count_line(preset, rows, elapsed_s, GATE)]
