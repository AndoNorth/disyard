"""Small clients the smoke gate and the e2e suite drive the deployed platform through, from the
host: the API in `api`, the object store in `store`, Temporal in `temporal`, the broker's
dead-letter queue in `broker`, the database in `postgres`, the observability stack in
`observability` and the pods in `pod`.

Every direct read of the platform goes through one of these, so the ladder document lists them."""


class Failed(Exception):
    """A call a client relies on answered something else."""
