"""The store client against an in-test store that records how each read was signed."""

import json

import pytest
from wdf_acceptance.clients.store import fetch

URI = "s3://wdf-results/acme/b/j/0/sim/r1a1/results.xml"
# Two identities with distinct keys, so a signature names which one signed.
CREDENTIALS = {
    "export": {"access_key": "EXPORTKEY", "secret_key": "export-secret"},
    "simulate": {"access_key": "SIMULATEKEY", "secret_key": "simulate-secret"},
}


@pytest.fixture
def store(serve, tmp_path, monkeypatch):
    (tmp_path / "objectstore.credentials.json").write_text(json.dumps(CREDENTIALS))
    monkeypatch.setenv("WDF_STATE_DIR", str(tmp_path))
    return serve()


def test_store_fetch_checks_identity(store):
    path = "/wdf-results/acme/b/j/0/sim/r1a1/results.xml"
    store.answer("GET", path, (200, b"<results/>"), (200, b"<results/>"))

    assert fetch(URI, "export", store.url) == b"<results/>"
    assert fetch(URI, "simulate", store.url) == b"<results/>"

    signed = [(c.path, c.headers["Authorization"]) for c in store.calls]
    assert [p for p, _ in signed] == [path, path]
    assert "Credential=EXPORTKEY/" in signed[0][1]
    assert "Credential=SIMULATEKEY/" in signed[1][1]


def test_store_fetch_refuses_an_unknown_identity_or_uri(store):
    with pytest.raises(KeyError, match="admin"):
        fetch(URI, "admin", store.url)
    with pytest.raises(ValueError, match="not an s3://<bucket>/<key> uri"):
        fetch("s3://wdf-results", "export", store.url)
    assert store.calls == []
