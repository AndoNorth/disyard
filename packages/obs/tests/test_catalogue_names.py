"""Every metric name an emitter declares is in the platform catalogue, and the `exit_class` values
of the catalogue are the exit classes of every pool.

The names are read from the source without importing it: the `COUNTERS`, `GAUGES`, `AGES` and
`HISTOGRAMS` maps or tuples of each `metrics.py`, and the names the job workflow creates on its
meter.
"""

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = yaml.safe_load((ROOT / "contracts/instrumentation/metrics.yaml").read_text())
DECLARATIONS = {"COUNTERS", "GAUGES", "AGES", "HISTOGRAMS"}
JOB = ROOT / "packages/workflow/src/dsy_workflow/job.py"


def declared(path: Path) -> set[str]:
    """The names in the top-level declarations of one `metrics.py`."""
    names = set()
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and {getattr(t, "id", "") for t in node.targets} & DECLARATIONS
        ):
            value = node.value
            items = value.keys if isinstance(value, ast.Dict) else getattr(value, "elts", [])
            names |= {item.value for item in items if isinstance(item, ast.Constant)}
    return names


def created(path: Path) -> set[str]:
    """The names passed first to a `create_*` call of a meter."""
    return {
        node.args[0].value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "").startswith("create_")
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


EMITTERS = sorted(ROOT.glob("packages/*/src/**/metrics.py"))


def emitted() -> dict[str, set[str]]:
    found = {str(p.relative_to(ROOT)): declared(p) for p in EMITTERS}
    found[str(JOB.relative_to(ROOT))] = created(JOB)
    return {path: names for path, names in found.items() if names}


def test_the_known_emitters_declare_names():
    found = emitted()
    for path in (
        "packages/admission/src/dsy_admission/service/metrics.py",
        "packages/dispatchapi/src/dsy_dispatchapi/service/metrics.py",
        "packages/workflow/src/dsy_workflow/job.py",
    ):
        assert found.get(path), f"{path} declares no names, so the reader missed them"


@pytest.mark.parametrize("path", sorted(emitted()))
def test_every_declared_name_is_catalogued(path):
    missing = emitted()[path] - set(CATALOGUE["metrics"])
    assert not missing, f"{path} emits names not in the catalogue: {sorted(missing)}"


def test_the_reader_finds_maps_and_tuples(tmp_path):
    source = tmp_path / "metrics.py"
    source.write_text(
        'COUNTERS = {"dsy_a_total": ()}\nGAUGES = ("dsy_b",)\nAGES = ("dsy_c_seconds",)\n'
        'HISTOGRAMS = {"dsy_d_seconds": ()}\nBUCKETS = {"dsy_e": []}\n'
    )
    assert declared(source) == {"dsy_a_total", "dsy_b", "dsy_c_seconds", "dsy_d_seconds"}


def test_exit_class_values_are_the_exit_classes_of_every_pool():
    pools = yaml.safe_load((ROOT / "contracts/workload/exit-classes.yaml").read_text())["pools"]
    classes = {c["class"] for pool in pools.values() for c in pool["classes"]}
    labelled = [
        (name, spec["labels"]["exit_class"]["values"])
        for name, spec in CATALOGUE["metrics"].items()
        if "exit_class" in spec["labels"]
    ]
    assert labelled, "no catalogued metric has an exit_class label"
    for name, values in labelled:
        assert sorted(values) == sorted(classes), name
        assert len(values) == len(set(values)), name
