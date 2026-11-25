"""Scenario C1 against a fake job: each verdict without a cluster."""

import pytest
from dsy_acceptance.e2e import control


class FakeJob:
    """Walks a script of status answers, one per read, and holds the last forever. Actions are
    recorded and may change the script."""

    def __init__(self, *script: dict):
        self.script, self.calls = list(script), []

    def submit_one(self, document: dict, batch_key: str) -> str:
        return "job-1"

    def status(self, job_id: str) -> dict:
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def act(self, job_id: str, action: str) -> dict:
        self.calls.append(action)
        return {
            "status": "running",
            "view": {"pause": "pausing", "resume": "resume-pending"}[action],
        }


def at(status: str, view: str | None = None, **tasks: str) -> dict:
    listed = [{"id": name, "state": state} for name, state in tasks.items()]
    return {"status": status, "view": view, "detail": {"tasks": listed}}


RUNNING = at("running", sim="running")
PAUSED = at("paused", sim="paused")
DONE = at("succeeded", sim="succeeded")
FAST = (1.0, 0.3, 1.0)


def run(fake: FakeJob, timeouts=FAST):
    return control.pause_resume(fake, fake.act, {}, "e2e-1", timeouts)


def test_a_paused_and_resumed_job_passes():
    fake = FakeJob(RUNNING, at("running", "pausing", sim="running"), PAUSED, DONE)
    trail = run(fake)
    assert fake.calls == ["pause", "resume"]
    assert trail.states == ["running", "pausing", "paused", "resume-pending", "succeeded"]


def test_a_job_that_never_pauses_names_its_last_state():
    fake = FakeJob(RUNNING, at("running", "pausing", sim="running"))
    with pytest.raises(AssertionError, match=r"no pause of job-1 .*last pausing"):
        run(fake)


def test_a_slot_that_is_not_released_fails():
    fake = FakeJob(RUNNING, at("paused", sim="running"))
    with pytest.raises(AssertionError, match=r"paused but tasks \['sim'\] still hold a slot"):
        run(fake)


def test_a_resume_that_does_not_finish_names_its_last_state():
    fake = FakeJob(RUNNING, PAUSED, at("running", "resume-pending", sim="ready"))
    with pytest.raises(AssertionError, match=r"no success of job-1 .*last resume-pending"):
        run(fake)


def test_a_job_that_ends_before_the_pause_fails():
    fake = FakeJob(RUNNING, DONE)
    with pytest.raises(AssertionError, match=r"ended succeeded before pause"):
        run(fake)
