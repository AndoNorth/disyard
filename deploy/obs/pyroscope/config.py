"""The manifests of the pyroscope module: Pyroscope in monolithic mode on a local volume.

The Python services push profiles to port 4040, and Pyroscope serves the same port to Grafana and
to `dsy_obs.query`, and its own series to Prometheus.
Usage: config.py render | args
`render` prints the objects of the module as a JSON list and `args` the container arguments.
Exit 0 done, 2 usage.
"""

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

NAME = "pyroscope"
PORT = 4040
DATA = "/data"
# The uid of the user the image runs as.
PYROSCOPE_USER = 10001

# Retention of decision 20, and the volume that S13 measured for it. Pyroscope has no size key, so
# the volume alarm is the only brake on a profile flood.
RETENTION = "72h"
VOLUME_SIZE = "2Gi"


def pyroscope_args() -> list[str]:
    return [
        "-target=all",
        f"-retention-period={RETENTION}",
        f"-pyroscopedb.data-path={DATA}",
        f"-storage.filesystem.dir={DATA}/v2/shared",
    ]


def _module() -> dict:
    return yaml.safe_load((HERE / "module.yaml").read_text())


def _image(module: dict) -> str:
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    return f"docker.io/{module['artifact']['pin']}@{pin[module['artifact']['pin']]['digest']}"


def render() -> list[dict]:
    module = _module()
    namespace = module["namespace"]
    topology = yaml.safe_load((ROOT / "contracts/topology/topology.yaml").read_text())
    meta = {"name": NAME, "namespace": namespace}
    labels = {"app": NAME, "app.kubernetes.io/name": NAME}
    return [
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{NAME}-data", "namespace": namespace},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": VOLUME_SIZE}},
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta,
            "spec": {
                "replicas": 1,
                # The volume takes one writer at a time, and a surge pod would not fit the node.
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": NAME}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
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
                            "runAsUser": PYROSCOPE_USER,
                            "runAsGroup": PYROSCOPE_USER,
                            "fsGroup": PYROSCOPE_USER,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "args": pyroscope_args(),
                                # Named `metrics` so the pod scrape finds Pyroscope's own series.
                                "ports": [{"name": "metrics", "containerPort": PORT}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/ready", "port": "metrics"},
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/ready", "port": "metrics"},
                                    "periodSeconds": 15,
                                    "failureThreshold": 6,
                                },
                                "resources": module["resources"][NAME],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {"name": "data", "mountPath": DATA},
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": f"{NAME}-data"},
                            },
                            {"name": "tmp", "emptyDir": {}},
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
    if argv == ["render"]:
        print(json.dumps(render()))
    elif argv == ["args"]:
        print("\n".join(pyroscope_args()))
    else:
        print("usage: config.py render | args", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
