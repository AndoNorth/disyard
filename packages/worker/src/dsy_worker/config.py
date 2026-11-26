"""The configuration of `python -m dsy_worker --pool <pool>`, read from the environment only."""

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dsy_servicekit.config import ConfigError, require
from dsy_workflow.contract import naming
from dsy_workflow.models import QUEUE

ENV = {
    "temporal_address": "DSY_TEMPORAL_ADDRESS",
    "namespace": "DSY_NAMESPACE",
    "store_endpoint": "DSY_STORE_ENDPOINT",
    # A credentials file of dsy_storecheck.s3.load_credentials holding the pool's own store role.
    "store_credentials": "DSY_STORE_CREDENTIALS",
    # The container's memory limit in MiB, from the downward API.
    "slot_memory_mib": "DSY_SLOT_MEMORY_MIB",
    "scratch": "DSY_SCRATCH",
    "ready_file": "DSY_READY_FILE",
}
# Required on the compile pool only: the package mirror the dependency fetch reads.
MIRROR_ENV = "DSY_MIRROR"
# Optional: put in front of the pool's queue name, so one run can hold queues of its own.
PREFIX_ENV = "DSY_POOL_QUEUE_PREFIX"


@dataclass(frozen=True)
class Config:
    pool: str
    temporal_address: str
    namespace: str
    store_endpoint: str
    store_credentials: Path
    slot_memory_mib: int
    scratch: Path
    ready_file: Path
    mirror: str | None = None
    queue_prefix: str = ""

    @property
    def queue(self) -> str:
        return self.queue_prefix + naming().pools[self.pool]

    @classmethod
    def from_env(cls, pool: str, environ: Mapping[str, str]) -> "Config":
        """The configuration of `pool` in `environ`, or ConfigError naming every required variable
        that is unset or empty, or the value that is malformed, before anything connects."""
        if pool not in naming().pools:
            raise ConfigError(f"no pool named {pool!r}, one of {', '.join(naming().pools)}")
        require(environ, ENV | ({"mirror": MIRROR_ENV} if pool == "compile" else {}))
        memory = environ[ENV["slot_memory_mib"]]
        if not memory.isdigit() or int(memory) == 0:
            raise ConfigError(f"{ENV['slot_memory_mib']} is {memory!r}, not a positive integer")
        config = cls(
            pool=pool,
            temporal_address=environ[ENV["temporal_address"]],
            namespace=environ[ENV["namespace"]],
            store_endpoint=environ[ENV["store_endpoint"]],
            store_credentials=Path(environ[ENV["store_credentials"]]),
            slot_memory_mib=int(memory),
            scratch=Path(environ[ENV["scratch"]]),
            ready_file=Path(environ[ENV["ready_file"]]),
            mirror=environ.get(MIRROR_ENV) if pool == "compile" else None,
            queue_prefix=environ.get(PREFIX_ENV, ""),
        )
        if not re.fullmatch(QUEUE, config.queue):
            raise ConfigError(f"{PREFIX_ENV} gives the queue {config.queue!r}, outside {QUEUE}")
        return config


def load(pool: str, environ: Mapping[str, str]) -> Config:
    """The configuration, or exit 2 with the error on stderr, as the workflow service does."""
    try:
        return Config.from_env(pool, environ)
    except ConfigError as error:
        print(f"dsy_worker: {error}", file=sys.stderr)
        raise SystemExit(2) from None
