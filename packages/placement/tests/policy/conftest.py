"""The `ci` cluster as `kubectl get nodes,pods -A -o json` prints it, split into the node list and
the pod list that the API answers, and the topology contract."""

import json
from pathlib import Path

import pytest
import yaml

CLUSTER = Path(__file__).with_name("ci-cluster.json")


@pytest.fixture(scope="session")
def topology(root) -> dict:
    return yaml.safe_load((root / "contracts/topology/topology.yaml").read_text())


@pytest.fixture
def cluster() -> tuple[dict, dict]:
    """A fresh NodeList and PodList per test, so a test can change them."""
    items = json.loads(CLUSTER.read_text())["items"]
    return (
        {"kind": "NodeList", "items": [i for i in items if i["kind"] == "Node"]},
        {"kind": "PodList", "items": [i for i in items if i["kind"] == "Pod"]},
    )
