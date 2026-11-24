"""The placement metrics, served on the Prometheus endpoint of the service's Temporal runtime.

The labels are the task pools and the two verdicts of the decision line, so no label holds a job
or a task. Every counter starts at zero for each label value, so every name is served from the
start.
"""

from collections.abc import Iterable

from temporalio.common import MetricMeter

POOLS = ("compile", "simulate", "aggregate")
VERDICTS = ("placed", "unschedulable")
# The buckets of the decision duration, in seconds: a decision is a few microseconds of policy.
BUCKETS = {
    "dsy_placement_place_seconds": [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1],
}


class Metrics:
    """The instruments of the placement names on one meter."""

    def __init__(self, meter: MetricMeter, pools: Iterable[str] = POOLS):
        self.place_seconds = meter.create_histogram_float("dsy_placement_place_seconds", unit="s")
        self.decisions = meter.create_counter("dsy_placement_decisions_total")
        self.escalations = meter.create_counter("dsy_placement_escalations_total")
        self.snapshot_age = meter.create_gauge_float("dsy_placement_snapshot_age_seconds", unit="s")
        for pool in pools:
            self.escalations.add(0, {"pool": pool})
            for verdict in VERDICTS:
                self.decisions.add(0, {"pool": pool, "verdict": verdict})

    def decided(self, pool: str, verdict: str, escalate: bool, seconds: float) -> None:
        self.decisions.add(1, {"pool": pool, "verdict": verdict})
        if escalate:
            self.escalations.add(1, {"pool": pool})
        self.place_seconds.record(seconds)

    def snapshot_age_s(self, age_s: float | None) -> None:
        """Sets the snapshot age; before the first good read there is none to set."""
        if age_s is not None:
            self.snapshot_age.set(age_s)
