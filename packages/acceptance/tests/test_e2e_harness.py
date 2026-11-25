"""The e2e harness without a cluster: baselines, polling and the per-scenario summary."""

import pytest
from dsy_acceptance import e2e, report
from dsy_acceptance.e2e.baseline import Baseline
from dsy_acceptance.e2e.summary import Summary


def test_batch_keys_are_unique_and_prefixed():
    keys = {e2e.batch_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(k.startswith("e2e-") and len(k) == len("e2e-") + 12 for k in keys)


def test_wait_returns_the_first_truthy_value():
    answers = iter([0, None, "ready"])
    assert e2e.wait(lambda: next(answers), 5, "ready", every_s=0.01) == "ready"


def test_wait_names_what_it_waited_for():
    with pytest.raises(TimeoutError, match="no thing within 0 s"):
        e2e.wait(lambda: None, 0, "thing", every_s=0.01)


class Counter:
    def __init__(self, *values: float):
        self.values = list(values)

    def read(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def test_a_baseline_reads_the_change():
    counter = Counter(5, 5, 6)
    taken = Baseline("completed", counter.read)
    assert taken.take() == 5
    assert taken.rose_by(1, timeout_s=5) == 1


def test_a_baseline_not_taken_is_an_error():
    with pytest.raises(RuntimeError, match="not taken"):
        Baseline("completed", lambda: 0).delta()


def test_a_counter_that_never_rises_fails_with_the_change_seen():
    taken = Baseline("completed", lambda: 3)
    taken.take()
    with pytest.raises(AssertionError, match="completed rose by 0 in 0 s, not 1"):
        taken.rose_by(1, timeout_s=0)


def test_a_counter_that_rises_too_far_fails_at_once():
    counter = Counter(0, 0, 2)
    taken = Baseline("completed", counter.read)
    taken.take()
    with pytest.raises(AssertionError, match="rose by 2, above the 1 asked"):
        taken.rose_by(1, timeout_s=5)


def test_the_summary_is_one_row_per_scenario_in_run_order_and_a_count_line():
    summary = Summary()
    summary.add("N5", "call", "passed", 2.0, "")
    summary.add("H1", "setup", "passed", 0.2, "")
    summary.add("H1", "call", "passed", 40.0, "")
    summary.add("H1", "call", "passed", 1.0, "")
    summary.add("N1", "call", "failed", 3.0, "AssertionError: no retry")
    summary.add("N1", "teardown", "passed", 0.0, "")
    summary.add("N2", "setup", "skipped", 0.0, "stack off")
    assert summary.lines("ci", 61.4) == [
        "ok H1 2 checks, 41 s",
        "FAIL N1 AssertionError: no retry",
        "skip N2 stack off",
        "ok N5 1 check, 2 s",
        "e2e ci: 2 ok, 1 failed, 1 skipped, 61 s",
    ]


def test_a_failed_teardown_fails_the_scenario():
    summary = Summary()
    summary.add("N5", "call", "passed", 1.0, "")
    summary.add("N5", "teardown", "failed", 1.0, "admission not restored")
    assert [row.verdict for row in summary.rows()] == [report.FAILED]


def test_the_count_line_names_its_gate():
    assert report.count_line("ci", [], 3, gate="e2e") == "e2e ci: 0 ok, 0 failed, 0 skipped, 3 s"
