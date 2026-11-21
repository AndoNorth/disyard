"""The upload route of a content object against SeaweedFS, under the `submit` identity."""

import hashlib
import io
import secrets

import pytest
from wft_artifactclient.client import ArtifactClient, kind_caps
from wft_artifactclient.errors import ArtifactNotFound
from wft_artifactclient.keys import content_key
from wft_dispatchapi.objects import CHUNK_BYTES, UPLOADS_IN_FLIGHT, Refused, Uploads
from wft_events.models import ContentReference
from wft_storecheck.provision import ADMIN
from wft_storecheck.s3 import client as s3_client

pytestmark = pytest.mark.integration


class Zeros(io.RawIOBase):
    """A body of `size` zero bytes that counts what was read from it."""

    def __init__(self, size: int):
        self.left, self.read_bytes = size, 0

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        n = self.left if n < 0 else min(n, self.left)
        self.left -= n
        self.read_bytes += n
        return bytes(n)


class Unread(io.RawIOBase):
    def read(self, n: int = -1) -> bytes:
        raise AssertionError("the body was read")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stored_keys(shelf, digest: str) -> list[str]:
    s3 = s3_client(shelf.endpoint, shelf.credentials[ADMIN])
    listing = s3.list_objects_v2(Bucket="wft-sources", Prefix=content_key("source", digest))
    return [entry["Key"] for entry in listing.get("Contents", [])]


def absent(client: ArtifactClient, digest: str) -> bool:
    reference = ContentReference(
        uri=f"s3://wft-sources/{content_key('source', digest)}",
        sha256=digest,
        size=0,
        name="probe",
        kind="source",
    )
    try:
        client.head(reference)
    except ArtifactNotFound:
        return True
    return False


def refusal(uploads: Uploads, kind: str, digest: str, body) -> Refused:
    with pytest.raises(Refused) as caught:
        uploads.put(kind, digest, body)
    return caught.value


def test_upload_twice_is_200_and_one_object(shelf, uploads, client, make_bundle, tmp_path):
    data = make_bundle()
    digest = sha256(data)
    status, answer = uploads.put("source", digest, io.BytesIO(data))
    assert (status, answer.created) == (201, True)
    assert answer.reference.sha256 == digest
    assert answer.reference.size == len(data)
    assert answer.reference.uri == f"s3://wft-sources/{content_key('source', digest)}"

    again_status, again = uploads.put("source", digest, io.BytesIO(data))
    assert (again_status, again.created) == (200, False)
    assert again.reference == answer.reference
    assert stored_keys(shelf, digest) == [content_key("source", digest)]
    client.get(answer.reference, tmp_path / "back.tar")
    assert (tmp_path / "back.tar").read_bytes() == data


def test_index_object_is_stored_without_a_bundle_check(uploads):
    data = f'{{"index": "{secrets.token_hex(8)}"}}\n'.encode()
    status, answer = uploads.put("index", sha256(data), io.BytesIO(data))
    assert (status, answer.reference.kind) == (201, "index")


def test_hash_mismatch_writes_nothing(uploads, client, make_bundle):
    data, claimed = make_bundle(), sha256(make_bundle())
    error = refusal(uploads, "source", claimed, io.BytesIO(data))
    assert (error.code, error.http) == ("HASH_MISMATCH", 422)
    assert absent(client, claimed) and absent(client, sha256(data))

    error = refusal(uploads, "source", "not-a-hash", Unread())
    assert (error.code, error.http) == ("HASH_MISMATCH", 422)


@pytest.mark.parametrize("kind", ["source", "index"])
def test_object_too_large_per_kind(uploads, kind):
    cap = kind_caps()[kind]
    body = Zeros(cap + 1)
    error = refusal(uploads, kind, "0" * 64, body)
    assert (error.code, error.http) == ("OBJECT_TOO_LARGE", 413)
    assert body.read_bytes <= cap + CHUNK_BYTES


def test_object_at_the_cap_is_not_too_large(uploads):
    error = refusal(uploads, "source", "0" * 64, Zeros(kind_caps()["source"]))
    assert error.code == "HASH_MISMATCH"


def test_unreadable_bundle_refused(uploads, client):
    data = b"not a tar archive " + secrets.token_bytes(8)
    error = refusal(uploads, "source", sha256(data), io.BytesIO(data))
    assert (error.code, error.http) == ("UNREADABLE_BUNDLE", 422)
    assert "tar" in error.message
    assert absent(client, sha256(data))


def test_bad_kind(uploads):
    for kind in ("manifest", "component", "Source"):
        error = refusal(uploads, kind, "0" * 64, Unread())
        assert (error.code, error.http) == ("BAD_KIND", 400)


def test_uploads_beyond_the_process_limit_are_refused(uploads):
    for _ in range(UPLOADS_IN_FLIGHT):
        uploads._slots.acquire()
    error = refusal(uploads, "index", "0" * 64, Unread())
    assert (error.code, error.http) == ("TOO_MANY_CALLS", 429)


def test_store_down_is_store_unavailable(uploads_down):
    data = b"{}\n"
    error = refusal(uploads_down, "index", sha256(data), io.BytesIO(data))
    assert (error.code, error.http) == ("STORE_UNAVAILABLE", 503)
