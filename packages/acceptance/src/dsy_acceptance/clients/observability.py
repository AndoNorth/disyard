"""The observability reads of the suite: the read client of the stack under its own name, so the
ladder lists one place for them."""

from dsy_obs.query import QueryError, loki, profiled, prom, targets, trace

__all__ = ["QueryError", "loki", "profiled", "prom", "targets", "trace"]
