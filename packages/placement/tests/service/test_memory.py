"""The service's peak resident memory over a run of placements and snapshot reads stays under the
budget that leaves a margin below its 128 MiB limit."""

import itertools
import re
from pathlib import Path

from wft_workflow.placement import PlacementRequest, ServicePlacer, task_pools

CALLS = 1_000
READS = 3
PEAK_MIB = 112


def node_reads(api) -> int:
    return sum(path.startswith("/api/v1/nodes?") for path, _ in api.requests)


def test_memory_peak(ci_api, spawn, until):
    url, process, _ = spawn(every_s=0.5)
    placer = ServicePlacer(url)
    # Every task type, placed and refused, so each answer path runs.
    requests = itertools.cycle(
        PlacementRequest(task_type=t, cpu_milli=1, memory_mib=m, run_timeout_s=600)
        for t in [*task_pools(), "no-such-type"]
        for m in (1, 1 << 20)
    )
    for request in itertools.islice(requests, CALLS):
        placer.place(request)
    until(lambda: node_reads(ci_api) >= READS)
    status = Path(f"/proc/{process.pid}/status").read_text()
    peak_kib = int(re.search(r"^VmHWM:\s+(\d+) kB$", status, re.MULTILINE).group(1))
    print(
        f"VmHWM {peak_kib} kB ({peak_kib / 1024:.1f} MiB) after {CALLS} calls and "
        f"{node_reads(ci_api)} snapshot reads"
    )
    assert peak_kib <= PEAK_MIB * 1024
