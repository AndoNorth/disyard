"""The shared logins module: one password per login however many processes start together, the
Secrets of a service, the usage exit, and, against a migrated app database, each of the four
services' logins converged and then found unchanged."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from dsy_clusterkit import logins

ROOT = Path(__file__).parents[3]
SERVICES = ["admission", "dispatchapi", "workflow", "obs/state-inputs"]
DATA = {
    "logins": {"DSY_A_DSN": ("svc_a", "dsy_reader"), "DSY_B_DSN": ("svc_b", "dsy_reader")},
    "namespace": "ns",
    "state_name": "svc",
}


def service(directory: str):
    spec = importlib.util.spec_from_file_location(
        directory.replace("/", "_"), ROOT / "deploy" / directory / "logins.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DSY_STATE_DIR", str(tmp_path))
    return tmp_path


def test_state_file_is_in_the_state_dir(state):
    assert logins.Spec(**DATA).state_file() == state / "svc.logins.json"


def test_passwords_are_generated_once_and_private(state):
    spec = logins.Spec(**DATA)
    first = spec.secrets_of_logins()
    assert sorted(first) == ["svc_a", "svc_b"]
    assert spec.secrets_of_logins() == first
    assert oct(spec.state_file().stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in state.iterdir()] == ["svc.logins.json"]


def test_two_processes_started_together_leave_one_password(state):
    start = state / "go"
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from dsy_clusterkit import logins\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(0.001)\n"
        f"print(logins.Spec(**{DATA!r}).secrets_of_logins())\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(start)], stdout=subprocess.PIPE, text=True
        )
        for _ in range(8)
    ]
    start.touch()
    seen = {proc.communicate()[0] for proc in procs}
    assert all(proc.returncode == 0 for proc in procs)
    on_disk = json.loads((state / "svc.logins.json").read_text())
    assert seen == {str(on_disk) + "\n"}
    assert [p.name for p in state.iterdir() if p.name != "go"] == ["svc.logins.json"]


def test_render_secrets_of_a_service_with_a_store(state, tmp_path):
    store = tmp_path / "store.json"
    store.write_text(json.dumps({"a": {"k": 1}, "b": {"k": 2}}))
    spec = logins.Spec(**DATA, store_roles=("b",))
    dsns, held = spec.render(str(store))
    assert [s["metadata"] for s in (dsns, held)] == [
        {"name": "svc-logins", "namespace": "ns"},
        {"name": "svc-store", "namespace": "ns"},
    ]
    known = spec.secrets_of_logins()
    assert (
        dsns["stringData"]["DSY_A_DSN"] == f"postgresql://svc_a:{known['svc_a']}@{logins.DATABASE}"
    )
    assert json.loads(held["stringData"]["credentials.json"]) == {"b": {"k": 2}}


def test_render_without_store_roles_is_one_secret():
    assert [s["metadata"]["name"] for s in logins.Spec(**DATA).render()] == ["svc-logins"]


def test_main_renders_and_refuses_bad_usage(capsys, state):
    assert logins.main(["render"], **DATA) == 0
    assert json.loads(capsys.readouterr().out)[0]["metadata"]["name"] == "svc-logins"
    for argv in ([], ["apply"], ["check", "a", "b"], ["render", "x"], ["drop", "dsn"]):
        assert logins.main(argv, **DATA) == 2
    assert "usage: logins.py render | apply DSN | check DSN" in capsys.readouterr().err
    assert logins.main(["render"], **DATA, store_roles=("b",)) == 2


@pytest.mark.parametrize("directory", SERVICES)
def test_service_data_is_one_call_to_main(directory):
    assert len((ROOT / "deploy" / directory / "logins.py").read_text().splitlines()) <= 40


@pytest.fixture
def server(appdb):
    """The migrated database on a server that holds none of the logins, which are server wide and
    so outlive the database of an earlier test."""
    with psycopg.connect(appdb, autocommit=True) as conn:
        for directory in SERVICES:
            for login, _ in service(directory).LOGINS.values():
                conn.execute(f"drop role if exists {login}")
    return appdb


@pytest.mark.integration
@pytest.mark.parametrize("directory", SERVICES)
def test_each_service_converges_then_check_finds_no_difference(directory, server, capsys):
    module = service(directory)
    items = [f"login: {login} in {group}" for login, group in module.LOGINS.values()]
    assert module.main("check", server) == 1
    capsys.readouterr()
    assert module.main("apply", server) == 0
    assert capsys.readouterr().out.splitlines() == [
        *(f"{item} created" for item in items),
        f"changed: {len(items)}",
    ]
    assert module.main("check", server) == 0
    assert capsys.readouterr().out.splitlines() == [f"{item} unchanged" for item in items]
    with psycopg.connect(server) as conn:
        for login, group in module.LOGINS.values():
            assert [g for g, *_ in conn.execute(logins.MEMBER_OF, (login,))] == [group]
