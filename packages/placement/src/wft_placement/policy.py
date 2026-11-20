"""The placement policy, the one module to replace for a different policy: every answer the service
gives comes from the placer that `policy()` returns, so callers look it up through this module."""

from wft_workflow.placement import Placer, StaticPlacer


def policy() -> Placer:
    """Each task on the queue of its pool with the timeouts of the pool table.

    The table has one memory class per pool, so an escalation keeps the class and is not flagged.
    """
    return StaticPlacer()
