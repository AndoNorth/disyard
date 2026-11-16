"""A RabbitMQ container for T1 tests, configured as the broker module deploys it, from the pinned
image, the rendered configuration and throwaway passwords."""

import json
import secrets
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from wdf_itest import Session
from wdf_itest.session import Container

from .. import topology
from ..contract import topology as spec
from ..models import TenancyProfile
from ..tenancy import load

IMAGE = "rabbitmq"
READY_TIMEOUT_S = 120
COOKIE = "/var/lib/rabbitmq/.erlang.cookie"


@dataclass(frozen=True)
class Broker:
    """A running broker: its container, its AMQP address, its detailed metrics endpoint and the
    password of each user."""

    container: Container
    address: str
    metrics: str
    passwords: dict[str, str]
    queues: list[str]

    def ctl(self, *args: str) -> str:
        """The output of a rabbitmqctl command run inside the container."""
        return self.container.exec("rabbitmqctl", "-q", *args).stdout


def _wait_ready(broker: Broker) -> None:
    """Waits until every queue answers with its policy applied and the metrics endpoint answers."""
    want = set(broker.queues)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        # rabbitmqctl runs as root and would create the Erlang cookie the broker cannot read, so it
        # waits for the broker to create its own.
        cookie = broker.container.exec("test", "-s", COOKIE, check=False)
        run = cookie.returncode == 0 and broker.container.exec(
            "rabbitmqctl", "-q", "list_queues", "name", "policy", "--formatter", "json", check=False
        )
        if run and run.returncode == 0:
            applied = {q["name"] for q in json.loads(run.stdout or "[]") if q["policy"]}
            if want <= applied:
                try:
                    urllib.request.urlopen(broker.metrics, timeout=5).close()
                    return
                except OSError:
                    pass
        if time.monotonic() >= deadline:
            logs = subprocess.run(
                ["docker", "logs", "--tail", "40", broker.container.id],
                capture_output=True,
                text=True,
            )
            raise TimeoutError(
                f"the broker was not ready in {READY_TIMEOUT_S} s:\n{logs.stdout}{logs.stderr}"
            )
        time.sleep(0.5)


@contextmanager
def broker_container(t1: Session, profile: str | TenancyProfile = "ci") -> Iterator[Broker]:
    """Starts the pinned RabbitMQ image in the T1 session `t1` with the configuration rendered from
    a tenancy profile, given by name or as a model, and yields it once every queue answers.

    The AMQP and Prometheus ports are published on the slot of the session, and the configuration
    is mounted read-only from a temporary directory removed with the container.
    """
    if isinstance(profile, str):
        profile = load(profile)
    passwords = {name: secrets.token_urlsafe(18) for name in spec().users.model_dump()}
    ports = spec().ports
    with tempfile.TemporaryDirectory(prefix="wdf-broker-") as tmp:
        conf = Path(tmp)
        topology.write(topology.render(profile, passwords), conf)
        # The broker runs as its own user inside the container, so the throwaway files are readable.
        for path in conf.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        mounts = {
            conf / topology.CONF: f"/etc/rabbitmq/{topology.CONF}",
            conf / topology.PLUGINS: f"/etc/rabbitmq/{topology.PLUGINS}",
            conf / topology.DEFINITIONS: topology.DEFINITIONS_PATH,
        }
        with t1.container(
            IMAGE,
            ports={0: ports.amqp, 1: ports.prometheus},
            env={"RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS": topology.ERL_ARGS},
            mounts=mounts,
            name="rabbitmq",
        ) as container:
            broker = Broker(
                container=container,
                address=f"127.0.0.1:{container.host_ports[0]}",
                metrics=f"http://127.0.0.1:{container.host_ports[1]}/metrics/detailed",
                passwords=passwords,
                queues=topology.intake_queues(profile) + [spec().dead_letter_queue.name],
            )
            _wait_ready(broker)
            yield broker
