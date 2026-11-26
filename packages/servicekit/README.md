# dsy-servicekit

Plumbing that two or more services copy, imported as `dsy_servicekit`.
Add here only what two services copy.
The package depends on `psycopg` only and imports no other `dsy_*` package, which `tests/test_layering.py` enforces.

## Public surface

`tests/test_surface.py` compares each module's `__all__` to the lists below.

- `dsy_servicekit.config`: `ConfigError`, `exiting_on_config_error`, `host_port`, `positive`, `require`
- `dsy_servicekit.lifecycle`: `ReadyFile`, `ThreadStop`, `stopping`
- `dsy_servicekit.pg`: `DEFAULT_CONNECT_TIMEOUT_S`, `MIN_CONNECT_TIMEOUT_S`, `connect`

### config

- `require(environ, names)`: field -> value for a mapping of field to variable name. Raises `ConfigError("missing environment: A, B")` naming every absent or empty variable.
- `positive(environ, name, default=None)`: the positive number in `name`, `default` when it is unset or empty, `ConfigError` otherwise.
- `host_port(value, name)`: `(host, port)` of `host:port`, or `ConfigError` naming `name`.
- `exiting_on_config_error(component)`: context manager for `main` that prints `<component>: <error>` to stderr and exits 2.

### lifecycle

- `ReadyFile(path)`: `arm()` unlinks a stale file, `set()` touches it, `clear()` unlinks it. A `path` of `None` makes every method a no-op.
- `stopping(loop=None)`: context manager yielding an `asyncio.Event` that SIGTERM and SIGINT set. The handlers are removed on exit.
- `ThreadStop`: the `signal.signal` form. `install()` makes SIGTERM and SIGINT set `event`.

### pg

- `connect(dsn, *, autocommit=False, connect_timeout=10, lock_timeout=None, statement_timeout=None)`: a `psycopg` connection. `connect_timeout` is whole seconds with a floor of `MIN_CONNECT_TIMEOUT_S` (2), because libpq rejects a fractional value and 1 is not reliable. The two timeouts are Postgres durations such as `"10s"`.
