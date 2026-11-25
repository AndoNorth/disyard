"""The harness of the level 4 suite: the constants of the cluster it runs against, the order of its
scenarios, polling with a timeout, counter baselines and the per-scenario summary.

Run by `scripts/e2e.sh` from the repository root, which holds the cluster lock and the budget timer.
The scenarios live under `tests/e2e/` and build on the clients and fixtures of this package.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from dsy_clusterkit import until

from ..clients import api
from ..smoke import CONTEXT, CONTROL_API, DISPATCH

__all__ = [
    "BATCH_PREFIX",
    "CONTEXT",
    "CONTROL_API",
    "DISPATCH",
    "ORDER",
    "TENANT",
    "batch_key",
    "wait",
]

# The tenant of the jobs, which the tenancy profile of preset ci holds beside acme.
TENANT = "e2e"
BATCH_PREFIX = "e2e"
# The order of a run. A scenario that changes shared state runs last and restores it in a finally,
# so a failed run leaves the cluster as the smoke gate expects it.
ORDER = ("H1", "H2", "N6", "N1", "N2", "N3", "N4", "C1", "N5")


def batch_key() -> str:
    """A fresh `e2e-<12 hex>` key, so no two runs share a job or a dedup entry."""
    return api.batch_key(BATCH_PREFIX)


def wait(check: Callable[[], Any], timeout_s: float, what: str, every_s: float = 1.0) -> Any:
    """The first truthy value of `check`, polled until the timeout, which raises TimeoutError that
    names `what`. Never a fixed sleep, because the observability backends lag."""
    return asyncio.run(until(check, timeout_s, what, every_s))
