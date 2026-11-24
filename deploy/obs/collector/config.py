"""The manifests of the collector module: one OpenTelemetry Collector for logs, events and traces.

The Python services push logs and spans to it by OTLP, and it watches the Kubernetes events of
every namespace itself. Logs go to Loki's OTLP endpoint with no processing that adds labels, so
what Loki indexes is its own default set of resource attributes and the correlation fields stay
structured metadata. Traces go to Tempo's OTLP HTTP receiver and are dropped, not queued, while
Tempo is absent, so a missing backend never grows the collector's memory.
Usage: config.py render | collector-config
`render` prints the objects of the module as a JSON list and `collector-config` the collector.yaml.
Exit 0 done, 2 usage.
"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

NAME = "collector"
OTLP_HTTP_PORT = 4318
OTLP_GRPC_PORT = 4317
METRICS_PORT = 8888
HEALTH_PORT = 13133
LOKI_ENDPOINT = "http://loki.dsy-obs:3100/otlp"
TEMPO_ENDPOINT = "http://tempo.dsy-obs:4318"
# The service name of the events, so a query by `service_name` finds them beside the services.
EVENTS_SERVICE = "kubernetes-events"
# The uid of the user the image runs as.
COLLECTOR_USER = 10001

# Stays under the container limit of module.yaml, which S13 measured the collector against.
MEMORY_LIMIT_MIB = 100
MEMORY_SPIKE_MIB = 20


def collector_config() -> dict:
    return {
        "extensions": {"health_check": {"endpoint": f"0.0.0.0:{HEALTH_PORT}"}},
        "receivers": {
            "otlp": {
                "protocols": {
                    "grpc": {"endpoint": f"0.0.0.0:{OTLP_GRPC_PORT}"},
                    "http": {"endpoint": f"0.0.0.0:{OTLP_HTTP_PORT}"},
                }
            },
            "k8s_objects": {
                "objects": [{"name": "events", "mode": "watch", "group": "events.k8s.io"}]
            },
        },
        "processors": {
            "memory_limiter": {
                "check_interval": "1s",
                "limit_mib": MEMORY_LIMIT_MIB,
                "spike_limit_mib": MEMORY_SPIKE_MIB,
            },
            "batch": {},
            "resource/events": {
                "attributes": [{"key": "service.name", "value": EVENTS_SERVICE, "action": "upsert"}]
            },
        },
        "exporters": {
            "otlp_http/loki": {"endpoint": LOKI_ENDPOINT},
            "otlp_http/tempo": {
                "endpoint": TEMPO_ENDPOINT,
                # A failed batch is dropped at once instead of waiting for a backend that is absent.
                "retry_on_failure": {"enabled": False},
                "sending_queue": {"enabled": False},
                "tls": {"insecure": True},
            },
        },
        "service": {
            "extensions": ["health_check"],
            "telemetry": {
                "metrics": {
                    "readers": [
                        {
                            "pull": {
                                "exporter": {
                                    "prometheus": {"host": "0.0.0.0", "port": METRICS_PORT}
                                }
                            }
                        }
                    ]
                }
            },
            "pipelines": {
                "logs/otlp": {
                    "receivers": ["otlp"],
                    "processors": ["memory_limiter", "batch"],
                    "exporters": ["otlp_http/loki"],
                },
                "logs/events": {
                    "receivers": ["k8s_objects"],
                    "processors": ["memory_limiter", "resource/events", "batch"],
                    "exporters": ["otlp_http/loki"],
                },
                "traces": {
                    "receivers": ["otlp"],
                    "processors": ["memory_limiter", "batch"],
                    "exporters": ["otlp_http/tempo"],
                },
            },
        },
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
    config = yaml.dump(collector_config(), sort_keys=False)
    meta = {"name": NAME, "namespace": namespace}
    labels = {"app": NAME, "app.kubernetes.io/name": NAME}
    cluster = "dsy-" + NAME
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": cluster},
            "rules": [
                {
                    "apiGroups": ["events.k8s.io"],
                    "resources": ["events"],
                    "verbs": ["list", "watch"],
                }
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
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-config", "namespace": namespace},
            "data": {"collector.yaml": config},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta,
            "spec": {
                "replicas": 1,
                # Every obs Deployment is Recreate, so a rollout needs no surge pod on the node.
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": NAME}},
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {"checksum/config": _checksum(config)},
                    },
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
                            "runAsUser": COLLECTOR_USER,
                            "runAsGroup": COLLECTOR_USER,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "args": ["--config=/etc/otelcol/collector.yaml"],
                                "ports": [
                                    {"name": "otlp-http", "containerPort": OTLP_HTTP_PORT},
                                    {"name": "otlp-grpc", "containerPort": OTLP_GRPC_PORT},
                                    # Named `metrics` so the pod scrape finds the collector itself.
                                    {"name": "metrics", "containerPort": METRICS_PORT},
                                    {"name": "health", "containerPort": HEALTH_PORT},
                                ],
                                "readinessProbe": {
                                    "httpGet": {"path": "/", "port": "health"},
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/", "port": "health"},
                                    "periodSeconds": 15,
                                    "failureThreshold": 6,
                                },
                                "resources": module["resources"][NAME],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [{"name": "config", "mountPath": "/etc/otelcol"}],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": f"{NAME}-config"}}],
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
                    {"name": "otlp-http", "port": OTLP_HTTP_PORT, "targetPort": "otlp-http"},
                    {"name": "otlp-grpc", "port": OTLP_GRPC_PORT, "targetPort": "otlp-grpc"},
                ],
            },
        },
    ]


def main(argv: list[str]) -> int:
    if argv == ["render"]:
        print(json.dumps(render()))
    elif argv == ["collector-config"]:
        print(yaml.dump(collector_config(), sort_keys=False), end="")
    else:
        print("usage: config.py render | collector-config", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
