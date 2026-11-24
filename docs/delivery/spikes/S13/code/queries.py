# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Prints the Prometheus view of a window: peak working set, CPU throttled share and mean CPU per container, and the
head series of Prometheus itself, over the last SECONDS seconds, through the forward on 127.0.0.1:19090.

Usage: queries.py SECONDS [namespace ...]
"""

import json
import sys
import urllib.parse
import urllib.request

ADDRESS = "http://127.0.0.1:19090"


def query(expr: str) -> list[dict]:
    url = f"{ADDRESS}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    return json.load(urllib.request.urlopen(url, timeout=60))["data"]["result"]


def by_container(expr: str) -> dict[tuple[str, str, str], float]:
    return {
        (
            r["metric"].get("namespace", ""),
            r["metric"].get("pod", ""),
            r["metric"].get("container", ""),
        ): float(r["value"][1])
        for r in query(expr)
    }


seconds = int(sys.argv[1])
window = f"{seconds}s"
namespaces = "|".join(sys.argv[2:]) or "dsy-obs|dsy-control|dsy-workers|dsy-infra"
selector = f'{{namespace=~"{namespaces}",container!="",container!="POD"}}'
peak = by_container(f"max_over_time(container_memory_working_set_bytes{selector}[{window}])")
throttled = by_container(f"increase(container_cpu_cfs_throttled_periods_total{selector}[{window}])")
periods = by_container(f"increase(container_cpu_cfs_periods_total{selector}[{window}])")
cpu = by_container(f"rate(container_cpu_usage_seconds_total{selector}[{window}])")
limit = by_container(f"container_spec_memory_limit_bytes{selector}")
cpu_limit = by_container(
    f"container_spec_cpu_quota{selector} / container_spec_cpu_period{selector}"
)
print(f"window {window}")
print(
    "namespace pod/container | peak working set MiB | of limit | throttled periods | mean cpu | cpu limit"
)
for key in sorted(peak):
    share = 100 * throttled.get(key, 0) / periods[key] if periods.get(key) else 0.0
    lim = limit.get(key)
    pct = f"{100 * peak[key] / lim:.0f}%" if lim else "-"
    print(
        f"{key[0]} {key[1]}/{key[2]} | {peak[key] / 2**20:.0f} | {pct} | {share:.0f}% | "
        f"{1000 * cpu.get(key, 0):.0f}m | {1000 * cpu_limit.get(key, 0):.0f}m"
    )
for name in (
    "prometheus_tsdb_head_series",
    "prometheus_tsdb_head_chunks",
    "prometheus_tsdb_storage_blocks_bytes",
):
    for r in query(name):
        print(name, r["value"][1])
print(
    "ingest samples/s",
    [r["value"][1] for r in query(f"rate(prometheus_tsdb_head_samples_appended_total[{window}])")],
)
