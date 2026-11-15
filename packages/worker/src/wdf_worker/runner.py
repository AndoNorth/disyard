"""The one way the worker starts a child: in its own process group that dies with the worker,
with its output read live into a tail and the heartbeat parser, and stopped as a group."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

STUBBED = ("Child",)


@dataclass(frozen=True)
class Exit:
    """How a child ended: its exit code, or the signal that ended it, and whether the container's
    OOM kill count rose while it ran."""

    code: int | None
    signal: int | None
    oom: bool


class Child:
    """A running child process."""

    @classmethod
    async def start(cls, argv: Sequence[str], cwd: Path) -> "Child":
        raise NotImplementedError("not implemented: owned by lane R")

    def lines(self) -> AsyncIterator[str]:
        """Each line of the merged stdout and stderr, as it is read."""
        raise NotImplementedError("not implemented: owned by lane R")

    async def stop(self) -> None:
        """Stops the whole process group: SIGTERM, then SIGKILL after a grace."""
        raise NotImplementedError("not implemented: owned by lane R")

    async def wait(self) -> Exit:
        raise NotImplementedError("not implemented: owned by lane R")

    @property
    def tail(self) -> bytes:
        """The last bytes of the output, at most the staging contract's tail cap."""
        raise NotImplementedError("not implemented: owned by lane R")
