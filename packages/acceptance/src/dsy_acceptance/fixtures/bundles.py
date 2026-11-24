"""Source bundles of the e2e jobs. Each is the bytes of a bundle.tar, ready for an upload."""

import tempfile
from pathlib import Path

from dsy_workloads import bundle

# An entry module that holds no Run class, so the compile task refuses it as compile_failed.
MISSING_ENTRY = "app"
MISSING_ENTRY_SOURCE = b"VALUE = 1\n"


def variant(name: str) -> bytes:
    """The workload library bundle of variant `name`: the same modules as every other variant and
    another source hash, so a second compile of it misses the cache."""
    with tempfile.TemporaryDirectory(prefix="dsy-bundle-") as work:
        return bundle.write(Path(work), name).read_bytes()


def missing_entry() -> bytes:
    """A bundle whose entry module, MISSING_ENTRY, has no Run class."""
    files = {**bundle.files(), f"{MISSING_ENTRY}.py": MISSING_ENTRY_SOURCE}
    with tempfile.TemporaryDirectory(prefix="dsy-bundle-") as work:
        return bundle.pack(Path(work), files).read_bytes()
