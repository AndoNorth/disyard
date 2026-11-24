"""Holds a session for a while and prints when and on which host ports, and is run by
test_containers.py.

Environment: DSY_RUN_ID, DSY_SLOT and DSY_T1_LOCK_DIR as for any session, HOLD_SECONDS,
HOLD_SESSION for the containers the session declares (default 2), and HOLD_CONTAINERS for the
number of canaries to run while holding, each publishing port index 0.
"""

import os
import sys
import time
from contextlib import ExitStack

from dsy_itest.session import Session


def main() -> None:
    hold = float(os.environ["HOLD_SECONDS"])
    count = int(os.environ["HOLD_CONTAINERS"])
    declared = int(os.environ.get("HOLD_SESSION", "2"))
    with Session(containers=declared) as session, ExitStack() as stack:
        for i in range(count):
            c = stack.enter_context(
                session.container(
                    "canary", ports={0: 8080}, command=["sleep", "600"], name=f"canary-{i}"
                )
            )
            print(f"port {c.host_ports[0]}", flush=True)
        print(f"held {time.time()}", flush=True)
        time.sleep(hold)
        print(f"released {time.time()}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
