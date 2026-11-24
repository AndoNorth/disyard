"""`python -m dsy_workflow.service`: the workflow worker service of the build that DSY_BUILD_ID
names, configured from the environment. Exit 2 when a variable is missing."""

import asyncio
import os
import sys
from pathlib import Path

from dsy_artifactreaper import schedule
from dsy_obs.setup import setup

from . import READY_ENV, Config, MissingEnv, real_control, serve


def main() -> int:
    setup("workflow")
    try:
        config = Config.from_env(os.environ)
    except MissingEnv as error:
        print(f"dsy_workflow.service: {error}", file=sys.stderr)
        return 2
    activities, emit = real_control(config)
    ready = os.environ.get(READY_ENV)
    asyncio.run(
        serve(
            config,
            activities,
            emit,
            schedule.default_pass(),
            ready_file=Path(ready) if ready else None,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
