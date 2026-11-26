"""`python -m dsy_workflow.service`: the workflow worker service of the build that DSY_BUILD_ID
names, configured from the environment. Exit 2 when a variable is missing."""

import asyncio
import os
import sys
from pathlib import Path

from dsy_artifactreaper import schedule
from dsy_obs.setup import setup
from dsy_servicekit.config import exiting_on_config_error
from dsy_servicekit.lifecycle import ReadyFile

from . import READY_ENV, Config, real_control, serve


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
