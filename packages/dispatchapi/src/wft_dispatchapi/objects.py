"""The upload of a content object: a source bundle or an aggregate index object, stored by hash.

An upload has no job yet, so the object goes into `wft-sources` unindexed, and a submit that names
it indexes and links it to each of its jobs.
"""

import hashlib
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO

from wft_artifactclient.client import CHUNK_BYTES, ArtifactClient, kind_caps
from wft_artifactclient.errors import ArtifactError, ArtifactNotFound
from wft_artifactclient.keys import HASH, content_key, uri_of
from wft_compile import bundle
from wft_events.models import ContentReference

from .contract import reasons
from .models import ObjectAnswer

# The name a stored object is given, which is not part of its key.
NAMES = {"source": bundle.NAME, "index": "index.jsonl"}
# Uploads at a time in one process, because each spools its body to a scratch file first.
UPLOADS_IN_FLIGHT = 2


class Refused(Exception):
    """An upload answered with a reason code of the API contract, and nothing stored."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message

    @property
    def http(self) -> int:
        return next(row.http for row in reasons().reasons if row.code == self.code)


class Uploads:
    """The upload route over a store client of the `submit` identity."""

    def __init__(self, client: ArtifactClient, scratch: Path | None = None):
        self.client, self.scratch = client, scratch
        self._slots = threading.BoundedSemaphore(UPLOADS_IN_FLIGHT)

    def put(self, kind: str, sha256: str, body: BinaryIO) -> tuple[int, ObjectAnswer]:
        """Stores the body under `sha256`, answering 201 when it was stored and 200 when the
        store already held it. Raises Refused."""
        if kind not in NAMES:
            raise Refused("BAD_KIND", f"{kind} is not source or index")
        if not HASH.fullmatch(sha256):
            raise Refused("HASH_MISMATCH", f"{sha256!r} is not a lowercase hex SHA-256")
        if not self._slots.acquire(blocking=False):
            raise Refused("TOO_MANY_CALLS", f"{UPLOADS_IN_FLIGHT} uploads are already in flight")
        try:
            with tempfile.NamedTemporaryFile(dir=self.scratch) as spool:
                size = _spool(body, spool, kind, sha256)
                path = Path(spool.name)
                if kind == "source":
                    try:
                        bundle.info(path)
                    except bundle.Refused as error:
                        raise Refused("UNREADABLE_BUNDLE", str(error)) from error
                reference = ContentReference(
                    uri=uri_of(kind, content_key(kind, sha256)),
                    sha256=sha256,
                    size=size,
                    name=NAMES[kind],
                    kind=kind,
                )
                try:
                    if self._present(reference):
                        return 200, ObjectAnswer(reference=reference, created=False)
                    self.client.put_content(path, kind, NAMES[kind])
                except ArtifactError as error:
                    raise Refused("STORE_UNAVAILABLE", str(error)) from error
                return 201, ObjectAnswer(reference=reference, created=True)
        finally:
            self._slots.release()

    def _present(self, reference: ContentReference) -> bool:
        try:
            self.client.head(reference)
        except ArtifactNotFound:
            return False
        return True


def _spool(body: BinaryIO, spool, kind: str, sha256: str) -> int:
    """Copies the body to the spool, refusing it past the kind's cap or under another hash."""
    cap, digest, size = kind_caps()[kind], hashlib.sha256(), 0
    while chunk := body.read(CHUNK_BYTES):
        size += len(chunk)
        if size > cap:
            raise Refused("OBJECT_TOO_LARGE", f"more than the {kind} cap of {cap} bytes")
        digest.update(chunk)
        spool.write(chunk)
    spool.flush()
    if digest.hexdigest() != sha256:
        raise Refused("HASH_MISMATCH", f"the body hashes to {digest.hexdigest()}")
    return size
