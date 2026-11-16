"""`python -m wdf_workflow.harness (--address HOST:PORT | --kube-context NAME) COMMAND`.

`describe` prints the namespace, its retention, its search attributes and the reaper's Schedule.
`smoke`, `restart` and `rollout` run `--jobs N` smoke jobs and print `finished:`, `failed:` and
`infra_retries:`, plus one line per scenario counter. `restart --target POD,...` restarts the named
Temporal Deployments' pods, `workflow-worker` or a pool worker (`compile-worker`, `simulate-worker`,
`aggregate-worker`) once tasks are in flight, and `--scenario` adds `pause-resume`, `zombie` or
`worker-death` with `--deaths N`. `rollout` deploys the build of WDF_BUILD_ID beside the current
one.

`smoke` and `restart` run on FakePools (`--pools fake`, the default) or on the deployed pool
workers (`--pools real`), where `smoke` also prints `verified:`, the jobs whose four simulate
results hold the level 0 `echo` vector. `--queue-prefix P` puts the jobs' pool queues behind P,
by default a fresh prefix per fake run and none for a real one, and a run prints it as
`queue_prefix:`.

Exit 0 when every job finished, 1 when one did not, 2 for a usage error or a command this tier
refuses, with the reason on stderr.
"""

import argparse
import asyncio
import os
import sys
import traceback

from ..contract import naming
from . import (
    POOLS,
    SCENARIOS,
    Local,
    Refused,
    describe,
    fresh_prefix,
    rollout,
    run_jobs,
    valid_prefix,
)


def targets(value: str) -> list[str]:
    named = [t for t in value.split(",") if t]
    if not named:
        raise argparse.ArgumentTypeError("names no pod")
    return named


def queue_prefix(value: str) -> str:
    if not valid_prefix(value):
        raise argparse.ArgumentTypeError(f"{value!r} makes a pool queue outside the QUEUE pattern")
    return value


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m wdf_workflow.harness")
    where = top.add_mutually_exclusive_group(required=True)
    where.add_argument("--address", metavar="HOST:PORT", help="T1: a dev server")
    where.add_argument("--kube-context", metavar="NAME", help="T2: the cluster of a kube context")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("describe")
    for name in ("smoke", "restart", "rollout"):
        command = commands.add_parser(name)
        command.add_argument("--jobs", type=int, default=1, metavar="N")
        if name == "rollout":
            continue
        command.add_argument("--scenario", choices=SCENARIOS)
        command.add_argument("--deaths", type=int, default=1, metavar="N")
        command.add_argument("--pools", choices=POOLS, default="fake")
        command.add_argument("--queue-prefix", type=queue_prefix, metavar="P")
        if name == "restart":
            command.add_argument(
                "--target",
                required=True,
                type=targets,
                metavar="POD,...",
            )
    return top


async def run(args: argparse.Namespace) -> int:
    if args.address:
        tier = Local(args.address)
    else:
        from .cluster import Cluster

        tier = Cluster(args.kube_context)
    scenario = getattr(args, "scenario", None)
    targets = getattr(args, "target", [])
    pools = getattr(args, "pools", "fake")
    prefix = getattr(args, "queue_prefix", None)
    if prefix is None:
        prefix = fresh_prefix() if pools == "fake" else ""
    tier.refuse(args.command, scenario, targets, pools)
    async with tier:
        if args.command == "describe":
            for line in await describe(tier.client):
                print(line)
            return 0
        if args.command == "rollout":
            build = os.environ.get(naming().workflow.build_id_env)
            if not build:
                raise Refused(f"rollout deploys the build of {naming().workflow.build_id_env}")
            counters = await rollout(tier, args.jobs, build, prefix)
        else:
            counters = await run_jobs(
                tier, args.command, args.jobs, scenario, args.deaths, targets, pools, prefix
            )
    if not counters.printed:
        for line in counters.lines():
            print(line)
    return 0 if counters.passed(args.jobs * (2 if args.command == "rollout" else 1)) else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, "jobs", 1) < 1 or getattr(args, "deaths", 1) < 1:
        print("harness: --jobs and --deaths take a positive count", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args))
    except Refused as refused:
        print(f"harness: refused: {refused}", file=sys.stderr)
        return 2


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
