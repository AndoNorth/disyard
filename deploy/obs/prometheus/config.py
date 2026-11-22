"""The manifests of the prometheus module: Prometheus with its scrape configuration and alert rules.

Prometheus discovers its targets through the Kubernetes API, so no module names itself in the
configuration: a pod with a container port named `metrics` or `prometheus`, or the scrape
annotation, is scraped by the `kubernetes-pods` job. The alert rules are the files of `rules/`,
mounted from a ConfigMap, and the checksum of the configuration and of the rules on the pod template
rolls the pod when either changes, because Prometheus reads both only at start.
Usage: config.py render | scrape-config
`render` prints the objects of the module as a JSON list and `scrape-config` the prometheus.yml.
Exit 0 done, 2 usage.
"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RULES = HERE / "rules"

TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
KUBELET_PORT = 10250
SCRAPE_INTERVAL = "30s"

# Retention of decision 20 and the size cap and volume that S13 measured for it.
RETENTION_TIME = "10d"
RETENTION_SIZE = "4GB"
VOLUME_SIZE = "5Gi"

NAME = "prometheus"
PORT = 9090
# The uid of `nobody`, the user of the image.
NOBODY = 65534

# The detailed metric families that the queue alerts read.
RABBITMQ_PORT = 15692
RABBITMQ_FAMILIES = ["queue_coarse_metrics", "queue_consumer_count"]


def _node_target(path: str | None = None) -> dict:
    """A scrape job of every node's kubelet, with this pod's service account token."""
    job = {
        "scheme": "https",
        "authorization": {"credentials_file": TOKEN},
        # The kubelet serves a certificate that the node signed for itself.
        "tls_config": {"insecure_skip_verify": True},
        "kubernetes_sd_configs": [{"role": "node"}],
        "relabel_configs": [
            {
                "source_labels": ["__meta_kubernetes_node_address_InternalIP"],
                "regex": "(.+)",
                "target_label": "__address__",
                "replacement": f"$1:{KUBELET_PORT}",
            },
            {"source_labels": ["__meta_kubernetes_node_name"], "target_label": "node"},
        ],
    }
    if path:
        job["metrics_path"] = path
    return job


def scrape_config() -> dict:
    return {
        "global": {"scrape_interval": SCRAPE_INTERVAL, "evaluation_interval": SCRAPE_INTERVAL},
        "rule_files": ["rules/*.yaml"],
        "scrape_configs": [
            {
                "job_name": "kubernetes-pods",
                # A series keeps the namespace and pod it names itself, which kube-state-metrics
                # series do; the target's own labels fill in the rest.
                "honor_labels": True,
                "kubernetes_sd_configs": [{"role": "pod"}],
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_pod_phase"],
                        "regex": "Pending|Succeeded|Failed|Unknown",
                        "action": "drop",
                    },
                    {
                        "source_labels": [
                            "__meta_kubernetes_pod_annotation_prometheus_io_scrape",
                            "__meta_kubernetes_pod_container_port_name",
                        ],
                        "regex": "true;.*|.*;(metrics|prometheus)",
                        "action": "keep",
                    },
                    {
                        "source_labels": [
                            "__meta_kubernetes_pod_annotation_prometheus_io_scrape",
                            "__meta_kubernetes_pod_annotation_prometheus_io_port",
                            "__meta_kubernetes_pod_ip",
                        ],
                        "regex": r"true;(\d+);(.+)",
                        "target_label": "__address__",
                        "replacement": "$2:$1",
                    },
                    {
                        "source_labels": ["__meta_kubernetes_namespace"],
                        "target_label": "namespace",
                    },
                    {"source_labels": ["__meta_kubernetes_pod_name"], "target_label": "pod"},
                ],
            },
            {
                "job_name": "rabbitmq-detailed",
                "metrics_path": "/metrics/detailed",
                "params": {"family": RABBITMQ_FAMILIES},
                "kubernetes_sd_configs": [{"role": "pod", "namespaces": {"names": ["wft-infra"]}}],
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_pod_container_port_number"],
                        "regex": str(RABBITMQ_PORT),
                        "action": "keep",
                    },
                    {
                        "source_labels": ["__meta_kubernetes_namespace"],
                        "target_label": "namespace",
                    },
                    {"source_labels": ["__meta_kubernetes_pod_name"], "target_label": "pod"},
                ],
            },
            {"job_name": "kubelet", **_node_target()},
            {"job_name": "cadvisor", **_node_target("/metrics/cadvisor")},
        ],
    }


def _rules() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(RULES.glob("*.yaml"))}


def _checksum(*texts: str) -> str:
    return hashlib.sha256("\0".join(texts).encode()).hexdigest()


def _module() -> dict:
    return yaml.safe_load((HERE / "module.yaml").read_text())


def _image(module: dict) -> str:
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    return f"docker.io/{module['artifact']['pin']}@{pin[module['artifact']['pin']]['digest']}"


def render() -> list[dict]:
    module = _module()
    namespace = module["namespace"]
    topology = yaml.safe_load((ROOT / "contracts/topology/topology.yaml").read_text())
    config = yaml.dump(scrape_config(), sort_keys=False)
    rules = _rules()
    meta = {"name": NAME, "namespace": namespace}
    labels = {"app": NAME, "app.kubernetes.io/name": NAME}
    cluster = "wft-" + NAME
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": cluster},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes", "pods"], "verbs": ["list", "watch"]},
                {"apiGroups": [""], "resources": ["nodes/metrics"], "verbs": ["get"]},
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
            "data": {"prometheus.yml": config},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-rules", "namespace": namespace},
            "data": rules,
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
                        "annotations": {
                            "checksum/config": _checksum(config),
                            "checksum/rules": _checksum(*rules.values()),
                        },
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
                            "runAsUser": NOBODY,
                            "runAsGroup": NOBODY,
                            "fsGroup": NOBODY,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "args": [
                                    "--config.file=/etc/prometheus/prometheus.yml",
                                    "--storage.tsdb.path=/prometheus",
                                    f"--storage.tsdb.retention.time={RETENTION_TIME}",
                                    f"--storage.tsdb.retention.size={RETENTION_SIZE}",
                                    "--web.enable-otlp-receiver",
                                    f"--web.listen-address=:{PORT}",
                                ],
                                # Named `metrics` so the pod scrape finds Prometheus itself.
                                "ports": [{"name": "metrics", "containerPort": PORT}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/-/ready", "port": "metrics"},
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/-/healthy", "port": "metrics"},
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
                                    {"name": "config", "mountPath": "/etc/prometheus"},
                                    {"name": "rules", "mountPath": "/etc/prometheus/rules"},
                                    {"name": "data", "mountPath": "/prometheus"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": f"{NAME}-config"}},
                            {"name": "rules", "configMap": {"name": f"{NAME}-rules"}},
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": f"{NAME}-data"},
                            },
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
    elif argv == ["scrape-config"]:
        print(yaml.dump(scrape_config(), sort_keys=False), end="")
    else:
        print("usage: config.py render | scrape-config", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
