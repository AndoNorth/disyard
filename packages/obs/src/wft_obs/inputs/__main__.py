"""`python -m wft_obs.inputs`: serves the state inputs as gauges and refreshes them every minute.

Environment: WFT_STATE_INPUTS_DSN (required), WFT_METRICS_ADDRESS (default 0.0.0.0:9464) and
WFT_READY_FILE, which is written once a pass has reached the database.
Exit 2 when the DSN is missing, 0 after SIGTERM or SIGINT.
"""

import os
import signal
import sys
import threading
from pathlib import Path

from wft_stateops.inputs import load

from wft_obs import metrics
from wft_obs.setup import setup

from .exporter import Exporter

DSN_ENV = "WFT_STATE_INPUTS_DSN"
ADDRESS_ENV = "WFT_METRICS_ADDRESS"
READY_ENV = "WFT_READY_FILE"
DEFAULT_ADDRESS = "0.0.0.0:9464"
INTERVAL_SECONDS = 60


def serve(exporter: Exporter, stop: threading.Event, ready: Path | None, interval: float) -> None:
    """Runs a pass, then another each `interval` seconds, until `stop` is set."""
    if ready:
        ready.unlink(missing_ok=True)
    while not stop.is_set():
        if exporter.run_once() and ready:
            ready.touch()
        stop.wait(interval)


def main() -> int:
    setup("state-inputs")
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        print(f"wft_obs.inputs: {DSN_ENV} is not set", file=sys.stderr)
        return 2
    runtime = metrics.runtime(os.environ.get(ADDRESS_ENV, DEFAULT_ADDRESS), {})
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    ready = Path(os.environ[READY_ENV]) if os.environ.get(READY_ENV) else None
    serve(Exporter(runtime.metric_meter, dsn, load()), stop, ready, INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
