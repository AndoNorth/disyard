"""The service in a process of its own, as `python -m dsy_dispatchapi.service` runs it, but over a
store kept in a directory and dsy_broker's FakeBroker, so a test holds only Postgres and the
Temporal dev server.

Run as a script, it reads the configuration of the module from the environment and the store's
directory from STORE_ENV. A test process loads it with runpy for DirStore.
"""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from dsy_artifactclient.client import Head, as_reference
from dsy_artifactclient.errors import ArtifactError, ArtifactNotFound
from dsy_artifactclient.keys import content_key, uri_of
from dsy_events.models import ContentReference

STORE_ENV = "DSY_TEST_STORE_DIR"


class StoreDown(ArtifactError):
    code = "STORE_UNAVAILABLE"


class DirStore:
    """The calls the service makes of its store client, over one file per content hash.

    Every call raises StoreDown while `available` is False.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.available = True

    def put_content(self, source, kind: str, name: str, *, conn=None, job_id=None, deadline=None):
        self._answer()
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as spool:
            if isinstance(source, bytes):
                spool.write(source)
            else:
                with open(source, "rb") as read:
                    shutil.copyfileobj(read, spool)
        digest = hashlib.sha256()
        with open(spool.name, "rb") as read:
            while chunk := read.read(1 << 20):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        path = self.root / sha256
        os.replace(spool.name, path)
        return ContentReference(
            uri=uri_of(kind, content_key(kind, sha256)),
            sha256=sha256,
            size=path.stat().st_size,
            name=name,
            kind=kind,
        )

    def head(self, reference, **_) -> Head:
        return Head(size=self._path(reference).stat().st_size, etag=None)

    def get(self, reference, dest: Path, **_) -> None:
        shutil.copyfile(self._path(reference), dest)

    def get_bytes(self, reference, max_bytes: int, **_) -> bytes:
        return self._path(reference).read_bytes()[: max_bytes + 1]

    def _path(self, reference) -> Path:
        self._answer()
        path = self.root / as_reference(reference).sha256
        if not path.exists():
            raise ArtifactNotFound(as_reference(reference).uri)
        return path

    def _answer(self) -> None:
        if not self.available:
            raise StoreDown("the store is down")


def main() -> None:
    import logging

    from dsy_broker.tenancy import load
    from dsy_broker.testing import FakeBroker
    from dsy_dispatchapi.gateway import TemporalGateway
    from dsy_dispatchapi.service import Config, Service, run

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env(os.environ)
    store = DirStore(Path(os.environ[STORE_ENV]))
    gateway = TemporalGateway(config.temporal_address)
    broker = FakeBroker(config.tenancy_profile)
    run(Service(config, load(config.tenancy_profile), store, gateway, broker.connect))


if __name__ == "__main__":
    main()
