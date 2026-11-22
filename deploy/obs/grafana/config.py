"""The manifests of the grafana module: Grafana with its four datasources and the dashboards.

Anonymous visitors are Editors, which is the role that gives Explore, and the admin credential
comes from the Secret that secrets.sh renders. Grafana's database lives on a local volume, so
annotations survive a pod replacement until the cluster is destroyed.
Usage: config.py render | datasources
`render` prints the objects of the module as a JSON list and `datasources` the provisioning file.
Exit 0 done, 2 usage.
"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DASHBOARDS = HERE / "dashboards"

NAME = "grafana"
PORT = 3000
# The uid of the user the image runs as.
GRAFANA_USER = 472
DATA = "/var/lib/grafana"
DASHBOARDS_PATH = "/var/lib/grafana-dashboards"
VOLUME_SIZE = "1Gi"
# A ConfigMap holds at most 1 MiB, and the dashboards share one.
CONFIGMAP_LIMIT = 1024 * 1024
# The workflow id of a job is wft_keys.workflow_id(job_id).
WORKFLOW_ID_PREFIX = "job-"


def _ports() -> dict[str, dict]:
    rows = yaml.safe_load((ROOT / "deploy/host/ports.yaml").read_text())["ports"]
    return {row["name"]: row for row in rows}


def temporal_workflow_url(workflow_id: str) -> str:
    """The workflow page of the Temporal UI, which the host reaches on its own port row."""
    namespace = yaml.safe_load((ROOT / "contracts/temporal/naming.yaml").read_text())["namespace"]
    port = _ports()["temporal-ui"]["host_port"]
    return f"http://127.0.0.1:{port}/namespaces/{namespace}/workflows/{workflow_id}"


def datasources() -> dict:
    return {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Prometheus",
                "uid": "prometheus",
                "type": "prometheus",
                "access": "proxy",
                "url": "http://prometheus.wft-obs:9090",
                "isDefault": True,
            },
            {
                "name": "Loki",
                "uid": "loki",
                "type": "loki",
                "access": "proxy",
                "url": "http://loki.wft-obs:3100",
                "jsonData": {
                    # The correlation ids are structured metadata, read by label name.
                    "derivedFields": [
                        {
                            "name": "trace_id",
                            "matcherType": "label",
                            "matcherRegex": "trace_id",
                            "url": "${__value.raw}",
                            "datasourceUid": "tempo",
                            "urlDisplayLabel": "Trace",
                        },
                        {
                            "name": "job_id",
                            "matcherType": "label",
                            "matcherRegex": "job_id",
                            "url": temporal_workflow_url(WORKFLOW_ID_PREFIX + "${__value.raw}"),
                            "urlDisplayLabel": "Workflow",
                        },
                    ]
                },
            },
            {
                "name": "Tempo",
                "uid": "tempo",
                "type": "tempo",
                "access": "proxy",
                "url": "http://tempo.wft-obs:3200",
            },
            {
                "name": "Pyroscope",
                "uid": "pyroscope",
                "type": "grafana-pyroscope-datasource",
                "access": "proxy",
                "url": "http://pyroscope.wft-obs:4040",
            },
        ],
    }


def datasources_file() -> str:
    """The provisioning file, in which Grafana expands a dollar name from its environment."""
    return yaml.dump(datasources(), sort_keys=False).replace("$", "$$")


def dashboard_provider() -> dict:
    return {
        "apiVersion": 1,
        "providers": [
            {
                "name": "weft",
                "type": "file",
                "disableDeletion": True,
                "allowUiUpdates": False,
                "options": {"path": DASHBOARDS_PATH},
            }
        ],
    }


def _dashboards() -> dict[str, str]:
    return {path.name: path.read_text() for path in sorted(DASHBOARDS.glob("*.json"))}


def _checksum(*texts: str) -> str:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(text.encode())
    return digest.hexdigest()


def _module() -> dict:
    return yaml.safe_load((HERE / "module.yaml").read_text())


def _image(module: dict) -> str:
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]
    return f"docker.io/{module['artifact']['pin']}@{pin[module['artifact']['pin']]['digest']}"


def _provisioning_mount(kind: str) -> dict:
    return {
        "name": "provisioning",
        "mountPath": f"/etc/grafana/provisioning/{kind}/{kind}.yaml",
        "subPath": f"{kind}.yaml",
    }


def _secret_env(name: str, key: str) -> dict:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": f"{NAME}-admin", "key": key}}}


def render() -> list[dict]:
    module = _module()
    namespace = module["namespace"]
    topology = yaml.safe_load((ROOT / "contracts/topology/topology.yaml").read_text())
    node_port = _ports()[NAME]["node_port"]
    provisioning = {
        "datasources.yaml": datasources_file(),
        "dashboards.yaml": yaml.dump(dashboard_provider(), sort_keys=False),
    }
    meta = {"name": NAME, "namespace": namespace}
    labels = {"app": NAME, "app.kubernetes.io/name": NAME}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-provisioning", "namespace": namespace},
            "data": provisioning,
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-dashboards", "namespace": namespace},
            "data": _dashboards(),
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
                        # The datasources are read at start. The dashboards reload by themselves.
                        "annotations": {"checksum/config": _checksum(*provisioning.values())},
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
                            "runAsUser": GRAFANA_USER,
                            "runAsGroup": GRAFANA_USER,
                            "fsGroup": GRAFANA_USER,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": NAME,
                                "image": _image(module),
                                "env": [
                                    _secret_env("GF_SECURITY_ADMIN_USER", "admin-user"),
                                    _secret_env("GF_SECURITY_ADMIN_PASSWORD", "admin-password"),
                                    {"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "true"},
                                    {"name": "GF_AUTH_ANONYMOUS_ORG_ROLE", "value": "Editor"},
                                    {"name": "GF_ANALYTICS_REPORTING_ENABLED", "value": "false"},
                                    {"name": "GF_ANALYTICS_CHECK_FOR_UPDATES", "value": "false"},
                                    # The preinstalled apps download 250 MB on each first start.
                                    {"name": "GF_PLUGINS_PREINSTALL_DISABLED", "value": "true"},
                                ],
                                # Named `metrics` so the pod scrape finds Grafana's own series.
                                "ports": [{"name": "metrics", "containerPort": PORT}],
                                "startupProbe": {
                                    "httpGet": {"path": "/api/health", "port": "metrics"},
                                    "periodSeconds": 5,
                                    "failureThreshold": 60,
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/api/health", "port": "metrics"},
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/api/health", "port": "metrics"},
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
                                    _provisioning_mount("datasources"),
                                    _provisioning_mount("dashboards"),
                                    {"name": "dashboards", "mountPath": DASHBOARDS_PATH},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": f"{NAME}-data"},
                            },
                            {"name": "tmp", "emptyDir": {}},
                            {"name": "provisioning", "configMap": {"name": f"{NAME}-provisioning"}},
                            {"name": "dashboards", "configMap": {"name": f"{NAME}-dashboards"}},
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
                "type": "NodePort",
                "selector": {"app": NAME},
                "ports": [
                    {"name": "http", "port": PORT, "targetPort": "metrics", "nodePort": node_port}
                ],
            },
        },
    ]


def main(argv: list[str]) -> int:
    if argv == ["render"]:
        print(json.dumps(render()))
    elif argv == ["datasources"]:
        print(datasources_file(), end="")
    else:
        print("usage: config.py render | datasources", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
