"""The admission service: its configuration and exit codes, the caps it holds over a run of real
job workflows on a dev server, the resumes it recovers after a restart, its drain on a stop and
the metric names it serves. The broker is FakeBroker, so Postgres and the dev server are the two
containers of the session."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import psycopg
import pytest
from temporalio.api.enums.v1 import EventType
from wdf_admission import service
from wdf_admission.service import ENV, GLOBAL_CAP_ENV, PREFIX_ENV, READY_ENV, Config, ConfigError
from wdf_admission.service.metrics import COUNTERS, GAUGES, HISTOGRAMS
from wdf_broker.tenancy import load
from wdf_broker.testing import FakeBroker
from wdf_broker.topology import intake_queues
from wdf_jobstate import request_action, transition
from wdf_keys import workflow_id
from wdf_workflow.models import StatusQuery

[QUEUE] = intake_queues(load("ci"))
LIMIT_S = 600
# Every variable the service needs, with values it refuses or never reaches before it connects.
FULL = {
    "WDF_TEMPORAL_ADDRESS": "127.0.0.1:1",
    "WDF_TEMPORAL_NAMESPACE": "wdf",
    "WDF_ADMISSION_DSN": "postgresql://admission@127.0.0.1:1/appdb",
    "WDF_BROKER_ADDRESS": "127.0.0.1:1",
    "WDF_BROKER_PASSWORD": "standin",
    "WDF_TENANCY_PROFILE": "ci",
    "WDF_METRICS_ADDRESS": "127.0.0.1:1",
}


def run_service(env: dict[str, str]) -> subprocess.CompletedProcess:
    """Runs `python -m wdf_admission.service` with only `env` of the WDF variables."""
    base = {name: value for name, value in os.environ.items() if not name.startswith("WDF_")}
    return subprocess.run(
        [sys.executable, "-m", "wdf_admission.service"],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def stale_ready(tmp_path) -> dict[str, str]:
    """A ready file left by a killed process, and the variable that names it."""
    ready = tmp_path / "ready"
    ready.touch()
    return {READY_ENV: str(ready)}


def test_config_from_env():
    config = Config.from_env({**FULL, GLOBAL_CAP_ENV: "5", PREFIX_ENV: "h-0a1b2c3d-"})
    assert (config.global_cap, config.prefix, config.profile) == (5, "h-0a1b2c3d-", "ci")
    assert Config.from_env(FULL).global_cap is None
    assert "standin" not in repr(config) and "admission@" not in repr(config)
    assert set(FULL) == set(ENV.values())


def test_service_missing_env_exit_2(tmp_path):
    env = {**FULL, **stale_ready(tmp_path)}
    del env["WDF_ADMISSION_DSN"], env["WDF_BROKER_PASSWORD"]
    done = run_service(env)
    assert done.returncode == 2, done.stderr
    assert "missing environment: WDF_ADMISSION_DSN, WDF_BROKER_PASSWORD" in done.stderr
    assert not (tmp_path / "ready").exists()


@pytest.mark.parametrize("prefix", ["Bad-", "h_0a1b2c3d-", "x" * 120])
def test_service_bad_prefix_exit_2(tmp_path, prefix):
    done = run_service({**FULL, **stale_ready(tmp_path), PREFIX_ENV: prefix})
    assert done.returncode == 2, done.stderr
    assert f"{PREFIX_ENV} gives the queue" in done.stderr
    assert not (tmp_path / "ready").exists()


@pytest.mark.parametrize("cap", ["0", "-1", "five"])
def test_service_bad_global_cap_exit_2(cap):
    with pytest.raises(ConfigError, match=GLOBAL_CAP_ENV):
        Config.from_env({**FULL, GLOBAL_CAP_ENV: cap})


def test_service_refuses_bad_profile(tmp_path):
    done = run_service({**FULL, **stale_ready(tmp_path), "WDF_TENANCY_PROFILE": "nope"})
    assert done.returncode == 1, done.stderr
    assert "unknown tenancy profile 'nope'" in done.stderr
    assert not (tmp_path / "ready").exists()


async def until(check, timeout_s: float = LIMIT_S, every_s: float = 0.1):
    """The first truthy answer of `check`, a function or a coroutine function."""
    deadline = time.monotonic() + timeout_s
    while True:
        found = check()
        if asyncio.iscoroutine(found):
            found = await found
        if found:
            return found
        assert time.monotonic() < deadline, "the condition did not hold in time"
        await asyncio.sleep(every_s)


class Sampler:
    """Samples the count of rows in `statuses` every 10 ms in a thread of its own, on a connection
    of its own, and keeps the largest."""

    def __init__(self, dsn: str, *statuses: str):
        self.dsn = dsn
        self.statuses = statuses
        self.largest = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample)

    def _sample(self) -> None:
        query = "SELECT count(*) FROM accepted_job WHERE status = ANY(%s)"
        with psycopg.connect(self.dsn, autocommit=True) as db:
            while not self._stop.wait(0.01):
                [n] = db.execute(query, (list(self.statuses),)).fetchone()
                self.largest = max(self.largest, n)
                self.samples += 1

    def __enter__(self) -> "Sampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


class Service:
    """A service under test in the test's own loop, on FakeBroker and the module's runtime."""

    def __init__(self, config: Config, broker: FakeBroker, runtime, **kw):
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(
            service.serve(
                config,
                load(config.profile),
                connect_broker=broker.connect,
                runtime=runtime,
                stop=self.stop,
                **kw,
            )
        )

    async def stopped(self, timeout_s: float = 30) -> float:
        """Stops the service and returns how long its drain took."""
        began = time.monotonic()
        self.stop.set()
        await asyncio.wait_for(self.task, timeout_s)
        return time.monotonic() - began


def publisher(broker: FakeBroker):
    connection = broker.connect()
    return lambda body: connection.publish(QUEUE, body)


@pytest.mark.integration
@pytest.mark.pair
def test_service_caps_hold(appdb, config, server, client, rig, runtime, submit, counts, prefix):
    """200 jobs under a global cap of 5 all finish on the fake pool behind the service's prefix,
    and the live count read from the rows never passes the cap."""
    broker = FakeBroker("ci")

    async def body():
        c = await client()
        async with rig(c):
            run = Service(
                config(temporal_address=server, global_cap=5, prefix=prefix), broker, runtime
            )
            with Sampler(appdb, "admitted", "running") as live:
                submit(publisher(broker), 200)
                await until(lambda: counts("succeeded", "failed") == 200)
            await run.stopped()
        return live

    live = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    print(f"\nsucceeded: {counts('succeeded')}")
    print(f"max_live: {live.largest} over {live.samples} samples")
    assert counts("succeeded") == 200
    assert 1 < live.largest <= 5


@pytest.mark.integration
@pytest.mark.pair
def test_service_restart_recovers(
    db, appdb, config, server, client, rig, runtime, submit, counts, prefix
):
    """A service restarted while its granted resumes are still `resume-pending` recovers them as
    granted from their workflows, so it does not signal them again and the jobs queued meanwhile
    wait, and the live count never passes the cap."""
    broker = FakeBroker("ci")
    publish = publisher(broker)
    settings = config(temporal_address=server, global_cap=2, prefix=prefix)

    async def applied(c, job_id: str, seq: int) -> bool:
        handle = c.get_workflow_handle(workflow_id(job_id))
        return (await handle.query("status", result_type=StatusQuery)).control_seq >= seq

    async def resume_signals(c, job_id: str) -> int:
        handle = c.get_workflow_handle(workflow_id(job_id))
        return sum(
            [
                event.workflow_execution_signaled_event_attributes.signal_name == "resume"
                async for event in handle.fetch_history_events()
                if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
            ]
        )

    async def body():
        c = await client()
        async with rig(c) as this:
            first = Service(settings, broker, runtime)
            this.pausing.update(paused := submit(publish, 2))
            await until(lambda: counts("paused") == 2)
            this.emitter.held = True
            seqs = {}
            for job_id in paused:
                with db.transaction():
                    seqs[job_id] = request_action(db, job_id, "resume").control_seq
                    transition(db, job_id, "paused", "resume-pending")
            for job_id, seq in seqs.items():
                await until(lambda job_id=job_id, seq=seq: applied(c, job_id, seq))
            await first.stopped()

            # Granted resumes still at `resume-pending` hold the cap's two slots.
            with Sampler(appdb, "admitted", "running", "resume-pending") as live:
                queued = submit(publish, 4)
                second = Service(settings, broker, runtime)
                await asyncio.sleep(5)
                waited = counts("accepted")
                signals = [await resume_signals(c, job_id) for job_id in paused]
                this.emitter.held = False
                await until(lambda: counts("succeeded") == 6)
                await second.stopped()
        return live, waited, queued, signals

    live, waited, queued, signals = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    print(f"\nwaited: {waited} of {len(queued)} queued, max_live: {live.largest}")
    print(f"resume signals per paused job: {signals}")
    assert signals == [1, 1]
    assert waited == 4
    assert live.largest <= 2
    assert counts("succeeded") == 6


@pytest.mark.integration
@pytest.mark.pair
def test_service_drain_releases(config, runtime, monkeypatch, tmp_path, submit, status):
    """A stop during a start that does not finish cuts the cycle after DRAIN_S, releases its
    message uncounted and leaves the row at `admitted` for the next process, whose message or
    stuck-row scan starts it."""
    monkeypatch.setattr(service, "DRAIN_S", 0.5)
    broker = FakeBroker("ci")
    ready = tmp_path / "ready"
    calls = []

    async def hanging(row, accepted):
        calls.append(row.job_id)
        await asyncio.Event().wait()

    async def body():
        run = Service(config(), broker, runtime, start=hanging, ready_file=ready)
        [job_id] = submit(publisher(broker), 1)
        await until(lambda: calls, 30)
        was_ready = ready.exists()
        return job_id, was_ready, await run.stopped()

    job_id, was_ready, drained_s = asyncio.run(body())
    [delivery] = broker.connect().consumer(QUEUE).drain(1, 1)
    print(f"\ndrained in {drained_s:.2f} s, delivery_count {delivery.delivery_count}")
    assert was_ready and not ready.exists()
    assert drained_s < 20
    assert (json.loads(delivery.body)["jobid"], delivery.delivery_count) == (job_id, 0)
    assert status(job_id) == "admitted"


@pytest.mark.integration
@pytest.mark.pair
def test_metrics_named(config, runtime, metrics_port, submit):
    """The service serves every admission metric name, with no tenant or job id in a label."""
    broker = FakeBroker("ci")
    calls = []

    async def recording(row, accepted):
        calls.append(row.job_id)
        return "started"

    def scrape() -> str:
        url = f"http://127.0.0.1:{metrics_port}/metrics"
        return urllib.request.urlopen(url, timeout=5).read().decode()

    async def body():
        run = Service(config(), broker, runtime, start=recording)
        submit(publisher(broker), 1)
        await until(lambda: calls, 30)
        text = await until(lambda: "wdf_admission_wait_seconds_count" in scrape() and scrape(), 30)
        await run.stopped()
        return text

    text = asyncio.run(body())
    served = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line[:1] != "#"}
    names = [*COUNTERS, *GAUGES, *(f"{name}_count" for name in HISTOGRAMS)]
    print("\n" + "\n".join(name for name in sorted(served) if name.startswith("wdf_admission")))
    assert [name for name in names if name not in served] == []
    assert "tenant=" not in text and calls[0] not in text
