"""The metrics of the dispatch and control API, served on the Prometheus endpoint of the service's
Temporal runtime.

No label holds a job, batch or tenant id. Every counter starts at zero for each label value it can
take, so every name is served from the start.
"""

import psycopg
from temporalio.common import MetricMeter

from ..contract import reasons
from ..models import EntryAnswer
from ..retrier import WITH_WORKFLOW

COUNTERS = {
    "wdf_dispatch_jobs_total": ("outcome", "reason"),
    "wdf_control_requests_total": ("action", "outcome"),
}
GAUGES = ("wdf_dispatch_unpublished_jobs", "wdf_control_outbox_jobs")
AGES = ("wdf_dispatch_unpublished_oldest_seconds", "wdf_control_outbox_oldest_seconds")
HISTOGRAMS = ("wdf_dispatch_submit_seconds",)
# A submit call spans one small batch to the largest, which takes minutes.
BUCKETS = {"wdf_dispatch_submit_seconds": [0.1, 0.5, 1, 5, 15, 60, 180, 600, 1800]}
# The reason label of an entry that was not rejected.
NO_REASON = "none"
ACTIONS = ("cancel", "terminate", "pause", "resume")
CONTROL_OUTCOMES = ("applied", "requested", "noop", "refused")

# The rows that the reconciler publishes, filtered on the status as its selection is.
UNPUBLISHED = """
SELECT count(*), EXTRACT(EPOCH FROM now() - min(created_at))::float FROM accepted_job
WHERE status = 'accepted' AND published_at IS NULL
"""
# The requests that the retrier delivers. `updated_at` stands for the time of the request, which a
# move of the row while it waits makes younger.
OUTBOX = """
SELECT count(*), EXTRACT(EPOCH FROM now() - min(updated_at))::float FROM accepted_job
WHERE requested_action IN ('cancel', 'terminate', 'pause') AND control_delivered_at IS NULL
AND status = ANY(%s)
"""


class Metrics:
    def __init__(self, meter: MetricMeter):
        self.counters = {name: meter.create_counter(name) for name in COUNTERS}
        self.gauges = {name: meter.create_gauge(name) for name in GAUGES}
        self.ages = {name: meter.create_gauge_float(name, unit="s") for name in AGES}
        self.histograms = {
            name: meter.create_histogram_float(name, unit="s") for name in HISTOGRAMS
        }
        entry = [row.code for row in reasons().reasons if "entry" in row.scopes]
        jobs = self.counters["wdf_dispatch_jobs_total"]
        for outcome, reason in [
            ("accepted", NO_REASON),
            ("duplicate", NO_REASON),
            *(("rejected", code) for code in entry),
        ]:
            jobs.add(0, {"outcome": outcome, "reason": reason})
        for action in ACTIONS:
            for outcome in CONTROL_OUTCOMES:
                self.control(action, outcome, 0)
        for name in GAUGES:
            self.gauges[name].set(0)
        for name in AGES:
            self.ages[name].set(0.0)

    def answered(self, answer: EntryAnswer) -> None:
        labels = {"outcome": answer.outcome, "reason": answer.reason or NO_REASON}
        self.counters["wdf_dispatch_jobs_total"].add(1, labels)

    def control(self, action: str, outcome: str, n: int = 1) -> None:
        self.counters["wdf_control_requests_total"].add(n, {"action": action, "outcome": outcome})

    def submitted(self, seconds: float) -> None:
        self.histograms["wdf_dispatch_submit_seconds"].record(seconds)

    def backlog(self, dispatch: psycopg.Connection, control: psycopg.Connection) -> None:
        """Sets the gauges of the rows that wait to be published and of the requests that wait to
        be delivered, from connections of the dispatch and the control logins."""
        for conn, query, args, name, age in (
            (dispatch, UNPUBLISHED, (), *_names("dispatch_unpublished")),
            (control, OUTBOX, (list(WITH_WORKFLOW),), *_names("control_outbox")),
        ):
            count, oldest = conn.execute(query, args).fetchone()
            self.gauges[name].set(count)
            self.ages[age].set(oldest or 0.0)


def _names(subject: str) -> tuple[str, str]:
    return f"wdf_{subject}_jobs", f"wdf_{subject}_oldest_seconds"
