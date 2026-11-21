"""Continuous profiles pushed to Pyroscope, only when `WFT_PYROSCOPE_ADDRESS` is set.

profiles.install("simulate-worker", pool="simulate")
"""

import os

import pyroscope

ADDRESS = "WFT_PYROSCOPE_ADDRESS"


def install(component: str, pool: str | None = None) -> bool:
    """Starts the profiler as application `wft.<component>`, tagged with `pool` when it is given,
    at the SDK's default sample rate; returns whether it started."""
    address = os.environ.get(ADDRESS)
    if not address:
        return False
    pyroscope.configure(
        application_name=f"wft.{component}",
        server_address=address,
        tags={"pool": pool} if pool else None,
    )
    return True
