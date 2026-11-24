"""The rendered broker configuration: one intake queue per tenant and class of the profile, the
runtime settings of the broker, and passwords only as salted hashes in the users definitions."""

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest
from dsy_broker import topology
from dsy_broker.contract import parse
from dsy_broker.models import Tenant
from dsy_broker.tenancy import load

ROOT = Path(__file__).resolve().parents[4]
VECTORS = ROOT / "contracts/tenancy/vectors/tenancy/valid"
PASSWORDS = {
    "dispatch": "dispatch-secret-1",
    "admission": "admission-secret-2",
    "operator": "operator-secret-3",
}


def vector_profile(name: str):
    return parse("tenancy", (VECTORS / name).read_text()).profiles["ci"]


def by_name(rows: list[dict]) -> dict[str, dict]:
    return {row["name"]: row for row in rows}


def test_topology_matches_tenancy():
    defs = topology.definitions(load("ci"))
    assert defs["vhosts"] == [{"name": "/"}]
    assert {(e["name"], e["type"], e["durable"]) for e in defs["exchanges"]} == {
        ("dsy.intake", "direct", True),
        ("dsy.dlx", "fanout", True),
    }
    queues = by_name(defs["queues"])
    profile = load("ci")
    intake = {f"intake.{t}.{c}" for t in profile.tenants for c in profile.priority_classes}
    assert set(queues) == intake | {"intake.dlq"}
    for queue in queues.values():
        assert queue["durable"] is True
        assert queue["arguments"] == {"x-queue-type": "quorum", "x-quorum-initial-group-size": 1}
    assert {(b["source"], b["destination"], b["routing_key"]) for b in defs["bindings"]} == {
        *(("dsy.intake", name, name) for name in intake),
        ("dsy.dlx", "intake.dlq", ""),
    }
    policies = by_name(defs["policies"])
    assert set(policies) == {"dsy-intake", "dsy-dlq"}
    assert re.search(policies["dsy-intake"]["pattern"], "intake.acme.standard")
    assert not re.search(policies["dsy-intake"]["pattern"], "intake.dlq")
    assert re.search(policies["dsy-dlq"]["pattern"], "intake.dlq")
    assert policies["dsy-intake"]["definition"] == {
        "delivery-limit": 5,
        "max-length": 1000,
        "overflow": "reject-publish",
        "dead-letter-exchange": "dsy.dlx",
        "dead-letter-strategy": "at-least-once",
        "delayed-retry-type": "failed",
        "delayed-retry-min": 1000,
        "delayed-retry-max": 30000,
    }
    assert policies["dsy-dlq"]["definition"] == {"delivery-limit": -1}
    assert all(p["apply-to"] == "quorum_queues" for p in policies.values())
    assert "users" not in defs and "permissions" not in defs


def test_topology_matches_tenancy_one_queue_per_tenant_and_class():
    profile = vector_profile("two-tenants.yaml")
    intake = {
        q["name"] for q in topology.definitions(profile)["queues"] if q["name"] != "intake.dlq"
    }
    assert intake == {
        "intake.acme.standard",
        "intake.acme.batch",
        "intake.globex-2.standard",
        "intake.globex-2.batch",
    }


def test_topology_matches_tenancy_a_tenant_adds_one_queue():
    profile = vector_profile("value-pr.yaml")
    before = topology.definitions(profile)
    tenants = {**profile.tenants, "initech": Tenant(quota=profile.tenants["acme"].quota)}
    after = topology.definitions(profile.model_copy(update={"tenants": tenants}))
    added = {q["name"] for q in after["queues"]} - {q["name"] for q in before["queues"]}
    assert added == {"intake.initech.standard"}
    assert len(after["queues"]) == len(before["queues"]) + 1
    assert len(after["bindings"]) == len(before["bindings"]) + 1
    assert after["exchanges"] == before["exchanges"]
    assert after["policies"] == before["policies"]


def test_conf_holds_runtime_settings():
    conf = topology.render(load("ci"))[topology.CONF]
    settings = dict(line.split(" = ", 1) for line in conf.splitlines())
    assert settings == {
        "definitions.import_backend": "local_filesystem",
        "definitions.local.path": "/etc/rabbitmq/definitions",
        "definitions.skip_if_unchanged": "false",
        "vm_memory_high_watermark.absolute": "192MiB",
        "disk_free_limit.absolute": "64MB",
        "raft.wal_max_size_bytes": "32000000",
    }
    assert topology.render(load("ci"))[topology.PLUGINS] == "[rabbitmq_prometheus].\n"


def secret_strings(users_file: str) -> list[str]:
    """Every password, and every hash of a rendered users file."""
    hashes = [u["password_hash"] for u in json.loads(users_file)["users"]]
    return list(PASSWORDS.values()) + hashes


def test_rendered_files_hold_no_secret():
    files = topology.render(load("ci"), PASSWORDS)
    assert set(files) == {
        topology.CONF,
        topology.PLUGINS,
        topology.TOPOLOGY,
        topology.USERS,
    }
    secrets = secret_strings(files[topology.USERS])
    for name, text in files.items():
        if name != topology.USERS:
            assert not [s for s in secrets if s in text], name
            assert "password" not in text, name
    for password in PASSWORDS.values():
        assert password not in files[topology.USERS]


def test_rendered_files_hold_no_secret_without_users():
    assert topology.USERS not in topology.render(load("ci"))


def test_users_hold_salted_hashes_and_the_contract_permissions():
    users = json.loads(topology.render(load("ci"), PASSWORDS)[topology.USERS])
    hashes = {u["name"]: u for u in users["users"]}
    assert set(hashes) == set(PASSWORDS)
    for name, entry in hashes.items():
        assert entry["hashing_algorithm"] == "rabbit_password_hashing_sha256"
        raw = base64.b64decode(entry["password_hash"])
        salt, digest = raw[:4], raw[4:]
        assert digest == hashlib.sha256(salt + PASSWORDS[name].encode()).digest()
    permissions = {p["user"]: p for p in users["permissions"]}
    assert permissions["dispatch"] == {
        "user": "dispatch",
        "vhost": "/",
        "configure": "",
        "write": r"^dsy\.intake$",
        "read": "",
    }
    assert all(p["configure"] == "" for p in permissions.values())
    assert permissions["admission"]["write"] == ""


def test_two_renders_salt_each_hash_afresh():
    first = json.loads(topology.render(load("ci"), PASSWORDS)[topology.USERS])
    second = json.loads(topology.render(load("ci"), PASSWORDS)[topology.USERS])
    assert [u["password_hash"] for u in first["users"]] != [
        u["password_hash"] for u in second["users"]
    ]


@pytest.mark.parametrize(
    "passwords",
    [
        {"dispatch": "a", "admission": "b"},
        {**PASSWORDS, "guest": "guest"},
        {**PASSWORDS, "operator": ""},
        {**PASSWORDS, "operator": 7},
    ],
    ids=["missing-user", "extra-user", "empty-password", "not-a-string"],
)
def test_users_refuse_other_than_one_password_per_user(passwords):
    with pytest.raises(ValueError):
        topology.users_definitions(passwords)


def test_cli_prints_topology_definitions(capsys):
    assert topology.main(["--profile", "ci"]) == 0
    assert json.loads(capsys.readouterr().out) == topology.definitions(load("ci"))


def test_cli_writes_every_file(tmp_path):
    users = tmp_path / "passwords.json"
    users.write_text(json.dumps(PASSWORDS))
    out = tmp_path / "conf"
    assert topology.main(["--profile", "ci", "--users", str(users), "--out", str(out)]) == 0
    written = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}
    assert written == set(topology.render(load("ci"), PASSWORDS))
    assert (out / topology.USERS).stat().st_mode & 0o777 == 0o600
    assert json.loads((out / topology.TOPOLOGY).read_text()) == topology.definitions(load("ci"))
    secrets = secret_strings((out / topology.USERS).read_text())
    for path in out.rglob("*"):
        if path.is_file() and path != out / topology.USERS:
            assert not [s for s in secrets if s in path.read_text()], path


def test_cli_writes_no_users_without_passwords(tmp_path):
    assert topology.main(["--profile", "ci", "--out", str(tmp_path)]) == 0
    assert not (tmp_path / topology.USERS).exists()
    assert (tmp_path / topology.TOPOLOGY).is_file()


@pytest.mark.parametrize(
    "argv",
    [[], ["--profile", "staging"], ["--profile", "ci", "--users", "passwords.json"]],
    ids=["no-profile", "unknown-profile", "users-without-out"],
)
def test_cli_usage(argv, capsys):
    assert topology.main(argv) == 2


def test_cli_refuses_a_users_file_without_every_user(tmp_path, capsys):
    users = tmp_path / "passwords.json"
    users.write_text(json.dumps({"dispatch": "a"}))
    out = tmp_path / "conf"
    assert topology.main(["--profile", "ci", "--users", str(users), "--out", str(out)]) == 1
    assert "error:" in capsys.readouterr().err
    assert not out.exists()
