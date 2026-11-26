"""The workflow modules: workers that a package adds to the workflow service through the entry-point
group `dsy.workflow.modules`, so a new workflow needs no edit of this package.

An entry point resolves to a function `workers(ctx: Context) -> list[Worker]`. The host calls it
once per module, runs each returned worker under its own supervisor and drains it with the built-in
workers. A module that cannot be imported or built, or that dies later, is logged at error level
and never keeps the host from serving jobs.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import entry_points

from temporalio.client import Client
from temporalio.worker import Worker

__all__ = ["Context", "load"]

GROUP = "dsy.workflow.modules"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Context:
    # The host's client, with the converter, tracing interceptor and runtime already applied.
    client: Client
    build_id: str
    # The host's DRAIN, to pass as the `graceful_shutdown_timeout` of each Worker the module builds.
    drain: timedelta


def load() -> list[tuple[str, Callable[[Context], list[Worker]]]]:
    """The modules of the entry-point group, sorted by name. An entry point that fails to import is
    skipped, and so is every entry point of a name that repeats, since neither is the right one."""
    points = sorted(entry_points(group=GROUP), key=lambda point: point.name)
    names = Counter(point.name for point in points)
    modules = []
    for point in points:
        if names[point.name] > 1:
            log.error(
                "module %s is skipped: its name is used by %d entry points",
                point.name,
                names[point.name],
            )
            continue
        try:
            modules.append((point.name, point.load()))
        except Exception:
            log.exception(
                "module %s is skipped: its entry point %s failed to import", point.name, point.value
            )
    return modules
