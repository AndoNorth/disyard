"""The parts of the kit that need no cluster."""

import asyncio
import json
import socket

import pytest
import wdf_clusterkit
from wdf_clusterkit import (
    Forward,
    appdb_password,
    free_port,
    oom_killed,
    ready,
    state_dir,
    store_credentials,
    until,
)


def test_state_dir_is_wdf_state_dir_first(monkeypatch, tmp_path):
    monkeypatch.setenv("WDF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state_dir() == tmp_path / "state"


def test_state_dir_falls_back_to_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("WDF_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state_dir() == tmp_path / "xdg/wasm-dag-forge"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert state_dir() == tmp_path / "home/.local/state/wasm-dag-forge"


def test_credentials_are_read_from_the_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("WDF_STATE_DIR", str(tmp_path))
    (tmp_path / "appdb.credential").write_text("secret\n")
    (tmp_path / "objectstore.credentials.json").write_text(
        json.dumps({"submit": {"access_key": "a", "secret_key": "s"}})
    )
    assert appdb_password() == "secret"
    submit = store_credentials()["submit"]
    assert (submit.access_key, submit.secret_key) == ("a", "s")


def test_free_port_can_be_bound():
    port = free_port()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_oom_killed_and_ready_pods():
    pod = {
        "metadata": {"name": "admission-1"},
        "status": {"conditions": [], "containerStatuses": []},
    }
    assert not oom_killed(pod) and not ready(pod)
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    assert ready(pod)
    pod["status"]["containerStatuses"] = [
        {"lastState": {"terminated": {"reason": "OOMKilled"}}, "state": {"running": {}}}
    ]
    assert oom_killed(pod)
    pod["metadata"]["deletionTimestamp"] = "2026-11-18T00:00:00Z"
    assert not ready(pod)


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self):
        return 0


def test_forward_binds_localhost_and_keeps_its_port_when_opened_again(monkeypatch):
    spawned = []
    monkeypatch.setattr(
        wdf_clusterkit.subprocess,
        "Popen",
        lambda argv, **kwargs: spawned.append(FakeProcess(argv)) or spawned[-1],
    )
    monkeypatch.setattr(wdf_clusterkit, "_listening", lambda port: True)
    forward = Forward("kind-wdf", "wdf-infra", "svc/temporal-frontend", 7233)
    forward.open()
    forward.open()
    expected = [
        "kubectl",
        "--context=kind-wdf",
        "--namespace=wdf-infra",
        "port-forward",
        "--address=127.0.0.1",
        "svc/temporal-frontend",
        f"{forward.port}:7233",
    ]
    assert [p.argv for p in spawned] == [expected, expected]
    assert spawned[0].terminated and forward.alive
    forward.close()
    assert not forward.alive


def test_until_returns_the_first_truthy_value_of_a_sync_or_async_check():
    values = iter([0, None, "ready"])

    async def awaited():
        return 42

    async def run():
        assert await until(lambda: next(values), 5, "value", every_s=0) == "ready"
        assert await until(awaited, 5, "value", every_s=0) == 42

    asyncio.run(run())


def test_until_raises_after_its_timeout():
    with pytest.raises(TimeoutError, match="no thing within 0 s"):
        asyncio.run(until(lambda: False, 0.01, "thing", every_s=0.005))
