"""The summary format and the exit mapping of the smoke recipe contract."""

import pytest
from wft_acceptance.report import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    Row,
    count_line,
    exit_code,
    fail,
    ok,
    skip,
)

# The summary example of the recipe contract.
EXAMPLE = [
    ok("cluster-verify", "ci: 10 ok, 0 failed"),
    ok("api", "dispatch and control /readyz ready"),
    ok("broker", "0 dead letters before the job"),
    ok("temporal", "namespace wft, build exit-a current (made current), deployed exit-a"),
    ok("placement", "3 pools"),
    ok("job-submit", "accepted 0b6e2f0a-..."),
    ok("job-run", "succeeded, build ran (attempt 1)"),
    fail("job-result", "echo blocks differ: <first differing key>"),
    skip("job-aggregate", "after job-result failed"),
    ok("dead-letters", "0, no rise"),
]


def test_row_line_format():
    assert [row.line for row in EXAMPLE] == [
        "ok cluster-verify ci: 10 ok, 0 failed",
        "ok api dispatch and control /readyz ready",
        "ok broker 0 dead letters before the job",
        "ok temporal namespace wft, build exit-a current (made current), deployed exit-a",
        "ok placement 3 pools",
        "ok job-submit accepted 0b6e2f0a-...",
        "ok job-run succeeded, build ran (attempt 1)",
        "FAIL job-result echo blocks differ: <first differing key>",
        "skip job-aggregate after job-result failed",
        "ok dead-letters 0, no rise",
    ]
    with pytest.raises(ValueError, match="unknown verdict"):
        Row("warn", "api", "slow")


def test_count_line_buckets():
    line = count_line("ci", EXAMPLE, 94.2)
    print(line)
    assert line == "smoke ci: 8 ok, 1 failed, 1 skipped, 94 s"
    assert count_line("ci", [], 0) == "smoke ci: 0 ok, 0 failed, 0 skipped, 0 s"


def test_exit_mapping():
    assert exit_code(EXAMPLE[:7]) == EXIT_OK == 0
    assert exit_code(EXAMPLE) == EXIT_FAILED == 1
    assert exit_code([ok("api", "ready"), skip("placement", "after api")]) == EXIT_FAILED
    assert exit_code([fail("budget", "300 s ran out in job-run")]) == EXIT_FAILED
    assert exit_code([], passed_through=2) == EXIT_USAGE == 2
    assert exit_code([], passed_through=4) == EXIT_REFUSED == 4
    with pytest.raises(ValueError, match="passes through"):
        exit_code([], passed_through=1)
