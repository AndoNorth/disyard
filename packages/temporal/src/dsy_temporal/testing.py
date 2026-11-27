"""The Temporal dev server container that tests and the corpus recorder start."""

from collections.abc import Iterator
from contextlib import contextmanager

IMAGE = "temporalio/temporal"


@contextmanager
def dev_container(t1, *, namespace: str = "", db_filename: str | None = None) -> Iterator:
    """Starts a dev server in a container of the T1 session and yields it once its port is open,
    the container being gone afterwards.

    The server has only its own namespace unless `namespace` names one. `db_filename` keeps the
    server's state in that file of the container, which survives a restart of the server where
    memory does not. The port opens before gRPC answers, which is the caller's to wait for.
    """
    command = ["server", "start-dev", "--ip", "0.0.0.0", "--headless"]
    if namespace:
        command += ["--namespace", namespace]
    if db_filename:
        command += ["--db-filename", db_filename]
    with t1.container(IMAGE, ports={0: 7233}, command=tuple(command)) as container:
        container.wait_ready(0)
        yield container
