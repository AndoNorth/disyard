"""Environment reading and the one configuration error of a service."""

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

__all__ = ["ConfigError", "exiting_on_config_error", "host_port", "positive", "require"]


class ConfigError(Exception):
    pass


def require(environ: Mapping[str, str], names: Mapping[str, str]) -> dict[str, str]:
    """field -> value. Raises ConfigError("missing environment: A, B") naming every absent or
    empty variable."""
    if missing := [name for name in names.values() if not environ.get(name)]:
        raise ConfigError(f"missing environment: {', '.join(missing)}")
    return {field: environ[name] for field, name in names.items()}


def positive(environ: Mapping[str, str], name: str, default: float | None = None) -> float:
    """The positive number in `name`, or `default` when it is unset or empty.
    Raises ConfigError when it is neither a positive number nor defaulted."""
    raw = environ.get(name)
    if not raw:
        if default is None:
            raise ConfigError(f"missing environment: {name}")
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    # nan fails the comparison, inf is not a usable bound
    if not 0 < value < float("inf"):
        raise ConfigError(f"{name} is {raw!r}, not a positive number")
    return value


def host_port(value: str, name: str) -> tuple[str, int]:
    """(host, port) of "host:port", or ConfigError naming `name`."""
    host, sep, port = value.rpartition(":")
    if not (sep and host and port.isascii() and port.isdecimal() and 0 < int(port) < 65536):
        raise ConfigError(f"{name} is {value!r}, not host:port")
    return host, int(port)


@contextmanager
def exiting_on_config_error(component: str) -> Iterator[None]:
    """For `main`: prints "<component>: <error>" to stderr and exits 2 on a ConfigError."""
    try:
        yield
    except ConfigError as error:
        print(f"{component}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
