"""The validator's SourceResolver over the bundles and index objects stored in `dsy-sources`.

It reads under the `submit` identity. A store that does not answer raises its ArtifactError, so a
caller tells an outage from a missing object.
"""

import tempfile
from pathlib import Path
from typing import Any

from dsy_artifactclient.client import ArtifactClient
from dsy_artifactclient.errors import ArtifactLost, ArtifactNotFound
from dsy_artifactclient.keys import content_key, uri_of
from dsy_compile import bundle
from dsy_events.models import ContentReference
from dsy_jobcheck.resolver import BundleInfo, BundleNotFound


class StoreSourceResolver:
    def __init__(self, client: ArtifactClient, scratch: Path | None = None):
        self.client, self.scratch = client, scratch

    def resolve_source(self, ref: Any) -> BundleInfo:
        reference = self._stored("source", ref.sha256)
        if reference is None:
            raise BundleNotFound(ref.sha256)
        with tempfile.TemporaryDirectory(dir=self.scratch) as scratch:
            path = Path(scratch) / bundle.NAME
            try:
                self.client.get(reference, path)
                return bundle.info(path)
            except (ArtifactLost, bundle.Refused) as error:
                raise BundleNotFound(f"{ref.sha256}: {error}") from error

    def exists(self, ref: Any) -> bool:
        return self._stored("index", ref.sha256) is not None

    def _stored(self, kind: str, sha256: str) -> ContentReference | None:
        """The reference of the stored object with its stored size, or None when it is absent."""
        reference = ContentReference(
            uri=uri_of(kind, content_key(kind, sha256)), sha256=sha256, size=0, name=kind, kind=kind
        )
        try:
            size = self.client.head(reference).size
        except ArtifactNotFound:
            return None
        return reference.model_copy(update={"size": size})
