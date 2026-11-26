"""`python -m dsy_workflow.service`: the workflow worker service of the build that DSY_BUILD_ID
names, configured from the environment. Exit 2 when a variable is missing."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dsy_artifactreaper import schedule
from dsy_obs.setup import setup
from dsy_servicekit.config import exiting_on_config_error
from dsy_servicekit.lifecycle import ReadyFile

from . import READY_ENV, Config, real_control, serve


def leave(code: int) -> None:
    """Ends the process without interpreter finalization. Finalization drops the Temporal runtime
    with the GIL held, and the drop waits for a runtime thread that is inside a result callback
    and needs the GIL, so the process can hang after a clean drain, for as long as it is left."""
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main() -> int:
    setup("workflow")
    path = os.environ.get(READY_ENV)
    ready = Path(path) if path else None
    ReadyFile(ready).arm()
    with exiting_on_config_error("dsy_workflow.service"):
        config = Config.from_env(os.environ)
    activities, emit = real_control(config)
    asyncio.run(
        serve(
            config,
            activities,
            emit,
            schedule.default_pass(),
            ready_file=ready,
        )
    )
    leave(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
