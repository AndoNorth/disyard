"""Test kit for a service process: spawn it, wait for ready, stop it, check its contract.

Standard library only, so a unit-only package can import it without pytest or psycopg."""

import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

__all__ = [
    "ServiceProcess",
    "check_missing_env_exits_2",
    "check_ready_file_lifecycle",
    "check_sigterm_exits_0_within",
    "free_port",
    "spawn",
    "until",
]

_START_TIMEOUT_S = 10.0


def free_port() -> int:
    """A TCP port that was free on loopback a moment ago."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(check: Callable[[], object], timeout_s: float, what: str, interval_s: float = 0.1):
    """The first truthy result of `check`; TimeoutError naming `what` at the deadline."""
    deadline = time.monotonic() + timeout_s
    while True:
        if result := check():
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout_s}s waiting for {what}")
        time.sleep(interval_s)


class ServiceProcess:
    """A running service. Its stderr is captured and read by `terminate`."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def wait_ready(self, ready_file: Path, timeout_s: float) -> None:
        def ready():
            if self.proc.poll() is not None:
                code = self.proc.returncode
                raise AssertionError(f"service exited {code} before ready: {self._stderr()}")
            return ready_file.exists()

        until(ready, timeout_s, f"{ready_file} to exist")

    def terminate(self, timeout_s: float) -> int:
        """SIGTERM, then the exit code. Past `timeout_s` it kills the service and raises."""
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"service ignored SIGTERM for {timeout_s}s") from None
        finally:
            self._drain()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self._drain()

    def _stderr(self) -> str:
        return self.proc.stderr.read() if self.proc.poll() is not None and self.proc.stderr else ""

    def _drain(self) -> None:
        if self.proc.stderr:
            self.proc.stderr.close()


def spawn(argv: list[str], env: Mapping[str, str]) -> ServiceProcess:
    """Start `argv` with exactly `env` as its environment."""
    return ServiceProcess(subprocess.Popen(argv, env=dict(env), stderr=subprocess.PIPE, text=True))


def check_missing_env_exits_2(argv: list[str], names: Iterable[str]) -> None:
    """With none of `names` set, the service exits 2 and names each of them on stderr."""
    names = list(names)
    env = {k: v for k, v in os.environ.items() if k not in names}
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=_START_TIMEOUT_S)
    assert done.returncode == 2, (
        f"exit {done.returncode} without {names}, expected 2: {done.stderr}"
    )
    missing = [name for name in names if name not in done.stderr]
    assert not missing, f"stderr does not name {missing}: {done.stderr}"


def check_ready_file_lifecycle(argv: list[str], env: Mapping[str, str], ready_file: Path) -> None:
    """No ready file before start, one once ready, none after SIGTERM."""
    assert not ready_file.exists(), f"{ready_file} exists before the service started"
    service = spawn(argv, env)
    try:
        service.wait_ready(ready_file, _START_TIMEOUT_S)
        service.terminate(_START_TIMEOUT_S)
    finally:
        service.kill()
    assert not ready_file.exists(), f"{ready_file} left behind after SIGTERM"


def check_sigterm_exits_0_within(
    argv: list[str], env: Mapping[str, str], ready_file: Path, seconds: float
) -> None:
    """Once ready, SIGTERM ends the service with exit 0 inside `seconds`."""
    service = spawn(argv, env)
    try:
        service.wait_ready(ready_file, _START_TIMEOUT_S)
        code = service.terminate(seconds)
    finally:
        service.kill()
    assert code == 0, f"exit {code} after SIGTERM, expected 0"
