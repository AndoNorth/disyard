"""The manifests of the loki module: Loki in monolithic mode on a local volume.

Logs and Kubernetes events arrive from the collector by OTLP, and Loki keeps the OTLP resource
attributes it indexes by default as labels and every other attribute, the correlation fields of
the log contract among them, as structured metadata.
Usage: config.py render | loki-config
`render` prints the objects of the module as a JSON list and `loki-config` the loki.yaml.
Exit 0 done, 2 usage.
"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

NAME = "loki"
PORT = 3100
GRPC_PORT = 9096
DATA = "/loki"
# The uid of the user the image runs as.
LOKI_USER = 10001

# Retention of decision 20, and the volume that S13 measured for it. Loki has no size key, so the
# volume alarm is the only brake on a log flood.
RETENTION = "240h"
VOLUME_SIZE = "2Gi"


def loki_config() -> dict:
    return {
        "auth_enabled": False,
        "server": {"http_listen_port": PORT, "grpc_listen_port": GRPC_PORT},
        "common": {
            "instance_addr": "127.0.0.1",
            "path_prefix": DATA,
            "storage": {
                "filesystem": {
                    "chunks_directory": f"{DATA}/chunks",
                    "rules_directory": f"{DATA}/rules",
                }
            },
            "replication_factor": 1,
            "ring": {"kvstore": {"store": "inmemory"}},
        },
        "schema_config": {
            "configs": [
                {
                    "from": "2024-04-01",
                    "store": "tsdb",
                    "object_store": "filesystem",
                    "schema": "v13",
                    "index": {"prefix": "index_", "period": "24h"},
                }
            ]
        },
        "limits_config": {
            "retention_period": RETENTION,
            # Structured metadata is how job_id and the other correlation fields stay out of the index.
            "allow_structured_metadata": True,
            "volume_enabled": True,
        },
        "compactor": {
            "working_directory": f"{DATA}/compactor",
            "retention_enabled": True,
            "delete_request_store": "filesystem",
        },
        "analytics": {"reporting_enabled": False},
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
    config = yaml.dump(loki_config(), sort_keys=False)
    meta = {"name": NAME, "namespace": namespace}
    labels = {"app": NAME, "app.kubernetes.io/name": NAME}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-config", "namespace": namespace},
            "data": {"loki.yaml": config},
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
                            "runAsUser": LOKI_USER,
                            "runAsGroup": LOKI_USER,
                            "fsGroup": LOKI_USER,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "args": ["-config.file=/etc/loki/loki.yaml"],
                                # Named `metrics` so the pod scrape finds Loki's own series.
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
                                    {"name": "config", "mountPath": "/etc/loki"},
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
                "ports": [{"name": "http", "port": PORT, "targetPort": "metrics"}],
            },
        },
    ]


def main(argv: list[str]) -> int:
    if argv == ["render"]:
        print(json.dumps(render()))
    elif argv == ["loki-config"]:
        print(yaml.dump(loki_config(), sort_keys=False), end="")
    else:
        print("usage: config.py render | loki-config", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
