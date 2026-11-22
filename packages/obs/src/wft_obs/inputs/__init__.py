"""The state inputs exporter: `python -m wft_obs.inputs`.

It reads the `sql`, `sql_labelled` and `invariants` inputs of `contracts/state/inputs.yaml` from the
app database as the `wft_reader` group, and serves each as the gauge `wft_state_<name>`. Install
`wft-obs[stateops]` to run it.
"""

from .exporter import Exporter, metric_name

__all__ = ["Exporter", "metric_name"]
