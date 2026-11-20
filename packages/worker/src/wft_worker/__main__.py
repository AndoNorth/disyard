"""`python -m wft_worker --pool {compile,simulate,aggregate}`: the pool worker service, configured
from the environment. Exit 2 when a variable is missing or malformed."""

import argparse
import asyncio
import logging
import os
import sys

from wft_workflow.contract import naming

from .config import load
from .service import serve

STUBBED = ()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m wft_worker")
    parser.add_argument("--pool", required=True, choices=list(naming().pools))
    args = parser.parse_args(argv)
    config = load(args.pool, os.environ)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(config))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
