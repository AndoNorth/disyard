"""StaticPlacer passes the placer contract, the contract refuses a placer that breaks it, and the
`place` local activity answers with the placer's verdict."""

import pytest
from temporalio.testing import ActivityEnvironment
from wft_workflow.contract import naming
from wft_workflow.placement import (
    ACTIVITY,
    Placement,
    PlacementRequest,
    PlaceResult,
    StaticPlacer,
    Unschedulable,
    place_activity,
)
from wft_workflow.testing import placer_contract

SIMULATE = PlacementRequest(task_type="simulate", cpu_milli=250, memory_mib=512, run_timeout_s=900)


def test_static_placer_passes_placer_contract():
    placer_contract(StaticPlacer())


def test_static_placer_places_on_the_pool_queue_with_v0_timeouts():
    assert StaticPlacer().place(SIMULATE) == Placement(
        queue="wft-simulate",
        memory_class="512Mi",
        escalated=False,
        schedule_to_start_s=1800,
        start_to_close_s=1020,
        heartbeat_s=60,
    )
    assert StaticPlacer().place(SIMULATE, escalate=True) == StaticPlacer().place(SIMULATE)


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"memory_mib": 513}, "insufficient_memory"),
        ({"cpu_milli": 251}, "insufficient_cpu"),
        ({"task_type": "render"}, "no_matching_pool"),
    ],
)
def test_static_placer_refuses_what_fits_no_slot(change, failure):
    answer = StaticPlacer().place(SIMULATE.model_copy(update=change))
    assert isinstance(answer, Unschedulable)
    assert answer.failure == failure


class Wrong(StaticPlacer):
    """StaticPlacer with one answer changed."""

    def __init__(self, change):
        super().__init__()
        self.change = change

    def place(self, request, escalate=False):
        return self.change(super().place(request, escalate))


def other_queue(answer):
    return answer.model_copy(update={"queue": "wft-gpu"}) if answer.kind == "placement" else answer


def always_escalated(answer):
    return answer.model_copy(update={"escalated": True}) if answer.kind == "placement" else answer


def no_margin(answer):
    if answer.kind != "placement":
        return answer
    return answer.model_copy(update={"start_to_close_s": 600})


def wrong_class(answer):
    if answer.kind != "unschedulable":
        return answer
    return answer.model_copy(update={"failure": "no_matching_pool"})


class ShrinksOnEscalate(StaticPlacer):
    def place(self, request, escalate=False):
        answer = super().place(request, escalate)
        if escalate and answer.kind == "placement":
            return answer.model_copy(update={"memory_class": "1Mi", "escalated": True})
        return answer


def test_placer_contract_refuses_an_escalation_to_a_smaller_class():
    with pytest.raises(AssertionError, match="escalated is True from 1024Mi to 1Mi"):
        placer_contract(ShrinksOnEscalate())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (other_queue, "queue wft-gpu is not"),
        (always_escalated, "escalated without being asked"),
        (no_margin, "leaves no margin"),
        (wrong_class, "expected insufficient_memory"),
    ],
)
def test_placer_contract_refuses_a_placer_that_breaks_it(change, message):
    with pytest.raises(AssertionError, match=message):
        placer_contract(Wrong(change))


def test_place_local_activity_answers_with_the_placer():
    assert ACTIVITY in {a.name for a in naming().activities.local}
    place = place_activity(StaticPlacer())
    result = ActivityEnvironment().run(place, SIMULATE, False)
    assert result == PlaceResult(StaticPlacer().place(SIMULATE))
    too_big = SIMULATE.model_copy(update={"memory_mib": 4096})
    refused = ActivityEnvironment().run(place, too_big, True)
    assert refused.root.failure == "insufficient_memory"
