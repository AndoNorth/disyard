"""`python -m wft_admission.service`: the admission service, configured from the environment.
Exit 2 when a variable is missing or refused, and 1 when the tenancy profile is refused, both before
the ready file exists."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from wft_broker.tenancy import load

from . import READY_ENV, Config, ConfigError, serve


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ready = Path(os.environ[READY_ENV]) if os.environ.get(READY_ENV) else None
    if ready:
        ready.unlink(missing_ok=True)
    try:
        config = Config.from_env(os.environ)
    except ConfigError as error:
        print(f"wft_admission.service: {error}", file=sys.stderr)
        return 2
    try:
        profile = load(config.profile)
    except KeyError as error:
        print(f"wft_admission.service: {error.args[0]}", file=sys.stderr)
        return 1
    asyncio.run(serve(config, profile, ready_file=ready))
    return 0


if __name__ == "__main__":
    sys.exit(main())
