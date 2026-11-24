"""Renders the configuration of a broker from the broker contract and a tenancy profile: the server
settings, the plugins, and the topology and users definitions that RabbitMQ imports at boot.

Usage: python -m dsy_broker.topology --profile NAME [--users FILE] [--out DIR]
Without --out, prints the topology definitions JSON. With --out, writes every file of render into
DIR, and with --users, a JSON object of the password of each user, also the users definitions.
Exit 0 done, 1 a users file that does not name exactly the users of the contract, 2 usage.
"""

import argparse
import base64
import hashlib
import json
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

from .contract import topology
from .models import Estimate, TenancyProfile
from .tenancy import load

CONF = "rabbitmq.conf"
PLUGINS = "enabled_plugins"
DEFINITIONS = "definitions"
TOPOLOGY = f"{DEFINITIONS}/topology.json"
USERS = f"{DEFINITIONS}/users.json"
# Where the broker reads the definitions directory, so its two files can come from two sources.
DEFINITIONS_PATH = "/etc/rabbitmq/definitions"
# One scheduler with busy waiting off, so an idle broker leaves the CPU of its node to the others.
ERL_ARGS = "+S 1:1 +sbwt none +sbwtdcpu none +sbwtdio none"
DISK_FREE_LIMIT = "64MB"
WAL_MAX_SIZE_BYTES = 32_000_000
HASHING = "rabbit_password_hashing_sha256"


def intake_queues(profile: TenancyProfile) -> list[str]:
    """The intake queue of each tenant and priority class of the profile."""
    template = topology().intake_queue.template
    return [
        template.replace("<tenant>", tenant).replace("<class>", cls)
        for tenant in profile.tenants
        for cls in profile.priority_classes
    ]


def _policy_definition(definition) -> dict:
    """The policy keys as RabbitMQ names them, with an estimate taken at its value."""
    return {
        name.replace("_", "-"): value.value if isinstance(value, Estimate) else value
        for name, value in definition
        if value is not None
    }


def _exchange(exchange) -> dict:
    return {
        "name": exchange.name,
        "vhost": topology().vhost,
        "type": exchange.type,
        "durable": exchange.durable,
        "auto_delete": False,
        "internal": False,
        "arguments": {},
    }


def _queue(name: str, spec) -> dict:
    return {
        "name": name,
        "vhost": topology().vhost,
        "durable": True,
        "auto_delete": False,
        "arguments": {
            "x-queue-type": spec.type,
            "x-quorum-initial-group-size": spec.initial_group_size,
        },
    }


def _binding(source: str, queue: str, routing_key: str) -> dict:
    return {
        "source": source,
        "vhost": topology().vhost,
        "destination": queue,
        "destination_type": "queue",
        "routing_key": routing_key,
        "arguments": {},
    }


def _policy(policy) -> dict:
    return {
        "vhost": topology().vhost,
        "name": policy.name,
        "pattern": policy.pattern,
        "apply-to": policy.apply_to,
        "priority": 0,
        "definition": _policy_definition(policy.definition),
    }


def definitions(profile: TenancyProfile) -> dict:
    """The topology definitions: the vhost, both exchanges, every queue, the bindings and the
    policies, and no user."""
    spec = topology()
    intake = intake_queues(profile)
    dlq = spec.dead_letter_queue
    return {
        "vhosts": [{"name": spec.vhost}],
        "exchanges": [_exchange(spec.intake_exchange), _exchange(spec.dead_letter_exchange)],
        "queues": [_queue(q, spec.intake_queue) for q in intake] + [_queue(dlq.name, dlq)],
        "bindings": [_binding(spec.intake_exchange.name, q, q) for q in intake]
        + [_binding(spec.dead_letter_exchange.name, dlq.name, "")],
        "policies": [_policy(spec.intake_policy), _policy(spec.dlq_policy)],
    }


def password_hash(password: str) -> str:
    """A salted hash of the scheme RabbitMQ checks, base64 of a 4-byte salt and the SHA-256 of
    the salt and the password."""
    salt = secrets.token_bytes(4)
    return base64.b64encode(salt + hashlib.sha256(salt + password.encode()).digest()).decode()


def users_definitions(passwords: Mapping[str, str]) -> dict:
    """The users definitions: each user of the contract with a salted hash of its password and its
    permissions.

    Raises ValueError unless `passwords` gives a non-empty password to exactly the users of the
    contract.
    """
    spec = topology()
    users = spec.users.model_dump()
    if not isinstance(passwords, Mapping) or set(passwords) != set(users):
        raise ValueError(f"the passwords name {sorted(passwords)}, the users are {sorted(users)}")
    if not all(isinstance(p, str) and p for p in passwords.values()):
        raise ValueError("every password is a non-empty string")
    return {
        "users": [
            {
                "name": name,
                "password_hash": password_hash(passwords[name]),
                "hashing_algorithm": HASHING,
                "tags": [],
            }
            for name in users
        ],
        "permissions": [
            {"user": name, "vhost": spec.vhost, **permissions}
            for name, permissions in users.items()
        ],
    }


def conf() -> str:
    """rabbitmq.conf: the definitions import at boot, and the memory, disk and log size limits."""
    return (
        "definitions.import_backend = local_filesystem\n"
        f"definitions.local.path = {DEFINITIONS_PATH}\n"
        "definitions.skip_if_unchanged = false\n"
        f"vm_memory_high_watermark.absolute = {topology().memory.high_watermark_mib}MiB\n"
        f"disk_free_limit.absolute = {DISK_FREE_LIMIT}\n"
        f"raft.wal_max_size_bytes = {WAL_MAX_SIZE_BYTES}\n"
    )


def render(profile: TenancyProfile, users: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every file of the broker configuration, keyed by its path relative to /etc/rabbitmq, with the
    users definitions only when `users` gives the password of each user."""
    files = {
        CONF: conf(),
        PLUGINS: "[rabbitmq_prometheus].\n",
        TOPOLOGY: json.dumps(definitions(profile), indent=2) + "\n",
    }
    if users is not None:
        files[USERS] = json.dumps(users_definitions(users), indent=2) + "\n"
    return files


def write(files: Mapping[str, str], out: Path) -> None:
    """Writes the rendered files under `out`, the users definitions readable by the owner only."""
    for name, text in files.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if name == USERS:
            path.chmod(0o600)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dsy_broker.topology", exit_on_error=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--users", type=Path)
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return 2
    if args.users is not None and args.out is None:
        print(
            "usage: --users needs --out, because only a written file holds passwords",
            file=sys.stderr,
        )
        return 2
    try:
        profile = load(args.profile)
    except KeyError as error:
        print(f"usage: {error.args[0]}", file=sys.stderr)
        return 2
    if args.out is None:
        print(json.dumps(definitions(profile), indent=2))
        return 0
    passwords = None
    if args.users is not None:
        try:
            passwords = json.loads(args.users.read_text())
            users_definitions(passwords)
        except (OSError, ValueError, TypeError) as error:
            print(f"error: {args.users}: {error}", file=sys.stderr)
            return 1
    files = render(profile, passwords)
    write(files, args.out)
    for name in files:
        print(f"wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
