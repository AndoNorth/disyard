"""Row 11 of the smoke gate: with the observability switch on, the stack holds what the run just
produced, read through forwards to Prometheus, Loki, Tempo and Pyroscope.

Four parts share one deadline and are polled together until all hold: every scrape target is up and
the `job` labels are the stack's own, each service that handled the job has a log line for it, the
trace of the id on dispatch's accepted line holds a span of the services that continue it, and each
component has a recent profile. A compile that hit the cache ran on no compile worker, so that
worker's log line and profile are not asked for.
"""

import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path

from wft_obs import query
from wft_obs.query import QueryError
from wft_obs.targets import JOBS

# The row prints this name, so its off line reads `ok telemetry: observability off, not checked`.
ROW = "telemetry:"
OBS = Path("scripts/cluster/obs.sh")
NAMESPACE = "wft-obs"
DEADLINE_S = 60.0
EVERY_S = 2.0
# How far back a profile still counts as the platform's own.
PROFILE_WINDOW_S = 300.0
# Every service that handled the job logs a line for it, placement excepted: its request carries
# no job id.
LOGGED = ("dispatch", "admission", "workflow", "simulate-worker", "aggregate-worker")
TRACED = ("dispatch", "admission", "workflow")
PROFILED = (
    "dispatch",
    "admission",
    "workflow",
    "placement",
    "simulate-worker",
    "aggregate-worker",
    "state-inputs",
)
COMPILE = "compile-worker"

Forwarded = Callable[..., AbstractContextManager[str]]


def switch() -> str:
    """The observability switch of the running cluster, `on` or `off`, as `up.sh` recorded it."""
    done = subprocess.run([str(OBS)], capture_output=True, text=True, check=True, timeout=10)
    answer = done.stdout.strip()
    if answer not in ("on", "off"):
        raise RuntimeError(f"{OBS} printed {answer!r}, want on or off")
    return answer


def log_query(component: str, job_id: str) -> str:
    """One service's lines for the job: a stream selector keeps Loki from scanning every stream."""
    return f'{{service_name="{component}"}} | job_id="{job_id}"'


def accepted_query(job_id: str) -> str:
    return f'{log_query("dispatch", job_id)} |= "accepted"'


@dataclass
class Backends:
    """The base address of each backend."""

    prometheus: str
    loki: str
    tempo: str
    pyroscope: str


@dataclass
class Check:
    """What the stack must show for one job; `gaps` lists what it does not show yet."""

    backends: Backends
    job_id: str
    # Whether the job's compile ran on a compile worker, and not from the cache.
    compiled: bool
    # Unix seconds, when the run began: the job's lines are all later.
    since: float
    trace_id: str | None = None

    def gaps(self) -> list[str]:
        now = time.time()
        return [*self.targets(), *self.logs(now), *self.spans(), *self.profiles(now)]

    def targets(self) -> list[str]:
        try:
            series = query.prom(self.backends.prometheus, "up")
        except QueryError as error:
            return [f"no answer for up: {error}"]
        jobs = {item["metric"].get("job", "") for item in series}
        down = sorted({item["metric"].get("job", "") for item in series if item["value"][1] != "1"})
        gaps = [f"target {job} is down" for job in down]
        gaps += [f"no target of job {job}" for job in sorted(JOBS - jobs)]
        gaps += [
            f"target job {job} is not a scrape job of the stack" for job in sorted(jobs - JOBS)
        ]
        return gaps

    def logs(self, now: float) -> list[str]:
        gaps = []
        for component in (*LOGGED, *([COMPILE] if self.compiled else [])):
            asked = (
                accepted_query(self.job_id)
                if component == "dispatch"
                else log_query(component, self.job_id)
            )
            try:
                streams = query.loki(self.backends.loki, asked, self.since, now)
            except QueryError as error:
                gaps.append(f"no log line of {component}: {error}")
                continue
            if not any(stream["values"] for stream in streams):
                gaps.append(f"no log line of {component}")
            elif component == "dispatch" and self.trace_id is None:
                self.trace_id = trace_id_of(streams)
        if self.trace_id is None and not any(g.startswith("no log line of dispatch") for g in gaps):
            gaps.append("no trace id on the accepted line of dispatch")
        return gaps

    def spans(self) -> list[str]:
        if self.trace_id is None:
            return [f"no span of {service}, there is no trace id" for service in TRACED]
        try:
            trace = query.trace(self.backends.tempo, self.trace_id)
        except QueryError as error:
            return [f"no span of {service} in trace {self.trace_id}: {error}" for service in TRACED]
        found = services_of(trace)
        return [f"no span of {s} in trace {self.trace_id}" for s in TRACED if s not in found]

    def profiles(self, now: float) -> list[str]:
        wanted = (*PROFILED, *([COMPILE] if self.compiled else []))
        try:
            held = query.profiled(self.backends.pyroscope, now - PROFILE_WINDOW_S, now)
        except QueryError as error:
            return [f"no profile of {component}: {error}" for component in wanted]
        return [f"no profile of {c}" for c in wanted if f"wft.{c}" not in held]


def trace_id_of(streams: list[dict]) -> str | None:
    """The trace id of a line Loki returned, which the service wrote as structured metadata."""
    for stream in streams:
        if stream["values"] and (found := stream["stream"].get("trace_id")):
            return found
    return None


def services_of(trace: dict) -> set[str]:
    """The `service.name` of every resource of an OTLP JSON trace that holds a span."""
    found = set()
    for resource in trace.get("batches") or trace.get("resourceSpans") or []:
        scopes = resource.get("scopeSpans") or resource.get("instrumentationLibrarySpans") or []
        if not any(scope.get("spans") for scope in scopes):
            continue
        for attribute in resource.get("resource", {}).get("attributes", []):
            if attribute.get("key") == "service.name":
                found.add(attribute["value"].get("stringValue", ""))
    return found


def check(forwarded: Forwarded, job_id: str, compiled: bool, since: float) -> str:
    """The row's pass detail once the four parts hold within the deadline, else a RuntimeError that
    names each component and signal still missing."""
    with ExitStack() as stack:

        def address(resource: str, port: int) -> str:
            return f"http://{stack.enter_context(forwarded(NAMESPACE, resource, port))}"

        backends = Backends(
            prometheus=address("svc/prometheus", 9090),
            loki=address("svc/loki", 3100),
            tempo=address("svc/tempo", 3200),
            pyroscope=address("svc/pyroscope", 4040),
        )
        wanted = Check(backends, job_id, compiled, since)
        deadline = time.monotonic() + DEADLINE_S
        while gaps := wanted.gaps():
            if time.monotonic() + EVERY_S > deadline:
                raise RuntimeError(f"after {DEADLINE_S:.0f} s: {'; '.join(gaps)}")
            time.sleep(EVERY_S)
    logged = len(LOGGED) + compiled
    return (
        f"{len(JOBS)} scrape jobs up, logs of {logged} services, "
        f"trace of {len(TRACED)} services, {len(PROFILED) + compiled} profiles"
    )
