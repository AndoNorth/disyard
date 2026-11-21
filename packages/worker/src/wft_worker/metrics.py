"""The worker metrics, served on the Prometheus endpoint of the service's Temporal runtime.

No label holds a job id, task id or attempt. A pool's counters start at zero for each exit class
the pool can end with, so every series is served from the start.
"""

from collections.abc import Mapping

from temporalio.common import MetricMeter
from temporalio.runtime import Runtime
from wft_obs.metrics import runtime
from wft_workflow.contract import exit_classes

STUBBED = ()

# The port the metrics are served on, which the pool's container port named `metrics` publishes.
ADDRESS_ENV = "WFT_METRICS_ADDRESS"
OK = "ok"

COUNTERS = {
    "wft_worker_tasks_total": ("pool", "exit_class"),
    "wft_worker_aggregate_inputs_total": (),
    "wft_worker_aggregate_rows_total": (),
}
HISTOGRAMS = {
    "wft_worker_task_seconds": ("pool", "exit_class"),
    "wft_worker_heartbeat_lag_seconds": ("pool",),
}
# The bounds of each histogram, held equal to the catalogue by a test.
BUCKETS = {
    "wft_worker_task_seconds": [1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 14400, 86400],
    "wft_worker_heartbeat_lag_seconds": [0.5, 1, 2, 5, 10, 15, 30, 60, 120],
}


def classes_of(pool: str) -> list[str]:
    """The exit classes a task of `pool` is counted under: ok and the pool's own classes."""
    return [OK, *(c for c, (p, _) in exit_classes().items() if p == pool)]


def runtime_of(environ: Mapping[str, str]) -> Runtime:
    """The runtime that serves the metrics on the address in `environ`, or the SDK's default
    runtime, which serves none, when the address is unset."""
    address = environ.get(ADDRESS_ENV)
    return runtime(address, BUCKETS) if address else Runtime.default()


class Metrics:
    """The instruments of the worker names of one pool on one meter."""

    def __init__(self, meter: MetricMeter, pool: str):
        self.pool = pool
        self.counters = {name: meter.create_counter(name) for name in COUNTERS}
        self.histograms = {
            name: meter.create_histogram_float(name, unit="s") for name in HISTOGRAMS
        }
        for exit_class in classes_of(pool):
            self.counters["wft_worker_tasks_total"].add(0, self._labels(exit_class))
        self.counters["wft_worker_aggregate_inputs_total"].add(0)
        self.counters["wft_worker_aggregate_rows_total"].add(0)

    def _labels(self, exit_class: str) -> dict[str, str]:
        return {"pool": self.pool, "exit_class": exit_class}

    def task_ended(self, exit_class: str, seconds: float) -> None:
        """Counts one task attempt under `exit_class` and records how long it ran. A class outside
        the pool's own, such as an artifact failure or a worker shutdown, is not a label value, so
        it is left to the outcome log line."""
        if exit_class not in classes_of(self.pool):
            return
        self.counters["wft_worker_tasks_total"].add(1, self._labels(exit_class))
        self.histograms["wft_worker_task_seconds"].record(seconds, self._labels(exit_class))

    def heartbeat_lag(self, seconds: float) -> None:
        self.histograms["wft_worker_heartbeat_lag_seconds"].record(seconds, {"pool": self.pool})
