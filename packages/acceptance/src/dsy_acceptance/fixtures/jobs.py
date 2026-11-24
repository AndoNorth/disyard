"""Job templates of the e2e suite and the values a host computes for them."""

from dsy_kernel.summary import expected_summary
from dsy_workflow.harness import REAL_ENTRY

from ..clients import api
from .bundles import MISSING_ENTRY


def chain(name: str, source: dict, simulate: dict, entry: str = REAL_ENTRY) -> dict:
    """The tasks `<name>-build` and `<name>-sim`: a compile of `source` at `entry`, and a simulate
    over its component that runs `simulate`."""
    one = api.template(source, simulate)["tasks"]
    one["build"]["params"]["entry"] = entry
    one["sim"]["inputs"] = [{"task": f"{name}-build", "output": "component"}]
    return {f"{name}-build": one["build"], f"{name}-sim": one["sim"]}


def two_chains(
    sources: tuple[dict, dict],
    simulates: tuple[dict, dict],
    entries: tuple[str, str] = (REAL_ENTRY, REAL_ENTRY),
    allow_partial: bool = False,
) -> dict:
    """Two compile then simulate chains, `a` and `b`, and one digest_list aggregate over both
    results. A chain whose entry is MISSING_ENTRY fails its compile."""
    tasks = {}
    for name, source, simulate, entry in zip("ab", sources, simulates, entries, strict=True):
        tasks.update(chain(name, source, simulate, entry))
    template = api.template(sources[0], simulates[0])
    agg = template["tasks"]["agg"]
    agg["inputs"] = [{"task": f"{name}-sim", "output": "result"} for name in "ab"]
    agg["allow_partial"] = allow_partial
    template["tasks"] = {**tasks, "agg": agg}
    return template


def failing_second_chain(
    sources: tuple[dict, dict], simulates: tuple[dict, dict], allow_partial: bool = False
) -> dict:
    """Chain `a` builds the workload library and chain `b` fails its compile."""
    return two_chains(sources, simulates, (REAL_ENTRY, MISSING_ENTRY), allow_partial)


def two_seed_summary(workload: str, args: dict, seeds: tuple[int, int]) -> dict:
    """The digest_list summary a job over `workload` at the two seeds must write, computed on the
    host: chain `a` runs seeds[0] and chain `b` seeds[1]."""
    inputs = {
        f"{name}-sim": {"workload": workload, "seed": s, "args": args}
        for name, s in zip("ab", seeds, strict=True)
    }
    return expected_summary("digest_list", {}, inputs)
