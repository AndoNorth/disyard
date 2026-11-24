"""`setup` starts the Pyroscope profiler only when `DSY_PYROSCOPE_ADDRESS` is set."""

import dsy_obs.setup as obs_setup
import pyroscope
import pytest
from dsy_obs import logs, profiles, traces

ADDRESS = "http://pyroscope.dsy-obs:4040"


@pytest.fixture
def configured(monkeypatch):
    """The calls `setup` makes to `pyroscope.configure`, with logs and traces left alone."""
    calls = []
    monkeypatch.setattr(pyroscope, "configure", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(logs, "install", lambda component: None)
    monkeypatch.setattr(traces, "install", lambda component: None)
    monkeypatch.setattr(obs_setup, "_installed", None)
    return calls


def test_with_the_address_unset_configure_is_never_called(configured, monkeypatch):
    monkeypatch.delenv(profiles.ADDRESS, raising=False)
    obs_setup.setup("simulate-worker", pool="simulate")
    assert configured == []


def test_with_the_address_set_configure_is_called_once_with_the_name_and_pool(
    configured, monkeypatch
):
    monkeypatch.setenv(profiles.ADDRESS, ADDRESS)
    obs_setup.setup("simulate-worker", pool="simulate")
    obs_setup.setup("simulate-worker", pool="simulate")
    assert configured == [
        {
            "application_name": "dsy.simulate-worker",
            "server_address": ADDRESS,
            "tags": {"pool": "simulate"},
        }
    ]


def test_a_component_without_a_pool_has_no_tags(configured, monkeypatch):
    monkeypatch.setenv(profiles.ADDRESS, ADDRESS)
    obs_setup.setup("dispatch")
    [call] = configured
    assert call["application_name"] == "dsy.dispatch" and call["tags"] is None
