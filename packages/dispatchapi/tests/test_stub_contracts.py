"""The stub of Temporal passes the conformance test that the Temporal gateway passes, and the test
refuses a gateway that breaks the rules control relies on."""

import pytest
from wdf_dispatchapi.ports import SIGNALS, NoWorkflow, TemporalUnavailable
from wdf_dispatchapi.testing import FakeGateway, gateway_contract
from wdf_workflow.contract import naming
from wdf_workflow.models import ControlSignal

JOB = "7d1c3a52-5a8b-4f3e-9d0e-2b6f1c4e8a90"


class SendsResume(FakeGateway):
    """A gateway that lets a resume through as a newer pause would."""

    def signal(self, job_id, name, signal):
        if name == "resume":
            self.workflows[job_id] = self._apply(self.workflows[job_id], "pause", signal)
            return
        super().signal(job_id, name, signal)


class DropsSignals(FakeGateway):
    """A gateway whose signals never reach the workflow."""

    def _apply(self, status, name, signal):
        return status


class AlwaysThere(FakeGateway):
    def exists(self, job_id):
        return True


def started(gateway: FakeGateway) -> FakeGateway:
    gateway.start(JOB)
    return gateway


def test_gateway_contract_holds_for_the_fake():
    gateway = started(FakeGateway())
    gateway_contract(gateway, JOB)
    assert [(name, s.control_seq) for _, name, s in gateway.signals] == [("pause", 1)] * 2


@pytest.mark.parametrize(
    "gateway,broken",
    [
        (SendsResume, "a resume was sent, which only admission sends"),
        (DropsSignals, "a pause with control_seq 1 never reached the workflow"),
        (AlwaysThere, "exists() is not False for a job with no workflow"),
    ],
)
def test_gateway_contract_names_what_a_broken_gateway_breaks(gateway, broken):
    with pytest.raises(AssertionError) as raised:
        gateway_contract(started(gateway()), JOB, timeout_s=0.3)
    assert broken in str(raised.value).split("; ")


def test_the_api_sends_every_signal_of_naming_but_resume():
    assert set(naming().signals) == {*SIGNALS, "resume"}


def test_fake_closed_workflow_exists_and_answers_but_takes_no_signal():
    gateway = started(FakeGateway())
    gateway.close(JOB)
    assert gateway.exists(JOB)
    assert gateway.status(JOB).job_status == "running"
    with pytest.raises(NoWorkflow):
        gateway.signal(JOB, "cancel", ControlSignal(control_seq=1, reason=None))


def test_fake_outage_answers_nothing():
    gateway = started(FakeGateway())
    gateway.available = False
    for call in (
        lambda: gateway.exists(JOB),
        lambda: gateway.status(JOB),
        lambda: gateway.signal(JOB, "pause", ControlSignal(control_seq=1, reason=None)),
    ):
        with pytest.raises(TemporalUnavailable):
            call()
    assert gateway.signals == []
