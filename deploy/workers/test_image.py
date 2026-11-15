"""Image tests of the pool workers, run by `just itest`: each pool's image built by image.sh, then
inspected and run.

The builds need Docker and the network, for the hash-pinned packages and the release assets of
nix/wasm-tools.nix, and every container carries the run label of the T1 session.
"""

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from wdf_itest.session import RUN_LABEL
from wdf_workloads import bundle

ROOT = Path(__file__).resolve().parents[2]
IMAGE_SH = ROOT / "deploy/workers/image.sh"
POOLS = ("compile", "simulate", "aggregate")
TOOLS = ("wasmtime", "wasm-tools", "uv")
ON_PATH = {"compile": ["uv", "wasm-tools"], "simulate": ["wasmtime"], "aggregate": []}
# The scratch environment a pool pod gives the worker, each a directory of the /var/wdf volume.
SCRATCH_ENV = {
    "HOME": "/var/wdf/home",
    "TMPDIR": "/var/wdf/tmp",
    "XDG_CACHE_HOME": "/var/wdf/cache",
    "UV_CACHE_DIR": "/var/wdf/cache/uv",
}
PROBE = f"""
import json, os, shutil
found = [t for t in {TOOLS!r} if shutil.which(t)]
print(json.dumps({{
    "uid": os.getuid(),
    "on_path": sorted(found),
    "mtimes": sorted({{os.stat(shutil.which(t)).st_mtime for t in found if t != "uv"}}),
    "etc_wdf": os.path.exists("/etc/wdf"),
}}))
"""

pytestmark = pytest.mark.integration


def run(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=check)


@pytest.fixture(scope="module")
def images(t1):
    refs = {pool: f"wdf-test-{pool}-worker:{t1.run_id.lower()}" for pool in POOLS}
    try:
        for pool, ref in refs.items():
            print(run(str(IMAGE_SH), pool, ref).stdout.strip())
        yield refs
    finally:
        run("docker", "rmi", "-f", *refs.values(), check=False)


@pytest.fixture
def docker_run(t1):
    def go(ref: str, *argv: str, entrypoint: str, options=()) -> subprocess.CompletedProcess:
        label = f"{RUN_LABEL}={t1.run_id}"
        docker = ["docker", "run", "--rm", "--label", label, *options, "--entrypoint", entrypoint]
        return run(*docker, ref, *argv, check=False)

    return go


@pytest.mark.parametrize("pool", POOLS)
def test_image_command_user_and_tools(images, docker_run, pool):
    config = json.loads(
        run("docker", "image", "inspect", "-f", "{{json .Config}}", images[pool]).stdout
    )
    probe = docker_run(images[pool], "-c", PROBE, entrypoint="python")
    print(config["Entrypoint"], config.get("Cmd"), probe.stdout.strip())
    assert config["Entrypoint"] == ["python", "-m", "wdf_worker", "--pool", pool]
    assert config.get("Cmd") is None
    assert probe.returncode == 0, probe.stderr
    found = json.loads(probe.stdout)
    assert found["uid"] == 65532
    assert found["on_path"] == ON_PATH[pool]
    assert found["mtimes"] in ([], [1])
    assert found["etc_wdf"] is False


@pytest.mark.parametrize(
    ("pool", "tool"), [("simulate", "wasmtime"), ("compile", "wasm-tools"), ("compile", "uv")]
)
def test_image_tool_versions_match_dev_shell(images, docker_run, pool, tool):
    shell = run(tool, "--version").stdout.split()[:2]
    image = docker_run(images[pool], "--version", entrypoint=tool)
    print(f"{pool}: {image.stdout.strip()}, dev shell: {' '.join(shell)}")
    assert image.returncode == 0, image.stderr
    assert image.stdout.split()[:2] == shell


def test_image_compile_builds_workload_bundle_read_only(t1, images, docker_run, tmp_path):
    bundle.write(tmp_path).chmod(0o644)
    tmp_path.chmod(0o755)
    label = f"{RUN_LABEL}={t1.run_id}"
    volume = run("docker", "volume", "create", "--label", label).stdout.strip()
    dirs = " ".join([*SCRATCH_ENV.values(), "/var/wdf/task/links"])
    build = (
        f"mkdir -p {dirs} && python -m wdf_compile build --bundle /in/{bundle.NAME} "
        "--entry wdf_workloads.main --out /var/wdf/task/out --find-links /var/wdf/task/links"
    )
    options = ["--read-only", "--user", "65532", "--network", "none"]
    options += ["--memory", "1g", "--memory-swap", "1g", "--cpus", "1"]
    options += ["--mount", f"type=volume,src={volume},dst=/var/wdf"]
    options += ["--mount", f"type=bind,src={tmp_path},dst=/in,readonly"]
    options += [arg for name, value in SCRATCH_ENV.items() for arg in ("-e", f"{name}={value}")]
    try:
        built = docker_run(images["compile"], "-c", build, entrypoint="sh", options=options)
    finally:
        run("docker", "volume", "rm", "-f", volume, check=False)
    print(f"exit {built.returncode}: {built.stdout.strip()} {built.stderr.strip()}")
    assert built.returncode == 0, built.stderr
    assert built.stdout.startswith("cache_key: ")


def test_image_sh_refuses_changed_hash(tmp_path):
    for f in ("deploy/workers/image.sh", "nix/wasm-tools.nix"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, tmp_path / f)
    pins = tmp_path / "nix/wasm-tools.nix"
    text = pins.read_text()
    sri = text.split('pname = "wasmtime"')[1].split('hash = "')[1].split('"')[0]
    other = base64.b64encode(hashlib.sha256(b"another asset").digest()).decode()
    pins.write_text(text.replace(sri, f"sha256-{other}"))
    ref = "wdf-test-refused-worker:local"
    refused = run(str(tmp_path / "deploy/workers/image.sh"), "simulate", ref, check=False)
    print(f"exit {refused.returncode}: {refused.stderr.strip()}")
    assert refused.returncode == 1
    assert "wasmtime: the SHA-256 of" in refused.stderr
    assert run("docker", "image", "inspect", ref, check=False).returncode != 0
