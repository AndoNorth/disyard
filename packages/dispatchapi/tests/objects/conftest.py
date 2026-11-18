"""A store client of the `submit` identity, as the service holds it, over the module's SeaweedFS,
and source bundles that no other test uploaded."""

import secrets

import pytest
from wdf_artifactclient.client import ArtifactClient, RetryPolicy
from wdf_compile import bundle
from wdf_dispatchapi.objects import Uploads

NO_WAIT = RetryPolicy(sleep=lambda seconds: None)


@pytest.fixture
def client(shelf) -> ArtifactClient:
    return ArtifactClient(shelf.endpoint, shelf.credentials["submit"], NO_WAIT)


@pytest.fixture
def uploads(client, tmp_path) -> Uploads:
    return Uploads(client, tmp_path)


@pytest.fixture
def uploads_down(shelf, tmp_path) -> Uploads:
    """Uploads through a client whose store never answers."""
    down = ArtifactClient("http://127.0.0.1:1", shelf.credentials["submit"], NO_WAIT)
    return Uploads(down, tmp_path)


@pytest.fixture
def make_bundle(tmp_path):
    """The bytes of a fresh bundle with a lockfile and the modules `job` and `job.main`."""

    def make() -> bytes:
        tree = tmp_path / secrets.token_hex(4)
        (tree / "job").mkdir(parents=True)
        (tree / "pyproject.toml").write_text('[project]\nname = "job"\nversion = "0"\n')
        (tree / "uv.lock").write_text(f"# {secrets.token_hex(8)}\n")
        (tree / "job" / "__init__.py").write_text("")
        (tree / "job" / "main.py").write_text("def run():\n    return 0\n")
        return bundle.write(tree, tree.parent / f"{tree.name}-out").read_bytes()

    return make
