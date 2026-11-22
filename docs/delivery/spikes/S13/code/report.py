# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Prints peak memory (MiB and percent of limit) and throttled share per container from a snapshots file.

Usage: report.py snapshots.jsonl [namespace ...]
"""

import json
import sys

UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}


def mib(quantity: str | None) -> float | None:
    if not quantity:
        return None
    for unit, size in UNITS.items():
        if quantity.endswith(unit):
            return float(quantity[: -len(unit)]) * size / 2**20
    return float(quantity) / 2**20


snapshots = [json.loads(line) for line in open(sys.argv[1])]
wanted = set(sys.argv[2:])
labels = [s["label"] for s in snapshots]
print("namespace container limit | " + " | ".join(labels))
rows = {}
for snap in snapshots:
    for c in snap["containers"]:
        if wanted and c["namespace"] not in wanted:
            continue
        key = (
            c["namespace"],
            c["pod"].rsplit("-", 2)[0] if c["namespace"] != "wft-infra" else c["pod"],
            c["container"],
        )
        rows.setdefault(key, {})[snap["label"]] = c
for key in sorted(rows):
    first = next(iter(rows[key].values()))
    limit = mib(first["limit_mem"])
    cells = []
    for label in labels:
        c = rows[key].get(label)
        if not c:
            cells.append("-")
            continue
        peak = c["peak"] / 2**20
        share = 100 * c["nr_throttled"] / c["nr_periods"] if c["nr_periods"] else 0
        cells.append(
            f"{peak:.0f}M {100 * peak / limit:.0f}% thr{share:.0f}% oom{c['ev_oom_kill']} r{c['restarts']}"
        )
    print(f"{key[0]} {key[1]}/{key[2]} {first['limit_mem']} | " + " | ".join(cells))
print("volumes:", {s["label"]: s["volumes"] for s in snapshots[-1:]})
