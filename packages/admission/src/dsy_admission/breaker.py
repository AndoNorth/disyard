"""The breakers of the admission cycle: one per dependency, opened by an outage of Temporal, the
database or the broker, and one for admission itself, opened when distinct messages in a row fail
with the same error class, so one systematic error cannot walk the backlog to the dead letters.

An open breaker backs off from 1 s, doubling to 30 s, and closes on its first good probe.
"""

from dataclasses import dataclass, field

import psycopg
from dsy_broker.models import BrokerUnavailable
from temporalio.service import RPCError, RPCStatusCode

DEPENDENCIES = ("temporal", "database", "broker")
INTERNAL = "internal"
FIRST_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
# Distinct messages in a row failing with one error class that open the internal breaker.
STREAK = 5
OUTAGES = {
    RPCStatusCode.UNAVAILABLE,
    RPCStatusCode.DEADLINE_EXCEEDED,
    RPCStatusCode.RESOURCE_EXHAUSTED,
}


def dependency_of(error: BaseException) -> str | None:
    """The dependency whose outage `error` shows, or None for an error of admission's own."""
    if isinstance(error, RPCError) and error.status in OUTAGES:
        return "temporal"
    if isinstance(error, psycopg.OperationalError):
        return "database"
    if isinstance(error, BrokerUnavailable):
        return "broker"
    return None


@dataclass
class Breaker:
    # Zero while closed.
    backoff_s: float = 0.0
    # The clock reading before which the next probe waits.
    due: float = 0.0

    @property
    def open(self) -> bool:
        return self.backoff_s > 0

    def fail(self, now: float) -> None:
        """Opens the breaker, or doubles its backoff when a probe failed."""
        self.backoff_s = min(self.backoff_s * 2, MAX_BACKOFF_S) if self.open else FIRST_BACKOFF_S
        self.due = now + self.backoff_s

    def wait(self, now: float) -> None:
        """Waits one more backoff without doubling it, after a probe that proved nothing."""
        self.due = now + self.backoff_s

    def close(self) -> None:
        self.backoff_s = 0.0


@dataclass
class Breakers:
    of: dict[str, Breaker] = field(
        default_factory=lambda: {name: Breaker() for name in (*DEPENDENCIES, INTERNAL)}
    )
    _streak: tuple[str, set[bytes]] = ("", set())

    def open_dependency(self) -> str | None:
        """The first dependency whose breaker is open, which the cycle only probes."""
        return next((name for name in DEPENDENCIES if self.of[name].open), None)

    def failed(self, error_class: str, message: bytes, now: float) -> bool:
        """Counts a message that failed with an error of admission's own, and opens the internal
        breaker, returning True, when STREAK distinct messages in a row failed with its class."""
        cls, seen = self._streak
        if cls != error_class:
            seen = set()
        seen.add(message)
        self._streak = (error_class, seen)
        if len(seen) < STREAK:
            return False
        self._streak = ("", set())
        self.of[INTERNAL].fail(now)
        return True

    def succeeded(self) -> None:
        """Breaks the streak, after a message handled with no error of admission's own."""
        self._streak = ("", set())

    def gauge(self) -> dict[str, int]:
        """The value of `dsy_admission_breaker_open` for each breaker."""
        return {name: int(breaker.open) for name, breaker in self.of.items()}
