"""Render tests of the rabbitmq module, run by `just test`.

No cluster is needed.
Most tests call config.py directly, and one loads the module through `tilt alpha tiltfile-result`
from a wrapper Tiltfile that exports the Tilt profile as the root Tiltfile does.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from wdf_broker import topology
from wdf_broker.contract import topology as spec
from wdf_broker.models import Estimate, Tenant
from wdf_broker.tenancy import load

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
_spec = importlib.util.spec_from_file_location("rabbitmq_config", HERE / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)

USERS = {"dispatch", "admission", "operator"}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("WDF_STATE_DIR", str(tmp_path))
    return tmp_path


def objects(manifests) -> dict:
    return {o["kind"]: o for o in manifests}


def pod_of(manifests) -> dict:
    return objects(manifests)["StatefulSet"]["spec"]["template"]


def test_statefulset_carries_the_runtime_settings():
    rendered = objects(config.render("ci"))
    sts = rendered["StatefulSet"]
    assert sts["metadata"] == {"name": "rabbitmq", "namespace": "wdf-infra"}
    assert sts["spec"]["replicas"] == 1
    pod = sts["spec"]["template"]["spec"]
    [container] = pod["containers"]
    [init] = pod["initContainers"]
    resources = {
        "requests": {"cpu": "100m", "memory": "320Mi"},
        "limits": {"cpu": "500m", "memory": "320Mi"},
    }
    assert container["resources"] == init["resources"] == resources
    assert f"{spec().memory.limit_mib}Mi" == resources["limits"]["memory"]
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]["rabbitmq"]
    assert container["image"] == init["image"] == f"docker.io/library/rabbitmq@{pin['digest']}"
    env = {e["name"]: e["value"] for e in container["env"]}
    assert (
        env["RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"]
        == "+S 1:1 +sbwt none +sbwtdcpu none +sbwtdio none"
    )
    assert pod["nodeSelector"] == {"wdf.dev/pool": "infra"}
    assert {p["containerPort"] for p in container["ports"]} == {5672, 15692}
    assert container["readinessProbe"]["tcpSocket"] == {"port": "amqp"}
    [claim] = sts["spec"]["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "data"
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"
    service = rendered["Service"]
    assert service["metadata"]["name"] == sts["spec"]["serviceName"] == "rabbitmq"
    assert {p["port"] for p in service["spec"]["ports"]} == {5672, 15692}


def test_volumes_mount_the_configuration_and_the_cookie():
    pod = pod_of(config.render("ci"))["spec"]
    [container] = pod["containers"]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts["/var/lib/rabbitmq"]["name"] == "data"
    assert mounts["/etc/rabbitmq/rabbitmq.conf"]["subPath"] == "rabbitmq.conf"
    assert mounts["/etc/rabbitmq/enabled_plugins"]["subPath"] == "enabled_plugins"
    assert topology.DEFINITIONS_PATH not in mounts, "the directory holds the volume's own entries"
    for file in ("topology.json", "users.json"):
        mount = mounts[f"{topology.DEFINITIONS_PATH}/{file}"]
        assert (mount["name"], mount["subPath"]) == ("definitions", file)
    volumes = {v["name"]: v for v in pod["volumes"]}
    sources = volumes["definitions"]["projected"]["sources"]
    assert [s["configMap"]["items"] for s in sources if "configMap" in s] == [
        [{"key": "topology.json", "path": "topology.json"}]
    ]
    assert [s["secret"]["items"] for s in sources if "secret" in s] == [
        [{"key": "users.json", "path": "users.json"}]
    ]
    [init] = pod["initContainers"]
    assert "/var/lib/rabbitmq/.erlang.cookie" in init["command"][-1]
    assert volumes["cookie"]["secret"]["items"] == [
        {"key": "erlang.cookie", "path": "erlang.cookie"}
    ]


def test_configmap_holds_no_secret(state):
    rendered = objects(config.render("ci"))
    data = rendered["ConfigMap"]["data"]
    assert set(data) == {"rabbitmq.conf", "enabled_plugins", "topology.json"}
    held = [p.read_text() for p in (state / "rabbitmq").iterdir()]
    assert len(held) == 4
    text = json.dumps(data)
    for value in held:
        assert value not in text
    assert "password" not in text
    definitions = json.loads(data["topology.json"])
    assert "users" not in definitions
    assert [q["name"] for q in definitions["queues"]] == ["intake.acme.standard", "intake.dlq"]


def test_secret_holds_the_users_and_the_cookie(state):
    secret = objects(config.render("ci"))["Secret"]["stringData"]
    assert set(secret) == {"users.json", "erlang.cookie"}
    assert {u["name"] for u in json.loads(secret["users.json"])["users"]} == USERS
    assert secret["erlang.cookie"] == (state / "rabbitmq/erlang.cookie").read_text()


def test_a_changed_tenancy_profile_rolls_the_pod():
    def annotations(manifests):
        return pod_of(manifests)["metadata"]["annotations"]

    before = annotations(config.render("ci"))
    assert annotations(config.render("ci")) == before
    profile = load("ci")
    wider = profile.model_copy(
        update={
            "tenants": {**profile.tenants, "beta": Tenant(quota=Estimate(value=10, estimate=True))}
        }
    )
    after = annotations(config.render("ci", wider))
    assert after["wdf.dev/config-digest"] != before["wdf.dev/config-digest"]
    assert after["wdf.dev/tenancy-profile"] == "ci"


def test_a_new_password_rolls_the_pod(state):
    def digest():
        return pod_of(config.render("ci"))["metadata"]["annotations"]["wdf.dev/config-digest"]

    before = digest()
    (state / "rabbitmq/admission.password").unlink()
    assert digest() != before


def test_tilt_profile_maps_to_its_tenancy_profile():
    pod = pod_of(config.render("full"))
    assert pod["metadata"]["annotations"]["wdf.dev/tenancy-profile"] == "dev"
    with pytest.raises(KeyError, match="no tenancy profile"):
        config.render("nope")


def test_password_created_in_an_empty_state_directory_and_kept(state):
    assert not (state / "rabbitmq").exists()
    first = config.password("admission")
    path = state / "rabbitmq/admission.password"
    assert path.read_text() == first and len(first) == 48
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert config.password("admission") == first
    assert config.password("dispatch") != first
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "admission.password",
        "dispatch.password",
    ]


def test_password_of_an_unknown_user_refused():
    with pytest.raises(KeyError, match="unknown broker user 'guest'"):
        config.password("guest")


def test_command_line(state, capsys):
    assert config.main(["password", "operator"]) == 0
    assert capsys.readouterr().out == (state / "rabbitmq/operator.password").read_text() + "\n"
    assert config.main(["password", "guest"]) == 2
    assert config.main(["render"]) == 2
    assert config.main(["render", "ci"]) == 0
    kinds = [o["kind"] for o in json.loads(capsys.readouterr().out)]
    assert kinds == ["ConfigMap", "Secret", "StatefulSet", "Service"]


@pytest.mark.skipif(shutil.which("tilt") is None, reason="tilt not on PATH")
def test_tiltfile_loads_the_module_for_the_exported_profile(tmp_path, state):
    (tmp_path / "Tiltfile").write_text(
        "os.putenv('WDF_TILT_PROFILE', 'ci')\n"
        f"load_dynamic('{HERE}/Tiltfile.module')\n"
        "config.set_enabled_resources(['rabbitmq'])\n"
    )
    env = {
        **os.environ,
        "KUBECONFIG": str(ROOT / "scripts/tilt/fixtures/kubeconfig-wdf.yaml"),
        "WDF_STATE_DIR": str(state),
    }
    run = subprocess.run(
        ["tilt", "alpha", "tiltfile-result", "-f", str(tmp_path / "Tiltfile")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    [manifest] = json.loads(run.stdout)["Manifests"]
    assert manifest["Name"] == "rabbitmq"
    kinds = {o["kind"] for o in yaml.safe_load_all(manifest["DeployTarget"]["yaml"]) if o}
    assert kinds == {"ConfigMap", "Secret", "StatefulSet", "Service"}
    assert sorted(p.name for p in (state / "rabbitmq").iterdir()) == sorted(
        [f"{u}.password" for u in USERS] + ["erlang.cookie"]
    )
