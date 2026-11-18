"""A read-only view of the pools through the Kubernetes API: the pool nodes and the live pods,
listed with the standard library and never watched, and a snapshot of them refreshed on a
background thread that a placement never waits for."""

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

CONTRACT = resources.files(__package__) / "contract"
TIMEOUT_S = 10
# A finished pod holds no requests on its node, so the API leaves it out of the list.
LIVE_PODS = "status.phase!=Succeeded,status.phase!=Failed"


@cache
def topology() -> dict:
    """The pool and namespace topology, read through package data."""
    return yaml.safe_load((CONTRACT / "topology.yaml").read_text())


class Reader:
    """Lists nodes and pods with a service account's token, trusting only the cluster's CA."""

    def __init__(self, api_url: str, token_file: str, ca_file: str):
        self.api_url = api_url.rstrip("/")
        self.token_file = Path(token_file)
        self.context = ssl.create_default_context(cafile=ca_file)

    def _list(self, path: str, query: dict[str, str]) -> dict:
        # The kubelet rotates a projected token in place, so each call reads the file again.
        token = self.token_file.read_text().strip()
        request = urllib.request.Request(
            f"{self.api_url}{path}?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=self.context) as response:
            return json.load(response)

    def nodes(self) -> dict:
        """The node list of every node that carries the pool label."""
        return self._list("/api/v1/nodes", {"labelSelector": topology()["pool_label"]})

    def pods(self) -> dict:
        """The pod list of every namespace, without the pods that finished: pool nodes also run
        kube-system daemon pods, whose requests count."""
        return self._list("/api/v1/pods", {"fieldSelector": LIVE_PODS})


def pool_snapshot(nodes: dict, pods: dict) -> Any:
    """The snapshot of the pools in a node list and a pod list."""
    from .snapshot import read

    return read(nodes, pods, topology())


class Snapshotter:
    """Reads a snapshot every `every_s` seconds once started. A failed read keeps the last good
    snapshot and records the error, and `current()` never waits for a read in flight."""

    def __init__(
        self,
        reader: Reader,
        every_s: float,
        build: Callable[[dict, dict], Any] = pool_snapshot,
    ):
        self.reader = reader
        self.every_s = every_s
        self.build = build
        self._lock = threading.Lock()
        self._snapshot = None
        self._read_at: float | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="snapshotter", daemon=True)

    def start(self) -> "Snapshotter":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def read(self) -> None:
        """One read of the pools, which never raises."""
        try:
            snapshot = self.build(self.reader.nodes(), self.reader.pods())
        except Exception as error:
            # Any failure, the API's or the snapshot's, leaves the last good snapshot in place.
            with self._lock:
                self._error = " ".join(f"{type(error).__name__}: {error}".split())
            return
        with self._lock:
            self._snapshot, self._read_at, self._error = snapshot, time.monotonic(), None

    def _run(self) -> None:
        while True:
            self.read()
            if self._stop.wait(self.every_s):
                return

    def current(self) -> tuple[Any, float | None, str | None]:
        """The last good snapshot, its age in seconds and the error of the last read, if it failed.
        Before the first good read, the snapshot and its age are None."""
        with self._lock:
            age_s = None if self._read_at is None else time.monotonic() - self._read_at
            return self._snapshot, age_s, self._error
