"""Scenario C1: a running paced job is paused and resumed through the control API.

The scenario takes its clients as arguments, so a fake drives every verdict without a cluster.
"""

import json
from collections.abc import Callable
from typing import Protocol

from dsy_workflow.harness.cluster import TERMINAL

from ..clients import api
from . import wait

# A level 1 workload of about 35 s in the 250m simulate slot, so a pause lands while it runs.
BURN = {"workload": "cpu_burn", "args": {"units": 6000, "heartbeat_every": 100}, "seed": 7}
# The states a paused and resumed job shows, in order, with others allowed between them.
SEQUENCE = ("pausing", "paused", "succeeded")
PAUSE_S = 60.0
RUN_S = 120.0
END_S = 240.0


class Jobs(Protocol):
    def submit_one(self, document: dict, batch_key: str) -> str: ...

    def status(self, job_id: str) -> dict: ...


def shown(answer: dict) -> str:
    """What a status read or a control answer shows of a job: its view, else its status."""
    return answer.get("view") or answer["status"]


def running_tasks(answer: dict) -> list[str]:
    return [
        t["id"] for t in (answer.get("detail") or {}).get("tasks", []) if t["state"] == "running"
    ]


class Trail:
    """The states a job showed, each once in a run of the same state."""

    def __init__(self) -> None:
        self.states: list[str] = []

    def add(self, answer: dict) -> dict:
        state = shown(answer)
        if not self.states or self.states[-1] != state:
            self.states.append(state)
        return answer

    @property
    def last(self) -> str:
        return self.states[-1] if self.states else "none"


def until(
    jobs: Jobs, job_id: str, trail: Trail, done: Callable[[dict], bool], timeout_s: float, what: str
) -> dict:
    """Polls the status until `done` holds, recording each state. A timeout is an AssertionError
    that names the state seen last."""

    def check() -> dict | None:
        answer = trail.add(jobs.status(job_id))
        if answer["status"] in TERMINAL and not done(answer):
            raise AssertionError(f"{job_id} ended {answer['status']} before {what}: {trail.states}")
        return answer if done(answer) else None

    try:
        return wait(check, timeout_s, f"{what} of {job_id}")
    except TimeoutError:
        raise AssertionError(
            f"no {what} of {job_id} in {timeout_s:.0f} s, last {trail.last}: {trail.states}"
        ) from None


def pause_resume(
    jobs: Jobs,
    act: Callable[[str, str], dict],
    document: dict,
    batch_key: str,
    timeouts: tuple[float, float, float] = (RUN_S, PAUSE_S, END_S),
) -> Trail:
    """Submits the paced job, pauses it while its simulate task runs, checks the pause released
    the slot, resumes it and waits for `succeeded`. Returns the states seen."""
    run_s, pause_s, end_s = timeouts
    job_id = jobs.submit_one(document, batch_key)
    trail = Trail()

    def simulating(answer: dict) -> bool:
        return answer["status"] == "running" and "sim" in running_tasks(answer)

    until(jobs, job_id, trail, simulating, run_s, "running simulate task")
    trail.add(act(job_id, "pause"))
    paused = until(jobs, job_id, trail, lambda a: a["status"] == "paused", pause_s, "pause")
    if held := running_tasks(paused):
        raise AssertionError(f"{job_id} is paused but tasks {held} still hold a slot")
    trail.add(act(job_id, "resume"))
    until(jobs, job_id, trail, lambda a: a["status"] == "succeeded", end_s, "success")
    rest = iter(trail.states)
    if not all(state in rest for state in SEQUENCE):
        raise AssertionError(f"{job_id} showed {trail.states}, not {list(SEQUENCE)} in order")
    return trail


def control_call(client: api.Api) -> Callable[[str, str], dict]:
    """`act(job_id, action)` for pause and resume on the control listener of `client`."""

    def act(job_id: str, action: str) -> dict:
        url = client.url(client.control, f"jobs/{job_id}/{action}")
        code, answer = api.request(
            url, "POST", json.dumps({"reason": "e2e"}).encode(), client.timeout_s
        )
        if code != 200:
            raise api.Failed(f"the {action} of {job_id} answered {code}: {answer}")
        return answer

    return act
