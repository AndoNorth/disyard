"""`python -m dsy_dispatchapi.smoke --scenario NAME [--jobs N] [--dispatch URL] [--control URL]
[--kube-context NAME]`.

NAME is a scenario of `dsy_dispatchapi.smoke`. The driver runs from the root of the tree, on the
host ports of the two listeners and the `kind-dsy` context by default. `upload` prints `created:`
and `already_present:`, `submit` prints `accepted:`, `duplicate_on_resend:` and `unknown_tenant:`,
`cancel-before-start` prints `row_only:`, `status:` and `workflow:`, `run` prints `status:` and
`results:`, `pause-resume` and `cancel-running` print `seen:`, and `restart` prints `rows:`,
`accepted_events:`, `lost:`, `doubled:`, `dlq:`, `unpublished_at:` as the backlog falls,
`unpublished_rises:` and `unpublished:`.

Exit 0 when the scenario showed what it expects, 1 when it did not or a call it relies on answered
otherwise, 2 for a usage error or a cluster it cannot reach, with the reason on stderr.
"""

import argparse
import os
import sys
import traceback

from dsy_clusterkit import CONTROL, Refused, reachable

from . import DEPLOYMENT, SCENARIOS, Failed, Smoke

# The jobs of each scenario when --jobs is not given.
JOBS = {"submit": 3, "restart": 2000}
# The scenarios that run one job whatever --jobs says.
ONE_JOB = ("cancel-before-start", "pause-resume", "cancel-running")


def positive(value: str) -> int:
    if not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive count")
    return int(value)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m dsy_dispatchapi.smoke")
    top.add_argument("--scenario", required=True, choices=SCENARIOS)
    top.add_argument("--jobs", type=positive, metavar="N")
    top.add_argument("--dispatch", default="http://127.0.0.1:18081", metavar="URL")
    top.add_argument("--control", default="http://127.0.0.1:18080", metavar="URL")
    top.add_argument("--kube-context", default="kind-dsy", metavar="NAME")
    return top


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as exit:
        return 2 if exit.code else 0
    if args.jobs is not None and args.scenario in ONE_JOB + ("upload",):
        print(f"smoke: {args.scenario} takes no --jobs", file=sys.stderr)
        return 2
    try:
        reachable(args.kube_context, CONTROL, f"deployment/{DEPLOYMENT}")
    except Refused as refused:
        print(f"smoke: refused: {refused}", file=sys.stderr)
        return 2
    jobs = args.jobs or JOBS.get(args.scenario, 1)
    smoke = Smoke(args.dispatch.rstrip("/"), args.control.rstrip("/"), args.kube_context, jobs)
    try:
        return 0 if smoke.run(args.scenario) else 1
    except Failed as failed:
        print(f"smoke: failed: {failed}", file=sys.stderr)
        return 1


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
