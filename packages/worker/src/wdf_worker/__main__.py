"""`python -m wdf_worker --pool {compile,simulate,aggregate}`: the pool worker service, configured
from the environment. Exit 2 when a variable is missing or malformed."""

import sys

STUBBED = ("main",)


def main(argv: list[str]) -> int:
    raise NotImplementedError("not implemented: owned by lane A")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
