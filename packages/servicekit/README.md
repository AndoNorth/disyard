# dsy-servicekit

Plumbing that two or more services copy, imported as `dsy_servicekit`.
Add here only what two services copy.
The package depends on `psycopg` only and imports no other `dsy_*` package, which `tests/test_layering.py` enforces.

## Public surface

`tests/test_surface.py` compares each module's `__all__` to the lists below.

- `dsy_servicekit.config`: `ConfigError`, `exiting_on_config_error`, `host_port`, `positive`, `require`
- `dsy_servicekit.lifecycle`: `ReadyFile`, `ThreadStop`, `stopping`
- `dsy_servicekit.pg`: `DEFAULT_CONNECT_TIMEOUT_S`, `MIN_CONNECT_TIMEOUT_S`, `connect`
- `dsy_servicekit.testing`: `ServiceProcess`, `blackhole`, `check_missing_env_exits_2`, `check_ready_file_lifecycle`, `check_sigterm_exits_0_within`, `free_port`, `spawn`, `until`

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

### testing

Standard library only, with no pytest import, so a unit-only package can use it.

- `free_port()`: a TCP port that was free on loopback a moment ago.
- `blackhole()`: a context manager that yields the port of a listener that never answers, which stops listening on exit.
- `until(check, timeout_s, what, interval_s=0.1)`: the first truthy result of `check`, or `TimeoutError` naming `what`.
- `spawn(argv, env)`: a `ServiceProcess` started with exactly `env`. `wait_ready(ready_file, timeout_s)` waits for the file and fails if the process exits first. `terminate(timeout_s)` sends SIGTERM and returns the exit code, or kills the process and raises when it outlives the timeout.
- `check_missing_env_exits_2(argv, names)`: exit 2 and every name on stderr when none is set.
- `check_ready_file_lifecycle(argv, env, ready_file)`: no file before start, one once ready, none after SIGTERM.
- `check_sigterm_exits_0_within(argv, env, ready_file, seconds)`: exit 0 inside `seconds` of SIGTERM.
