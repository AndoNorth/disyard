"""The ready file of a service and its stop on SIGTERM and SIGINT."""

import asyncio
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["ReadyFile", "ThreadStop", "stopping"]

_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class ReadyFile:
    """The file that marks a service ready. A path of None makes every method a no-op."""

    def __init__(self, path: Path | None):
        self._path = path

    def arm(self) -> None:
        """Remove a stale file, so a reader never sees the previous run's mark."""
        self.clear()

    def set(self) -> None:
        if self._path:
            self._path.touch()

    def clear(self) -> None:
        if self._path:
            self._path.unlink(missing_ok=True)


@contextmanager
def stopping(loop: asyncio.AbstractEventLoop | None = None) -> Iterator[asyncio.Event]:
    """An event that SIGTERM and SIGINT set, with the handlers removed on exit."""
    loop = loop or asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in _SIGNALS:
        loop.add_signal_handler(signum, stop.set)
    try:
        yield stop
    finally:
        for signum in _SIGNALS:
            loop.remove_signal_handler(signum)


class ThreadStop:
    """The signal.signal form of `stopping`, for a service whose main thread blocks."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def install(self) -> None:
        for signum in _SIGNALS:
            signal.signal(signum, lambda *_: self.event.set())
