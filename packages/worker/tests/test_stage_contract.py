"""The worker reads of its task request only what the worker stage of stages.yaml declares."""

from pathlib import Path

import pytest
from wft_stages import Facts, run_stage_contract
from wft_stages.contract import ContractViolation
from wft_worker import read_request
from wft_workflow.models import TaskInput

VECTORS = Path(__file__).resolve().parents[3] / "contracts/activity/vectors/task-input/valid"


def worker_stage(also_reads: tuple[str, ...] = ()):
    """The worker as a stage: it reads its request through read_request, and then `also_reads`."""

    def stage(inbound, facts: Facts) -> list:
        read_request(inbound)
        for field in also_reads:
            inbound.data[field]
        return []

    return stage


def test_stage_contract_worker():
    run_stage_contract("worker", worker_stage())


@pytest.mark.parametrize("vector", sorted(p.name for p in VECTORS.glob("*.json")))
def test_stage_contract_worker_on_each_task_input_vector(vector):
    task = TaskInput.model_validate_json((VECTORS / vector).read_text())
    run_stage_contract("worker", worker_stage(), [(task.request, Facts())])


@pytest.mark.parametrize("field", ["memory_mib", "cpu_cores"])
def test_stage_contract_refuses_a_worker_that_reads_the_slot_of_the_request(field):
    with pytest.raises(ContractViolation, match=field):
        run_stage_contract("worker", worker_stage((field,)))
