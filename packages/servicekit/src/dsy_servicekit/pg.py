"""Postgres connects with bounded waits."""

import psycopg

__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "MIN_CONNECT_TIMEOUT_S", "connect"]

DEFAULT_CONNECT_TIMEOUT_S = 10
# libpq takes a decimal integer of seconds, and 1 is not reliable.
MIN_CONNECT_TIMEOUT_S = 2


def connect(
    dsn: str,
    *,
    autocommit: bool = False,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    lock_timeout: str | None = None,
    statement_timeout: str | None = None,
) -> psycopg.Connection:
    """A connection whose connect is bounded by `connect_timeout` seconds, raised to the floor of
    MIN_CONNECT_TIMEOUT_S, and whose lock and statement waits are bounded when given, as Postgres
    durations such as "10s"."""
    options = [
        f"-c {name}={value}"
        for name, value in (
            ("lock_timeout", lock_timeout),
            ("statement_timeout", statement_timeout),
        )
        if value is not None
    ]
    return psycopg.connect(
        dsn,
        autocommit=autocommit,
        connect_timeout=max(int(connect_timeout), MIN_CONNECT_TIMEOUT_S),
        **({"options": " ".join(options)} if options else {}),
    )
