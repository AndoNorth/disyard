"""The limits of a batch submit call, with the defaults of the API contract, and the count of
submit calls in flight that holds a tenant and the process to theirs.

Every default is an estimate that the characterization epic replaces, so each limit can be set
from the environment without a rebuild.
"""

import threading
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields

from wdf_dispatchapi.models import ENTRIES_MAX, ENTRY_LINE_MAX_BYTES, HEADER_LINE_MAX_BYTES

ENV_PREFIX = "WDF_DISPATCH_"


class ConfigError(Exception):
    pass


class TooManyCalls(Exception):
    """The tenant or the process already has its limit of submit calls in flight."""


@dataclass(frozen=True)
class Limits:
    entries: int = ENTRIES_MAX
    entry_line_bytes: int = ENTRY_LINE_MAX_BYTES
    header_line_bytes: int = HEADER_LINE_MAX_BYTES
    calls_per_tenant: int = 2
    calls_per_process: int = 4
    requests: int = 16
    # No byte read for this long ends a call, as does an answer write that takes this long.
    idle_timeout_s: float = 60.0
    write_timeout_s: float = 60.0
    chunk_entries: int = 1_000

    def __post_init__(self):
        if bad := [f.name for f in fields(self) if not getattr(self, f.name) > 0]:
            raise ConfigError(f"limits must be positive: {', '.join(bad)}")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Limits":
        """The defaults, each replaced by `WDF_DISPATCH_<NAME>` where that is set."""
        values = {}
        for field in fields(cls):
            name = ENV_PREFIX + field.name.upper()
            if text := environ.get(name):
                try:
                    values[field.name] = field.type(text)
                except ValueError:
                    raise ConfigError(f"{name} is not a number: {text!r}") from None
        return cls(**values)


class CallSlots:
    """The submit calls in flight, per tenant and for the process."""

    def __init__(self, limits: Limits):
        self._per_tenant = limits.calls_per_tenant
        self._per_process = limits.calls_per_process
        self._lock = threading.Lock()
        self._tenants: Counter[str] = Counter()

    @contextmanager
    def hold(self, tenant: str) -> Iterator[None]:
        """Holds a slot for one call, or raises TooManyCalls without waiting."""
        with self._lock:
            if self._tenants[tenant] >= self._per_tenant:
                raise TooManyCalls(f"tenant {tenant} has {self._per_tenant} submit calls in flight")
            if self._tenants.total() >= self._per_process:
                raise TooManyCalls(f"the process has {self._per_process} submit calls in flight")
            self._tenants[tenant] += 1
        try:
            yield
        finally:
            with self._lock:
                self._tenants[tenant] -= 1
