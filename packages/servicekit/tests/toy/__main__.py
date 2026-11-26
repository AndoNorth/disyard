"""A reference service built only from dsy_servicekit: `python -m toy`.
Needs TOY_NAME, exits 2 without it, marks TOY_READY_FILE ready and exits 0 on SIGTERM."""

import asyncio
import os
import sys
from pathlib import Path

from dsy_servicekit.config import exiting_on_config_error, require
from dsy_servicekit.lifecycle import ReadyFile, stopping


async def run(ready: ReadyFile) -> None:
    with stopping() as stop:
        ready.set()
        try:
            await stop.wait()
        finally:
            ready.clear()


def main() -> int:
    path = os.environ.get("TOY_READY_FILE")
    ready = ReadyFile(Path(path) if path else None)
    ready.arm()
    with exiting_on_config_error("toy"):
        require(os.environ, {"name": "TOY_NAME"})
    asyncio.run(run(ready))
    return 0


if __name__ == "__main__":
    sys.exit(main())
