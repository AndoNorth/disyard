"""Usage: python -m dsy_workloads.bundle --out DIR [--variant NAME]

Writes DIR/bundle.tar with dsy_kernel, dsy_workloads, a pyproject.toml and a uv.lock with no
dependencies. A variant adds a VARIANT marker file, so bundles of different variants differ in
bytes, and in source hash, while holding the same modules.
"""

import argparse
import io
import sys
import tarfile
from pathlib import Path

import dsy_kernel

import dsy_workloads

STUBBED = ()

NAME = "bundle.tar"
VARIANT_FILE = "VARIANT"
PACKAGES = (dsy_kernel, dsy_workloads)
PYPROJECT = b"""\
[project]
name = "dsy-workloads"
version = "0"
requires-python = ">=3.13"
dependencies = []
"""
# What `uv lock` writes for PYPROJECT, kept as bytes so the bundle never depends on the uv at hand.
LOCKFILE = b"""\
version = 1
revision = 3
requires-python = ">=3.13"

[[package]]
name = "dsy-workloads"
version = "0"
source = { virtual = "." }
"""


def files(variant: str | None = None) -> dict[str, bytes]:
    """Every file of the bundle by its path in the archive: the source modules of each package, and
    the marker of the variant when there is one."""
    found = {"pyproject.toml": PYPROJECT, "uv.lock": LOCKFILE}
    if variant is not None:
        found[VARIANT_FILE] = variant.encode()
    for package in PACKAGES:
        root = Path(package.__file__).parent
        for path in root.rglob("*.py"):
            found[path.relative_to(root.parent).as_posix()] = path.read_bytes()
    return found


def write(out: Path, variant: str | None = None) -> Path:
    """Writes out/bundle.tar sorted, with zero times and owners, so equal sources and variants
    give equal bytes."""
    return pack(out, files(variant))


def pack(out: Path, found: dict[str, bytes]) -> Path:
    """Writes out/bundle.tar from the files by path, in the form of write."""
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out / NAME, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(found.items()):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            tar.addfile(entry, io.BytesIO(data))
    return out / NAME


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m dsy_workloads.bundle")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant")
    args = parser.parse_args(argv)
    write(args.out, args.variant)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
