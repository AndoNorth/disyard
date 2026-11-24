"""The Temporal UI of the temporal module, rendered through Tilt with the switch on and off.

No cluster is needed: the render helper of the module's own tests runs a wrapper Tiltfile.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]

pytestmark = pytest.mark.skipif(
    shutil.which("tilt") is None or shutil.which("helm") is None, reason="tilt or helm not on PATH"
)

MODULE = ROOT / "deploy/temporal"


def load_render():
    spec = importlib.util.spec_from_file_location("temporal_render", MODULE / "test_render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_render()


def render(tmp_path, monkeypatch, obs):
    """The module rendered under the switch, with the ports file beside it."""
    monkeypatch.setenv("DSY_OBS", obs)
    (tmp_path / "deploy/host").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/host/ports.yaml", tmp_path / "deploy/host/ports.yaml")
    return helper.render(tmp_path)


def web_objects(objects):
    return {k: o for k, o in objects.items() if o["metadata"]["name"] == "temporal-web"}


def test_switch_off_renders_no_web_object(tmp_path, monkeypatch):
    objects, deps, _ = render(tmp_path, monkeypatch, "off")
    assert web_objects(objects) == {}
    assert "temporal-web" not in deps


def test_switch_on_renders_the_ui_on_the_infra_node_inside_its_resources(tmp_path, monkeypatch):
    objects, _, _ = render(tmp_path, monkeypatch, "on")
    deployment = objects["Deployment", "temporal-web"]
    pod = deployment["spec"]["template"]["spec"]
    [container] = pod["containers"]
    module = yaml.safe_load((MODULE / "module.yaml").read_text())
    assert container["resources"] == module["resources"]["temporal-web"]
    assert container["resources"]["requests"] == container["resources"]["limits"]
    assert container["resources"]["limits"]["memory"] == "64Mi"
    assert pod["nodeSelector"] == {"dsy.dev/pool": "infra"}
    assert pod["tolerations"] == [helper.INFRA_TOLERATION]
    assert deployment["metadata"]["namespace"] == "dsy-infra"
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_ui_image_is_the_pinned_digest(tmp_path, monkeypatch):
    objects, _, _ = render(tmp_path, monkeypatch, "on")
    pin = yaml.safe_load((ROOT / "deploy/versions.yaml").read_text())["images"]["temporalio/ui"]
    [container] = objects["Deployment", "temporal-web"]["spec"]["template"]["spec"]["containers"]
    assert container["image"] == f"temporalio/ui:{pin['version']}@{pin['digest']}"


def test_ui_service_is_the_node_port_of_the_temporal_ui_row(tmp_path, monkeypatch):
    objects, _, _ = render(tmp_path, monkeypatch, "on")
    rows = yaml.safe_load((ROOT / "deploy/host/ports.yaml").read_text())["ports"]
    [row] = [r for r in rows if r["name"] == "temporal-ui"]
    service = objects["Service", "temporal-web"]["spec"]
    assert service["type"] == "NodePort"
    [port] = service["ports"]
    assert port["nodePort"] == row["node_port"] == 30233


def test_ui_reads_the_frontend_and_waits_for_the_schema(tmp_path, monkeypatch):
    objects, deps, enabled = render(tmp_path, monkeypatch, "on")
    [container] = objects["Deployment", "temporal-web"]["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["TEMPORAL_ADDRESS"] == "temporal-frontend:7233"
    assert deps["temporal-web"] == ["temporal-schema"]
    assert "temporal-web" in deps["temporal"]
    assert "temporal-web" in enabled
