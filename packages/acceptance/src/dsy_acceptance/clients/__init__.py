"""Small clients the smoke gate and the e2e suite drive the deployed platform through, from the
host: the API in `api`, the object store in `store`, Temporal in `temporal` and the broker's
dead-letter queue in `broker`."""


class Failed(Exception):
    """A call a client relies on answered something else."""
