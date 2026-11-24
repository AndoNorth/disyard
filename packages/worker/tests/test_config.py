"""The configuration comes from the environment only, and a missing or malformed variable exits 2
before anything connects."""

from pathlib import Path

import pytest
from dsy_worker.config import ENV, MIRROR_ENV, PREFIX_ENV, load

COMPLETE = {
    ENV["temporal_address"]: "temporal-frontend.dsy-infra:7233",
    ENV["namespace"]: "dsy",
    ENV["store_endpoint"]: "http://objectstore.dsy-infra:8333",
    ENV["store_credentials"]: "/etc/dsy/store/credentials.json",
    ENV["slot_memory_mib"]: "384",
    ENV["scratch"]: "/var/dsy/task",
    ENV["ready_file"]: "/var/dsy/ready",
}


@pytest.mark.parametrize("pool", ["simulate", "aggregate"])
def test_complete_environment_polls_the_contract_queue(pool):
    config = load(pool, COMPLETE)
    assert config.queue == f"dsy-{pool}"
    assert config.slot_memory_mib == 384
    assert config.scratch == Path("/var/dsy/task")
    assert config.mirror is None


def test_compile_reads_the_mirror():
    config = load("compile", {**COMPLETE, MIRROR_ENV: "http://mirror.dsy-infra/simple"})
    assert (config.queue, config.mirror) == ("dsy-compile", "http://mirror.dsy-infra/simple")


def test_prefix_goes_in_front_of_the_queue():
    config = load("simulate", {**COMPLETE, PREFIX_ENV: "h-1a2b3c4d-"})
    assert config.queue == "h-1a2b3c4d-dsy-simulate"


def test_other_pools_ignore_the_mirror():
    assert load("simulate", {**COMPLETE, MIRROR_ENV: "http://mirror"}).mirror is None


def exits_2(capsys, pool: str, environ: dict) -> str:
    with pytest.raises(SystemExit) as exited:
        load(pool, environ)
    assert exited.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize("name", list(ENV.values()))
def test_missing_variable_exits_2_naming_it(capsys, name):
    environ = {key: value for key, value in COMPLETE.items() if key != name}
    assert name in exits_2(capsys, "simulate", environ)
    assert name in exits_2(capsys, "simulate", {**COMPLETE, name: ""})


def test_compile_without_a_mirror_exits_2(capsys):
    assert MIRROR_ENV in exits_2(capsys, "compile", COMPLETE)


def test_every_missing_variable_is_named_at_once(capsys):
    err = exits_2(capsys, "compile", {})
    assert all(name in err for name in [*ENV.values(), MIRROR_ENV])


@pytest.mark.parametrize("prefix", ["H-1a2b-", "1-", "h_1-", "h" * 120 + "-"])
def test_prefix_outside_the_queue_pattern_exits_2(capsys, prefix):
    assert PREFIX_ENV in exits_2(capsys, "simulate", {**COMPLETE, PREFIX_ENV: prefix})


@pytest.mark.parametrize("memory", ["0", "-1", "384Mi", "3.5"])
def test_slot_memory_not_a_positive_integer_exits_2(capsys, memory):
    environ = {**COMPLETE, ENV["slot_memory_mib"]: memory}
    assert ENV["slot_memory_mib"] in exits_2(capsys, "simulate", environ)


def test_unknown_pool_exits_2(capsys):
    assert "no pool named 'gpu'" in exits_2(capsys, "gpu", COMPLETE)
