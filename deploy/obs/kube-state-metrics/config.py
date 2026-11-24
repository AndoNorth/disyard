"""The manifests of the kube-state-metrics module: pod, deployment, node and claim state as series.

Prometheus finds the pod through its container port named `metrics`, so the module needs no scrape
configuration of its own.
Usage: config.py render
`render` prints the objects of the module as a JSON list.
Exit 0 done, 2 usage.
"""

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

NAME = "kube-state-metrics"
PORT = 8080
TELEMETRY_PORT = 8081
# The state the alerts and dashboards read, and nothing more, so the process stays inside its limit.
RESOURCES = {
    "": ["nodes", "pods", "persistentvolumeclaims"],
    "apps": ["deployments", "statefulsets"],
}
# The uid of `nobody`, which the image runs as.
NOBODY = 65534


def _module() -> dict:
    return yaml.safe_load((HERE / "module.yaml").read_text())


def _image(module: dict) -> str:
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    return f"registry.k8s.io/{module['artifact']['pin']}@{pin[module['artifact']['pin']]['digest']}"


def render() -> list[dict]:
    module = _module()
    namespace = module["namespace"]
    topology = yaml.safe_load((ROOT / "contracts/topology/topology.yaml").read_text())
    meta = {"name": NAME, "namespace": namespace}
    cluster = "dsy-" + NAME
    served = sorted(r for group in RESOURCES.values() for r in group)
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": cluster},
            "rules": [
                {"apiGroups": [group], "resources": names, "verbs": ["list", "watch"]}
                for group, names in RESOURCES.items()
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": cluster},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": cluster,
            },
            "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": namespace}],
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta,
            "spec": {
                "replicas": 1,
                # One replica, and a second one beside it would count every object twice.
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": NAME}},
                "template": {
                    "metadata": {"labels": {"app": NAME, "app.kubernetes.io/name": NAME}},
                    "spec": {
                        "serviceAccountName": NAME,
                        "nodeSelector": {topology["pool_label"]: "obs"},
                        "tolerations": [
                            {
                                "key": topology["pool_label"],
                                "operator": "Equal",
                                "value": "obs",
                                "effect": topology["taint_effect"],
                            }
                        ],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": NOBODY,
                            "runAsGroup": NOBODY,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "args": [
                                    f"--port={PORT}",
                                    f"--telemetry-port={TELEMETRY_PORT}",
                                    f"--resources={','.join(served)}",
                                ],
                                "ports": [
                                    {"name": "metrics", "containerPort": PORT},
                                    {"name": "telemetry", "containerPort": TELEMETRY_PORT},
                                ],
                                "readinessProbe": {
                                    "httpGet": {"path": "/readyz", "port": "telemetry"},
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/livez", "port": "metrics"},
                                    "periodSeconds": 15,
                                    "failureThreshold": 6,
                                },
                                "resources": module["resources"][NAME],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": meta,
            "spec": {
                "selector": {"app": NAME},
                "ports": [{"name": "http", "port": PORT, "targetPort": "metrics"}],
            },
        },
    ]


def main(argv: list[str]) -> int:
    if argv != ["render"]:
        print("usage: config.py render", file=sys.stderr)
        return 2
    print(json.dumps(render()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
