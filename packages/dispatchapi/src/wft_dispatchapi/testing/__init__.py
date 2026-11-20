"""The stub of Temporal for control and the reads at T0, and the conformance test that holds the
stub and the Temporal gateway to the same behaviour."""

import threading
import time
import uuid

from wft_workflow.models import ControlSignal, StatusQuery, TaskStatus

from ..ports import SIGNALS, NoWorkflow, SignalName, TemporalUnavailable

__all__ = ["FakeGateway", "gateway_contract", "running_status"]


def running_status() -> StatusQuery:
    """The answer of a running job with one running task and no control applied."""
    task = TaskStatus(
        id="task",
        state="running",
        task_attempt=1,
        guest_left=0,
        infra_left=0,
        reschedules=0,
        last_class=None,
        checkpoint=None,
    )
    return StatusQuery(
        job_status="running", pause_reason=None, resume_counter=0, control_seq=0, tasks=[task]
    )


class FakeGateway:
    """Workflows held in memory, which take signals by the sequence rule of JobWorkflow.

    A test starts and closes workflows, sets the answer of a status query in `workflows`, reads
    every accepted signal from `signals`, and sets `available` to False for a Temporal outage.
    Deleting a job from `workflows` purges its workflow.
    """

    def __init__(self) -> None:
        self.workflows: dict[str, StatusQuery] = {}
        self.closed: set[str] = set()
        self.signals: list[tuple[str, str, ControlSignal]] = []
        self.available = True
        self._lock = threading.Lock()

    def start(self, job_id: str, status: StatusQuery | None = None) -> None:
        with self._lock:
            self.workflows[job_id] = status or running_status()
            self.closed.discard(job_id)

    def close(self, job_id: str) -> None:
        with self._lock:
            self.closed.add(job_id)

    def exists(self, job_id: str) -> bool:
        with self._lock:
            self._answer()
            return job_id in self.workflows

    def signal(self, job_id: str, name: SignalName, signal: ControlSignal) -> None:
        if name not in SIGNALS:
            raise ValueError(f"the API never sends the signal {name!r}")
        with self._lock:
            self._answer()
            if job_id not in self.workflows or job_id in self.closed:
                raise NoWorkflow(job_id)
            self.signals.append((job_id, name, signal))
            self.workflows[job_id] = self._apply(self.workflows[job_id], name, signal)

    def status(self, job_id: str) -> StatusQuery:
        with self._lock:
            self._answer()
            if job_id not in self.workflows:
                raise NoWorkflow(job_id)
            return self.workflows[job_id]

    def _answer(self) -> None:
        if not self.available:
            raise TemporalUnavailable("the fake is down")

    @staticmethod
    def _apply(status: StatusQuery, name: str, signal: ControlSignal) -> StatusQuery:
        """A cancel or terminate always takes its sequence, and a pause only a newer one."""
        if name != "pause" or signal.control_seq > status.control_seq:
            seq = max(status.control_seq, signal.control_seq)
            return status.model_copy(update={"control_seq": seq})
        return status


def gateway_contract(gateway, job_id: str, timeout_s: float = 10.0) -> None:
    """Raises AssertionError, naming every broken rule, unless `gateway` behaves as control and
    the reads rely on.

    `job_id` names an open workflow whose status query answers and that no control signal has
    reached. The check leaves it with a pause applied.
    """
    violations = []
    absent = str(uuid.uuid4())
    if gateway.exists(absent) is not False:
        violations.append("exists() is not False for a job with no workflow")
    for call, args in (
        ("status", (absent,)),
        ("signal", (absent, "pause", ControlSignal(control_seq=1, reason=None))),
    ):
        try:
            getattr(gateway, call)(*args)
            violations.append(f"{call}() of a job with no workflow did not raise NoWorkflow")
        except NoWorkflow:
            pass
    if gateway.exists(job_id) is not True:
        violations.append("exists() is not True for an open workflow")
    applied = gateway.status(job_id).control_seq
    seq = applied + 1
    try:
        gateway.signal(job_id, "resume", ControlSignal(control_seq=seq, reason=None))
        violations.append("a resume was sent, which only admission sends")
    except ValueError:
        pass
    if gateway.status(job_id).control_seq != applied:
        violations.append("a refused resume changed the workflow's control_seq")
    pause = ControlSignal(control_seq=seq, reason="gateway contract")
    gateway.signal(job_id, "pause", pause)
    if not _reaches(gateway, job_id, seq, timeout_s):
        violations.append(f"a pause with control_seq {seq} never reached the workflow")
    gateway.signal(job_id, "pause", pause)
    if gateway.status(job_id).control_seq != seq:
        violations.append("a repeated pause changed the workflow's control_seq")
    if violations:
        raise AssertionError("; ".join(violations))


def _reaches(gateway, job_id: str, seq: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while gateway.status(job_id).control_seq != seq:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True
