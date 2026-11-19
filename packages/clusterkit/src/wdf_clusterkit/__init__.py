"""What every driver of a deployed cluster needs from the host: kubectl on a kube context,
port-forwards bound to 127.0.0.1, the credentials of the state directory, the state of a pod and a
poller with a deadline.

What a driver does with the cluster, its journeys, its counters and the deployments it scales or
restarts, stays in the driver.
"""

import asyncio
import inspect
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wdf_storecheck.s3 import Creds, load_credentials

INFRA = "wdf-infra"
CONTROL = "wdf-control"
WORKERS = "wdf-workers"
# How long a restarted pod, a rollout, a forward or a new build has to come up.
UP_S = 300.0


class Refused(Exception):
    """A command or scenario that a driver cannot run, which its command line exits 2 for."""


def state_dir() -> Path:
    state = os.environ.get("WDF_STATE_DIR") or str(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "wasm-dag-forge"
    )
    return Path(state)


def appdb_password() -> str:
    return (state_dir() / "appdb.credential").read_text().strip()


def store_credentials() -> dict[str, Creds]:
    return load_credentials(state_dir() / "objectstore.credentials.json")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class Forward:
    """A `kubectl port-forward` on a fixed local port, so the clients that use it reconnect to the
    same address once it is opened again after its pod restarted."""

    def __init__(self, context: str, namespace: str, resource: str, remote: int):
        self.argv = [
            "kubectl",
            f"--context={context}",
            f"--namespace={namespace}",
            "port-forward",
            "--address=127.0.0.1",
            resource,
        ]
        self.remote = remote
        self.port = free_port()
        self.process: subprocess.Popen | None = None
        # The watchdog and a restart both open a forward again, each from its own thread.
        self.lock = threading.Lock()

    def _spawn(self) -> None:
        self.process = subprocess.Popen(
            [*self.argv, f"{self.port}:{self.remote}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def open(self) -> None:
        with self.lock:
            self._close()
            self._spawn()
            deadline = time.monotonic() + UP_S
            while not _listening(self.port):
                if self.process.poll() is not None:
                    # The resource is not up yet, as right after its pod restarted.
                    self._spawn()
                if time.monotonic() > deadline:
                    self._close()
                    raise TimeoutError(f"{' '.join(self.argv)} did not listen in {UP_S:.0f} s")
                time.sleep(0.5)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait()

    def close(self) -> None:
        with self.lock:
            self._close()


def kubectl(context: str, *args: str, capture: bool = True) -> str:
    """Runs kubectl on the context and returns its output, or nothing when `capture` is off and it
    printed to the terminal. A failure raises CalledProcessError, with the error it printed when
    the output is captured."""
    done = subprocess.run(
        ["kubectl", f"--context={context}", *args], check=True, capture_output=capture, text=True
    )
    return done.stdout if capture else ""


def reachable(context: str, namespace: str, resource: str) -> None:
    """Refuses a context whose cluster does not answer or has no such resource in the namespace."""
    try:
        kubectl(
            context,
            "--request-timeout=10s",
            f"--namespace={namespace}",
            "get",
            resource,
            "-o",
            "name",
        )
    except FileNotFoundError:
        raise Refused("kubectl is not on PATH") from None
    except subprocess.CalledProcessError as error:
        raise Refused(
            f"no {resource} in {namespace} on {context}: {error.stderr.strip()}"
        ) from None


def ready(pod: dict) -> bool:
    if pod["metadata"].get("deletionTimestamp"):
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def oom_killed(pod: dict) -> bool:
    """Whether a container of the pod was last terminated, or is terminated, by the OOM killer."""
    for status in pod.get("status", {}).get("containerStatuses", []):
        for state in (status.get("lastState", {}), status.get("state", {})):
            if state.get("terminated", {}).get("reason") == "OOMKilled":
                return True
    return False


async def until(check: Callable[[], Any], timeout_s: float, what: str, every_s: float = 0.5):
    """The first truthy value of `check`, which may be awaitable, polled every `every_s`."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        value = check()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"no {what} within {timeout_s:.0f} s")
        await asyncio.sleep(every_s)
