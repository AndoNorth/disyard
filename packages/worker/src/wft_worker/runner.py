"""The one way the worker starts a child: in its own process group that dies with the worker,
with its output read live into a tail and the heartbeat parser, and stopped as a group."""

import asyncio
import ctypes
import os
import signal
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from wft_runtime.heartbeat import beats
from wft_runtime.run import TAIL_BYTES

STUBBED = ()

# The container's own cgroup v2 counters, which count an OOM kill of any process inside it.
MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")
# Each read of the output, and the longest line handed on: a longer run of bytes with no newline
# is cut into lines of this size.
CHUNK_BYTES = TAIL_BYTES
# How long a stopped group has after SIGTERM before SIGKILL.
STOP_GRACE_S = 5.0

PR_SET_PDEATHSIG = 1
# Loaded before any fork, so the child only calls into it.
_prctl = ctypes.CDLL(None, use_errno=True).prctl


@dataclass(frozen=True)
class Exit:
    """How a child ended: its exit code, or the signal that ended it, and whether the container's
    OOM kill count rose while it ran."""

    code: int | None
    signal: int | None
    oom: bool


class Child:
    """A running child process.

    `lines()` is the one reader of the output. `wait()` and `stop()` read what is left, so they
    are called once a `lines()` loop has ended or been abandoned, never beside it.
    The parent death signal fires when the thread that started the child ends, so a child is
    started from the event loop's own thread, which lives as long as the worker.
    """

    def __init__(self, process: asyncio.subprocess.Process, oom_before: int | None, events: Path):
        self._process = process
        self._oom_before = oom_before
        self._events = events
        self._tail = bytearray()
        self._partial = bytearray()
        # Once the group is reaped its id may be reused, so it is never signalled again.
        self._reaped = False
        self.beat: dict | None = None
        """The progress of the latest heartbeat line read, None before the first."""

    @classmethod
    async def start(
        cls, argv: Sequence[str], cwd: Path, memory_events: Path = MEMORY_EVENTS
    ) -> "Child":
        parent = os.getpid()
        oom_before = _oom_kills(memory_events)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=lambda: _die_with(parent),
        )
        return cls(process, oom_before, memory_events)

    async def lines(self) -> AsyncIterator[str]:
        """Each line of the merged stdout and stderr, as it is read, without its line end."""
        stream = self._process.stdout
        while chunk := await stream.read(CHUNK_BYTES):
            self._keep(chunk)
            self._partial += chunk
            *done, rest = self._partial.split(b"\n")
            self._partial = bytearray(rest)
            while len(self._partial) >= CHUNK_BYTES:
                done.append(self._partial[:CHUNK_BYTES])
                del self._partial[:CHUNK_BYTES]
            for line in done:
                yield self._parse(line)
        if self._partial:
            line, self._partial = self._partial, bytearray()
            yield self._parse(line)

    async def stop(self) -> None:
        """Stops the whole process group: SIGTERM, then SIGKILL after a grace."""
        self._signal_group(signal.SIGTERM)
        try:
            async with asyncio.timeout(STOP_GRACE_S):
                await self._reap()
        except TimeoutError:
            self._signal_group(signal.SIGKILL)

    async def wait(self) -> Exit:
        """Waits for the child, and SIGKILLs what is left of its group, since one task's processes
        never outlive it."""
        await self._reap()
        returncode = self._process.returncode
        oom = returncode != 0 and _rose(self._oom_before, _oom_kills(self._events))
        if returncode < 0:
            return Exit(None, -returncode, oom)
        return Exit(returncode, None, oom)

    @property
    def tail(self) -> bytes:
        """The last bytes of the output, at most the staging contract's tail cap."""
        return bytes(self._tail[-TAIL_BYTES:])

    async def _reap(self) -> None:
        """Reads the output while the child runs, so a full pipe never holds it up, then kills the
        rest of the group, which closes the pipe of any grandchild still holding it."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._read_rest())
            await self._process.wait()
            self._signal_group(signal.SIGKILL)
        self._reaped = True

    async def _read_rest(self) -> None:
        async for _ in self.lines():
            pass

    def _keep(self, chunk: bytes) -> None:
        self._tail += chunk
        if len(self._tail) > 2 * TAIL_BYTES:
            del self._tail[:-TAIL_BYTES]

    def _parse(self, line: bytearray) -> str:
        text = line.decode(errors="replace").rstrip("\r")
        for beat in beats([text]):
            self.beat = beat
        return text

    def _signal_group(self, sig: int) -> None:
        if self._reaped:
            return
        try:
            os.killpg(self._process.pid, sig)
        except ProcessLookupError:
            pass


def _die_with(parent: int) -> None:
    """Runs in the child before exec: SIGKILL when the worker dies, and exit if it already has."""
    if _prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(127)
    if os.getppid() != parent:
        os._exit(127)


def _oom_kills(events: Path) -> int | None:
    """The `oom_kill` count of a memory.events file, None where there is none to read."""
    try:
        text = events.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            return int(value)
    return None


def _rose(before: int | None, after: int | None) -> bool:
    return before is not None and after is not None and after > before
