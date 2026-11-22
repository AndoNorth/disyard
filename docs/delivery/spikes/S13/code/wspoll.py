# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Polls the working set of every service and worker container once a second from its node's cgroups.

The working set is memory.current less inactive_file, the figure cAdvisor reports, and a one second poll sees a peak
that cAdvisor's 30 second scrape misses.
  wspoll.py run OUT SECONDS  polls for SECONDS, appending `epoch pod/container current inactive` lines to OUT
  wspoll.py max OUT      prints the highest working set and the highest usage per container in OUT, in MiB
"""

import json
import subprocess
import sys

NAMESPACES = {"wft-control", "wft-workers"}


def kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", "--context", "kind-wft", *args], check=True, capture_output=True, text=True
    ).stdout


def run(out: str, seconds: int) -> None:
    pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
    by_node: dict[str, dict[str, str]] = {}
    for pod in pods:
        if pod["metadata"]["namespace"] not in NAMESPACES or not pod["spec"].get("nodeName"):
            continue
        for status in pod["status"].get("containerStatuses", []):
            if "containerID" in status and "running" in status["state"]:
                name = f"{pod['metadata']['name']}/{status['name']}"
                by_node.setdefault(pod["spec"]["nodeName"], {})[
                    status["containerID"].split("//")[1]
                ] = name
    procs = []
    sink = open(out, "a")
    for node, ids in by_node.items():
        listing = subprocess.run(
            [
                "docker",
                "exec",
                node,
                "find",
                "/sys/fs/cgroup",
                "-type",
                "d",
                "-name",
                "cri-containerd-*.scope",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        paths = {
            p: ids[p.rsplit("cri-containerd-", 1)[1][:-6]]
            for p in listing
            if p.rsplit("cri-containerd-", 1)[1][:-6] in ids
        }
        script = (
            f"end=$(( $(date +%s) + {seconds} )); while [ $(date +%s) -lt $end ]; do "
            + "; ".join(
                f"echo $(date +%s) {name} $(cat {p}/memory.current) $(sed -n 's/^inactive_file //p' {p}/memory.stat)"
                for p, name in paths.items()
            )
            + "; sleep 1; done"
        )
        procs.append(
            subprocess.Popen(
                ["docker", "exec", node, "sh", "-c", script], stdout=sink, stderr=subprocess.DEVNULL
            )
        )
    for proc in procs:
        proc.wait()


def peaks(out: str) -> None:
    best: dict[str, list[int]] = {}
    for line in open(out):
        parts = line.split()
        if len(parts) != 4:
            continue
        _, name, current, inactive = parts
        working = int(current) - int(inactive)
        entry = best.setdefault(name, [0, 0])
        entry[0], entry[1] = max(entry[0], working), max(entry[1], int(current))
    for name, (working, current) in sorted(best.items()):
        print(f"{name}: working set {working / 2**20:.0f} MiB, usage {current / 2**20:.0f} MiB")


if sys.argv[1] == "run":
    run(sys.argv[2], int(sys.argv[3]))
else:
    peaks(sys.argv[2])
