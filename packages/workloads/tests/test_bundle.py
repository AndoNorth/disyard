"""The bundle holds the kernel and the workloads under a lockfile with no dependencies, and the
builder gives the same bytes every time."""

import subprocess
import sys
import tarfile

from dsy_compile import bundle as compile_bundle
from dsy_workloads import bundle


def write(out) -> bytes:
    done = subprocess.run(
        [sys.executable, "-m", "dsy_workloads.bundle", "--out", str(out)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return (out / bundle.NAME).read_bytes()


def test_bundle_is_reproducible(tmp_path):
    first, second = write(tmp_path / "a"), write(tmp_path / "b")
    print(f"{len(first)} bytes, the same twice: {first == second}")
    assert first == second
    info = compile_bundle.info(tmp_path / "a" / bundle.NAME)
    assert {"dsy_workloads.main", "dsy_kernel.writer"} <= info.modules
    with tarfile.open(tmp_path / "a" / bundle.NAME) as tar:
        assert all(m.mtime == m.uid == m.gid == 0 and m.isfile() for m in tar)


def test_bundle_lockfile_is_current(tmp_path):
    """uv accepts the lockfile as up to date for the bundle's pyproject.toml."""
    compile_bundle.unpack(bundle.write(tmp_path), tmp_path / "src")
    done = subprocess.run(
        ["uv", "lock", "--locked", "--offline", "--no-config"],
        cwd=tmp_path / "src",
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_variants_differ_in_bytes_and_share_modules(tmp_path):
    plain, a, again = (
        bundle.write(tmp_path / name, variant)
        for name, variant in (("p", None), ("a", "a"), ("b", "a"))
    )
    other = bundle.write(tmp_path / "c", "b")
    assert len({p.read_bytes() for p in (plain, a, other)}) == 3
    assert a.read_bytes() == again.read_bytes()
    modules = {compile_bundle.info(p).modules for p in (plain, a, other)}
    assert len(modules) == 1
    assert compile_bundle.info(plain).source_sha256 != compile_bundle.info(a).source_sha256
