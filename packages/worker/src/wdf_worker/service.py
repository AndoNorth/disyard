"""The pool worker service: one activity worker on the pool's queue, one task at a time, which
drains on SIGTERM."""

from .config import Config

STUBBED = ("serve",)


async def serve(config: Config) -> None:
    raise NotImplementedError("not implemented: owned by lane A")
