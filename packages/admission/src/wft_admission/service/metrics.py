"""The admission metrics, served on the Prometheus endpoint of the service's Temporal runtime.

No label holds a tenant, a job id or a task id. Every counter and gauge starts at zero for each
label value the tenancy profile and the cycle can give it, so every name is served from the start.
"""

from collections import Counter
from collections.abc import Mapping

from temporalio.common import MetricMeter

from ..breaker import DEPENDENCIES, INTERNAL
from ..intake import EVENT_REASONS
from ..policy import Caps

COUNTERS = {
    "wft_admission_admitted_total": ("priority_class", "job_class"),
    "wft_admission_resumed_total": ("priority_class",),
    "wft_admission_readmitted_total": ("reason",),
    "wft_admission_skipped_total": ("reason",),
    "wft_admission_start_failed_total": (),
    "wft_admission_queue_missing_total": (),
    "wft_admission_duplicates_total": (),
    "wft_admission_row_message_mismatch_total": (),
    "wft_admission_dead_lettered_total": ("reason",),
}
GAUGES = {
    "wft_admission_live_jobs": ("job_class",),
    "wft_admission_open_jobs": (),
    "wft_admission_paused_jobs": ("job_class",),
    "wft_admission_resumes_waiting": ("priority_class",),
    "wft_admission_breaker_open": ("dependency",),
}
HISTOGRAMS = {
    "wft_admission_wait_seconds": ("priority_class",),
    "wft_admission_snapshot_seconds": (),
}
# The buckets of each histogram, in seconds: a wait spans a free slot to a long backlog, and a
# snapshot is a few milliseconds until its scan of the rows grows.
BUCKETS = {
    "wft_admission_wait_seconds": [0.1, 1, 5, 15, 60, 300, 900, 3600, 4 * 3600, 24 * 3600],
    "wft_admission_snapshot_seconds": [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1],
}
READMITTED = ("resumed-before-start", "stuck-admitted")
SKIPPED = ("started", "ended", "outstanding-action", "no-workflow", "stale-admitted")
DEAD_LETTERED = (*EVENT_REASONS, "ADMISSION_FAILED")


class Metrics:
    """The instruments of the admission names on one meter, and the counts already sent."""

    def __init__(self, meter: MetricMeter, priority_classes: list[str], caps: Caps):
        self.counters = {name: meter.create_counter(name) for name in COUNTERS}
        self.gauges = {name: meter.create_gauge(name) for name in GAUGES}
        self.histograms = {
            name: meter.create_histogram_float(name, unit="s") for name in HISTOGRAMS
        }
        self._sent: Counter[tuple[str, ...]] = Counter()
        self.priority_classes = priority_classes
        self.job_classes = list(caps.job_class)
        zero = {
            "wft_admission_admitted_total": [
                (p, j) for p in priority_classes for j in self.job_classes
            ],
            "wft_admission_resumed_total": [(p,) for p in priority_classes],
            "wft_admission_readmitted_total": [(r,) for r in READMITTED],
            "wft_admission_skipped_total": [(r,) for r in SKIPPED],
            "wft_admission_dead_lettered_total": [(r,) for r in DEAD_LETTERED],
        }
        for name, labels in COUNTERS.items():
            for values in zero.get(name, [()]):
                self.counters[name].add(0, dict(zip(labels, values, strict=True)))
        self.rows({}, 0, {}, {})
        closed = dict.fromkeys((*DEPENDENCIES, INTERNAL), 0)
        self.gauge("wft_admission_breaker_open", closed, "dependency")

    def count(self, counts: Mapping[tuple[str, ...], int]) -> None:
        """Sends what each counter of `counts`, keyed by name and label values, gained since the
        last call."""
        for key, total in counts.items():
            if gained := total - self._sent[key]:
                name, *values = key
                self.counters[name].add(gained, dict(zip(COUNTERS[name], values, strict=True)))
                self._sent[key] = total

    def gauge(self, name: str, values: Mapping[str, int], label: str = "") -> None:
        """Sets a gauge, by the value of its one label when it has one."""
        if not label:
            self.gauges[name].set(values[""])
            return
        for value, n in values.items():
            self.gauges[name].set(n, {label: value})

    def rows(
        self,
        live: Mapping[str, int],
        open_: int,
        paused: Mapping[str, int],
        waiting: Mapping[str, int],
    ) -> None:
        """Sets the gauges of the job rows, with zero for every class the snapshot did not name."""
        by_class = dict.fromkeys(self.job_classes, 0)
        self.gauge("wft_admission_live_jobs", {**by_class, **live}, "job_class")
        self.gauge("wft_admission_open_jobs", {"": open_})
        self.gauge("wft_admission_paused_jobs", {**by_class, **paused}, "job_class")
        resumes = {**dict.fromkeys(self.priority_classes, 0), **waiting}
        self.gauge("wft_admission_resumes_waiting", resumes, "priority_class")

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        self.histograms[name].record(seconds, labels or None)
