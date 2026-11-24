"""The control retrier and the resume finisher over rows in Postgres, against a dev server that
keeps its state over a stop, or against FakeGateway where the test steers what Temporal answers.

A request is recorded by the control routes, as an operator's call would record it, and a claim is
played by the guarded move that admission's claim makes.
"""

import asyncio
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

import pytest
from dsy_dispatchapi.control import control_job
from dsy_dispatchapi.gateway import TemporalGateway
from dsy_dispatchapi.models import ControlRequest
from dsy_dispatchapi.ports import NoWorkflow
from dsy_dispatchapi.retrier import BACKOFF_MAX_S, Retrier
from dsy_dispatchapi.testing import FakeGateway
from dsy_events import from_bytes
from dsy_eventstore import events_for_job
from dsy_itest.session import Container
from dsy_jobstate import get_job, mark_published, request_action, transition
from dsy_keys import cloudevent_id, workflow_id
from dsy_stages.recording import type_name
from dsy_workflow.contract import naming
from dsy_workflow.models import ControlSignal
from dsy_workflow.testing import connect
from temporalio.api.enums.v1 import EventType

pytestmark = [pytest.mark.integration, pytest.mark.pair]

LIMIT_S = 180
SIGNALED = EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED


@dataclass(frozen=True)
class Server:
    """A dev server whose state is a file inside its container, so it survives a stop."""

    container: Container

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.container.host_ports[0]}"

    def stop(self) -> None:
        subprocess.run(["docker", "stop", self.container.id], check=True, capture_output=True)

    def start(self) -> None:
        subprocess.run(["docker", "start", self.container.id], check=True, capture_output=True)
        self.container.wait_ready(0)


@pytest.fixture(scope="module")
def server(t1) -> Server:
    with t1.container(
        "temporalio/temporal",
        ports={0: 7233},
        command=(
            "server",
            "start-dev",
            "--ip",
            "0.0.0.0",
            "--headless",
            "--namespace",
            naming().namespace,
            "--db-filename",
            "/tmp/temporal.db",
        ),
    ) as container:
        container.wait_ready(0)
        yield Server(container)


@pytest.fixture
def retrier(logins):
    """A retrier over `gateway`, with a clock the test moves."""

    def make(gateway, **kwargs) -> Retrier:
        return Retrier(logins["control"], logins["reader"], gateway, **kwargs)

    return make


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def claimed(db, row, to: str = "running") -> None:
    """Moves the row as admission and the workflow would, up to `to`."""
    transition(db, row.job_id, "accepted", "admitted")
    for expected, then in (("admitted", "running"), ("running", "paused")):
        if to == expected:
            return
        transition(db, row.job_id, expected, then)


def outbox(db, row) -> tuple:
    found = get_job(db, row.job_id)
    return found.status, found.requested_action, found.control_seq, found.control_delivered_at


async def signals(client, row) -> list[tuple[str, int]]:
    """The signals the job's workflow took, as (name, control_seq) pairs in history order."""
    handle = client.get_workflow_handle(workflow_id(row.job_id))
    taken = []
    async for event in handle.fetch_history_events():
        if event.event_type == SIGNALED:
            attributes = event.workflow_execution_signaled_event_attributes
            [payload] = attributes.input.payloads
            signal = client.data_converter.payload_converter.from_payload(payload, ControlSignal)
            taken.append((attributes.signal_name, signal.control_seq))
    return taken


def test_request_during_outage_delivered_after(server, db, seed, control_conn, retrier, kit):
    """With the dev server stopped, a cancel and a pause are requested and no pass delivers them;
    once it is started again, both are delivered and their delivery recorded."""
    cancelled, paused = seed(2)

    async def body():
        client = await connect(server.address)
        for row in (cancelled, paused):
            await kit.start(client, db, row, task_queue=f"nobody-{uuid.uuid4()}")
            transition(db, row.job_id, "admitted", "running")
        with TemporalGateway(server.address, call_timeout_s=1) as gateway:
            loops = retrier(gateway)
            await asyncio.to_thread(server.stop)
            for row, action in ((cancelled, "cancel"), (paused, "pause")):
                answer = control_job(
                    control_conn, gateway, "acme", row.job_id, action, ControlRequest()
                )
                assert answer.outcome == "requested"
            assert await asyncio.to_thread(loops.deliver) == 0
            assert [outbox(db, row)[3] for row in (cancelled, paused)] == [None, None]
            await asyncio.to_thread(server.start)
            delivered = 0
            while delivered < 2:
                delivered += await asyncio.to_thread(loops.deliver)
                await asyncio.sleep(0.5)
        client = await connect(server.address)
        return [await signals(client, row) for row in (cancelled, paused)]

    taken = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert taken == [[("cancel", 1)], [("pause", 1)]]
    for row, action in ((cancelled, "cancel"), (paused, "pause")):
        status, outstanding, seq, delivered_at = outbox(db, row)
        assert (status, outstanding, seq) == ("running", action, 1)
        assert delivered_at is not None


def test_signal_twice_after_crash_is_harmless(
    server, db, seed, control_conn, retrier, kit, workflow_dsn, monkeypatch
):
    """A pass that dies after the signal and before the delivery is recorded leaves the request
    undelivered, so the next pass signals it again, and the job pauses once."""
    [row] = seed(1)

    async def body():
        client = await connect(server.address)
        async with kit.rig(client, workflow_dsn):
            await kit.start(client, db, row)
            await kit.until(lambda: get_job(db, row.job_id).status == "running", "running row")
            with TemporalGateway(server.address) as gateway:
                control_job(control_conn, gateway, "acme", row.job_id, "pause", ControlRequest())
                with monkeypatch.context() as crash:
                    crash.setattr("dsy_dispatchapi.retrier.mark_delivered", _die)
                    with pytest.raises(Died):
                        await asyncio.to_thread(retrier(gateway).deliver)
                assert outbox(db, row)[3] is None
                await asyncio.to_thread(retrier(gateway).deliver)
                await kit.until(lambda: get_job(db, row.job_id).status == "paused", "paused row")
                status = await asyncio.to_thread(gateway.status, row.job_id)
                taken = await signals(client, row)
                await client.get_workflow_handle(workflow_id(row.job_id)).terminate()
        return status, taken

    status, taken = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert taken == [("pause", 1), ("pause", 1)]
    assert (status.job_status, status.control_seq) == ("paused", 1)
    lifecycle = [type_name(e.type) for e in events_for_job(db, row.job_id) if e.task_id is None]
    assert lifecycle.count("job.paused") == 1
    assert outbox(db, row)[:3] == ("paused", None, 1)


class Died(Exception):
    """The process died before the delivery was recorded."""


def _die(*args) -> None:
    raise Died()


def test_resume_is_never_signalled(server, db, seed, control_conn, retrier, kit):
    """A resume outstanding on a job whose workflow exists is admission's to send: no pass of
    either loop signals it, and the row stays at resume-pending."""
    [row] = seed(1)

    async def body():
        client = await connect(server.address)
        await kit.start(client, db, row, task_queue=f"nobody-{uuid.uuid4()}")
        transition(db, row.job_id, "admitted", "running")
        transition(db, row.job_id, "running", "paused")
        with TemporalGateway(server.address) as gateway:
            answer = control_job(
                control_conn, gateway, "acme", row.job_id, "resume", ControlRequest()
            )
            assert (answer.outcome, answer.status) == ("requested", "resume-pending")
            loops = retrier(gateway)
            assert await asyncio.to_thread(loops.deliver) == 0
            assert await asyncio.to_thread(loops.finish) == 0
        return await signals(client, row)

    assert asyncio.run(asyncio.wait_for(body(), LIMIT_S)) == []
    assert outbox(db, row) == ("resume-pending", "resume", 1, None)


def test_resume_pending_without_workflow_returns_to_accepted(server, db, seed, retrier):
    """A started job whose workflow is gone while its resume waits returns to accepted with the
    row-only `job.resumed`, keeping its receipt, for admission to start again."""
    [row] = seed(1)
    mark_published(db, row.job_id)
    claimed(db, row, to="paused")
    request_action(db, row.job_id, "resume")
    transition(db, row.job_id, "paused", "resume-pending")
    with TemporalGateway(server.address) as gateway:
        assert retrier(gateway).finish() == 1
    after = get_job(db, row.job_id)
    assert (after.status, after.requested_action, after.control_seq) == ("accepted", None, 1)
    assert after.published_at is not None
    [resumed] = [from_bytes(e.body.encode()) for e in events_for_job(db, row.job_id)]
    assert (resumed.type, resumed.source) == ("dev.dsy.job.resumed.v1", "/dsy/control")
    assert resumed.id == cloudevent_id(row.job_id, "", "", "job.resumed", 1)


@pytest.mark.parametrize("action,ended", [("cancel", "cancelled"), ("terminate", "terminated")])
@pytest.mark.parametrize("stopped", ["paused", "resume-pending"])
def test_stop_of_a_stopped_job_without_workflow_ends_on_the_row(
    server, db, seed, retrier, action, ended, stopped
):
    [row] = seed(1)
    claimed(db, row, to="paused")
    if stopped == "resume-pending":
        request_action(db, row.job_id, "resume")
        transition(db, row.job_id, "paused", "resume-pending")
    request_action(db, row.job_id, action)
    seq = get_job(db, row.job_id).control_seq
    with TemporalGateway(server.address) as gateway:
        assert retrier(gateway).deliver() == 1
    assert outbox(db, row)[:3] == (ended, None, seq)
    [event] = [from_bytes(e.body.encode()) for e in events_for_job(db, row.job_id)]
    assert (event.type, event.source) == (f"dev.dsy.job.{ended}.v1", "/dsy/control")
    assert event.id == cloudevent_id(row.job_id, "", "", f"job.{ended}", seq)


def test_reason_is_read_from_the_requested_event(db, seed, control_conn, retrier):
    [row] = seed(1)
    claimed(db, row)
    gateway = FakeGateway()
    gateway.start(row.job_id)
    control_job(control_conn, gateway, "acme", row.job_id, "cancel", ControlRequest(reason="why"))
    assert retrier(gateway).deliver() == 1
    assert gateway.signals == [(row.job_id, "cancel", ControlSignal(control_seq=1, reason="why"))]


class Absent(FakeGateway):
    """A gateway whose workflows are never there, which counts the signals tried."""

    def __init__(self):
        super().__init__()
        self.tried = 0

    def signal(self, job_id, name, signal):
        self.tried += 1
        raise NoWorkflow(job_id)


def test_backoff_doubles_up_to_a_minute_and_never_gives_up(db, seed, retrier):
    [row] = seed(1)
    claimed(db, row)
    request_action(db, row.job_id, "pause")
    gateway, clock = Absent(), Clock()
    loops = retrier(gateway, clock=clock)
    tried_at = []
    for second in range(0, 300):
        clock.now = float(second)
        before = gateway.tried
        loops.deliver()
        if gateway.tried > before:
            tried_at.append(second)
    assert tried_at == [0, 1, 3, 7, 15, 31, 63, 123, 183, 243]
    assert tried_at[-1] - tried_at[-2] == BACKOFF_MAX_S
    assert outbox(db, row)[3] is None


def test_finisher_reads_in_pages_and_starts_again_at_the_end(db, seed, retrier):
    rows = sorted(seed(3), key=lambda row: row.job_id)
    gateway = FakeGateway()
    for row in rows:
        claimed(db, row, to="paused")
        request_action(db, row.job_id, "resume")
        transition(db, row.job_id, "paused", "resume-pending")
        gateway.start(row.job_id)
    loops = retrier(gateway, batch=2)
    assert loops.finish() == 0
    assert loops.after == rows[1].job_id
    del gateway.workflows[rows[0].job_id]
    assert loops.finish() == 0
    assert loops.after is None
    assert loops.finish() == 1
    assert [get_job(db, row.job_id).status for row in rows] == [
        "accepted",
        "resume-pending",
        "resume-pending",
    ]


def test_run_delivers_until_stopped(db, seed, control_conn, retrier):
    """The loop delivers on its cadence, survives a pass that fails, and returns once stopped."""
    [row] = seed(1)
    claimed(db, row)
    gateway = FakeGateway()
    gateway.start(row.job_id)
    control_job(control_conn, gateway, "acme", row.job_id, "pause", ControlRequest())
    loops = retrier(gateway)
    loops.control_dsn, dsn = "postgresql://nobody@127.0.0.1:1/none", loops.control_dsn
    stop = threading.Event()
    runner = threading.Thread(target=loops.run, args=(stop,))
    runner.start()
    try:
        time.sleep(1.5)
        assert gateway.signals == []
        loops.control_dsn = dsn
        deadline = time.monotonic() + 10
        while outbox(db, row)[3] is None:
            assert time.monotonic() < deadline, "the loop delivered nothing"
            time.sleep(0.1)
    finally:
        stop.set()
        runner.join(5)
    assert not runner.is_alive()
    assert [(name, s.control_seq) for _, name, s in gateway.signals] == [("pause", 1)]
