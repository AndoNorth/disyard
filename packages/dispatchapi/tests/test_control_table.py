"""The decision function equals the pinned control table and the views of the status contract,
over every combination of status, workflow, outstanding action, action and patch."""

from itertools import product

import pytest
import yaml
from wdf_dispatchapi import contract
from wdf_dispatchapi.control_table import ASK_GATEWAY, NOOP, TERMINAL, Decision, decide, view
from wdf_dispatchapi.models import CONTRACT, ControlTable

STATUS = yaml.safe_load((CONTRACT / "status.yaml").read_text())
STATUSES = STATUS["statuses"]
ACTIONS = contract.ACTIONS
OUTSTANDING = (None, *ACTIONS)
WORKFLOWS = {"with": True, "without": False}


def workflow_cases(status: str) -> tuple[str, ...]:
    return contract.WORKFLOW_CASES.get(status, contract.EITHER)


def patch_cases(action: str) -> tuple[str, ...]:
    return contract.PATCH_CASES if action == "resume" else ("none",)


def combinations():
    """Every (status, has_workflow, outstanding, action, patch) the table decides."""
    for status, outstanding, action in product(STATUSES, OUTSTANDING, ACTIONS):
        for workflow, patch in product(workflow_cases(status), patch_cases(action)):
            yield status, WORKFLOWS[workflow], outstanding, action, patch


def expected(table: ControlTable, status, has_workflow, outstanding, action, patch) -> Decision:
    """The decision that the table file gives, read as its comments say."""
    if outstanding is not None:
        (rule,) = [r for r in table.overlay if r.outstanding == outstanding and action in r.action]
        if rule.rule == "noop":
            return NOOP
        if rule.rule == "refused":
            return Decision("refused", rule.refusal)
    workflow = "with" if has_workflow else "without"
    (cell,) = [
        c
        for c in table.cells
        if status in c.status
        and c.workflow in (workflow, "either")
        and c.action == action
        and patch in c.patch
    ]
    return Decision(
        cell.outcome,
        cell.refusal,
        cell.request,
        tuple(cell.moves),
        cell.pending_patch == "set-or-clear",
        tuple(cell.events),
    )


def mismatches(table: ControlTable) -> list[tuple]:
    return [
        combination
        for combination in combinations()
        if decide(*combination) != expected(table, *combination)
    ]


def test_function_equals_control_yaml():
    # 17 (status, workflow) pairs, 5 outstanding actions and 6 (action, patch) pairs.
    assert len(list(combinations())) == 510
    assert mismatches(contract.control_table()) == []


def changed(cell: dict) -> dict:
    """The cell with its outcome changed, still a valid cell of the contract."""
    keep = {k: cell[k] for k in ("status", "workflow", "action", "patch") if k in cell}
    if cell["outcome"] == "noop":
        return {**keep, "outcome": "refused", "refusal": "STOPPING"}
    return {**keep, "outcome": "noop"}


PINNED = yaml.safe_load((CONTRACT / "control.yaml").read_text())


@pytest.mark.parametrize("index", range(len(PINNED["cells"])))
def test_function_equals_control_yaml_fails_on_a_copy_with_one_cell_changed(index):
    copy = {**PINNED, "cells": list(PINNED["cells"])}
    copy["cells"][index] = changed(copy["cells"][index])
    table = contract.parse("control", yaml.safe_dump(copy))
    assert mismatches(table) != []


@pytest.mark.parametrize("rule", [r for r in PINNED["overlay"] if r["rule"] != "table"], ids=str)
def test_function_equals_control_yaml_fails_on_a_copy_with_one_overlay_rule_changed(rule):
    copy = {**PINNED, "overlay": [r for r in PINNED["overlay"] if r is not rule]}
    copy["overlay"].append(
        {"outstanding": rule["outstanding"], "action": rule["action"], "rule": "table"}
    )
    table = contract.parse("control", yaml.safe_dump(copy))
    assert mismatches(table) != []


def full_product():
    """Every combination, with a workflow the gateway did not answer for added."""
    for status, has_workflow, outstanding, action, patch in combinations():
        yield status, has_workflow, outstanding, action, patch
        if has_workflow and status not in ("admitted", "running"):
            yield status, None, outstanding, action, patch


def test_nothing_leaves_a_terminal_status():
    for status, has_workflow, outstanding, action, patch in full_product():
        if status in TERMINAL:
            decision = decide(status, has_workflow, outstanding, action, patch)
            assert decision.outcome in ("noop", "refused"), (status, outstanding, action)
            assert decision == NOOP or decision.refusal is not None


def after(status, has_workflow, outstanding, action, decision):
    """The row once the decision's writes committed: an applied cell ends with the move that
    clears its own request, and a requested one leaves its action outstanding."""
    if decision.moves:
        status = decision.moves[-1].to
    if decision.outcome == "applied":
        return status, False if status == "accepted" else has_workflow, None
    if decision.outcome == "requested":
        return status, has_workflow, action
    return status, has_workflow, outstanding


def test_repeat_is_a_noop():
    for status, has_workflow, outstanding, action, patch in full_product():
        first = decide(status, has_workflow, outstanding, action, patch)
        row = after(status, has_workflow, outstanding, action, first)
        repeat = "same" if first.set_patch and patch != "none" else patch
        second = decide(*row, action, repeat)
        case = (status, has_workflow, outstanding, action, patch, first, second)
        assert second.outcome in ("noop", "refused"), case
        if first.outcome in ("requested", "noop"):
            assert second == NOOP, case


def test_row_only_only_without_a_workflow():
    for status, has_workflow, outstanding, action, patch in full_product():
        decision = decide(status, has_workflow, outstanding, action, patch)
        if any(move.row_only for move in decision.moves):
            assert has_workflow is False, (status, has_workflow, outstanding, action, patch)


def test_unknown_workflow_requests_a_stop_and_refuses_a_resume():
    for status, outstanding, action, patch in product(
        sorted(ASK_GATEWAY), OUTSTANDING, ACTIONS, contract.PATCH_CASES
    ):
        if action != "resume" and patch != "none":
            continue
        unknown = decide(status, None, outstanding, action, patch)
        if action == "resume" and outstanding in (None, "resume"):
            assert unknown == Decision("refused", "TEMPORAL_UNAVAILABLE")
        else:
            assert unknown == decide(status, True, outstanding, action, patch)


@pytest.mark.parametrize(
    ("status", "has_workflow"), [("accepted", True), ("admitted", False), ("running", False)]
)
def test_a_workflow_that_contradicts_the_status_is_refused(status, has_workflow):
    with pytest.raises(ValueError):
        decide(status, has_workflow, None, "cancel")


@pytest.mark.parametrize("action", ["cancel", "terminate", "pause"])
def test_only_a_resume_carries_a_patch(action):
    with pytest.raises(ValueError):
        decide("paused", True, None, action, "other")


def test_view_equals_status_yaml():
    for status, outstanding in product(STATUSES, OUTSTANDING):
        shown = [
            name
            for name, v in STATUS["views"].items()
            if v["over"] == f"job.{outstanding}.requested" and status in v["shown_while"]
        ]
        assert view(status, outstanding) == (shown[0] if shown else None), (status, outstanding)
