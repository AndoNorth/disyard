"""The metrics of a process, served on the Prometheus endpoint of its Temporal runtime.

Names are held to the catalogue by the static test over each emitter's declarations, not here.
"""

from collections.abc import Mapping, Sequence

from temporalio.common import MetricMeter
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from wft_events.metrics import registry


def runtime(address: str, buckets: Mapping[str, Sequence[float]]) -> Runtime:
    """A runtime serving `/metrics` on `address`, with the catalogue's buckets for every histogram
    of the event registry and `buckets` for the emitter's own histograms."""
    overrides = {name: list(h.bounds) for name, h in registry.histograms.items()}
    overrides.update({name: list(bounds) for name, bounds in buckets.items()})
    metrics = PrometheusConfig(bind_address=address, histogram_bucket_overrides=overrides)
    return Runtime(telemetry=TelemetryConfig(metrics=metrics))


# A counter with no increment is not exported, so these start at 0 for the component and the
# series exists before the first drop.
DROP_COUNTERS = ("wft_obs_log_dropped_total", "wft_obs_span_dropped_total")


def export_registry(meter: MetricMeter, component: str) -> None:
    """Mirrors every later call of the event registry onto `meter`, and starts the drop counters
    of `component` at 0; call it once per process, at startup, since a call made before it is not
    replayed."""
    histograms = {name: meter.create_histogram_float(name) for name in registry.histograms}
    counters = {name: meter.create_counter(name) for name in DROP_COUNTERS}
    for counter in counters.values():
        counter.add(0, {"component": component})

    def mirror(name: str, value: float, labels: dict[str, str]) -> None:
        if name in histograms:
            histograms[name].record(value)
            return
        if name not in counters:
            counters[name] = meter.create_counter(name)
        counters[name].add(int(value), labels or None)

    registry.listeners.append(mirror)
