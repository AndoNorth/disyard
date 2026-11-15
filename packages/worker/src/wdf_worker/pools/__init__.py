"""The task path of each pool, `wdf_worker.pools.<pool>.run(task, context)`, and the context the
activity hands it."""

from dataclasses import dataclass
from pathlib import Path

from wdf_artifactclient.client import ArtifactClient


@dataclass
class Context:
    """What a task path runs with. The path sets `beat` to each progress beat it parses, and the
    activity heartbeats the latest one."""

    store: ArtifactClient
    scratch: Path
    slot_memory_mib: int
    mirror: str | None = None
    beat: dict | None = None
