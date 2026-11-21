"""The reader lists nodes and pods over TLS with the service account's token, and reads the pool
label from the topology contract shipped as package data."""

import urllib.error
from importlib import resources
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from wft_placement import kube


def query(path: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(path).query)


def test_reader_sends_token(api, tls, other_ca, token):
    reader = kube.Reader(api.url, str(token), str(tls[0]))
    reader.nodes()
    token.write_text("token-2\n")
    reader.pods()
    assert [auth for _, auth in api.requests] == ["Bearer token-1", "Bearer token-2"]

    # A server that the given CA does not vouch for never sees the token.
    with pytest.raises(urllib.error.URLError, match="CERTIFICATE_VERIFY_FAILED"):
        kube.Reader(api.url, str(token), str(other_ca)).nodes()
    assert len(api.requests) == 2


def test_reader_lists_pool_nodes(api, tls, token, root):
    nodes = kube.Reader(api.url, str(token), str(tls[0])).nodes()

    assert nodes == api.nodes
    [(path, _)] = api.requests
    assert urlsplit(path).path == "/api/v1/nodes"
    assert query(path) == {"labelSelector": [kube.topology()["pool_label"]]}
    assert kube.topology()["pool_label"] == "wft.dev/pool"
    # The label comes from the package's own copy of the topology contract.
    shipped = resources.files("wft_placement") / "contract" / "topology.yaml"
    assert yaml.safe_load(shipped.read_text()) == kube.topology()
    pinned = yaml.safe_load((root / "contracts/topology/topology.yaml").read_text())
    assert kube.topology() == pinned


def test_reader_lists_live_pods(api, tls, token):
    pods = kube.Reader(api.url, str(token), str(tls[0])).pods()

    assert pods == api.pods
    [(path, _)] = api.requests
    # Cluster wide, since pool nodes also run kube-system daemon pods.
    assert urlsplit(path).path == "/api/v1/pods"
    assert query(path) == {"fieldSelector": ["status.phase!=Succeeded,status.phase!=Failed"]}
