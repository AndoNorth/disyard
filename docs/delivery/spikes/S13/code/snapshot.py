# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Prints one JSON snapshot of every container's cgroup counters and every obs volume's bytes.

Each container's cgroup is read from its node with `docker exec`, because the obs images have no shell:
memory.peak (the highest usage since the container started), memory.current, memory.events (the oom counters)
and cpu.stat (nr_periods, nr_throttled), beside the container's memory limit from the pod spec.
The obs volumes are the local-path directories of the obs node, measured with `du -sb`.
Usage: snapshot.py LABEL >> file.jsonl
"""

import json
import subprocess
import sys
import time

NAMESPACES = {"dsy-obs", "dsy-control", "dsy-workers", "dsy-infra"}


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def node_exec(node: str, script: str) -> str:
    return run("docker", "exec", node, "sh", "-c", script)


def kv(text: str) -> dict[str, int]:
    return {k: int(v) for k, v in (line.split() for line in text.splitlines() if line.strip())}


def main(label: str) -> None:
    pods = json.loads(run("kubectl", "--context", "kind-dsy", "get", "pods", "-A", "-o", "json"))[
        "items"
    ]
    by_node: dict[str, list] = {}
    for pod in pods:
        if pod["metadata"]["namespace"] in NAMESPACES and pod["spec"].get("nodeName"):
            by_node.setdefault(pod["spec"]["nodeName"], []).append(pod)
    containers = []
    for node, node_pods in by_node.items():
        ids = {}
        for pod in node_pods:
            limits = {
                c["name"]: c.get("resources", {}).get("limits", {})
                for c in pod["spec"]["containers"]
            }
            for status in pod["status"].get("containerStatuses", []):
                if "containerID" in status and "running" in status["state"]:
                    ids[status["containerID"].split("//")[1]] = (
                        pod,
                        status,
                        limits[status["name"]],
                    )
        listing = node_exec(
            node, "find /sys/fs/cgroup -type d -name 'cri-containerd-*.scope'"
        ).split()
        for path in listing:
            cid = path.rsplit("cri-containerd-", 1)[1][: -len(".scope")]
            if cid not in ids:
                continue
            pod, status, limits = ids[cid]
            raw = node_exec(
                node,
                f"cd {path} && echo peak $(cat memory.peak) && echo current $(cat memory.current) && "
                "sed 's/^/ev_/' memory.events && cat cpu.stat",
            )
            counters = kv(raw)
            containers.append(
                {
                    "namespace": pod["metadata"]["namespace"],
                    "pod": pod["metadata"]["name"],
                    "container": status["name"],
                    "restarts": status["restartCount"],
                    "limit_mem": limits.get("memory"),
                    "limit_cpu": limits.get("cpu"),
                    **{
                        k: counters.get(k)
                        for k in (
                            "peak",
                            "current",
                            "ev_oom",
                            "ev_oom_kill",
                            "ev_max",
                            "nr_periods",
                            "nr_throttled",
                            "throttled_usec",
                            "usage_usec",
                        )
                    },
                }
            )
    volumes = {}
    for line in node_exec(
        "dsy-worker2", "du -sb /var/local-path-provisioner/pvc-* 2>/dev/null"
    ).splitlines():
        size, path = line.split()
        volumes[path.rsplit("_", 1)[-1]] = int(size)
    print(
        json.dumps(
            {"label": label, "at": time.time(), "containers": containers, "volumes": volumes}
        )
    )


main(sys.argv[1])
