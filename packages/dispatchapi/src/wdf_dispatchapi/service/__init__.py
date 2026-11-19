"""The dispatch and control API service: one process with two HTTP listeners, `dispatch-api` for
uploads, batch submit and batch reads and `control-api` for control and the job status read, and
the loops that carry the rows on: the reconciler, which publishes the rows a submit left
unpublished, and the control retrier with its resume finisher.

`python -m wdf_dispatchapi.service` reads its configuration from the environment only, the names of
ENV, LISTEN_ENV and the limits of `wdf_dispatchapi.limits`. It serves its metrics on the Prometheus
endpoint of WDF_METRICS_ADDRESS. On SIGTERM or SIGINT it stops: readiness answers not-ready, the
listeners take no new call, a submit call finishes the chunk in hand and a batch control call the
job in hand and each then answers an ErrorLine, and the loops end, within DRAIN_S.
"""

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path

import psycopg
import yaml
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from wdf_artifactclient.client import ArtifactClient
from wdf_artifactclient.errors import ArtifactError, ArtifactNotFound
from wdf_artifactclient.keys import content_key, uri_of
from wdf_broker.client import Connection, Credentials, connect
from wdf_broker.models import TenancyProfile
from wdf_broker.tenancy import load
from wdf_events.models import ContentReference
from wdf_jobdoc.profile import LimitsFile, LimitsProfile, PoolTable, Slot
from wdf_jobstate.models import CONTRACT as JOBSTATE_CONTRACT
from wdf_storecheck.s3 import load_credentials
from wdf_workflow.contract import pools

from ..gateway import TemporalGateway
from ..limits import ConfigError, Limits
from ..objects import Uploads
from ..patch import Resume
from ..ports import TemporalUnavailable, WorkflowGateway
from ..publish import Publisher
from ..reconciler import Reconciler
from ..resolver import StoreSourceResolver
from ..retrier import Retrier
from ..submit import Submit
from .metrics import BUCKETS, Metrics
from .routes import Handler

log = logging.getLogger(__name__)

ENV = {
    "temporal_address": "WDF_TEMPORAL_ADDRESS",
    # A login in each group role the service writes or reads as.
    "dispatch_dsn": "WDF_DISPATCH_DSN",
    "control_dsn": "WDF_CONTROL_DSN",
    "artifacts_dsn": "WDF_ARTIFACTS_DSN",
    "reader_dsn": "WDF_READER_DSN",
    "store_endpoint": "WDF_STORE_ENDPOINT",
    # A credentials file of wdf_storecheck.s3.load_credentials holding the role `submit`.
    "store_credentials": "WDF_SUBMIT_CREDENTIALS",
    "broker_address": "WDF_BROKER_ADDRESS",
    # The password of the broker user `dispatch`.
    "broker_password": "WDF_BROKER_PASSWORD",
    "tenancy_profile": "WDF_TENANCY_PROFILE",
    "metrics_address": "WDF_METRICS_ADDRESS",
}
# Optional: the address of each listener, at its container port by default.
LISTEN_ENV = {"dispatch-api": "WDF_DISPATCH_API_LISTEN", "control-api": "WDF_CONTROL_API_LISTEN"}
LISTEN = {"dispatch-api": "0.0.0.0:8081", "control-api": "0.0.0.0:8080"}
BROKER_USER = "dispatch"
STORE_ROLE = "submit"
# The limits profile that checks every template and every job's params.
LIMITS_PROFILE = "ci"
# How long the calls in flight have to end after a stop, below the pod's grace period.
DRAIN_S = 25.0
# How often the gauges of the waiting rows are read (an estimate).
GAUGES_EVERY_S = 15.0
CONNECT_TIMEOUT_S = 5
# What readiness asks the store and Temporal about: an object and a workflow that never exist.
PROBE_SHA = "0" * 64
PROBE_OBJECT = ContentReference(
    uri=uri_of("source", content_key("source", PROBE_SHA)),
    sha256=PROBE_SHA,
    size=0,
    name="probe",
    kind="source",
)
PROBE_JOB = "00000000-0000-0000-0000-000000000000"
LOGINS = ("dispatch", "control", "artifacts", "reader")


def _address(text: str, name: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ConfigError(f"{name}={text!r} is not host:port")
    return host, int(port)


LISTEN_ADDRESSES = {name: _address(text, name) for name, text in LISTEN.items()}


@dataclass(frozen=True)
class Config:
    temporal_address: str
    # The DSNs and the broker password are secrets, so a printed configuration leaves them out.
    dispatch_dsn: str = field(repr=False)
    control_dsn: str = field(repr=False)
    artifacts_dsn: str = field(repr=False)
    reader_dsn: str = field(repr=False)
    store_endpoint: str
    store_credentials: str
    broker_address: str
    broker_password: str = field(repr=False)
    tenancy_profile: str
    metrics_address: str
    listen: dict[str, tuple[str, int]] = field(default_factory=lambda: dict(LISTEN_ADDRESSES))
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Config":
        """The configuration of `environ`, or ConfigError naming every variable that is unset or
        empty, or the first one that is refused."""
        if missing := [name for name in ENV.values() if not environ.get(name)]:
            raise ConfigError(f"missing environment: {', '.join(missing)}")
        listen = {
            listener: _address(environ.get(name) or LISTEN[listener], name)
            for listener, name in LISTEN_ENV.items()
        }
        return cls(
            **{key: environ[name] for key, name in ENV.items()},
            listen=listen,
            limits=Limits.from_env(environ),
        )

    def dsn(self, login: str) -> str:
        return getattr(self, f"{login}_dsn")


def limits_profile() -> LimitsProfile:
    """The limits profile of LIMITS_PROFILE from package data, with the pool table of the
    temporal contract, whose slots are the `ci` slots."""
    limits = LimitsFile.model_validate(
        yaml.safe_load((JOBSTATE_CONTRACT / "limits.yaml").read_text())
    )
    table = PoolTable(
        **{
            name: Slot(cpu_milli=pool.cpu_milli, memory_mib=pool.memory_mib)
            for name, pool in pools().pools.items()
        }
    )
    numbers = limits.profiles[LIMITS_PROFILE].model_dump()
    return LimitsProfile(name=LIMITS_PROFILE, pools=table, **numbers)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def _handler(service: "Service", listener: str) -> type[Handler]:
    return type(f"Handler[{listener}]", (Handler,), {"service": service, "listener": listener})


class Service:
    """The listeners and the loops over a store client of the `submit` identity, a workflow gateway
    and the broker's `connect`, bound once constructed and serving once started."""

    def __init__(
        self,
        config: Config,
        tenancy: TenancyProfile,
        store: ArtifactClient,
        gateway: WorkflowGateway,
        connect_broker: Callable[[str, Credentials], Connection],
        scratch: Path | None = None,
    ):
        self.config, self.tenancy, self.store, self.gateway = config, tenancy, store, gateway
        self.limits = config.limits
        self.stopping = threading.Event()
        self._in_flight = 0
        self._lock = threading.Condition()
        # The runtime serves the metrics endpoint for as long as it is held.
        self.runtime = Runtime(
            telemetry=TelemetryConfig(
                metrics=PrometheusConfig(
                    bind_address=config.metrics_address, histogram_bucket_overrides=BUCKETS
                )
            )
        )
        self.metrics = Metrics(self.runtime.metric_meter)
        profile = limits_profile()
        resolver = StoreSourceResolver(store, scratch)
        credentials = Credentials(BROKER_USER, config.broker_password)
        self.publisher = Publisher(connect_broker, config.broker_address, credentials)
        self.uploads = Uploads(store, scratch)
        self.submit = Submit(store, resolver, profile, tenancy, self.limits, self.publisher)
        self.resume = Resume(store, resolver, profile)
        self.reconciler = Reconciler(self.publisher)
        self.retrier = Retrier(config.control_dsn, config.reader_dsn, gateway)
        self.servers = {
            listener: _Server(address, _handler(self, listener))
            for listener, address in config.listen.items()
        }
        self._threads: list[threading.Thread] = []

    def url(self, listener: str) -> str:
        host, port = self.servers[listener].server_address[:2]
        return f"http://{host}:{port}"

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @contextmanager
    def request(self) -> Iterator[bool]:
        """Holds one of the process's requests in flight for the block, and yields False without
        holding one when all are taken."""
        with self._lock:
            admitted = self._in_flight < self.limits.requests
            self._in_flight += admitted
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    self._in_flight -= 1
                    self._lock.notify_all()

    def connect(self, login: str) -> psycopg.Connection:
        return psycopg.connect(
            self.config.dsn(login), autocommit=True, connect_timeout=CONNECT_TIMEOUT_S
        )

    def ready(self) -> bool:
        """Whether every database login connects, the store answers a head and Temporal answers,
        while the service is not stopping."""
        try:
            for login in LOGINS:
                self.connect(login).close()
            try:
                self.store.head(PROBE_OBJECT)
            except ArtifactNotFound:
                pass
            self.gateway.exists(PROBE_JOB)
        except (psycopg.Error, ArtifactError, TemporalUnavailable) as error:
            log.info("not ready: %s", error)
            return False
        return not self.stopping.is_set()

    def start(self) -> None:
        loops = {
            **{f"listen-{name}": server.serve_forever for name, server in self.servers.items()},
            "reconciler": lambda: self.reconciler.run(self.config.dispatch_dsn),
            "retrier": lambda: self.retrier.run(self.stopping),
            "gauges": self._gauges,
        }
        for name, target in loops.items():
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info(
            "listening: %s; metrics on %s",
            ", ".join(f"{name} {self.url(name)}" for name in self.servers),
            self.config.metrics_address,
        )

    def stop(self, drain_s: float = DRAIN_S) -> None:
        """Stops taking calls, waits up to `drain_s` for the calls in flight, and ends the loops.
        A call still in flight after it is cut short with the process."""
        self.stopping.set()
        self.reconciler.stop()
        for server in self.servers.values():
            server.shutdown()
        deadline = time.monotonic() + drain_s
        with self._lock:
            while self._in_flight and (left := deadline - time.monotonic()) > 0:
                self._lock.wait(left)
            if self._in_flight:
                log.warning("%d calls are still in flight, they are cut short", self._in_flight)
        for server in self.servers.values():
            server.server_close()
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        log.info("stopped")

    def _gauges(self) -> None:
        while not self.stopping.is_set():
            try:
                with self.connect("dispatch") as dispatch, self.connect("control") as control:
                    self.metrics.backlog(dispatch, control)
            except psycopg.Error as error:
                log.warning("the gauges were not read, the next pass retries: %s", error)
            self.stopping.wait(GAUGES_EVERY_S)


def build(config: Config, tenancy: TenancyProfile) -> Service:
    """The service over the real store, Temporal and broker of the configuration."""
    credentials = load_credentials(Path(config.store_credentials))[STORE_ROLE]
    store = ArtifactClient(config.store_endpoint, credentials)
    return Service(config, tenancy, store, TemporalGateway(config.temporal_address), connect)


def run(service: Service) -> None:
    """Serves until SIGTERM or SIGINT, then stops."""
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    service.start()
    while not stop.wait(1.0):
        pass
    log.info("stopping, draining for at most %s s", DRAIN_S)
    service.stop()


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Exit 2 when the configuration is refused, and 1 when the tenancy profile is."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = Config.from_env(environ)
    except ConfigError as error:
        print(f"wdf_dispatchapi.service: {error}", file=sys.stderr)
        return 2
    try:
        tenancy = load(config.tenancy_profile)
    except KeyError as error:
        print(f"wdf_dispatchapi.service: {error.args[0]}", file=sys.stderr)
        return 1
    run(build(config, tenancy))
    return 0
