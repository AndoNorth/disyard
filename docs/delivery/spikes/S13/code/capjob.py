# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Submits one job, waits for it and prints what it produced.

`cap` (the default): the simulate task runs io_churn with a row count chosen so results.xml is the largest document
that fits the 64 MiB staging cap, the peak that WORKER_RESERVE_MIB is sized for.
`compile`: the smoke job over a source bundle that carries a fresh file, so its compile misses the cache and runs on
the compile pool.
Usage, from the root of the tree: uv run --frozen python docs/delivery/spikes/S13/code/capjob.py [cap|compile]
"""

import asyncio
import io
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

from wft_acceptance.clients import api
from wft_acceptance.smoke import CONTROL_API, DISPATCH, TENANT, fetched
from wft_kernel import rows, writer
from wft_workflow.harness import echo_params
from wft_workloads import bundle

CAP = 67108864
ROW_BYTES = 1024


class Counter(io.RawIOBase):
    def __init__(self) -> None:
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.size += len(data)
        return len(data)


def size_of(count: int) -> int:
    sink = Counter()
    writer.write(sink, 7, rows.blocks("io_churn", 7, {"rows": count, "row_bytes": ROW_BYTES}))
    return sink.size


def rows_at_cap() -> int:
    low, high = 1, CAP // (2 * ROW_BYTES)
    while low < high:
        mid = (low + high + 1) // 2
        low, high = (mid, high) if size_of(mid) <= CAP else (low, mid - 1)
    return low


async def until_ended(client: api.Api, job_id: str, timeout_s: float) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        status = client.status(job_id)
        if status["status"] not in ("accepted", "running", "queued"):
            return status
        await asyncio.sleep(2)
    raise TimeoutError(f"{job_id} did not end in {timeout_s} s")


def with_fresh_file(data: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data)) as old, tarfile.open(fileobj=out, mode="w") as new:
        for member in old.getmembers():
            new.addfile(member, old.extractfile(member) if member.isfile() else None)
        nonce = uuid.uuid4().hex.encode()
        info = tarfile.TarInfo("s13-nonce.txt")
        info.size = len(nonce)
        new.addfile(info, io.BytesIO(nonce))
    return out.getvalue()


def main(mode: str) -> int:
    client = api.Api(DISPATCH, CONTROL_API, TENANT, 15.0)
    with tempfile.TemporaryDirectory(prefix="wft-s13-") as work:
        data = bundle.write(Path(work)).read_bytes()
    if mode == "compile":
        data = with_fresh_file(data)
        params = echo_params()
    else:
        count = rows_at_cap()
        print(
            f"io_churn rows={count} row_bytes={ROW_BYTES} -> {size_of(count)} of {CAP} bytes",
            flush=True,
        )
        params = {
            "workload": "io_churn",
            "args": {"rows": count, "row_bytes": ROW_BYTES, "heartbeat_every": 1000},
            "seed": 7,
        }
    source = client.upload_source(data)
    job_id = client.submit_one(api.template(source, params), api.batch_key("s13cap"))
    print("accepted", job_id, flush=True)
    ended = asyncio.run(until_ended(client, job_id, 900))
    print("status", ended["status"], flush=True)
    if ended["status"] != "succeeded":
        print(ended, file=sys.stderr)
        return 1
    data = fetched(ended, "sim")
    print(f"simulate result {len(data)} bytes", flush=True)
    return 0


sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "cap"))
