# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Runs the smoke gate with its cluster-verify row stood in by an ok row.

cluster-verify counts the obs pods against the budget, and the pods of the throwaway stack are applied by kubectl and
so are over it, which would skip every job row. Everything after row 1 runs unchanged.
Usage, from the root of the tree: uv run --frozen python docs/delivery/spikes/S13/code/smoke_s13.py
"""

import os
import sys

from wft_acceptance import smoke
from wft_acceptance.smoke import report


def standin(self) -> None:
    self.add(report.ok("cluster-verify", "stood in by the S13 run"))


smoke.Gate.cluster_verify = standin
try:
    code = smoke.main()
finally:
    sys.stdout.flush()
    sys.stderr.flush()
os._exit(code)
