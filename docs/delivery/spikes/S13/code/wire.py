# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Turns the throwaway wiring on for every service and pool worker Deployment, and opens the pool egress to dsy-obs.

Each Deployment gains an init container that copies the payload image onto an emptyDir, the volume mounted at /wiring,
PYTHONPATH, the collector and Pyroscope addresses of deploy/tilt/obs.star, and the component name.
Usage: wire.py
"""

import json
import subprocess

CONTEXT = "kind-dsy"
TARGETS = [
    # namespace, deployment, container, component, pool
    ("dsy-control", "admission", "admission", "admission", ""),
    ("dsy-control", "dispatch", "dispatch", "dispatch", ""),
    ("dsy-control", "placement", "placement", "placement", ""),
    ("dsy-workers", "compile-worker", "compile-worker", "compile-worker", "compile"),
    ("dsy-workers", "simulate-worker", "simulate-worker", "simulate-worker", "simulate"),
    ("dsy-workers", "aggregate-worker", "aggregate-worker", "aggregate-worker", "aggregate"),
]


def kubectl(*args: str, data: str | None = None) -> str:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, *args],
        check=True,
        capture_output=True,
        text=True,
        input=data,
    ).stdout


def workflow_target() -> tuple[str, str, str, str, str]:
    names = kubectl("-n", "dsy-control", "get", "deploy", "-o", "name").split()
    name = next(n.split("/")[1] for n in names if n.startswith("deployment.apps/workflow-worker"))
    return ("dsy-control", name, "workflow-worker", "workflow", "")


def patch(namespace: str, name: str, container: str, component: str, pool: str) -> None:
    env = [
        {"name": "PYTHONPATH", "value": "/wiring"},
        {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://collector.dsy-obs:4318"},
        {"name": "DSY_PYROSCOPE_ADDRESS", "value": "http://pyroscope.dsy-obs:4040"},
        {"name": "S13_COMPONENT", "value": component},
        {"name": "S13_POOL", "value": pool},
    ]
    body = {
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [
                        {
                            "name": "s13-wiring",
                            "image": "localhost:5001/s13-wiring:1",
                            "command": ["cp", "-r", "/payload/.", "/wiring/"],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "250m", "memory": "64Mi"},
                            },
                            "securityContext": {
                                "runAsUser": 65532,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "volumeMounts": [{"name": "s13-wiring", "mountPath": "/wiring"}],
                        }
                    ],
                    "containers": [
                        {
                            "name": container,
                            "env": env,
                            "volumeMounts": [
                                {"name": "s13-wiring", "mountPath": "/wiring", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [{"name": "s13-wiring", "emptyDir": {"sizeLimit": "256Mi"}}],
                }
            }
        }
    }
    kubectl(
        "-n", namespace, "patch", "deployment", name, "--type", "strategic", "-p", json.dumps(body)
    )
    print(f"wired {namespace}/{name}")


policy = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "s13-obs-egress", "namespace": "dsy-workers"},
    "spec": {
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "dsy-obs"}
                        },
                        "podSelector": {"matchLabels": {"app": app}},
                    }
                ],
                "ports": [{"protocol": "TCP", "port": port}],
            }
            for app, port in (("collector", 4318), ("pyroscope", 4040))
        ],
    },
}
kubectl("apply", "-f", "-", data=json.dumps(policy))
for target in [*TARGETS, workflow_target()]:
    patch(*target)
