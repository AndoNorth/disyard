"""The compile cache client against a real store and database.

The database login is a stand-in member of `dsy_artifacts`, the group role of the workflow's record
activity, and expiry is run by a stand-in member of `dsy_reaper`.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from dsy_artifactclient import cache
from dsy_artifactclient import commit as commit_module
from dsy_artifactclient.cache import Hit, Lost, Miss, WasmGone, Won, lookup
from dsy_artifactclient.client import ArtifactClient, RetryPolicy
from dsy_artifactclient.errors import ArtifactLost, StoreUnavailable
from dsy_artifactclient.keys import split_uri
from dsy_artifactindex import Invalidated, NoEntry, expired_cache_entries, uses_of
from dsy_servicekit.testing import free_port
from dsy_storecheck.s3 import client as s3_client

pytestmark = [pytest.mark.integration, pytest.mark.pair]

VERSIONS = {"componentize_py_version": "0.17.0", "wit_version": "0.2.1"}


def payload(label: str, size: int = 4096) -> bytes:
    return hashlib.shake_256(f"cache {label}".encode()).digest(size)


def key_of(label: str) -> str:
    return hashlib.sha256(f"key {label}".encode()).hexdigest()


@pytest.fixture
def role(shelf):
    """A client of a role of the contract, with no real waiting between tries."""
    policy = RetryPolicy(sleep=lambda seconds: None)
    return lambda name: ArtifactClient(shelf.endpoint, shelf.credentials[name], policy)


@pytest.fixture
def login(login):
    """Opens an autocommit connection as a fresh login of the group role; `login.dsn` is its DSN."""
    opened: list[psycopg.Connection] = []

    def connect(group: str) -> psycopg.Connection:
        conn = psycopg.connect(login(group), autocommit=True)
        opened.append(conn)
        return conn

    connect.dsn = login
    yield connect
    for conn in opened:
        conn.close()


@pytest.fixture
def artifacts(login):
    return login("dsy_artifacts")


@pytest.fixture
def reaper(login):
    return login("dsy_reaper")


@pytest.fixture
def admin(shelf):
    return s3_client(shelf.endpoint, shelf.credentials["admin"])


@pytest.fixture
def job() -> str:
    return str(uuid.uuid4())


def compiled(role, label: str, size: int = 4096):
    """The wasm and the build-info that a compile attempt uploads."""
    compile_role = role("compile")
    wasm = compile_role.put_content(payload(label, size), "component", "component.wasm")
    info = compile_role.put_content(payload(f"{label} info"), "build-info", "build-info.json")
    return wasm, info


def recorded(role, artifacts, job, label: str, key: str | None = None):
    """The stand-in record activity: commit the accepted attempt, then fill the cache."""
    wasm, info = compiled(role, label)
    commit_module.commit(artifacts, role("workflow"), [wasm, info], job, "compile-0")
    key = key or key_of(label)
    assert isinstance(cache.record(artifacts, wasm, info, key, VERSIONS), Won)
    return key, wasm, info


def overwrite(admin, reference, data: bytes) -> None:
    bucket, key = split_uri(reference.uri)
    admin.put_object(Bucket=bucket, Key=key, Body=data)


def test_first_writer_wins_between_two_builds(role, artifacts, job):
    key = key_of("race")
    first = compiled(role, "first build")
    second = compiled(role, "second build")
    for wasm, info in (first, second):
        commit_module.commit(artifacts, role("workflow"), [wasm, info], job, "compile-0")

    assert isinstance(cache.record(artifacts, *first, key, VERSIONS), Won)
    assert isinstance(cache.record(artifacts, *second, key, VERSIONS), Lost)

    hit = lookup(artifacts, role("workflow"), key)
    assert isinstance(hit, Hit)
    assert hit.wasm.uri == first[0].uri
    assert hit.build_info.uri == first[1].uri


def test_record_before_the_commit_finds_no_wasm_and_the_stand_in_commits_first(
    role, artifacts, job, monkeypatch
):
    wasm, info = compiled(role, "uncommitted")
    uncommitted = cache.record(artifacts, wasm, info, key_of("uncommitted"), VERSIONS)
    assert isinstance(uncommitted, WasmGone)
    assert isinstance(lookup(artifacts, role("workflow"), key_of("uncommitted")), Miss)

    order: list[str] = []

    def spy(module, name):
        original = getattr(module, name)

        def run(*args, **kwargs):
            order.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, run)

    spy(commit_module, "commit")
    spy(cache, "record")
    recorded(role, artifacts, job, "ordered")
    assert order == ["commit", "record"]


@pytest.mark.parametrize("how", ["invalidation", "expiry"])
def test_record_again_after_invalidation_and_after_expiry(how, role, artifacts, reaper, job):
    key, wasm, info = recorded(role, artifacts, job, f"again {how}")
    now = datetime.now(UTC)
    if how == "invalidation":
        assert isinstance(cache.invalidate(artifacts, key, now), Invalidated)
    else:
        assert expired_cache_entries(reaper, now + timedelta(days=15), now) == [key]
    assert isinstance(lookup(artifacts, role("workflow"), key), Miss)

    assert isinstance(cache.record(artifacts, wasm, info, key, VERSIONS), Won)
    assert isinstance(lookup(artifacts, role("workflow"), key), Hit)
    assert isinstance(cache.invalidate(artifacts, key_of("no such entry"), now), NoEntry)


@pytest.mark.parametrize("damage", ["missing", "wrong-size"])
def test_lookup_miss_when_the_wasm_is_missing_or_the_wrong_size(
    damage, role, artifacts, admin, job
):
    key, wasm, _ = recorded(role, artifacts, job, f"damaged {damage}")
    assert isinstance(lookup(artifacts, role("workflow"), key), Hit)

    if damage == "missing":
        bucket, object_key = split_uri(wasm.uri)
        admin.delete_object(Bucket=bucket, Key=object_key)
    else:
        overwrite(admin, wasm, b"short")

    assert isinstance(lookup(artifacts, role("workflow"), key), Miss)


def test_lookup_error_is_raised_and_never_a_miss(shelf, role, artifacts, login, job):
    key, *_ = recorded(role, artifacts, job, "outage")
    closed = free_port()
    unreachable = ArtifactClient(
        f"http://127.0.0.1:{closed}",
        shelf.credentials["workflow"],
        RetryPolicy(sleep=lambda seconds: None),
    )
    with pytest.raises(StoreUnavailable):
        lookup(artifacts, unreachable, key)

    stale = login("dsy_artifacts")
    stale.close()
    with pytest.raises(psycopg.OperationalError):
        lookup(stale, role("workflow"), key)


def test_corrupt_cached_wasm_is_lost_and_invalidation_makes_a_miss(
    role, artifacts, admin, job, tmp_path
):
    key, wasm, _ = recorded(role, artifacts, job, "corrupt")
    overwrite(admin, wasm, bytes(wasm.size))

    hit = lookup(artifacts, role("workflow"), key)
    assert isinstance(hit, Hit)
    with pytest.raises(ArtifactLost):
        role("workflow").get(hit.wasm, tmp_path / "component.wasm")

    assert isinstance(cache.invalidate(artifacts, key, datetime.now(UTC)), Invalidated)
    assert isinstance(lookup(artifacts, role("workflow"), key), Miss)


def test_entry_names_wasm_and_build_info(role, artifacts, job):
    key, wasm, info = recorded(role, artifacts, job, "named")

    row = artifacts.execute(
        "SELECT wasm_uri, build_info_uri, componentize_py_version, wit_version FROM artifact"
        " WHERE kind = 'compile-cache' AND cache_key = %s",
        (key,),
    ).fetchone()
    assert row == (wasm.uri, info.uri, *VERSIONS.values())

    hit = lookup(artifacts, role("workflow"), key)
    assert (hit.wasm, hit.build_info) == (wasm, info)


def test_cache_hit_links_wasm_and_build_info_to_the_job(role, artifacts, job):
    key, wasm, info = recorded(role, artifacts, job, "linked")
    other = str(uuid.uuid4())
    before = artifacts.execute(
        "SELECT last_used_at FROM artifact WHERE cache_key = %s", (key,)
    ).fetchone()[0]

    hit = lookup(artifacts, role("workflow"), key)
    assert isinstance(hit, Hit)
    cache.use(artifacts, hit, other)

    for reference in (wasm, info):
        assert {use.job_id for use in uses_of(artifacts, reference.uri)} == {job, other}
    after = artifacts.execute(
        "SELECT last_used_at FROM artifact WHERE cache_key = %s", (key,)
    ).fetchone()[0]
    assert after > before
