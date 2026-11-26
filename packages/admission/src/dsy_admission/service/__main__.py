"""`python -m dsy_admission.service`: the admission service, configured from the environment.
Exit 2 when a variable is missing or refused, and 1 when the tenancy profile is refused, both before
the ready file exists."""

import asyncio
import os
import sys
from pathlib import Path

from dsy_broker.tenancy import load
from dsy_obs.setup import setup
from dsy_servicekit.config import exiting_on_config_error
from dsy_servicekit.lifecycle import ReadyFile

from . import READY_ENV, Config, serve


def main() -> int:
    setup("admission")
    path = os.environ.get(READY_ENV)
    ready_path = Path(path) if path else None
    ReadyFile(ready_path).arm()
    with exiting_on_config_error("dsy_admission.service"):
        config = Config.from_env(os.environ)
    try:
        profile = load(config.profile)
    except KeyError as error:
        print(f"dsy_admission.service: {error.args[0]}", file=sys.stderr)
        return 1
    asyncio.run(serve(config, profile, ready_file=ready_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
