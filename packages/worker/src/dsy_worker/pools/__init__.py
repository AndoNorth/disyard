"""The task path of each pool, `dsy_worker.pools.<pool>.run(task, context)`, and the context the
activity hands it."""

from dataclasses import dataclass
from pathlib import Path

from dsy_artifactclient.client import ArtifactClient

from ..metrics import Metrics


@dataclass
class Context:
    """What a task path runs with. The path sets `beat` to each progress beat it parses, and the
    activity heartbeats the latest one. `metrics` is where a path counts what only it can see."""

    store: ArtifactClient
    scratch: Path
    slot_memory_mib: int
    mirror: str | None = None
    beat: dict | None = None
    metrics: Metrics | None = None
