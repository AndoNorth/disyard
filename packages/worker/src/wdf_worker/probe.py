"""`python -m wdf_worker.probe`: one line per platform endpoint, `<name>: ok` or `<name>: refused`,
and exit 0 when each answer is what the pool's NetworkPolicy allows."""

import sys

STUBBED = ("main",)


def main(argv: list[str]) -> int:
    raise NotImplementedError("not implemented: owned by lane A")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
