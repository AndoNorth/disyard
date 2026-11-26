"""`python -m dsy_obs.inputs`: serves the state inputs as gauges and refreshes them every minute.

Environment: DSY_STATE_INPUTS_DSN (required), DSY_METRICS_ADDRESS (default 0.0.0.0:9464) and
DSY_READY_FILE, which is written once a pass has reached the database.
Exit 2 when the DSN is missing, 0 after SIGTERM or SIGINT.
"""

import os
import sys
import threading
from pathlib import Path

from dsy_servicekit.config import exiting_on_config_error, require
from dsy_servicekit.lifecycle import ReadyFile, ThreadStop
from dsy_stateops.inputs import load

from dsy_obs import metrics
from dsy_obs.setup import setup

from .exporter import Exporter

DSN_ENV = "DSY_STATE_INPUTS_DSN"
ADDRESS_ENV = "DSY_METRICS_ADDRESS"
READY_ENV = "DSY_READY_FILE"
DEFAULT_ADDRESS = "0.0.0.0:9464"
INTERVAL_SECONDS = 60


def serve(exporter: Exporter, stop: threading.Event, ready: Path | None, interval: float) -> None:
    """Runs a pass, then another each `interval` seconds, until `stop` is set."""
    mark = ReadyFile(ready)
    mark.arm()
    while not stop.is_set():
        if exporter.run_once():
            mark.set()
        stop.wait(interval)


def main() -> int:
    setup("state-inputs")
    with exiting_on_config_error("dsy_obs.inputs"):
        dsn = require(os.environ, {"dsn": DSN_ENV})["dsn"]
    runtime = metrics.runtime(os.environ.get(ADDRESS_ENV, DEFAULT_ADDRESS), {})
    metrics.export_registry(runtime.metric_meter, "state-inputs")
    stopper = ThreadStop()
    stopper.install()
    ready = Path(os.environ[READY_ENV]) if os.environ.get(READY_ENV) else None
    serve(Exporter(runtime.metric_meter, dsn, load()), stopper.event, ready, INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
