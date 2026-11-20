"""The dead letters of the intake queues, counted by the operator's dead-letter tool."""

import subprocess
import sys

from wft_clusterkit import state_dir

from . import Failed


def dead_letters(address: str, timeout_s: float | None = None) -> int:
    """The messages in the dead-letter queue: the sum of the `count <reason> <n>` lines that
    `python -m wft_broker.dlq list` prints, as the operator, against the broker at `address`."""
    listed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wft_broker.dlq",
            "list",
            "--address",
            address,
            "--credentials-dir",
            str(state_dir() / "rabbitmq"),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if listed.returncode != 0:
        raise Failed(f"wft_broker.dlq list exited {listed.returncode}: {listed.stderr.strip()}")
    counts = [line.split() for line in listed.stdout.splitlines() if line.startswith("count ")]
    return sum(int(n) for _, _, n in counts)
