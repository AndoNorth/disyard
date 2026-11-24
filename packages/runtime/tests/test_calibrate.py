"""The calibration command measures the work units per second of cpu_burn in a real guest, checks
the rate against a catalogue, and writes the catalogue with the nominal duration of each level 1
argument set."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from dsy_compile.build import build
from dsy_runtime import pins
from dsy_runtime.calibrate import FIXTURES, TOLERANCE, differs, load
from dsy_workloads import bundle

RATE = "units_per_second: "


@pytest.fixture(scope="module")
def component(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("calibrate")
    return build(bundle.write(work), "dsy_workloads.main", work / "built").component


def calibrate(*args) -> subprocess.CompletedProcess:
    done = subprocess.run(
        [sys.executable, "-m", "dsy_runtime.calibrate", *map(str, args)],
        capture_output=True,
        text=True,
    )
    print(done.stdout, done.stderr, sep="")
    return done


def printed_rate(done: subprocess.CompletedProcess) -> float:
    first = done.stdout.splitlines()[0]
    assert first.startswith(RATE)
    return float(first.removeprefix(RATE))


@pytest.fixture(scope="module")
def measured(component) -> float:
    done = calibrate("--component", component, "--runs", 3)
    assert done.returncode == 0
    return printed_rate(done)


def test_calibrate_prints_units_per_second(measured):
    assert measured > 0


@pytest.mark.parametrize("factor", [1.5, 0.5])
def test_check_refuses_rate_outside_tolerance(component, measured, factor, tmp_path):
    off = tmp_path / "catalogue.yaml"
    catalogued = round(measured * factor, 1)
    off.write_text(yaml.safe_dump(load() | {"units_per_second": catalogued}))
    done = calibrate("--component", component, "--runs", 3, "--check", off)
    # Host load moves the rate between the fixture's run and this one, so the expected decision
    # comes from the rate this run measured.
    outside = differs(printed_rate(done), catalogued, TOLERANCE)
    assert done.returncode == outside
    assert ("reason: rate-outside-tolerance: " in done.stderr) == outside


def test_tolerance_is_a_fraction_of_the_catalogued_rate():
    assert not differs(100.0, 100.0, 0.2)
    assert not differs(120.0, 100.0, 0.2) and not differs(80.0, 100.0, 0.2)
    assert differs(120.1, 100.0, 0.2) and differs(79.9, 100.0, 0.2)


def test_catalogue_matches_pins_and_fixtures():
    catalogue = load()
    assert catalogue["cpu_basis"] == "host"
    assert catalogue["tolerance"] == TOLERANCE == 0.2
    assert catalogue["versions"] == {
        "componentize_py": pins.COMPONENTIZE_PY_VERSION,
        "wasmtime": pins.WASMTIME_VERSION,
        "wit": pins.WIT_VERSION,
    }
    spread = catalogue["spread"]
    assert spread["min"] <= catalogue["units_per_second"] <= spread["max"]
    fixtures = {
        d.name: json.loads(d.joinpath("input.json").read_text()) for d in FIXTURES.iterdir()
    }
    durations = catalogue["durations"]
    assert {name: entry["params"] for name, entry in durations.items()} == fixtures
    assert all(entry["seconds"] > 0 for entry in durations.values())


def test_write_records_every_argument_set(component, tmp_path):
    written = tmp_path / "catalogue.yaml"
    done = calibrate("--component", component, "--runs", 1, "--write", written)
    assert done.returncode == 0
    assert written.read_text().startswith("# Written by python -m dsy_runtime.calibrate --write")
    catalogue = load(written)
    assert catalogue.keys() == load().keys()
    assert catalogue["durations"].keys() == load()["durations"].keys()
    assert catalogue["spread"]["runs"] == 1
