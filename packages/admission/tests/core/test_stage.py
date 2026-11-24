"""The admission stage against its declaration in the stage contract."""

from dsy_admission import stage
from dsy_stages import Facts, run_stage_contract
from dsy_stages.contract import sample_event


def test_stage_contract_admission():
    run_stage_contract("admission", stage.admission)


def test_dead_lettered_carries_the_given_reason():
    refused = {"dlq_reason": "UNSUPPORTED_VERSION", "detail": "unsupported-major: 2.0"}
    facts = Facts(emit=("job.dead.lettered",), data={"job.dead.lettered": refused})
    [event] = stage.admission(sample_event("job.accepted", Facts()), facts)
    assert (event.source, event.data) == ("/dsy/admission", refused)
