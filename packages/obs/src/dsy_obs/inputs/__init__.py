"""The state inputs exporter: `python -m dsy_obs.inputs`.

It reads the `sql`, `sql_labelled` and `invariants` inputs of `contracts/state/inputs.yaml` from the
app database as the `dsy_reader` group, and serves each as the gauge `dsy_state_<name>`. Install
`dsy-obs[stateops]` to run it.
"""

from .exporter import Exporter, metric_name

__all__ = ["Exporter", "metric_name"]
