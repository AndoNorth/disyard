"""The validator's resolver over SeaweedFS: what the upload route stored is what it resolves."""

import hashlib
import io
import json

import pytest
from dsy_compile import bundle
from dsy_dispatchapi.resolver import StoreSourceResolver
from dsy_jobcheck import validate
from dsy_jobcheck.resolver import BundleNotFound, ResolverContractError, resolver_contract
from dsy_jobdoc.profile_files import load_profile

pytestmark = pytest.mark.integration


def upload(uploads, kind: str, data: bytes):
    return uploads.put(kind, hashlib.sha256(data).hexdigest(), io.BytesIO(data))[1].reference


@pytest.fixture
def resolver(client, tmp_path) -> StoreSourceResolver:
    return StoreSourceResolver(client, tmp_path)


def test_resolver_contract(resolver, uploads, make_bundle):
    present = [upload(uploads, "source", make_bundle()) for _ in range(2)]
    resolver_contract(resolver, present)


def test_resolver_contract_fails_for_an_absent_bundle(resolver, uploads, make_bundle):
    data = make_bundle()
    missing = upload(uploads, "source", make_bundle()).model_copy(
        update={"sha256": hashlib.sha256(data).hexdigest()}
    )
    with pytest.raises(ResolverContractError, match="known to be present and was not found"):
        resolver_contract(resolver, [missing])


def test_resolved_bundle_is_what_was_uploaded(resolver, uploads, make_bundle, tmp_path):
    data = make_bundle()
    (tmp_path / "local.tar").write_bytes(data)
    assert resolver.resolve_source(upload(uploads, "source", data)) == bundle.info(
        tmp_path / "local.tar"
    )


def test_index_object_exists_once_uploaded(resolver, uploads):
    data = b'{"rows": 1}\n'
    reference = upload(uploads, "index", data)
    assert resolver.exists(reference) is True


def test_stored_object_that_is_not_a_bundle_is_not_found(resolver, uploads):
    reference = upload(uploads, "index", b"an index object, not a tar archive\n")
    with pytest.raises(BundleNotFound):
        resolver.resolve_source(reference)


def test_validator_checks_bundles_through_the_store(resolver, uploads, make_bundle, root):
    reference = upload(uploads, "source", make_bundle())
    document = json.loads((root / "contracts/job/vectors/valid/level0-echo.json").read_text())
    source = document["tasks"]["build"]["params"]
    source["source"] = reference.model_dump()
    profile = load_profile("ci", root, root / "contracts/job/vectors/slots-fixture")

    source["entry"] = "job.main"
    assert validate(json.dumps(document).encode(), profile, resolver).ok

    source["entry"] = "job.absent"
    report = validate(json.dumps(document).encode(), profile, resolver)
    assert [issue.code for issue in report.issues] == ["entry-not-in-bundle"]
