"""The API client against in-test listeners that answer with the API contract's vectors, and the
smoke job's shape against the dispatch smoke driver's."""

import hashlib
import json
from pathlib import Path

import pytest
from dsy_acceptance.clients import Failed
from dsy_acceptance.clients.api import Api, batch, batch_key, template
from dsy_dispatchapi import smoke as driver
from dsy_workflow.harness import echo_params

VECTORS = Path(__file__).resolve().parents[4] / "contracts/api/vectors"
TENANT = "acme"


def vector(name: str) -> bytes:
    return (VECTORS / name).read_bytes()


CREATED = vector("object-answer/valid/created-source.json")
PRESENT = json.dumps({**json.loads(CREATED), "created": False}).encode()
ACCEPTED = vector("entry-answer/valid/accepted.json").strip() + b"\n"
REJECTED = vector("entry-answer/valid/rejected-invalid-params.json").strip() + b"\n"
SUBMITTED = vector("summary-line/valid/submit.json").strip() + b"\n"
STATUS = vector("job-status/valid/running-with-detail.json")
CANCELLED = vector("control-answer/valid/applied.json").strip() + b"\n"
CANCEL_SUMMARY = vector("summary-line/valid/control.json").strip() + b"\n"


@pytest.fixture
def listeners(serve):
    dispatch, control = serve(), serve()
    return dispatch, control, Api(dispatch.url, control.url, TENANT)


def test_api_against_server(listeners):
    dispatch, control, api = listeners
    data = b"a source bundle"
    upload = f"/v1/tenants/{TENANT}/objects/source/{hashlib.sha256(data).hexdigest()}"
    dispatch.answer("PUT", upload, (201, CREATED), (200, PRESENT))
    dispatch.answer("POST", f"/v1/tenants/{TENANT}/batches", (200, ACCEPTED + SUBMITTED))
    job_id = json.loads(ACCEPTED)["job_id"]
    control.answer("GET", f"/v1/tenants/{TENANT}/jobs/{job_id}", (200, STATUS))
    key = batch_key()
    cancel = f"/v1/tenants/{TENANT}/batches/{key}/cancel"
    control.answer("POST", cancel, (200, CANCELLED + CANCEL_SUMMARY))

    reference = json.loads(CREATED)["reference"]
    assert api.upload_source(data) == reference
    assert api.upload_source(data) == reference
    document = template(reference, echo_params())
    assert api.submit_one(document, key) == job_id
    assert api.status(job_id) == json.loads(STATUS)
    assert api.cancel_batch(key) == json.loads(CANCEL_SUMMARY)["summary"]

    assert [(c.method, c.path, c.body) for c in dispatch.calls] == [
        ("PUT", upload, data),
        ("PUT", upload, data),
        ("POST", f"/v1/tenants/{TENANT}/batches", b"".join(batch(key, document, 1))),
    ]
    assert dispatch.calls[2].headers["Content-Type"] == "application/x-ndjson"
    assert [(c.method, c.path, c.body) for c in control.calls] == [
        ("GET", f"/v1/tenants/{TENANT}/jobs/{job_id}", b""),
        ("POST", cancel, b'{"reason": "smoke"}'),
    ]


def test_api_raises_on_an_answer_it_cannot_use(listeners):
    dispatch, control, api = listeners
    data = b"a source bundle"
    upload = f"/v1/tenants/{TENANT}/objects/source/{hashlib.sha256(data).hexdigest()}"
    dispatch.answer("PUT", upload, (507, b'{"error": "STORE_FULL"}'))
    dispatch.answer("POST", f"/v1/tenants/{TENANT}/batches", (200, REJECTED + SUBMITTED))
    dispatch.answer("POST", f"/v1/tenants/{TENANT}/batches", (200, b""))

    with pytest.raises(Failed, match="upload of the source bundle answered 507"):
        api.upload_source(data)
    with pytest.raises(Failed, match="not accepted: .*INVALID_PARAMS"):
        api.submit_one({}, batch_key())
    with pytest.raises(Failed, match="not accepted: no answer"):
        api.submit_one({}, batch_key())
    with pytest.raises(Failed, match="status read of nobody answered 404"):
        api.status("nobody")
    # A batch the control listener refuses outright.
    with pytest.raises(Failed, match="answered 404"):
        api.cancel_batch(batch_key())


def test_template_matches_driver():
    source = json.loads(CREATED)["reference"]
    for simulate, checkpoint in ((echo_params(), False), (driver.BURN, True)):
        document = template(source, simulate, checkpoint)
        assert document == driver.template(source, simulate, checkpoint)
        assert batch("smoke-0123456789ab", document, 3) == driver.batch(
            "smoke-0123456789ab", document, 3
        )


def test_batch_key_is_fresh_per_run():
    first, second = batch_key(), batch_key()
    assert first != second
    assert first.startswith("smoke-") and len(first) == len("smoke-") + 12
