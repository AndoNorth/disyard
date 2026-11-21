"""A Temporal client of the namespace of the naming contract, with the payload converter the
workflows use."""

from temporalio.client import Client
from wft_workflow import testing


async def connect(address: str) -> Client:
    """One attempt, so a caller's deadline is not spent on a server that does not answer."""
    return await testing.connect(address, attempts=1)
