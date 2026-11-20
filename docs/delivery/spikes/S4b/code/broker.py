"""Renders the broker configuration of the spec's D4, D5, D12, D14 and D19 and runs RabbitMQ in one
container under a wft_itest T1 lock.

The topology and the users are two files of one definitions directory, imported at boot.
"""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from wft_itest.session import RUN_LABEL, Session, host_port

IMAGE = os.environ.get(
    "S4B_IMAGE",
    "rabbitmq:4.4.0@sha256:1166559c9e77b0208b9d96bfaf265ddbdd491ce3cebb8cf990aabe9274f16d2c",
)
INTAKE = ["intake.acme.standard", "intake.acme.low", "intake.beta.standard", "intake.beta.low"]
TINY = "intake.acme.tiny"
DLQ = "intake.dlq"
INTAKE_PATTERN = r"^intake\.[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$"
INTAKE_POLICY = {
    "delivery-limit": 5,
    "max-length": 1000,
    "overflow": "reject-publish",
    "dead-letter-exchange": "wft.dlx",
    "dead-letter-strategy": "at-least-once",
}
DLQ_POLICY = {"delivery-limit": -1}
USERS = ["dispatch", "admission", "operator"]
ERL_ARGS = "+S 1:1 +sbwt none +sbwtdcpu none +sbwtdio none"
CONF = """\
definitions.import_backend = local_filesystem
definitions.local.path = /etc/rabbitmq/definitions
definitions.skip_if_unchanged = false
vm_memory_high_watermark.absolute = {watermark_mib}MiB
disk_free_limit.absolute = 64MB
raft.wal_max_size_bytes = 32000000
"""


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(4)
    return base64.b64encode(salt + hashlib.sha256(salt + password.encode()).digest()).decode()


def queue(name):
    return {
        "name": name,
        "vhost": "/",
        "durable": True,
        "auto_delete": False,
        "arguments": {"x-queue-type": "quorum", "x-quorum-initial-group-size": 1},
    }


def policy(name, pattern, definition, priority=0):
    return {
        "vhost": "/",
        "name": name,
        "pattern": pattern,
        "apply-to": "quorum_queues",
        "priority": priority,
        "definition": definition,
    }


def topology() -> dict:
    intake = INTAKE + [TINY]
    return {
        "vhosts": [{"name": "/"}],
        "exchanges": [
            {
                "name": "wft.intake",
                "vhost": "/",
                "type": "direct",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            },
            {
                "name": "wft.dlx",
                "vhost": "/",
                "type": "fanout",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            },
        ],
        "queues": [queue(q) for q in intake + [DLQ]],
        "bindings": [
            {
                "source": "wft.intake",
                "vhost": "/",
                "destination": q,
                "destination_type": "queue",
                "routing_key": q,
                "arguments": {},
            }
            for q in intake
        ]
        + [
            {
                "source": "wft.dlx",
                "vhost": "/",
                "destination": DLQ,
                "destination_type": "queue",
                "routing_key": "",
                "arguments": {},
            }
        ],
        "policies": [
            policy("wft-intake", INTAKE_PATTERN, INTAKE_POLICY),
            # A queue three messages long for the full-queue case, which outranks wft-intake.
            policy("wft-tiny", r"^intake\.acme\.tiny$", {**INTAKE_POLICY, "max-length": 3}, 1),
            policy("wft-dlq", r"^intake\.dlq$", DLQ_POLICY),
        ],
    }


def users(passwords: dict[str, str]) -> dict:
    intake_read = INTAKE_PATTERN
    perms = {
        "dispatch": ("^$", r"^wft\.intake$", "^$"),
        "admission": ("^$", "^$", intake_read),
        "operator": ("^$", r"^wft\.intake$", rf"^intake\.dlq$|{intake_read}"),
    }
    return {
        "users": [
            {
                "name": u,
                "password_hash": password_hash(passwords[u]),
                "hashing_algorithm": "rabbit_password_hashing_sha256",
                "tags": [],
            }
            for u in USERS
        ],
        "permissions": [
            {"user": u, "vhost": "/", "configure": c, "write": w, "read": r}
            for u, (c, w, r) in perms.items()
        ],
    }


def render(out: Path, passwords: dict[str, str], watermark_mib: int = 154) -> None:
    (out / "definitions").mkdir(parents=True, exist_ok=True)
    (out / "definitions/topology.json").write_text(json.dumps(topology(), indent=1))
    (out / "definitions/users.json").write_text(json.dumps(users(passwords), indent=1))
    (out / "rabbitmq.conf").write_text(CONF.format(watermark_mib=watermark_mib))
    (out / "enabled_plugins").write_text("[rabbitmq_prometheus].\n")
    for path in out.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


class Broker:
    def __init__(self, cid, name, amqp_port, prom_port, passwords):
        self.id, self.name = cid, name
        self.address = f"127.0.0.1:{amqp_port}"
        self.metrics = f"http://127.0.0.1:{prom_port}/metrics/detailed"
        self.passwords = passwords

    def exec(self, *cmd, check=True) -> str:
        run = subprocess.run(
            ["docker", "exec", "-u", "rabbitmq", self.id, *cmd], capture_output=True, text=True
        )
        if check and run.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} exited {run.returncode}: {run.stderr.strip()}")
        return run.stdout

    def ctl(self, *args) -> str:
        return self.exec("rabbitmqctl", "-q", *args)

    def queue_counts(self) -> dict[str, tuple[int, int]]:
        rows = self.ctl(
            "list_queues", "name", "messages_ready", "messages_unacknowledged", "--no-table-headers"
        )
        out = {}
        for line in rows.splitlines():
            name, ready, unacked = line.split()
            out[name] = (int(ready), int(unacked))
        return out

    def wait_counts(self, name, ready, unacked, timeout=20) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        while True:
            got = self.queue_counts().get(name)
            if got == (ready, unacked) or time.monotonic() > deadline:
                return got
            time.sleep(0.2)

    def scrape(self, query: str) -> str:
        with urllib.request.urlopen(f"{self.metrics}?{query}", timeout=10) as r:
            return r.read().decode()

    def wait_ready(self, timeout=120):
        deadline = time.monotonic() + timeout
        want = set(INTAKE + [TINY, DLQ])
        while time.monotonic() < deadline:
            run = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-u",
                    "rabbitmq",
                    self.id,
                    "rabbitmqctl",
                    "-q",
                    "list_queues",
                    "name",
                    "--no-table-headers",
                ],
                capture_output=True,
                text=True,
            )
            if run.returncode == 0 and want <= set(run.stdout.split()):
                try:
                    self.scrape("family=queue_coarse_metrics")
                    return
                except OSError:
                    pass
            time.sleep(0.5)
        logs = subprocess.run(
            ["docker", "logs", "--tail", "40", self.id], capture_output=True, text=True
        )
        raise RuntimeError(f"broker not ready in {timeout} s:\n{logs.stdout}{logs.stderr}")

    def restart(self):
        subprocess.run(["docker", "restart", "-t", "10", self.id], check=True, capture_output=True)
        self.wait_ready()

    def cgroup(self) -> Path:
        pid = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", self.id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        rel = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
        return Path("/sys/fs/cgroup") / rel.lstrip("/")


@contextmanager
def running(workdir: Path, memory: str = "1g", watermark_mib: int = 154):
    """Starts one broker container with fresh passwords and yields a Broker, under one T1 lock."""
    passwords = {u: secrets.token_urlsafe(18) for u in USERS}
    conf = workdir / f"conf-{secrets.token_hex(4)}"
    render(conf, passwords, watermark_mib)
    with Session(containers=1) as session:
        amqp, prom = host_port(session.slot, 0), host_port(session.slot, 1)
        name = f"wft-{session.run_id}-s4b-{secrets.token_hex(3)}"
        cid = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"{RUN_LABEL}={session.run_id}",
                "--memory",
                memory,
                "--memory-swap",
                memory,
                "-p",
                f"127.0.0.1:{amqp}:5672",
                "-p",
                f"127.0.0.1:{prom}:15692",
                "-e",
                f"RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS={ERL_ARGS}",
                "--mount",
                f"type=bind,src={conf}/rabbitmq.conf,dst=/etc/rabbitmq/rabbitmq.conf,readonly",
                "--mount",
                f"type=bind,src={conf}/enabled_plugins,dst=/etc/rabbitmq/enabled_plugins,readonly",
                "--mount",
                f"type=bind,src={conf}/definitions,dst=/etc/rabbitmq/definitions,readonly",
                IMAGE,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        broker = Broker(cid, name, amqp, prom, passwords)
        try:
            broker.wait_ready()
            yield broker
        finally:
            subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True)
