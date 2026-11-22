"""The manifests of the tempo module: Tempo in monolithic mode on a local volume.

Spans arrive from the collector by OTLP HTTP, and Tempo keeps them as blocks on the volume for the
retention of decision 20.
Usage: config.py render | tempo-config
`render` prints the objects of the module as a JSON list and `tempo-config` the tempo.yaml.
Exit 0 done, 2 usage.
"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

NAME = "tempo"
PORT = 3200
OTLP_HTTP_PORT = 4318
DATA = "/var/tempo"
# The uid of the user the image runs as.
TEMPO_USER = 10001

# Retention of decision 20, and the volume that S13 measured for it. Tempo has no size key, so the
# volume alarm is the only brake on a span flood.
RETENTION = "72h"
VOLUME_SIZE = "4Gi"


def tempo_config() -> dict:
    return {
        "server": {"http_listen_port": PORT},
        "distributor": {
            "receivers": {
                "otlp": {"protocols": {"http": {"endpoint": f"0.0.0.0:{OTLP_HTTP_PORT}"}}}
            }
        },
        "storage": {
            "trace": {
                "backend": "local",
                "local": {"path": f"{DATA}/blocks"},
                "wal": {"path": f"{DATA}/wal"},
            }
        },
        # The worker enforces time retention: a block outlives the scheduler's own key.
        "backend_worker": {"compaction": {"block_retention": RETENTION}},
        "usage_report": {"reporting_enabled": False},
    }


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _module() -> dict:
    return yaml.safe_load((HERE / "module.yaml").read_text())


def _image(module: dict) -> str:
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    return f"docker.io/{module['artifact']['pin']}@{pin[module['artifact']['pin']]['digest']}"


def render() -> list[dict]:
    module = _module()
    namespace = module["namespace"]
    topology = yaml.safe_load((ROOT / "contracts/topology/topology.yaml").read_text())
    config = yaml.dump(tempo_config(), sort_keys=False)
    meta = {"name": NAME, "namespace": namespace}
    labels = {"app": NAME, "app.kubernetes.io/name": NAME}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-config", "namespace": namespace},
            "data": {"tempo.yaml": config},
        },
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
                    "metadata": {
                        "labels": labels,
                        "annotations": {"checksum/config": _checksum(config)},
                    },
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
                            "runAsUser": TEMPO_USER,
                            "runAsGroup": TEMPO_USER,
                            "fsGroup": TEMPO_USER,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "args": ["-config.file=/etc/tempo/tempo.yaml"],
                                # Named `metrics` so the pod scrape finds Tempo's own series.
                                "ports": [
                                    {"name": "metrics", "containerPort": PORT},
                                    {"name": "otlp-http", "containerPort": OTLP_HTTP_PORT},
                                ],
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
                                    {"name": "config", "mountPath": "/etc/tempo"},
                                    {"name": "data", "mountPath": DATA},
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": f"{NAME}-config"}},
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
                "ports": [
                    {"name": "http", "port": PORT, "targetPort": "metrics"},
                    {"name": "otlp-http", "port": OTLP_HTTP_PORT, "targetPort": "otlp-http"},
                ],
            },
        },
    ]


def main(argv: list[str]) -> int:
    if argv == ["render"]:
        print(json.dumps(render()))
    elif argv == ["tempo-config"]:
        print(yaml.dump(tempo_config(), sort_keys=False), end="")
    else:
        print("usage: config.py render | tempo-config", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
