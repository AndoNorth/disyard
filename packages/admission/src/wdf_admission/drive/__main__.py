"""`python -m wdf_admission.drive --kube-context NAME JOURNEY [--jobs N] [--global-cap N]`.

JOURNEY is `smoke`, `control`, `deadletter` or `restart`, as `wdf_admission.drive` says. The driver
runs from the root of the tree, puts a fresh queue prefix, and the global cap of `--global-cap`, on
the admission deployment for the run, waiting for each rollout, and removes both after, also when
the run fails or is interrupted. Every journey prints `queue_prefix:`, `published:`, `finished:`,
`failed:`, `max_live:` and `duplicates:`, then `control` prints `skipped:`, `readmitted:` and
`resumed:`, `deadletter` prints `dead_lettered:`, `events:` and `rows: accepted N`, and `restart`
prints `restarts:` and `oom_killed:`.

Exit 0 when the journey passed, 1 when it did not, 2 for a usage error or a cluster it cannot reach,
with the reason on stderr.
"""

import argparse
import asyncio
import os
import sys
import traceback

from wdf_clusterkit import CONTROL, Refused, reachable
from wdf_workflow.harness import fresh_prefix

from . import (
    CLEARED,
    DEPLOYMENT,
    DISPATCH,
    JOURNEYS,
    OPERATOR,
    Counters,
    drive,
    env_args,
    password,
    set_env,
    split,
)


def positive(value: str) -> int:
    if not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive count")
    return int(value)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m wdf_admission.drive")
    top.add_argument("--kube-context", required=True, metavar="NAME")
    journeys = top.add_subparsers(dest="journey", required=True)
    for name in JOURNEYS:
        journey = journeys.add_parser(name)
        if name != "deadletter":
            journey.add_argument("--jobs", type=positive, default=20, metavar="N")
        journey.add_argument("--global-cap", type=positive, metavar="N")
    return top


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as exit:
        return 2 if exit.code else 0
    jobs = getattr(args, "jobs", 2)
    if args.journey == "control" and not all(split(jobs)):
        print(
            "drive: control takes at least 4 jobs, a quarter for each of its moves", file=sys.stderr
        )
        return 2
    if args.journey == "restart" and jobs < 2:
        print("drive: restart takes at least 2 jobs, half before each restart", file=sys.stderr)
        return 2
    context = args.kube_context
    try:
        reachable(context, CONTROL, f"deployment/{DEPLOYMENT}")
        passwords = {user: password(user) for user in (DISPATCH, OPERATOR)}
    except Refused as refused:
        print(f"drive: refused: {refused}", file=sys.stderr)
        return 2
    counters = Counters(args.journey, jobs, args.global_cap, fresh_prefix())
    try:
        set_env(context, env_args(counters.queue_prefix, args.global_cap))
        asyncio.run(drive(context, counters, passwords))
    finally:
        set_env(context, CLEARED)
    for line in counters.lines():
        print(line)
    return 0 if counters.passed() else 1


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as exit:
        code = exit.code
    except BaseException:
        traceback.print_exc()
        code = 1
    # The interpreter's teardown drops temporalio's runtime, which waits for its blocking threads,
    # and one handing a late result back to the event loop parks for good once it asks for the GIL
    # there. Nothing is left to tear down once the run has printed, so the process ends here.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
