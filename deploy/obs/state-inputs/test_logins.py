"""The service login against a migrated app database: two applies, the second changing nothing, the
login in only `wft_reader` and able to log in, drift found and repaired, and the Secret of the
module."""

import importlib.util
import json
from pathlib import Path

import psycopg
import pytest

_spec = importlib.util.spec_from_file_location(
    "state_inputs_logins", Path(__file__).with_name("logins.py")
)
logins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logins)

[(LOGIN, GROUP)] = logins.LOGINS.values()
ITEM = f"login: {LOGIN} in {GROUP}"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("WFT_STATE_DIR", str(tmp_path))
    return tmp_path


def run(mode, dsn, capsys) -> tuple[int, list[str], list[str]]:
    code = logins.main(mode, dsn)
    out, err = capsys.readouterr()
    return code, out.splitlines(), err.splitlines()


def memberships(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        return [g for g, *_ in conn.execute(logins.MEMBER_OF, (LOGIN,))]


@pytest.fixture
def server(appdb):
    """The migrated database, on a server that does not hold the login, which is server wide and so
    outlives the database of an earlier test."""
    with psycopg.connect(appdb, autocommit=True) as conn:
        conn.execute(f"drop role if exists {LOGIN}")
    return appdb


@pytest.mark.integration
def test_apply_twice_then_check(server, capsys):
    appdb = server
    assert run("check", appdb, capsys) == (1, [f"{ITEM} missing"], [f"differs: {ITEM}"])
    assert run("apply", appdb, capsys) == (0, [f"{ITEM} created", "changed: 1"], [])
    assert run("apply", appdb, capsys) == (0, [f"{ITEM} unchanged", "changed: 0"], [])
    assert run("check", appdb, capsys) == (0, [f"{ITEM} unchanged"], [])
    assert memberships(appdb) == [GROUP]


@pytest.mark.integration
def test_changed_password_found_and_repaired(server, capsys):
    appdb = server
    logins.main("apply", appdb)
    logins.state_file().write_text(json.dumps({LOGIN: "rotated"}))
    capsys.readouterr()

    assert run("check", appdb, capsys) == (1, [f"{ITEM} differs: password"], [f"differs: {ITEM}"])
    assert run("apply", appdb, capsys) == (0, [f"{ITEM} updated", "changed: 1"], [])
    assert run("check", appdb, capsys)[0] == 0


@pytest.mark.integration
def test_drifted_membership_and_attributes_repaired(server, capsys):
    appdb = server
    logins.main("apply", appdb)
    with psycopg.connect(appdb, autocommit=True) as conn:
        conn.execute(f"grant wft_admission to {LOGIN}")
        conn.execute(f"alter role {LOGIN} createrole")
    capsys.readouterr()

    code, out, _ = run("check", appdb, capsys)
    assert code == 1
    assert out == [f"{ITEM} differs: attributes; member of wft_admission, {GROUP}"]
    assert run("apply", appdb, capsys)[:2] == (0, [f"{ITEM} updated", "changed: 1"])
    assert memberships(appdb) == [GROUP]


def test_render_secret(state):
    first = logins.render()
    assert logins.render() == first
    assert oct(logins.state_file().stat().st_mode & 0o777) == "0o600"
    [dsns] = first
    assert dsns["metadata"] == {"name": "state-inputs-logins", "namespace": "wft-obs"}
    known = json.loads(logins.state_file().read_text())
    assert dsns["stringData"] == {
        "WFT_STATE_INPUTS_DSN": (
            f"postgresql://{LOGIN}:{known[LOGIN]}@appdb.wft-infra.svc.cluster.local:5432/postgres"
        )
    }
