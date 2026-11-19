"""`python -m wdf_placement.show [--url URL]`: the pool view of a placement service, one line per
pool, then the snapshot's age and the error of its last read."""

import argparse
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8080"


def lines(view: dict) -> list[str]:
    out = []
    for name, pool in view["pools"].items():
        slot, capacity = pool["slot"], pool["capacity"]
        if capacity:
            ready = capacity["ready_workers"]
            free = f"{capacity['free_cpu_milli']}m/{capacity['free_memory_mib']}Mi"
            images = ",".join(capacity["images"]) or "none"
        else:
            ready = free = images = "none"
        out.append(
            f"{name} queue={pool['queue']} slot={slot['cpu_milli']}m/{slot['memory_mib']}Mi"
            f" ready_workers={ready} free={free} images={images}"
        )
    snapshot = view["snapshot"]
    out.append(f"snapshot_age_s={'none' if snapshot is None else round(snapshot['age_s'])}")
    out.append(f"last_error={view['last_error'] or 'none'}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wdf_placement.show", description=__doc__)
    parser.add_argument("--url", default=URL, help=f"the placement service (default {URL})")
    args = parser.parse_args(argv)
    with urllib.request.urlopen(args.url.rstrip("/") + "/pools", timeout=10) as response:
        view = json.load(response)
    print("\n".join(lines(view)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
